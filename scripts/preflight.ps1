<#
.SYNOPSIS
    개발 트리에서 승격 전 검사를 돌린다.
.DESCRIPTION
    ruff / mypy / pytest 를 순서대로 돌리고, 현재 전략 지문과 워킹트리
    상태를 출력한다. 지문이 운영과 다르면 승격은 2단이며 증거 카운터가
    리셋된다 (설계 3절).

    ruff/mypy는 개발 도구라 모든 venv에 늘 설치돼 있지는 않다. 이 스크립트는
    돌리기 전에 각 도구가 임포트 가능한지 먼저 확인하고, 없으면 건너뛰어
    통과시키는 대신 분명한 한글 메시지를 내고 실패한다 — 조용히 넘어가는
    게이트는 게이트가 아니다.

    설계: docs/superpowers/specs/2026-09-10-prod-dev-separation-design.md
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$py = Join-Path $repoRoot ".venv\Scripts\python.exe"

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

function Test-PyModule([string]$Python, [string]$Module) {
    # `python -m <module>`을 바로 돌리면 없을 때 "No module named ..." 라는
    # 파이썬 트레이스백이 그대로 나온다. 여기서 먼저 임포트 가능 여부만
    # 조용히 확인해서, 없는 경우를 스크립트가 직접 판단하고 분명한 한글
    # 메시지로 실패시킨다.
    & $Python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('$Module') else 1)" *> $null
    return ($LASTEXITCODE -eq 0)
}

$installHint = ".venv\Scripts\python.exe -m pip install -r requirements-dev.txt"

Write-Section "역할 확인"
& $py -m src.utils.tree_role --check
if ($LASTEXITCODE -ne 0) { Fail "이 트리의 역할을 확인할 수 없습니다 (.stock-role)" }
Write-Ok "이 트리는 승격 전 검사를 돌릴 수 있습니다"

Write-Section "ruff"
if (-not (Test-PyModule $py "ruff")) {
    Fail "ruff가 이 venv에 설치되어 있지 않습니다." @(
        "$installHint 를 실행한 뒤 다시 시도하세요."
    )
}
& $py -m ruff check .
if ($LASTEXITCODE -ne 0) { Fail "ruff 위반이 있습니다" }
Write-Ok "0건"

Write-Section "mypy"
if (-not (Test-PyModule $py "mypy")) {
    Fail "mypy가 이 venv에 설치되어 있지 않습니다." @(
        "$installHint 를 실행한 뒤 다시 시도하세요."
    )
}
& $py -m mypy .
if ($LASTEXITCODE -ne 0) { Fail "mypy 오류가 있습니다" }
Write-Ok "오류 없음"

Write-Section "pytest"
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { Fail "테스트가 실패했습니다" }
Write-Ok "전체 통과"

Write-Section "워킹트리"
$dirty = & git -C $repoRoot status --porcelain
if ($dirty) {
    Write-Host "  [경고] 커밋되지 않은 변경이 있습니다. 태그 전에 커밋하세요:" -ForegroundColor Yellow
    $dirty | ForEach-Object { Write-Host "    $_" }
} else {
    Write-Ok "클린"
}

Write-Section "전략 지문"
$fp = & $py -c "from src.release import strategy_fingerprint; print(strategy_fingerprint())"
if ($LASTEXITCODE -ne 0) { Fail "지문을 계산할 수 없습니다" }
Write-Host "FINGERPRINT=$fp"

Write-Host ""
Write-Host "  이 값이 현재 운영 지문과 다르면 2단 승격입니다." -ForegroundColor Yellow
Write-Host "  운영 지문 확인:  운영 트리에서 같은 명령을 돌리거나" -ForegroundColor Yellow
Write-Host "                   data/logs/<날짜>.jsonl 의 STRATEGY_FINGERPRINT_LOCKED 를 봅니다." -ForegroundColor Yellow
Write-Host ""
Write-Host "  2단이면 승격 시 아래를 붙입니다:" -ForegroundColor Yellow
Write-Host "    scripts\promote.ps1 -Tag <태그> -AcknowledgeFingerprint $fp" -ForegroundColor Yellow
Write-Host ""
Write-Host "사전 검사 통과" -ForegroundColor Green
exit 0
