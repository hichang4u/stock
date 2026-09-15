<#
.SYNOPSIS
    사람이 손으로 승격할 때 쓰는 promote.ps1 래퍼. 운영 트리에서 실행한다.
.DESCRIPTION
    promote.ps1은 지문이 바뀌는 승격을 두 번 실행하도록 설계돼 있다 —
    1차가 새 지문을 찍고 멈추면 운영자가 그 값을 -AcknowledgeFingerprint로
    붙여 2차를 돌린다. 이 래퍼는 그 두 번을 한 번으로 줄이되, "지문 변경은
    사람이 확인한다"는 게이트는 그대로 둔다: 1차 출력에서 값을 읽어 승격
    전/후 지문을 보여주고 y 입력을 받은 뒤에만 2차를 실행한다.

    순서:
      1. 역할 확인 — .stock-role 이 prod 가 아니면 중단
         (2026-09-15 개발 트리에서 실행한 실수 방지)
      2. .env 의 UTF-8 BOM 제거 — PS 5.1 Set-Content -Encoding UTF8 이
         남긴 BOM 을 dotenv 가 가짜 키로 읽는다. 내용은 그대로, 인코딩만
      3. promote.ps1 -Tag 1차 실행 (출력은 화면에 그대로 흘리면서 캡처)
      4. 지문 무변경이면 1차가 재시작까지 끝낸 것 — 종료
         지문 변경이면 값을 파싱해 보여주고 y 를 받은 뒤 2차 실행
      5. 오늘 이벤트 로그의 STRATEGY_FINGERPRINT_LOCKED 로 새 지문이 실제로
         기동됐는지 확인

    promote.ps1 자체는 건드리지 않는다 — 그쪽은 stdin 없는 자동화 세션에서도
    돌도록 Read-Host 를 쓰지 않는다. 이 래퍼는 사람 전용이라 프롬프트를 쓴다.

    이 저장소는 PowerShell 5.1에서 돈다. `??`, `?.`, 삼항 연산자, `&&`/`||`는
    쓰지 않는다.
.EXAMPLE
    PS D:\Private\stock-prod> .\scripts\promote_interactive.ps1 -Tag release/20260915-2
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Tag
)

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$promote = Join-Path $repoRoot "scripts\promote.ps1"
$envPath = Join-Path $repoRoot ".env"
$logDir = Join-Path $repoRoot "data\logs"

function Section([string]$t) { Write-Host ""; Write-Host "=== $t ===" -ForegroundColor Cyan }
function Fail([string]$t) { Write-Host ""; Write-Host "  [실패] $t" -ForegroundColor Red; Write-Host ""; exit 1 }

function Get-KstToday() {
    $kst = [System.TimeZoneInfo]::FindSystemTimeZoneById("Korea Standard Time")
    return [System.TimeZoneInfo]::ConvertTime([DateTimeOffset]::Now, $kst).ToString("yyyyMMdd")
}

function Invoke-Promote([string[]]$ExtraArgs) {
    # promote.ps1 은 Fail 에서 `exit 1` 을 부른다. 같은 프로세스에서 `&` 로
    # 부르면 그 exit 가 이 래퍼까지 끝내 버리므로 자식 powershell.exe 로 돌린다.
    # 자식의 stderr(git 안내문 등)는 PS 5.1 이 ErrorRecord 로 감싸므로 이 구간만
    # Continue 로 낮추고 문자열로 편다. 출력은 화면에 그대로 흘리면서 캡처한다.
    $lines = New-Object System.Collections.Generic.List[string]
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $promote, "-Tag", $Tag) + $ExtraArgs
        & powershell.exe @argList 2>&1 | ForEach-Object {
            $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { "$_" }
            Write-Host $line
            $lines.Add($line)
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    return @{ ExitCode = $code; Lines = $lines.ToArray() }
}

function Find-Fingerprint([string[]]$Lines, [string]$Pattern) {
    foreach ($line in $Lines) {
        $m = [regex]::Match($line, $Pattern)
        if ($m.Success) { return $m.Groups[1].Value }
    }
    return $null
}

function Wait-FingerprintLocked([string]$Expected, [int]$TimeoutSec) {
    # restart_main.ps1 은 새 프로세스를 띄우고 바로 돌아온다. main.py 가
    # 지문을 고정하고 로그를 쓰기까지 몇 초 걸리므로 폴링한다.
    $logPath = Join-Path $logDir ("{0}.jsonl" -f (Get-KstToday))
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $logPath) {
            $tail = Get-Content -LiteralPath $logPath -Tail 50 -Encoding UTF8 -ErrorAction SilentlyContinue
            $last = $null
            foreach ($raw in $tail) {
                if ($raw -notlike '*STRATEGY_FINGERPRINT_LOCKED*') { continue }
                try { $row = $raw | ConvertFrom-Json } catch { continue }
                if ($row.event -eq "STRATEGY_FINGERPRINT_LOCKED") { $last = $row }
            }
            if ($null -ne $last -and $last.fingerprint -eq $Expected) { return $last }
        }
        Start-Sleep -Seconds 2
    }
    return $null
}

