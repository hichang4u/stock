<#
.SYNOPSIS
    C1 종가 스크리닝 런처. Task Scheduler(StockBot_OvernightScreen, 15:32)가 부른다.
.DESCRIPTION
    scripts\overnight_screen.py 를 운영 트리에서 실행해 data\overnight\candidates\<date>.jsonl
    을 남긴다. run_backfill.ps1 과 같은 이유로 콘솔 인코딩을 UTF-8로 두고 로그를 UTF-8로
    쓰며, 파이썬 호출 구간만 $ErrorActionPreference="Continue" 로 낮춰 stderr(트레이스백)가
    로그에 남게 한다(PS 5.1 + Stop + 2>&1 은 첫 stderr 줄에서 스크립트를 끝낸다).

    -Phase 1520 은 같은 스크리닝을 15:20 장중 값으로 돌려 data\overnight\candidates_1520\ 에
    따로 남긴다(StockBot_OvernightScreen1520, 15:20). 판정에 쓰지 않는 병행 기록이다
    (스펙 §6(a)). 15:19~15:29 사이에만 돈다.

    이 저장소는 PowerShell 5.1에서 돈다. `??`, `?.`, 삼항, `&&`/`||`는 쓰지 않는다.
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [ValidateSet("close", "1520")]
    [string]$Phase = "close"
)

$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$script = Join-Path $repoRoot "scripts\overnight_screen.py"
$logDir = Join-Path $repoRoot "data\logs"

# run_backfill.ps1의 $EARLIEST 가드와 같은 패턴이다. C1은 종가 기준 후보를 기록하므로
# 장중에 돌면 그 시점의 장중 값을 종가로 잘못 남긴다 — 15:31 전에는 막는다.
$EARLIEST = [TimeSpan]::new(15, 31, 0)
# 15:20 판은 마감 동시호가 안에서 "그때 알 수 있는 값"을 재는 것이 목적이다. 그 창을
# 벗어나면 다른 시점의 값이 섞이므로 15:19~15:29에만 돈다.
$INTRADAY_FROM = [TimeSpan]::new(15, 19, 0)
$INTRADAY_UNTIL = [TimeSpan]::new(15, 29, 0)

function Write-Ok([string]$Text)   { Write-Host "  [OK] $Text" -ForegroundColor Green }
function Write-Fail([string]$Text) { Write-Host "  [실패] $Text" -ForegroundColor Red }

if (-not (Test-Path $venvPython)) { Write-Fail "가상환경이 없습니다: $venvPython"; exit 1 }
if (-not (Test-Path $script)) { Write-Fail "스크립트가 없습니다: $script"; exit 1 }

$now = (Get-Date).TimeOfDay
if ($Phase -eq "1520") {
    if (-not $DryRun -and ($now -lt $INTRADAY_FROM -or $now -ge $INTRADAY_UNTIL)) {
        Write-Fail "15:20 판은 15:19~15:29 사이에만 돕니다 — 다른 시점의 장중 값을 섞지 않기 위한 가드."
        Write-Host "    계획만 보려면: run_overnight_screen.ps1 -Phase 1520 -DryRun"
        exit 1
    }
} elseif (-not $DryRun -and $now -lt $EARLIEST) {
    Write-Fail "장 마감 전입니다. 15:31 이후에만 돕니다 — 장중 값을 종가로 기록하지 않기 위한 가드."
    Write-Host "    계획만 보려면: run_overnight_screen.ps1 -DryRun"
    exit 1
}

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logName = "overnight_screen_$stamp.log"
if ($Phase -eq "1520") { $logName = "overnight_screen_1520_$stamp.log" }
$logPath = Join-Path $logDir $logName
Write-Host "  로그: $logPath"

$env:PYTHONIOENCODING = "utf-8"
$pyArgs = @("-u", $script, "--phase", $Phase)
if ($DryRun) { $pyArgs += "--dry-run" }

$previousOutputEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$writer = [System.IO.StreamWriter]::new($logPath, $false, [System.Text.UTF8Encoding]::new($false))
$writer.AutoFlush = $true

$code = 0
Push-Location -LiteralPath $repoRoot
$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & $venvPython $pyArgs 2>&1 | ForEach-Object {
        $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { "$_" }
        Write-Host $line
        $writer.WriteLine($line)
    }
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 1 }
} finally {
    $ErrorActionPreference = $previousErrorAction
    Pop-Location
    $writer.Dispose()
    [Console]::OutputEncoding = $previousOutputEncoding
}

if ($code -ne 0) {
    Write-Fail "스크리닝이 종료 코드 $code 로 끝났습니다. 로그를 확인하세요."
    exit $code
}
Write-Ok "완료"
exit 0
