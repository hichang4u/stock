[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [switch]$AllowUnsafeState,
    [ValidateRange(5, 120)]
    [int]$StartupTimeoutSec = 30
)

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$mainScript = Join-Path $repoRoot "main.py"
$guardScript = Join-Path $repoRoot "scripts\restart_guard.py"
$logDir = Join-Path $repoRoot "data\logs"

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "가상환경 Python을 찾을 수 없습니다: $venvPython"
}
if (-not (Test-Path -LiteralPath $mainScript -PathType Leaf)) {
    throw "main.py를 찾을 수 없습니다: $mainScript"
}

function Invoke-RestartGuard {
    # PowerShell 7.3+에서 exit code 2가 예외로 바뀌더라도 unsafe override와
    # JSON 사유 처리가 먼저 실행되도록 이 호출에 한해 기존 동작을 사용한다.
    $previousLastExitCode = $global:LASTEXITCODE
    $nativePreference = Get-Variable PSNativeCommandUseErrorActionPreference `
        -ErrorAction SilentlyContinue
    if ($null -ne $nativePreference) {
        $previousNativePreference = $nativePreference.Value
        $PSNativeCommandUseErrorActionPreference = $false
    }
    try {
        $guardOutput = & $venvPython $guardScript --root $repoRoot
        $guardExitCode = $LASTEXITCODE
    } finally {
        if ($null -ne $nativePreference) {
            $PSNativeCommandUseErrorActionPreference = $previousNativePreference
        }
        $global:LASTEXITCODE = $previousLastExitCode
    }
    try {
        $guard = $guardOutput | ConvertFrom-Json
    } catch {
        throw "재시작 안전점검 결과를 해석할 수 없습니다: $guardOutput"
    }
    return [PSCustomObject]@{
        Guard = $guard
        ExitCode = $guardExitCode
    }
}

function Assert-RestartSafe([string]$Phase, [switch]$ReportOnly) {
    $check = Invoke-RestartGuard
    $unsafe = $check.ExitCode -ne 0 -or -not $check.Guard.safe
    if ($unsafe) {
        $reasonText = ($check.Guard.reasons -join ", ")
        if (-not $reasonText) {
            $reasonText = "GUARD_EXIT_CODE:$($check.ExitCode)"
        }
        if ($ReportOnly) {
            Write-Warning "안전점검 결과($Phase): 재시작 불가: $reasonText"
            return
        }
        if (-not $AllowUnsafeState) {
            throw "활성 주문/포지션 가능성으로 재시작을 차단했습니다($Phase): $reasonText"
        }
        Write-Warning ("안전점검을 무시하고 재시작합니다($Phase): " + `
            $reasonText)
    } elseif ($ReportOnly) {
        Write-Host "안전점검 결과($Phase): 재시작 가능"
    }
}

Assert-RestartSafe "초기 점검" -ReportOnly:$WhatIfPreference

$allProcesses = @(Get-CimInstance Win32_Process)
$launcherCandidates = @(
    $allProcesses | Where-Object {
        $_.ExecutablePath -and
        [System.IO.Path]::GetFullPath($_.ExecutablePath) -eq $venvPython -and
        $_.CommandLine -match '(^|[\s"''])main\.py([\s"'']|$)'
    }
)
if ($launcherCandidates.Count -gt 1) {
    $ids = ($launcherCandidates.ProcessId -join ", ")
    throw "이 저장소의 main.py 후보가 여러 개입니다. 수동 확인이 필요합니다: $ids"
}

$launcher = $launcherCandidates | Select-Object -First 1
$targetProcesses = @()
$processDepth = @{}
if ($null -ne $launcher) {
    $processById = @{}
    foreach ($process in $allProcesses) {
        $processById[[uint32]$process.ProcessId] = $process
    }
    $queue = [System.Collections.Generic.Queue[uint32]]::new()
    $queue.Enqueue([uint32]$launcher.ProcessId)
    $processDepth[[uint32]$launcher.ProcessId] = 0
    while ($queue.Count -gt 0) {
        $parentId = $queue.Dequeue()
        $parent = $processById[$parentId]
        $children = @($allProcesses | Where-Object { $_.ParentProcessId -eq $parentId })
        foreach ($child in $children) {
            $childId = [uint32]$child.ProcessId
            if ($processDepth.ContainsKey($childId)) {
                continue
            }
            # 스냅샷의 ParentProcessId가 재사용된 과거 PID를 가리키면 현재
            # 부모보다 자식 생성시각이 앞선다. 그런 항목과 시각 미확인 항목은
            # 종료 대상에 포함하지 않는다.
            if (
                $null -eq $parent.CreationDate -or
                $null -eq $child.CreationDate -or
                $child.CreationDate -lt $parent.CreationDate
            ) {
                continue
            }
            $queue.Enqueue($childId)
            $processDepth[$childId] = $processDepth[$parentId] + 1
            $targetProcesses += $child
        }
    }
    $targetProcesses += $launcher
}

