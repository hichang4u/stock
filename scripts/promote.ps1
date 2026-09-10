<#
.SYNOPSIS
    운영 트리를 릴리스 태그로 승격한다.
.DESCRIPTION
    운영 트리에서만 실행된다. 태그를 fetch 해 지문 변화를 보여주고,
    포지션이 안전할 때만 체크아웃 후 재시작한다.

    지문이 바뀌는 승격은 -AcknowledgeFingerprint 로 예상 지문을 명시해야
    한다. 이 값의 권위 있는 출처는 이 스크립트 자신이다 — 체크아웃 직후
    운영 트리 기준으로 지문을 계산해 화면에 찍어 준다(개발 트리의
    preflight.ps1이 찍는 값은 .env가 달라 참고용일 뿐, 그대로 믿으면 안
    된다). 값이 다르면 중단하고 그 자리에서 정확한 재실행 명령을 알려준다
    — 맹목적으로 붙일 수 있는 스위치가 아니다.

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

function Get-Fingerprint([string]$RollbackRef = "") {
    # main.py는 시작할 때 load_dotenv()를 먼저 호출해 .env의 전략 override
    # (F1_~F5_, PAPER_FAST_ 등, src/release.py의 _STRATEGY_ENV_PREFIXES)를
    # os.environ에 채운 뒤 지문을 계산한다. PowerShell은 .env를 로드하지
    # 않으므로 여기서 dotenv를 직접 부르지 않으면 override가 빠진, 운영
    # 프로세스에서는 절대 나오지 않는 값을 계산하게 된다. scripts/preflight.ps1
    # 과 반드시 같은 방식을 쓴다.
    #
    # `python -c`의 `import src.release`와 `load_dotenv()`는 둘 다 현재
    # working directory 기준이다 — 이 스크립트를 $repoRoot가 아닌 다른
    # 위치(예: 개발 트리)에서 호출하면 엉뚱한 src/release.py와 엉뚱한 .env를
    # 읽어 놓고도 정상 종료해 버린다. 역할 검사(.stock-role)는 이미
    # $repoRoot에 고정돼 있어 이 문제를 잡아내지 못한다. Push-Location으로
    # 이 호출 구간만 $repoRoot에 강제로 고정한다.
    Push-Location -LiteralPath $repoRoot
    try {
        $v = & $py -c "from dotenv import load_dotenv; load_dotenv(); from src.release import strategy_fingerprint; print(strategy_fingerprint())"
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($exitCode -ne 0) {
        if ($RollbackRef) {
            $restored = Restore-OriginRef $RollbackRef
            if ($restored) {
                Fail "지문을 계산할 수 없습니다. 원래 위치($RollbackRef)로 되돌렸습니다."
            }
            Fail @"
지문을 계산할 수 없고, 원래 위치($RollbackRef)로 되돌리는 것도 실패했습니다.
  트리 상태를 직접 확인하세요:  git -C $repoRoot status
  수동 복구:  git -C $repoRoot checkout --detach $RollbackRef
"@
        }
        Fail "지문을 계산할 수 없습니다"
    }
    return $v.Trim()
}

function Restore-OriginRef([string]$Ref) {
    # git checkout은 정보성 안내("Previous HEAD position was ...",
    # "HEAD is now at ...")를 stderr로 낸다. PS 5.1은 $ErrorActionPreference
    # = "Stop"인 상태에서 `2>`로 리다이렉트된 네이티브 명령의 stderr 줄을
    # 터미네이팅 NativeCommandError로 승격시킨다 — git 자체는 성공했는데도
    # 이 스크립트가 그 자리에서 죽어, 뒤이어 나와야 할 Fail 안내(특히
    # -AcknowledgeFingerprint 값)를 전혀 보여주지 못한다. 이 호출 구간에서만
    # Continue로 낮춰 정보성 줄을 조용히 버린 뒤 원래대로 복원한다.
    #
    # 반환값은 실제 복원 성공 여부다(호출부가 그동안 $LASTEXITCODE를 보지
    # 않고 "되돌렸습니다"를 무조건 출력했다 — 정작 운영자가 진실을 가장
    # 필요로 하는 실패 경로에서 거짓 안심을 준 것과 같다).
    $previousErrorAction = $ErrorActionPreference
    $restoreOk = $false
    try {
        $ErrorActionPreference = "Continue"
        & git -C $repoRoot checkout --detach $Ref 2>$null
        $restoreOk = ($LASTEXITCODE -eq 0)
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    return $restoreOk
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

Section "워킹트리 확인"
# 태그 하나로 롤백을 보장하려면 지금 트리가 태그가 가리키는 내용과
# 정확히 같아야 한다. 커밋되지 않은 변경(예: 원격에서 다른 브랜치로
# checkout --detach한 뒤 충돌 없이 얹힌 로컬 수정)이 있으면 승격은
# "성공"을 찍지만 실제로 도는 코드는 태그가 아니라 그 변경분이다.
# tree_role.check_release_state가 운영 기동 시 같은 검사를 하지만,
# 승격 시점에도 독립적으로 막아야 기동 전에 조기 발견된다.
$dirty = & git -C $repoRoot status --porcelain
if ($LASTEXITCODE -ne 0) { Fail "워킹트리 상태를 확인할 수 없습니다" }
if ($dirty) {
    $firstDirty = ($dirty | Select-Object -First 1)
    Fail "운영 워킹트리가 깨끗하지 않습니다: $firstDirty"
}
Write-Host "  [OK] 클린" -ForegroundColor Green

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

$after = Get-Fingerprint $origin_ref
Write-Host ""
Write-Host "  승격 전 지문: $before"
Write-Host "  승격 후 지문: $after"

if ($before -ne $after) {
    Write-Host ""
    Write-Host "  지문이 바뀝니다 — 2단 승격입니다." -ForegroundColor Yellow
    Write-Host "  stable_paper_trades 카운터가 0에서 다시 시작하고," -ForegroundColor Yellow
    Write-Host "  baseline-$after 실험이 새로 열립니다." -ForegroundColor Yellow
    if ($AcknowledgeFingerprint -ne $after) {
        $restored = Restore-OriginRef $origin_ref
        if ($restored) {
            Fail @"
지문 변경을 확인하지 않았습니다. 원래 위치($origin_ref)로 되돌렸습니다.
  다시 실행:  scripts\promote.ps1 -Tag $Tag -AcknowledgeFingerprint $after
"@
        }
        Fail @"
지문 변경을 확인하지 않았고, 원래 위치($origin_ref)로 되돌리는 것도
  실패했습니다. 트리가 지금 새 태그에 체크아웃된 채로 남아 있을 수 있습니다.
  직접 확인:  git -C $repoRoot status
  수동 복구:  git -C $repoRoot checkout --detach $origin_ref
"@
    }
    Write-Host "  [확인됨] $AcknowledgeFingerprint" -ForegroundColor Green
} else {
    Write-Host "  지문 무변경 — 1단 승격입니다." -ForegroundColor Green
}

Section "재시작"
# restart_main.ps1은 [CmdletBinding(SupportsShouldProcess, ConfirmImpact="High")]라
# 기본적으로 확인 프롬프트를 띄운다. 자동화된 세션은 stdin이 닫혀 있어
# 프롬프트에서 멈추므로 -Confirm:$false로 억제해야 한다.
#
# 별도의 powershell.exe -File로 실행하면(이전 구현) 그 자식 프로세스가 인자를
# 문자열로 재파싱하면서 -Confirm:$false를 SwitchParameter로 바인딩하지 못해
# "Cannot convert 'System.String' to ... 'SwitchParameter'" 예외가 난다
# (PS 5.1에서 실측). 그 예외는 restart_main.ps1의 본문이 시작되기도 전인
# 파라미터 바인딩 단계에서 나므로 restart_guard 재점검조차 돌지 않고, 트리는
# 이미 새 태그로 체크아웃된 채 남는다. `&`로 같은 프로세스의 자식 스코프에서
# 직접 호출하면 스위치가 정상적으로 바인딩된다. restart_main.ps1은 내부에서
# 자체 $ErrorActionPreference를 설정하고 함수를 정의하지만, `&` 호출은 자식
# 스코프를 만들어 그 변경이 이 스크립트로 새지 않는다(task-7-report.md
# Fix Round 1의 실측 확인 참고).
#
# restart_main.ps1은 내부에서 restart_guard.py를 다시 돌리지만, 여기서 한 번 더
# 앞서 확인해 둔 이유는 체크아웃으로 트리가 바뀌기 *전에* 안전 상태를 걸러내기
# 위해서다.
#
# restart_main.ps1은 실패 시 throw만 하고 exit code를 따로 내지 않는다(내부에서
# 외부 네이티브 명령을 쓰지 않아 $LASTEXITCODE도 신뢰할 수 없다). 그래서 성공/
# 실패 판정은 예외 포착으로만 한다.
$restartFailed = $false
$restartError = ""
try {
    & (Join-Path $repoRoot "scripts\restart_main.ps1") -Confirm:$false
} catch {
    $restartFailed = $true
    $restartError = $_.Exception.Message
}

if ($restartFailed) {
    Write-Host ""
    Write-Host "  [실패] 재시작에 실패했습니다." -ForegroundColor Red
    if ($restartError) { Write-Host "  사유: $restartError" -ForegroundColor Red }
    Write-Host ""
    Write-Host "  트리를 원래 위치($origin_ref)로 되돌립니다." -ForegroundColor Yellow
    $restored = Restore-OriginRef $origin_ref
    if ($restored) {
        Fail @"
재시작 실패로 원래 위치($origin_ref)로 되돌렸습니다.
  주의: 재시작이 일부만 진행됐을 수 있습니다 — 기존 프로세스가 이미 종료됐지만
  새 프로세스는 뜨지 않았을 가능성이 있습니다. 시스템이 정상이라고 가정하지
  말고 프로세스가 실제로 돌고 있는지 직접 확인하세요:
    Get-CimInstance Win32_Process -Filter "Name='python.exe'"
  돌고 있지 않다면 수동으로 기동하세요:
    .\scripts\start_main.ps1
"@
    }
    Fail @"
재시작 실패했고, 원래 위치($origin_ref)로 되돌리는 것도 실패했습니다.
  트리가 지금 어느 상태인지, 프로세스가 돌고 있는지 직접 확인하세요:
    git -C $repoRoot status
    Get-CimInstance Win32_Process -Filter "Name='python.exe'"
  수동 복구:  git -C $repoRoot checkout --detach $origin_ref
  그 뒤 수동 기동:  .\scripts\start_main.ps1
"@
}

Write-Host ""
Write-Host "승격 완료: $Tag (지문 $after)" -ForegroundColor Green
exit 0
