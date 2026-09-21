<#
.SYNOPSIS
    C1 종가 스크리닝 런처. Task Scheduler(StockBot_OvernightScreen, 15:32)가 부른다.
.DESCRIPTION
    scripts\overnight_screen.py 를 운영 트리에서 실행해 data\overnight\candidates\<date>.jsonl
    을 남긴다. run_backfill.ps1 과 같은 이유로 콘솔 인코딩을 UTF-8로 두고 로그를 UTF-8로
    쓰며, 파이썬 호출 구간만 $ErrorActionPreference="Continue" 로 낮춰 stderr(트레이스백)가
    로그에 남게 한다(PS 5.1 + Stop + 2>&1 은 첫 stderr 줄에서 스크립트를 끝낸다).

    이 저장소는 PowerShell 5.1에서 돈다. `??`, `?.`, 삼항, `&&`/`||`는 쓰지 않는다.
#>
[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$script = Join-Path $repoRoot "scripts\overnight_screen.py"
$logDir = Join-Path $repoRoot "data\logs"

function Write-Ok([string]$Text)   { Write-Host "  [OK] $Text" -ForegroundColor Green }
function Write-Fail([string]$Text) { Write-Host "  [실패] $Text" -ForegroundColor Red }

if (-not (Test-Path $venvPython)) { Write-Fail "가상환경이 없습니다: $venvPython"; exit 1 }
if (-not (Test-Path $script)) { Write-Fail "스크립트가 없습니다: $script"; exit 1 }
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logPath = Join-Path $logDir "overnight_screen_$stamp.log"
Write-Host "  로그: $logPath"

$env:PYTHONIOENCODING = "utf-8"
$pyArgs = @("-u", $script)
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
