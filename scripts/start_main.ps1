<#
.SYNOPSIS
    stock.bat이 호출하는 수동 실행 런처.
.DESCRIPTION
    사전 점검 8단계를 수행한 뒤 main.py를 포그라운드로 실행하고,
    화면과 data\logs\launcher_*.log에 동시에 출력한다.

    이 스크립트는 실행 중인 프로세스를 종료하지 않는다. 재시작은
    안전점검(restart_guard.py)을 거치는 scripts\restart_main.ps1이 담당한다.

    설계: docs/superpowers/plans/2026-08-14-stock-bat-launcher.md
#>
[CmdletBinding()]
param(
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$mainScript = Join-Path $repoRoot "main.py"
$envPath = Join-Path $repoRoot ".env"
$logDir = Join-Path $repoRoot "data\logs"

# 장 시간 안내용 상수. src/schedule_times.py의 값을 옮겨 적었다.
# 런처가 전략 모듈을 import하면 기동 경로가 무거워지므로 상수로 둔다.
# 변경 시 src/schedule_times.py와 함께 맞춘다.
$FIRST_JOB = [TimeSpan]::new(8, 59, 45)      # PAPER_FAST_PROBE
$ENTRY_DEADLINE = [TimeSpan]::new(9, 11, 0)  # F3_FILL_DEADLINE
$EXIT_TIME = [TimeSpan]::new(15, 15, 0)      # F5_EXEC

function Write-Section([string]$Text) {
    Write-Host ""
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}

function Write-Ok([string]$Text) {
    Write-Host "  [OK] $Text" -ForegroundColor Green
}

function Write-Info([string]$Text) {
    Write-Host "  $Text"
}

function Fail([string]$Reason, [string[]]$HowToFix) {
    Write-Host ""
    Write-Host "  [실패] $Reason" -ForegroundColor Red
    if ($HowToFix) {
        Write-Host ""
        Write-Host "  해결 방법:" -ForegroundColor Yellow
        foreach ($line in $HowToFix) {
            Write-Host "    $line" -ForegroundColor Yellow
        }
    }
    Write-Host ""
    exit 1
}

function Read-DotEnv([string]$Path) {
    $map = @{}
    foreach ($line in (Get-Content -LiteralPath $Path -Encoding UTF8)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $idx = $trimmed.IndexOf("=")
        if ($idx -lt 1) { continue }
        $key = $trimmed.Substring(0, $idx).Trim()
        $value = $trimmed.Substring($idx + 1).Trim()
        if ($value.Length -ge 2) {
            $quoted = ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                      ($value.StartsWith("'") -and $value.EndsWith("'"))
            if ($quoted) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        $map[$key] = $value
    }
    return $map
}

function Get-KstNow() {
    $kst = [System.TimeZoneInfo]::FindSystemTimeZoneById("Korea Standard Time")
    return [System.TimeZoneInfo]::ConvertTime([DateTimeOffset]::Now, $kst)
}

Write-Section "사전 점검"

# 1. 가상환경
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Fail "가상환경을 찾을 수 없습니다: $venvPython" @(
        "저장소 루트에서 아래를 차례로 실행하세요.",
        "",
        "  python -m venv .venv",
        "  .\.venv\Scripts\Activate.ps1",
        "  pip install -r requirements.txt"
    )
}
Write-Ok "가상환경"

# 2. main.py
if (-not (Test-Path -LiteralPath $mainScript -PathType Leaf)) {
    Fail "main.py를 찾을 수 없습니다: $mainScript" @(
        "stock.bat이 저장소 루트에 있는지 확인하세요.",
        "다른 폴더로 복사해서 실행하면 이 오류가 납니다."
    )
}
Write-Ok "main.py"

# 3. .env
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    Fail ".env 파일이 없습니다: $envPath" @(
        "저장소 루트에서 아래를 실행한 뒤 계좌 정보를 채우세요.",
        "",
        "  copy .env.example .env",
        "",
        "앱 키 발급과 Telegram 봇 설정은 docs\DEV_ENV.md 6~7장을 보세요."
    )
}
$envMap = Read-DotEnv $envPath
Write-Ok ".env"

# 4. 실행 모드
$mode = $envMap["KIS_MODE"]
if (-not $mode) {
    Fail ".env에 KIS_MODE가 없습니다." @(
        ".env에 아래 한 줄을 추가하세요.",
        "",
        "  KIS_MODE=PAPER"
    )
}
if ($mode -notin @("DRY_RUN", "PAPER", "REAL")) {
    Fail "KIS_MODE 값을 알 수 없습니다: $mode" @(
        "DRY_RUN, PAPER, REAL 중 하나여야 합니다.",
        "처음이라면 PAPER를 쓰세요."
    )
}

$account = $envMap["KIS_ACCT_NO"]
if (-not $account) { $account = $envMap["KIS_ACCOUNT_NO"] }
if (-not $account) { $account = "(미설정)" }
$maskedAccount = if ($account.Length -gt 4) {
    ("*" * ($account.Length - 4)) + $account.Substring($account.Length - 4)
} else {
    $account
}

