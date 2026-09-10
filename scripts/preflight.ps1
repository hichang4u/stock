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
# `python -m src.utils.tree_role`은 모듈 검색을 현재 working directory
# 기준으로 한다. 이 스크립트를 $repoRoot가 아닌 다른 위치(예: 다른 저장소나
# 홈 디렉터리)에서 호출하면 "No module named 'src.utils'"만 뱉고 죽는다 —
# fail-closed라 안전은 하지만 원인을 알 수 없는 트레이스백만 보여준다.
# Push-Location으로 이 호출 구간만 $repoRoot에 강제로 고정한다.
Push-Location -LiteralPath $repoRoot
try {
    & $py -m src.utils.tree_role --check
    $roleCheckExit = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($roleCheckExit -ne 0) { Fail "이 트리의 역할을 확인할 수 없습니다 (.stock-role)" }
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
# 전략 파일에 남은 100건은 mypy-baseline.txt 에 동결돼 있다. 고치면 파일이
# 바뀌어 전략 지문이 리셋되고, REAL 전환에 필요한 깨끗한 PAPER 청산 20건을
# 다시 쌓아야 한다 — 전략을 실제로 손보는 시점에 함께 치를 비용이다.
# 여기서는 새로 생긴 오류만 막는다.
& $py (Join-Path $repoRoot "scripts\mypy_baseline.py")
if ($LASTEXITCODE -ne 0) { Fail "새로 생긴 mypy 오류가 있습니다" }
Write-Ok "새 오류 없음"

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
# main.py는 시작할 때 load_dotenv()를 가장 먼저 호출해 .env의 전략 override
# (F1_~F5_, PAPER_FAST_ 등)를 os.environ에 채운 뒤 지문을 계산한다. 여기서도
# 같은 순서로 .env를 로드하지 않으면 env override가 빠진, 운영에서는 절대
# 나오지 않는 값을 계산하게 된다.
# import와 load_dotenv() 둘 다 현재 working directory 기준이라, 여기도
# 역할 확인과 같은 이유로 $repoRoot에 고정해야 한다 (위 "역할 확인" 참고).
Push-Location -LiteralPath $repoRoot
try {
    $fp = & $py -c "from dotenv import load_dotenv; load_dotenv(); from src.release import strategy_fingerprint; print(strategy_fingerprint())"
    $fpExit = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($fpExit -ne 0) { Fail "지문을 계산할 수 없습니다" }
Write-Host "FINGERPRINT=$fp"

Write-Host ""
Write-Host "  이 값은 '이 트리'의 지문입니다 (이 트리의 .env 기준)." -ForegroundColor Yellow
Write-Host "  .env는 트리마다 다르고 gitignore 대상이라, 운영 트리에서 같은 코드를" -ForegroundColor Yellow
Write-Host "  체크아웃해도 이 값과 정확히 같으리라는 보장이 없습니다." -ForegroundColor Yellow
Write-Host "  이 실행은 방금 만든 변경이 지문을 바꿨는지 확인하는 용도로만 쓰세요." -ForegroundColor Yellow
Write-Host ""
Write-Host "  승격 시 실제로 넘길 -AcknowledgeFingerprint 값은 운영 트리에서" -ForegroundColor Yellow
Write-Host "  scripts\promote.ps1이 그 트리 기준으로 계산해 알려줍니다." -ForegroundColor Yellow
Write-Host "  (참고용 비교는 data/logs/<날짜>.jsonl 의 STRATEGY_FINGERPRINT_LOCKED로도 됩니다.)" -ForegroundColor Yellow
Write-Host ""
Write-Host "사전 검사 통과" -ForegroundColor Green
exit 0