$description = if ($launcher) {
    "main.py 프로세스 트리 PID=" + (($targetProcesses.ProcessId | Sort-Object) -join ",")
} else {
    "실행 중인 main.py 없음; 새 프로세스 시작"
}

if (-not $PSCmdlet.ShouldProcess($repoRoot, $description)) {
    return
}

# 확인 프롬프트를 읽는 동안 상태가 ENTERING/HOLDING으로 바뀔 수 있다.
# 종료와 최대한 가까운 시점에 다시 검사해 최초 점검과 kill 사이 TOCTOU를 닫는다.
Assert-RestartSafe "종료 직전 재점검"

# 자식부터 종료한다. 안전점검을 통과한 IDLE/CLOSED 상태에서만 수행되며,
# -AllowUnsafeState는 사용자가 위험을 명시적으로 인수한 경우에만 허용한다.
foreach ($process in @($targetProcesses | Sort-Object {
    $processDepth[[uint32]$_.ProcessId]
} -Descending)) {
    $live = Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
    if ($null -ne $live) {
        try {
            Stop-Process -Id $process.ProcessId -ErrorAction Stop
        } catch {
            # 부모/자식 종료가 전파되는 짧은 사이에 PID가 먼저 사라질 수 있다.
            if ($null -ne (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue)) {
                throw
            }
        }
    }
}

$stopDeadline = (Get-Date).AddSeconds(10)
do {
    $remaining = @(
        $targetProcesses | Where-Object {
            $null -ne (Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue)
        }
    )
    if ($remaining.Count -eq 0) { break }
    Start-Sleep -Milliseconds 200
} while ((Get-Date) -lt $stopDeadline)
if ($remaining.Count -gt 0) {
    throw "기존 프로세스가 종료되지 않았습니다: $($remaining.ProcessId -join ', ')"
}

New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$stdoutPath = Join-Path $logDir "main_restart_stdout.log"
$stderrPath = Join-Path $logDir "main_restart_stderr.log"
$uiPort = 8080
$envPath = Join-Path $repoRoot ".env"
if (Test-Path -LiteralPath $envPath) {
    $portLine = Get-Content -LiteralPath $envPath | Where-Object { $_ -match '^UI_PORT=\d+$' } | Select-Object -Last 1
    if ($portLine) {
        $uiPort = [int]($portLine -replace '^UI_PORT=', '')
    }
}

# 기존 프로세스를 내린 뒤 실제 바인딩으로 예약/점유 포트를 즉시 판별한다.
$portProbe = [System.Net.Sockets.TcpListener]::new(
    [System.Net.IPAddress]::Loopback,
    $uiPort
)
try {
    $portProbe.Start()
} catch {
    throw "UI 포트 $uiPort 를 사용할 수 없습니다. .env의 UI_PORT를 사용 가능한 포트로 변경하세요: $($_.Exception.Message)"
} finally {
    $portProbe.Stop()
}

$previousPythonUtf8 = $env:PYTHONUTF8
$env:PYTHONUTF8 = "1"
try {
    $started = Start-Process `
        -FilePath $venvPython `
        -ArgumentList @("main.py") `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -PassThru
} finally {
    if ($null -eq $previousPythonUtf8) {
        Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONUTF8 = $previousPythonUtf8
    }
}

$readyUri = "http://127.0.0.1:$uiPort/api/status"
$startupDeadline = (Get-Date).AddSeconds($StartupTimeoutSec)
$ready = $false
do {
    if ($started.HasExited) {
        $stderrTail = @(Get-Content -LiteralPath $stderrPath -Tail 10 -ErrorAction SilentlyContinue)
        throw "새 main.py가 즉시 종료됐습니다. 로그: $stderrPath`n$($stderrTail -join [Environment]::NewLine)"
    }
    try {
        Invoke-RestMethod -Uri $readyUri -Method Get -TimeoutSec 2 | Out-Null
        $ready = $true
        break
    } catch {
        Start-Sleep -Milliseconds 500
    }
} while ((Get-Date) -lt $startupDeadline)

if (-not $ready) {
    throw "main.py는 시작됐지만 ${StartupTimeoutSec}초 안에 UI가 준비되지 않았습니다: $readyUri"
}

Write-Host "재시작 완료: launcher PID=$($started.Id), UI=$readyUri"
Write-Host "stdout=$stdoutPath"
Write-Host "stderr=$stderrPath"