$modeColor = if ($mode -eq "REAL") { "Red" } else { "Green" }
Write-Host "  [OK] 실행 모드: " -ForegroundColor Green -NoNewline
Write-Host $mode -ForegroundColor $modeColor
Write-Info "     계좌: $maskedAccount"

if ($mode -eq "REAL") {
    Write-Host ""
    Write-Host "  ****  실계좌 모드입니다. 실제 돈으로 주문이 나갑니다.  ****" -ForegroundColor Red
    Write-Host ""
    $answer = Read-Host "  계속하려면 y를 입력하세요"
    if ($answer -ne "y") {
        Write-Host ""
        Write-Host "  사용자가 취소했습니다." -ForegroundColor Yellow
        Write-Host ""
        exit 0
    }
}

# 5. 중복 실행
$uiPort = 8080
if ($envMap.ContainsKey("UI_PORT")) {
    $parsedPort = 0
    if ([int]::TryParse($envMap["UI_PORT"], [ref]$parsedPort)) {
        $uiPort = $parsedPort
    }
}

function Get-RunningMain([string]$ExpectedPython) {
    # main.py는 main.pid의 첫 바이트에 msvcrt 잠금을 건다(main.py의
    # _try_lock_pid_file). 실행 중에는 Get-Content가 잠금 위반으로 실패하므로
    # PID 파일을 읽지 않는다. 대신 이 저장소의 venv python이 main.py를 돌리고
    # 있는지로 판정한다. 실행 파일 경로까지 대조하므로 PID 재사용에도 안전하고,
    # 런처를 거치지 않고 띄운 봇도 잡아낸다.
    $candidates = Get-CimInstance Win32_Process `
        -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue
    foreach ($proc in $candidates) {
        if (-not $proc.ExecutablePath) { continue }
        if ([System.IO.Path]::GetFullPath($proc.ExecutablePath) -ne $ExpectedPython) {
            continue
        }
        if ($proc.CommandLine -and $proc.CommandLine -like "*main.py*") {
            return $proc
        }
    }
    return $null
}

$running = Get-RunningMain $venvPython
if ($running) {
    Write-Host ""
    Write-Host "  이미 실행 중입니다." -ForegroundColor Yellow
    Write-Info "     PID: $($running.ProcessId)"
    Write-Info "     화면: http://127.0.0.1:$uiPort"
    Write-Host ""
    Write-Host "  재시작하려면 이 창을 닫고 아래를 실행하세요." -ForegroundColor Yellow
    Write-Host "    .\scripts\restart_main.ps1" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  (지금 실행 중인 봇은 그대로 둡니다.)"
    Write-Host ""
    exit 0
}
Write-Ok "중복 실행 없음"

# 6. UI 포트
$probe = [System.Net.Sockets.TcpListener]::new(
    [System.Net.IPAddress]::Loopback,
    $uiPort
)
$portFree = $true
$portError = ""
try {
    $probe.Start()
} catch {
    $portFree = $false
    $portError = $_.Exception.Message
} finally {
    try { $probe.Stop() } catch { }
}
if (-not $portFree) {
    Fail "화면 포트 $uiPort 를 쓸 수 없습니다. ($portError)" @(
        "다른 프로그램이 이 포트를 쓰고 있습니다.",
        ".env에 아래처럼 다른 포트를 지정하세요.",
        "",
        "  UI_PORT=8081"
    )
}
Write-Ok "화면 포트 $uiPort"

# 7. 장 시간 안내 (차단하지 않음)
$now = Get-KstNow
$timeOfDay = $now.TimeOfDay
$isWeekend = $now.DayOfWeek -in @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday)

Write-Section "지금 시각 기준 안내"
Write-Info "현재 (KST): $($now.ToString('yyyy-MM-dd HH:mm:ss'))"

if ($isWeekend) {
    Write-Host "  주말입니다. 장이 열리지 않아 매매는 일어나지 않습니다." -ForegroundColor Yellow
} elseif ($timeOfDay -lt $FIRST_JOB) {
    Write-Host "  장 시작 전입니다. 모든 단계가 예정대로 동작합니다." -ForegroundColor Green
} elseif ($timeOfDay -lt $ENTRY_DEADLINE) {
    Write-Host "  진입 시도 구간입니다. 늦게 켰다면 일부 단계를 건너뜁니다." -ForegroundColor Yellow
} elseif ($timeOfDay -lt $EXIT_TIME) {
    Write-Host "  09:11이 지나 신규 매수는 하지 않습니다." -ForegroundColor Yellow
    Write-Info "보유 중이라면 감시와 마감 청산은 정상 동작합니다."
} else {
    Write-Host "  오늘 매매 시간(15:15)이 지났습니다." -ForegroundColor Yellow
}
Write-Info "공휴일 여부는 확인하지 않습니다. 휴장일이면 봇이 스스로 판단합니다."

# 8. 트리 역할과 릴리스 상태
Write-Section "8. 트리 역할과 릴리스 상태"
& $venvPython -m src.utils.tree_role --check
if ($LASTEXITCODE -ne 0) {
    Fail "이 트리는 기동할 수 없는 상태입니다" @(
        "운영 트리라면 릴리스 태그에 있고 워킹트리가 깨끗해야 합니다.",
        "  현재 상태 확인:  .venv\Scripts\python.exe -m src.utils.tree_role --check",
        "  승격:            scripts\promote.ps1 -Tag <릴리스태그>",
        "개발 트리라면 .stock-role 파일에 dev 를 넣으세요."
    )
}
Write-Ok "역할·릴리스 상태 확인됨"

if ($CheckOnly) {
    Write-Host ""
    Write-Host "점검만 수행했습니다 (-CheckOnly). 실행하지 않습니다." -ForegroundColor Cyan
    Write-Host ""
    exit 0
}

Write-Section "실행"

New-Item -ItemType Directory -Path $logDir -Force | Out-Null

# 런처 로그는 실행마다 쌓인다. 최근 30개만 남긴다.
# 새 파일을 곧 만들 것이므로 기존 29개까지만 남기고 지운다.
Get-ChildItem -LiteralPath $logDir -Filter "launcher_*.log" -File `
    -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 29 |
    Remove-Item -Force -ErrorAction SilentlyContinue

$stamp = (Get-KstNow).ToString("yyyyMMdd_HHmmss")
$consoleLog = Join-Path $logDir "launcher_$stamp.log"

Write-Info "모드: $mode"
Write-Info "화면: http://127.0.0.1:$uiPort"
Write-Info "로그: $consoleLog"
Write-Host ""
Write-Host "  중지하려면 이 창에서 Ctrl+C를 누르세요." -ForegroundColor Yellow
Write-Host ""

# PYTHONUTF8/PYTHONUNBUFFERED는 _STRATEGY_ENV_PREFIXES 밖이라 지문에
# 영향이 없다. 전략 환경변수(F1_~F5_, VI_, KIS_RATE_ 등)를 여기에
# 추가하면 지문이 바뀌어 PAPER 실적이 리셋된다.
$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"

# main.py의 로그는 logging.StreamHandler() 기본값이라 stderr로 나온다.
# Windows PowerShell 5.1은 네이티브 명령의 stderr를 NativeCommandError로
# 감싸므로, $ErrorActionPreference="Stop"이면 첫 로그 줄에서 런처가 죽는다.
# 실행 구간에서만 Continue로 낮추고 끝나면 되돌린다.
$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"

# PYTHONUTF8=1로 파이썬은 UTF-8을 내보내지만, PS 5.1은 콘솔 코드페이지
# (기본 CP949)로 디코딩해 한글이 깨진다. 실행 구간에서만 UTF-8로 맞춘다.
$previousOutputEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

# Tee-Object는 PS 5.1에서 -Encoding을 받지 못해 로그를 UTF-16으로 쓴다.
# 사후 분석이 UTF-8 기준이므로 StreamWriter로 직접 tee한다. AutoFlush로
# 화면과 파일이 같은 속도로 흐른다.
$writer = [System.IO.StreamWriter]::new(
    $consoleLog,
    $false,
    [System.Text.UTF8Encoding]::new($false)
)
$writer.AutoFlush = $true

$exitCode = 0
Push-Location -LiteralPath $repoRoot
try {
    # -u와 PYTHONUNBUFFERED로 버퍼링을 없애야 실시간으로 흘러간다.
    # 2>&1로 트레이스백까지 파일에 남긴다. stderr 줄은 ErrorRecord로
    # 들어오므로 문자열로 펴서 빨간 오류 서식과 위치 안내를 없앤다.
    & $venvPython -u main.py 2>&1 | ForEach-Object {
        $line = if ($_ -is [System.Management.Automation.ErrorRecord]) {
            $_.ToString()
        } else {
            "$_"
        }
        Write-Host $line
        $writer.WriteLine($line)
    }
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
    $writer.Dispose()
    [Console]::OutputEncoding = $previousOutputEncoding
    $ErrorActionPreference = $previousErrorAction
}

Write-Section "종료"

$CTRL_C_EXIT = -1073741510  # 0xC000013A STATUS_CONTROL_C_EXIT

if ($exitCode -eq 0) {
    Write-Host "  정상 종료되었습니다." -ForegroundColor Green
} elseif ($exitCode -eq $CTRL_C_EXIT) {
    Write-Host "  Ctrl+C로 중단했습니다." -ForegroundColor Yellow
} else {
    Write-Host "  비정상 종료되었습니다 (종료 코드: $exitCode)" -ForegroundColor Red
    Write-Host ""
    Write-Host "  마지막 로그 15줄:" -ForegroundColor Yellow
    Get-Content -LiteralPath $consoleLog -Tail 15 -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Host "    $_" }
}

Write-Host ""
Write-Info "실행 로그: $consoleLog"
Write-Info "이벤트 로그: $logDir\$((Get-KstNow).ToString('yyyyMMdd')).jsonl"
Write-Host ""
Write-Info "Ctrl+C로 중단하면 실행 로그의 마지막 몇 줄이 빠질 수 있습니다."
Write-Info "사후 확인은 이벤트 로그(.jsonl)를 먼저 보세요."
Write-Host ""

exit $exitCode