# 자식 powershell 이 콘솔 코드페이지를 물려받으므로, 한글 출력을 정확히
# 파싱하려면 이 콘솔을 UTF-8 로 맞춘다. 끝나면 되돌린다.
$previousOutputEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    Section "역할 확인"
    if (-not (Test-Path -LiteralPath $promote -PathType Leaf)) { Fail "promote.ps1 이 없습니다: $promote" }
    $role = (Get-Content -LiteralPath (Join-Path $repoRoot ".stock-role") -ErrorAction SilentlyContinue)
    if ($null -ne $role) { $role = ($role | Select-Object -First 1).Trim().ToLower() }
    $roleLabel = "(없음)"
    if ($null -ne $role) { $roleLabel = $role }
    if ($role -ne "prod") {
        Fail "승격은 운영 트리에서만 실행합니다. 현재 역할: $roleLabel`n  운영 트리:  D:\Private\stock-prod\scripts\promote_interactive.ps1 -Tag $Tag"
    }
    Write-Host "  [OK] prod ($repoRoot)" -ForegroundColor Green

    Section ".env 인코딩"
    if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) { Fail ".env 가 없습니다: $envPath" }
    $bytes = [System.IO.File]::ReadAllBytes($envPath)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        [System.IO.File]::WriteAllBytes($envPath, $bytes[3..($bytes.Length - 1)])
        Write-Host "  [OK] UTF-8 BOM 제거 (내용 무변경)" -ForegroundColor Green
    } else {
        Write-Host "  [OK] BOM 없음" -ForegroundColor Green
    }

    Section "승격 1차: $Tag"
    $first = Invoke-Promote @()
    $ack = Find-Fingerprint $first.Lines '-AcknowledgeFingerprint\s+([0-9a-f]{12})'
    $after = $null

    if ($first.ExitCode -eq 0) {
        $after = Find-Fingerprint $first.Lines '승격 완료: .*\(지문 ([0-9a-f]{12})\)'
        Write-Host ""
        Write-Host "  지문 무변경 — 1차에서 재시작까지 끝났습니다." -ForegroundColor Green
    } elseif ($null -ne $ack) {
        $before = Find-Fingerprint $first.Lines '승격 전 지문: ([0-9a-f]{12})'
        $after = $ack
        Write-Host ""
        Write-Host "  지문이 바뀝니다." -ForegroundColor Yellow
        Write-Host "    승격 전: $before"
        Write-Host "    승격 후: $after"
        Write-Host "  stable_paper_trades 카운터가 0에서 다시 시작하고 baseline-$after 실험이 새로 열립니다." -ForegroundColor Yellow
        Write-Host ""
        $answer = Read-Host "  이 지문으로 승격하려면 y 를 입력하세요"
        if ($answer -ne "y") {
            Write-Host ""
            Write-Host "  취소했습니다. 1차가 트리를 원래 태그로 되돌려 놓았고 프로세스는 그대로 돕니다." -ForegroundColor Yellow
            Write-Host ""
            exit 0
        }

        Section "승격 2차: $Tag (지문 $after)"
        $second = Invoke-Promote @("-AcknowledgeFingerprint", $after)
        if ($second.ExitCode -ne 0) {
            Fail "2차 승격이 실패했습니다 (종료 코드 $($second.ExitCode)). 위 promote.ps1 안내를 따르세요."
        }
    } else {
        Fail "1차 승격이 실패했습니다 (종료 코드 $($first.ExitCode)). 위 promote.ps1 안내를 따르세요."
    }

    Section "기동 확인"
    if ($null -eq $after) {
        Write-Host "  [경고] 출력에서 지문을 읽지 못해 기동 확인을 건너뜁니다." -ForegroundColor Yellow
        Write-Host "  직접 확인:  Get-Content $logDir\$(Get-KstToday).jsonl -Tail 20"
        exit 0
    }
    $locked = Wait-FingerprintLocked $after 30
    if ($null -eq $locked) {
        Fail "30초 안에 STRATEGY_FINGERPRINT_LOCKED($after) 가 로그에 나타나지 않았습니다.`n  프로세스 확인:  Get-CimInstance Win32_Process -Filter `"Name='python.exe'`"`n  로그 확인:      Get-Content $logDir\$(Get-KstToday).jsonl -Tail 20"
    }
    Write-Host "  [OK] $($locked.ts)  STRATEGY_FINGERPRINT_LOCKED  $($locked.fingerprint)" -ForegroundColor Green
    Write-Host ""
    Write-Host "승격 완료: $Tag (지문 $after)" -ForegroundColor Green
    exit 0
} finally {
    [Console]::OutputEncoding = $previousOutputEncoding
}
