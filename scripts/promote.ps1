<#
.SYNOPSIS
    운영 트리를 릴리스 태그로 승격한다.
.DESCRIPTION
    운영 트리에서만 실행된다. 태그를 fetch 해 지문 변화를 보여주고,
    포지션이 안전할 때만 체크아웃 후 재시작한다.

    지문이 바뀌는 승격은 -AcknowledgeFingerprint 로 예상 지문을 명시해야
    한다. 값은 개발 트리의 preflight.ps1 이 출력한다. 승격 후 실제 지문과
    다르면 중단한다 — 맹목적으로 붙일 수 있는 스위치가 아니다.

    이 저장소는 PowerShell 5.1에서 돈다. `??`, `?.`, 삼항 연산자, `&&`/`||`는
    쓰지 않는다. 자동화된 세션이 stdin을 닫은 채 이 스크립트를 돌리므로
    Read-Host 등 입력을 기다리는 호출도 쓰지 않는다.

    설계: docs/superpowers/specs/2026-09-10-prod-dev-separation-design.md
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Tag,
    [string]$AcknowledgeFingerprint = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$py = Join-Path $repoRoot ".venv\Scripts\python.exe"

function Section([string]$t) { Write-Host ""; Write-Host "=== $t ===" -ForegroundColor Cyan }
function Fail([string]$t) { Write-Host "  [실패] $t" -ForegroundColor Red; exit 1 }

function Get-Fingerprint {
    # main.py는 시작할 때 load_dotenv()를 먼저 호출해 .env의 전략 override
    # (F1_~F5_, PAPER_FAST_ 등, src/release.py의 _STRATEGY_ENV_PREFIXES)를
    # os.environ에 채운 뒤 지문을 계산한다. PowerShell은 .env를 로드하지
    # 않으므로 여기서 dotenv를 직접 부르지 않으면 override가 빠진, 운영
    # 프로세스에서는 절대 나오지 않는 값을 계산하게 된다. scripts/preflight.ps1
    # 과 반드시 같은 방식을 쓴다.
    $v = & $py -c "from dotenv import load_dotenv; load_dotenv(); from src.release import strategy_fingerprint; print(strategy_fingerprint())"
    if ($LASTEXITCODE -ne 0) { Fail "지문을 계산할 수 없습니다" }
    return $v.Trim()
}

Section "역할 확인"
$role = (Get-Content (Join-Path $repoRoot ".stock-role") -ErrorAction SilentlyContinue)
if ($null -ne $role) { $role = $role.Trim().ToLower() }
$roleLabel = "(없음)"
if ($null -ne $role) { $roleLabel = $role }
if ($role -ne "prod") { Fail "승격은 운영 트리에서만 실행합니다. 현재 역할: $roleLabel" }
Write-Host "  [OK] prod" -ForegroundColor Green

Section "안전 상태 확인"
# restart_guard.py의 main()은 안전하면 0, 아니면 2를 반환한다 (1이 아니다).
# 둘 다 커버하도록 -ne 0으로 분기한다.
& $py (Join-Path $repoRoot "scripts\restart_guard.py") --root $repoRoot
if ($LASTEXITCODE -ne 0) { Fail "포지션이 안전 상태가 아닙니다. 청산 후 다시 시도하세요" }
Write-Host "  [OK] IDLE/CLOSED" -ForegroundColor Green

$before = Get-Fingerprint
Write-Host "  현재 지문: $before"

# 체크아웃 전에 원래 위치를 저장해 둔다. 실패 경로에서 이 위치로 되돌린다.
$origin_ref = (& git -C $repoRoot rev-parse HEAD).Trim()

Section "태그 가져오기"
& git -C $repoRoot fetch origin --tags
if ($LASTEXITCODE -ne 0) { Fail "origin fetch 실패" }
& git -C $repoRoot rev-parse --verify "refs/tags/$Tag" | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "태그를 찾을 수 없습니다: $Tag" }

Section "변경 내역"
& git -C $repoRoot log --oneline "HEAD..refs/tags/$Tag"

Section "체크아웃"
& git -C $repoRoot checkout --detach "refs/tags/$Tag"
if ($LASTEXITCODE -ne 0) { Fail "체크아웃 실패" }

$after = Get-Fingerprint
Write-Host ""
Write-Host "  승격 전 지문: $before"
Write-Host "  승격 후 지문: $after"

if ($before -ne $after) {
    Write-Host ""
    Write-Host "  지문이 바뀝니다 — 2단 승격입니다." -ForegroundColor Yellow
    Write-Host "  stable_paper_trades 카운터가 0에서 다시 시작하고," -ForegroundColor Yellow
    Write-Host "  baseline-$after 실험이 새로 열립니다." -ForegroundColor Yellow
    if ($AcknowledgeFingerprint -ne $after) {
        & git -C $repoRoot checkout --detach $origin_ref 2>$null
        Fail @"
지문 변경을 확인하지 않았습니다. 원래 위치($origin_ref)로 되돌렸습니다.
  다시 실행:  scripts\promote.ps1 -Tag $Tag -AcknowledgeFingerprint $after
"@
    }
    Write-Host "  [확인됨] $AcknowledgeFingerprint" -ForegroundColor Green
} else {
    Write-Host "  지문 무변경 — 1단 승격입니다." -ForegroundColor Green
}

Section "재시작"
# restart_main.ps1은 [CmdletBinding(SupportsShouldProcess, ConfirmImpact="High")]라
# 기본적으로 확인 프롬프트를 띄운다. 자동화된 세션은 stdin이 닫혀 있어
# 프롬프트에서 멈추므로 -Confirm:$false로 명시적으로 억제한다. restart_main.ps1은
# 내부에서 restart_guard.py를 다시 돌리지만, 여기서 한 번 더 앞서 확인해 둔
# 이유는 체크아웃으로 트리가 바뀌기 *전에* 안전 상태를 걸러내기 위해서다.
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repoRoot "scripts\restart_main.ps1") -Confirm:$false
if ($LASTEXITCODE -ne 0) { Fail "재시작 실패" }

Write-Host ""
Write-Host "승격 완료: $Tag (지문 $after)" -ForegroundColor Green
exit 0
