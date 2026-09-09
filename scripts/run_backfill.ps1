<#
.SYNOPSIS
    backfill.bat이 호출하는 트랙 B 전 세션 분봉 백필 런처.
.DESCRIPTION
    scripts\track_b_backfill.py를 실행해 그날 F1 스냅샷의 랭크 1~5 종목에
    대한 09:00~15:30 분봉을 data\backtest_bars에 채운다.

    매 거래일 장 마감 후 한 번 돌리는 것을 전제로 한다. 이미 전 세션이
    채워진 쌍은 호출 없이 건너뛰므로 여러 번 돌려도 안전하고, 중단해도
    재실행하면 이어서 채운다.

    백필이 성공하면 이어서 data\ 백업을 뜬다(하루 한 번). 20260903에
    f1_snapshots와 backtest_bars를 통째로 잃은 사고의 대응이며, 표본이
    새로 생긴 날이 곧 백업할 이유가 가장 큰 날이다. -NoBackup 으로 끈다.

    왜 사람이 돌려야 하는지는
    docs/TRACK_B_UNIVERSE_REANALYSIS_20260831.md 제10절에 있다 — 실시간 봉
    수집은 트랙 A가 고른 종목만 따라가므로, 랭크 1 표본은 이 백필로만 쌓인다.
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$NoBackup
)

$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$backfillScript = Join-Path $repoRoot "scripts\track_b_backfill.py"
$logDir = Join-Path $repoRoot "data\logs"

# track_b_backfill.py의 EARLIEST_BACKFILL과 같은 값이다. 그 스크립트도 스스로
# 막지만, 여기서 먼저 걸러야 사용자가 파이썬 예외 대신 이유를 본다.
# 변경 시 scripts/track_b_backfill.py와 함께 맞춘다.
$EARLIEST = [TimeSpan]::new(15, 40, 0)

function Write-Section([string]$Text) {
    Write-Host ""
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}

function Write-Ok([string]$Text)   { Write-Host "  [OK] $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "  [주의] $Text" -ForegroundColor Yellow }
function Write-Fail([string]$Text) { Write-Host "  [실패] $Text" -ForegroundColor Red }

Write-Section "사전 점검"

if (-not (Test-Path $venvPython)) {
    Write-Fail "가상환경을 찾을 수 없습니다: $venvPython"
    Write-Host "    python -m venv .venv 를 먼저 실행하세요."
    exit 1
}
Write-Ok "가상환경"

if (-not (Test-Path $backfillScript)) {
    Write-Fail "백필 스크립트가 없습니다: $backfillScript"
    exit 1
}
Write-Ok "백필 스크립트"

$now = (Get-Date).TimeOfDay
if (-not $DryRun -and $now -lt $EARLIEST) {
    Write-Fail ("장 마감 전입니다. 현재 {0:hh\:mm}, 백필은 15:40 이후에만 돕니다." -f $now)
    Write-Host "    장중 분봉 API 호출이 본 매매의 유량 예산을 쓰지 않게 하려는 가드입니다."
    Write-Host "    계획만 보려면: backfill.bat -DryRun"
    exit 1
}
Write-Ok ("시각 {0:hh\:mm}" -f $now)

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logPath = Join-Path $logDir "backfill_$stamp.log"

Write-Section "백필 실행"
Write-Host "  로그: $logPath"
Write-Host ""

$env:PYTHONIOENCODING = "utf-8"
$pyArgs = @("-u", $backfillScript)
if ($DryRun) { $pyArgs += "--dry-run" }

# 아래 두 가지는 scripts\start_main.ps1과 같은 이유다.
# 1) PS 5.1은 콘솔 코드페이지(기본 CP949)로 디코딩해 한글이 깨진다.
# 2) Tee-Object는 PS 5.1에서 -Encoding을 못 받아 로그를 UTF-16으로 쓴다.
$previousOutputEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$writer = [System.IO.StreamWriter]::new(
    $logPath,
    $false,
    [System.Text.UTF8Encoding]::new($false)
)
$writer.AutoFlush = $true

$output = New-Object System.Collections.Generic.List[string]
$code = 0
Push-Location -LiteralPath $repoRoot
try {
    & $venvPython $pyArgs 2>&1 | ForEach-Object {
        $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { "$_" }
        Write-Host $line
        $writer.WriteLine($line)
        $output.Add($line)
    }
    $code = $LASTEXITCODE
} finally {
    Pop-Location
    $writer.Dispose()
    [Console]::OutputEncoding = $previousOutputEncoding
}

Write-Section "결과"

if ($code -ne 0) {
    Write-Fail "백필이 종료 코드 $code 로 끝났습니다. 위 출력과 로그를 확인하세요."
    exit $code
}

$summary = $output | Where-Object { $_ -match "'filled'" } | Select-Object -Last 1
if ($summary) {
    Write-Host "  $summary"
    if ($summary -match "'failed':\s*(\d+)" -and [int]$Matches[1] -gt 0) {
        Write-Warn "실패한 쌍이 있습니다. 다시 돌리면 이어서 채웁니다."
    }
    if ($summary -match "'budget_exhausted':\s*True") {
        Write-Warn "호출 예산이 소진됐습니다. 다시 돌려 나머지를 채우세요."
    }
}
Write-Ok "완료"

if ($DryRun) {
    Write-Host "  (계획 확인이므로 백업은 뜨지 않습니다)"
    exit 0
}
if ($NoBackup) {
    Write-Warn "-NoBackup 이 지정돼 백업을 건너뜁니다."
    exit 0
}

Write-Section "백업"

$backupScript = Join-Path $repoRoot "scripts\backup_data.py"
if (-not (Test-Path $backupScript)) {
    Write-Fail "백업 스크립트가 없습니다: $backupScript"
    exit 2
}

# 백필 로그에 이어 붙인다. 백업이 조용히 안 도는 것이 이 장치의 가장 나쁜 실패다.
$appender = [System.IO.StreamWriter]::new(
    $logPath,
    $true,
    [System.Text.UTF8Encoding]::new($false)
)
$appender.AutoFlush = $true
$backupCode = 0
Push-Location -LiteralPath $repoRoot
try {
    & $venvPython "-u" $backupScript "--skip-if-today" 2>&1 | ForEach-Object {
        $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { "$_" }
        Write-Host "  $line"
        $appender.WriteLine($line)
    }
    $backupCode = $LASTEXITCODE
} finally {
    Pop-Location
    $appender.Dispose()
}

if ($backupCode -ne 0) {
    Write-Fail "백업이 종료 코드 $backupCode 로 끝났습니다."
    Write-Host "    백필 자체는 성공했습니다 - 채워진 봉은 그대로입니다."
    Write-Host "    되돌릴 수 없는 손실을 막는 장치이므로 원인을 확인하세요."
    exit 2
}
Write-Ok "백업"
exit 0
