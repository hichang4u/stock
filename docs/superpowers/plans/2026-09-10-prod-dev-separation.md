# 운영·개발 환경 분리 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 운영 프로세스를 편집 대상 트리에서 떼어내, 명시적 승격을 거친 릴리스 태그에서만 돌게 만든다.

**Architecture:** 운영을 `D:\Private\stock-prod`로 이전하고 현재 트리를 개발로 남긴다. 각 트리 루트의 `.stock-role` 파일이 역할을 선언하고, 워치독·런처·계좌 단위 스크립트가 그 파일을 읽어 규칙을 적용한다. 승격은 `preflight.ps1`(검사) → 태그 푸시 → `promote.ps1`(운영에서 fetch·검증·재시작) 한 경로로만 이뤄진다.

**Tech Stack:** Python 3.12, pytest 8.3.5 + pytest-asyncio (asyncio_mode=auto), ruff 0.4.10, mypy 1.10.1, PowerShell 5.1, Windows Task Scheduler, git

**Spec:** `docs/superpowers/specs/2026-09-10-prod-dev-separation-design.md`

## Global Constraints

- Python 3.12. ruff `line-length = 100`, lint select `["E", "F", "W", "I"]`. mypy `strict = false`, `ignore_missing_imports = true`.
- pytest는 `pytest.ini`의 `asyncio_mode = auto`를 쓴다. async 테스트에 `@pytest.mark.asyncio`를 붙이지 않는다.
- **전략 파일을 건드리면 지문이 바뀐다.** 목록은 `src/release.py`의 `_STRATEGY_FILES` 20개다. Phase 1은 이 목록의 파일을 **하나도** 수정하지 않는다.
- 전략 env 접두사(`F1_`~`F5_`, `PAPER_FAST_`, `VI_`, `TRAILING_SHADOW_`, `STRATEGY_TICK_`, `BALANCE_SNAPSHOT_`, `EXIT_RECONCILE_`, `KIS_RATE_`, `KIS_MAX_TRANSIENT_`, `KIS_TRANSIENT_`, `KIS_LOW_PRIORITY_`)도 지문에 들어간다.
- `.env`, `.env.paper`, `.env.real`, `data/`는 gitignore 대상이다. 커밋하지 않는다.
- 새 테스트는 실제 `data/` 아래에 쓰지 않는다. 반드시 `tmp_path`를 쓴다.
- 한국어 주석·로그 메시지는 기존 코드 스타일을 따른다.

## 타이밍 판단 (실행 전 읽을 것)

`PAPER_FAST_HYBRID` 전환은 **2026-09-11 08:00 기동**에 활성화되어 지문을 `833edca5fff4`로 바꾼다. Phase 2를 그 전에 올리면 지문 리셋이 한 번으로 끝난다.

**권고: 서두르지 않는다.** Phase 1만 11건의 작업이고 Phase 3은 수동 마이그레이션이다. 장중에 이걸 몰아치면 마이그레이션을 그르칠 위험이 리셋 한 번의 가치보다 크다. 두 번 리셋(내일 1회 + Phase 2 배포 시 1회)을 받아들이고, 잃는 것은 그 사이 거래일의 정상청산 몇 건뿐이다.

Phase 1은 지문에 영향이 없으므로 **언제 올려도 무해하다.** Phase 1만 오늘 끝내고 Phase 2·3을 다음 세션으로 미루는 것이 가장 안전한 배분이다.

## File Structure

**신규**

| 파일 | 책임 |
|---|---|
| `src/utils/tree_role.py` | `.stock-role` 읽기, 릴리스 상태(태그·클린) 판정. 워치독·런처·스크립트가 공유하는 단일 구현 |
| `tests/test_tree_role.py` | 위 모듈 테스트 |
| `scripts/preflight.ps1` | 개발 트리에서 ruff·mypy·pytest 실행 + 지문 변경 여부 출력 |
| `scripts/promote.ps1` | 운영 트리에서 fetch·검증·체크아웃·재시작 |

**수정**

| 파일 | 변경 |
|---|---|
| `tests/test_watchdog_check.py` | `log_dir=tmp_path` 주입 + 격리 회귀 테스트 |
| `scripts/watchdog_check.py` | spawn 직전 릴리스 상태 검사 |
| `scripts/start_main.ps1` | 사전점검에 역할 검사 8단계 추가 |
| `scripts/paper_close_all.py` | `prod`가 아니면 거부 |
| `.gitignore` | `.stock-role` 추가 |
| `src/schedule_times.py` | **[Phase 2 · 지문 변경]** env 오버라이드 |
| `src/modules/f3_entry.py` | **[Phase 2 · 지문 변경]** 보유종목 제외 |

---

# Phase 1 — 지문 무변경 (1단 배포)

전략 파일을 건드리지 않는다. 언제 올려도 증거 카운터가 깨지지 않는다.

---

### Task 1: 워치독 테스트가 운영 로그를 오염시키지 않게 한다

`watchdog_check.main()`은 이미 `log_dir` 파라미터를 받는다. 테스트가 넘기지 않아 실제 `data/logs/watchdog_<날짜>.log`에 `PID=999` 항목이 섞였다(2026-09-10 실측). **소스 변경 없이 테스트만 고친다.**

**Files:**
- Test: `tests/test_watchdog_check.py`

**Interfaces:**
- Consumes: `watchdog_check.main(now, pid_path, spawn, log_dir) -> int` (기존 시그니처, 변경 없음)
- Produces: 없음

- [ ] **Step 1: 격리 회귀 테스트를 쓴다**

`tests/test_watchdog_check.py` 끝에 추가:

```python
def test_main_writes_no_log_outside_the_given_dir(tmp_path):
    """로그 디렉터리를 주입하면 그 밖에는 한 줄도 쓰지 않는다.

    주입을 빠뜨린 테스트가 운영 워치독 로그에 PID=999를 남긴 적이 있다
    (2026-09-10). 실제 경로로 새는 것을 이 테스트가 막는다.
    """
    pid_file = tmp_path / "main.pid"
    log_dir = tmp_path / "logs"
    other = tmp_path / "운영로그"
    other.mkdir()

    watchdog_check.main(
        now=_at(9, 0),
        pid_path=pid_file,
        spawn=lambda: 999,
        log_dir=log_dir,
    )

    assert list(log_dir.glob("watchdog_*.log")), "주입한 디렉터리에 로그가 없다"
    assert not list(other.iterdir()), "주입하지 않은 디렉터리에 썼다"
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/Scripts/python.exe -m pytest tests/test_watchdog_check.py::test_main_writes_no_log_outside_the_given_dir -v`

Expected: FAIL — `_at`이 정의돼 있고 `log_dir` 주입이 없는 기존 호출들과 달리 이 테스트는 통과할 수도 있다. **통과하면 그것이 정상이다** — 이 테스트는 새 가드이지 버그 재현이 아니다. 다음 단계가 진짜 수정이다.

- [ ] **Step 3: 기존 호출 3곳에 `log_dir`을 주입한다**

`test_main_does_not_spawn_while_the_bot_is_alive`, `test_main_spawns_when_dead_inside_the_window`, `test_main_does_not_spawn_outside_the_window` 세 테스트의 `watchdog_check.main(...)` 호출에 `log_dir=tmp_path`를 추가한다. 예:

```python
    watchdog_check.main(
        now=_at(9, 0), pid_path=pid_file, spawn=lambda: calls.append(1) or 999,
        log_dir=tmp_path,
    )
```

- [ ] **Step 4: 유출이 멈췄는지 확인한다**

```bash
BEFORE=$(ls data/logs/ | wc -l)
.venv/Scripts/python.exe -m pytest tests/test_watchdog_check.py -v
AFTER=$(ls data/logs/ | wc -l)
test "$BEFORE" = "$AFTER" && echo "OK: 운영 로그 디렉터리 변화 없음" || echo "FAIL: 파일이 생겼다"
```

Expected: `OK: 운영 로그 디렉터리 변화 없음`, 테스트 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add tests/test_watchdog_check.py
git commit -m "test(watchdog): keep the log out of the real data directory"
```

---

### Task 2: 트리 역할 모듈

`.stock-role`을 읽고 릴리스 상태를 판정하는 단일 구현. 워치독(Python)과 런처(PowerShell)와 계좌 스크립트(Python)가 모두 이걸 쓴다.

**Files:**
- Create: `src/utils/tree_role.py`
- Create: `tests/test_tree_role.py`

**Interfaces:**
- Consumes: 없음
- Produces:
  - `read_role(root: Path) -> str | None` — `.stock-role` 내용을 소문자·공백제거해 반환. 없으면 `None`
  - `ReleaseState` — `NamedTuple(ok: bool, reason: str, detail: str)`
  - `check_release_state(root: Path, role: str | None) -> ReleaseState` — `prod`면 태그·클린 검사, `dev`면 통과, 역할 없으면 실패
  - `main(argv: list[str] | None = None) -> int` — `--check` CLI. 통과 0, 실패 1

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_tree_role.py`:

```python
"""트리 역할 판정 — 역할 불명인 트리는 절대 통과시키지 않는다."""

from pathlib import Path

from src.utils import tree_role


def _role(root: Path, value: str) -> None:
    (root / ".stock-role").write_text(value, encoding="utf-8")


def test_read_role_returns_none_when_the_file_is_missing(tmp_path):
    assert tree_role.read_role(tmp_path) is None


def test_read_role_normalizes_case_and_whitespace(tmp_path):
    _role(tmp_path, "  PROD\n")
    assert tree_role.read_role(tmp_path) == "prod"


def test_missing_role_never_passes(tmp_path):
    """역할 파일이 없으면 기동을 막는다. fail-closed."""
    state = tree_role.check_release_state(tmp_path, None)
    assert state.ok is False
    assert state.reason == "ROLE_MISSING"


def test_unknown_role_never_passes(tmp_path):
    state = tree_role.check_release_state(tmp_path, "staging")
    assert state.ok is False
    assert state.reason == "ROLE_UNKNOWN"


def test_dev_role_passes_without_git_checks(tmp_path):
    """개발 트리는 더티해도 좋다 — 그것이 개발 트리의 용도다."""
    state = tree_role.check_release_state(tmp_path, "dev")
    assert state.ok is True
    assert state.reason == "DEV"
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tree_role.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.utils.tree_role'`

- [ ] **Step 3: 최소 구현을 쓴다**

`src/utils/tree_role.py`:

```python
"""트리 역할(.stock-role)과 릴리스 상태 판정 — 단일 출처.

운영과 개발이 같은 코드베이스의 서로 다른 체크아웃에서 돈다. 어느 쪽인지
모르는 트리가 프로세스를 띄우면 운영 데이터·계좌를 개발 실수로부터 지킬 수
없으므로, 역할이 불명이면 fail-closed로 막는다.

워치독(scripts/watchdog_check.py)과 런처(scripts/start_main.ps1),
계좌 단위 스크립트가 모두 이 모듈을 쓴다. 판정이 갈리면 우회로가 생긴다.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

ROLE_FILE = ".stock-role"
ROLE_PROD = "prod"
ROLE_DEV = "dev"
RELEASE_TAG_PREFIX = "release/"


class ReleaseState(NamedTuple):
    ok: bool
    reason: str
    detail: str


def read_role(root: Path) -> str | None:
    """`.stock-role` 내용을 정규화해 반환한다. 없거나 읽을 수 없으면 None."""
    try:
        raw = (Path(root) / ROLE_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    value = raw.strip().lower()
    return value or None


def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, repr(exc)
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def check_release_state(root: Path, role: str | None) -> ReleaseState:
    """역할별 기동 허용 여부. 운영만 태그·클린을 요구한다."""
    root = Path(root)
    if role is None:
        return ReleaseState(False, "ROLE_MISSING", f"{ROLE_FILE}이 없다")
    if role == ROLE_DEV:
        return ReleaseState(True, "DEV", "개발 트리 — git 검사 생략")
    if role != ROLE_PROD:
        return ReleaseState(False, "ROLE_UNKNOWN", f"알 수 없는 역할: {role}")

    code, dirty = _git(root, "status", "--porcelain")
    if code != 0:
        return ReleaseState(False, "GIT_FAILED", dirty)
    if dirty:
        first = dirty.splitlines()[0]
        return ReleaseState(False, "TREE_DIRTY", f"수정된 파일이 있다: {first}")

    code, tag = _git(root, "describe", "--tags", "--exact-match")
    if code != 0 or not tag.startswith(RELEASE_TAG_PREFIX):
        return ReleaseState(False, "NOT_AT_RELEASE_TAG", tag or "태그 없음")
    return ReleaseState(True, "AT_RELEASE_TAG", tag)


def main(argv: list[str] | None = None) -> int:
    """`python -m src.utils.tree_role --check` — 통과 0, 실패 1."""
    root = Path(__file__).resolve().parents[2]
    role = read_role(root)
    state = check_release_state(root, role)
    print(f"role={role or '(없음)'} ok={state.ok} reason={state.reason} detail={state.detail}")
    return 0 if state.ok else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tree_role.py -v`
Expected: 5 passed

- [ ] **Step 5: `.gitignore`에 역할 파일을 추가한다**

`.gitignore` 끝에 한 줄:

```
.stock-role
```

`data/` 다음 줄에 넣는다. 트리별 파일이므로 추적하지 않는다. 운영 트리가 태그를 체크아웃한 시점에 이 규칙이 이미 있어야 워킹트리가 더티해지지 않는다.

- [ ] **Step 6: 커밋**

```bash
git add src/utils/tree_role.py tests/test_tree_role.py .gitignore
git commit -m "feat(ops): read the tree's role before anything starts a process"
```

---

### Task 3: 운영 트리의 릴리스 상태 검사

`prod` 역할에서 태그·클린을 실제로 판정한다. Task 2에서 `dev`와 실패 경로만 덮었다.

**Files:**
- Modify: `tests/test_tree_role.py`
- Test: `tests/test_tree_role.py`

**Interfaces:**
- Consumes: `tree_role.check_release_state(root, role) -> ReleaseState` (Task 2)
- Produces: 없음

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_tree_role.py` 끝에 추가. 실제 git 저장소를 `tmp_path`에 만든다 — 모킹하면 `git describe`의 실제 동작을 검증하지 못한다.

```python
import subprocess


def _git_repo(root: Path) -> None:
    """태그 하나를 가진 최소 저장소. 서명·훅 없이 결정적으로 만든다."""
    run = lambda *a: subprocess.run(["git", *a], cwd=str(root), check=True,
                                    capture_output=True)
    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "test")
    run("config", "commit.gpgsign", "false")
    (root / "파일.txt").write_text("내용", encoding="utf-8")
    run("add", ".")
    run("commit", "-q", "-m", "초기")


def test_prod_passes_at_a_release_tag(tmp_path):
    _git_repo(tmp_path)
    subprocess.run(["git", "tag", "release/20260911-1"], cwd=str(tmp_path), check=True)
    state = tree_role.check_release_state(tmp_path, "prod")
    assert state.ok is True
    assert state.reason == "AT_RELEASE_TAG"
    assert state.detail == "release/20260911-1"


def test_prod_refuses_a_dirty_tree(tmp_path):
    """운영 트리에서 파일을 고치면 다음 기동이 막힌다 — 편집이 곧 배포가 되지 않게."""
    _git_repo(tmp_path)
    subprocess.run(["git", "tag", "release/20260911-1"], cwd=str(tmp_path), check=True)
    (tmp_path / "파일.txt").write_text("몰래 고침", encoding="utf-8")
    state = tree_role.check_release_state(tmp_path, "prod")
    assert state.ok is False
    assert state.reason == "TREE_DIRTY"


def test_prod_refuses_a_non_release_tag(tmp_path):
    """아무 태그나 통과시키면 릴리스 태그의 의미가 없다."""
    _git_repo(tmp_path)
    subprocess.run(["git", "tag", "v1.0"], cwd=str(tmp_path), check=True)
    state = tree_role.check_release_state(tmp_path, "prod")
    assert state.ok is False
    assert state.reason == "NOT_AT_RELEASE_TAG"


def test_prod_refuses_an_untagged_commit(tmp_path):
    _git_repo(tmp_path)
    state = tree_role.check_release_state(tmp_path, "prod")
    assert state.ok is False
    assert state.reason == "NOT_AT_RELEASE_TAG"
```

- [ ] **Step 2: 실패 여부를 확인한다**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tree_role.py -v`

Expected: 4개 신규 테스트가 PASS한다 — Task 2의 구현이 이미 이 경로를 덮기 때문이다. **하나라도 FAIL하면 Task 2 구현에 버그가 있다는 뜻이므로 그것을 고친다.** 특히 `test_prod_refuses_a_non_release_tag`가 실패하면 `startswith(RELEASE_TAG_PREFIX)` 검사가 빠진 것이다.

- [ ] **Step 3: 전체 스위트가 깨지지 않았는지 확인한다**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 전부 PASS (2026-09-10 기준 1331건 + 신규 9건)

- [ ] **Step 4: 커밋**

```bash
git add tests/test_tree_role.py
git commit -m "test(ops): pin what a production tree must look like to start"
```

---

### Task 4: 워치독이 릴리스 상태를 확인한 뒤에만 띄운다

**이 작업이 Phase 1의 핵심이다.** 워치독은 `start_main.ps1`이 아니라 `main.py`를 `subprocess.Popen`으로 직접 띄운다(`scripts/watchdog_check.py:_spawn`). 런처에만 검사를 넣으면 **운영의 주 경로인 워치독 재기동이 검사를 통과하지 않는다.**

**Files:**
- Modify: `scripts/watchdog_check.py`
- Test: `tests/test_watchdog_check.py`

**Interfaces:**
- Consumes: `tree_role.read_role(root)`, `tree_role.check_release_state(root, role)` (Task 2)
- Produces: `watchdog_check.main(now, pid_path, spawn, log_dir, release_check) -> int` — `release_check`는 `Callable[[], ReleaseState]`, 기본값은 실제 검사

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_watchdog_check.py` 끝에 추가:

```python
from src.utils.tree_role import ReleaseState


def test_main_does_not_spawn_when_the_release_state_is_bad(tmp_path):
    """더티한 운영 트리를 워치독이 되살리면 분리가 무의미해진다."""
    pid_file = tmp_path / "main.pid"
    calls = []
    watchdog_check.main(
        now=_at(9, 0),
        pid_path=pid_file,
        spawn=lambda: calls.append(1) or 999,
        log_dir=tmp_path,
        release_check=lambda: ReleaseState(False, "TREE_DIRTY", "파일.txt"),
    )
    assert calls == [], "릴리스 상태가 나쁜데 프로세스를 띄웠다"
    logged = (tmp_path / f"watchdog_{_at(9, 0).strftime('%Y%m%d')}.log").read_text("utf-8")
    assert "TREE_DIRTY" in logged, "거부 사유가 로그에 없다"


def test_main_spawns_when_the_release_state_is_good(tmp_path):
    pid_file = tmp_path / "main.pid"
    calls = []
    watchdog_check.main(
        now=_at(9, 0),
        pid_path=pid_file,
        spawn=lambda: calls.append(1) or 999,
        log_dir=tmp_path,
        release_check=lambda: ReleaseState(True, "AT_RELEASE_TAG", "release/20260911-1"),
    )
    assert calls == [1]
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/Scripts/python.exe -m pytest tests/test_watchdog_check.py -k release_state -v`
Expected: FAIL — `TypeError: main() got an unexpected keyword argument 'release_check'`

- [ ] **Step 3: 워치독에 검사를 넣는다**

`scripts/watchdog_check.py` 상단 import에 추가 (`ROOT` 정의 뒤에 와야 `sys.path`가 잡힌다):

```python
sys.path.insert(0, str(ROOT))

from src.utils import tree_role  # noqa: E402
```

`_spawn` 아래에 기본 검사 함수를 추가:

```python
def _default_release_check() -> tree_role.ReleaseState:
    return tree_role.check_release_state(ROOT, tree_role.read_role(ROOT))
```

`main`의 시그니처와 spawn 직전을 고친다:

```python
def main(
    now: datetime | None = None,
    pid_path: Path = PID_FILE,
    spawn=None,
    log_dir: Path = LOG_DIR,
    release_check=None,
) -> int:
```

`say("프로세스 사망 감지 — 재시작")` **앞**에 삽입:

```python
    # 워치독은 main.py를 직접 띄운다(런처를 거치지 않는다). 그래서 릴리스
    # 상태 검사가 여기에도 있어야 한다 — 런처에만 두면 운영의 주 경로가
    # 검사를 통과하지 않는다.
    state = (release_check or _default_release_check)()
    if not state.ok:
        say(f"기동 거부 — {state.reason}: {state.detail}")
        return 0
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/Scripts/python.exe -m pytest tests/test_watchdog_check.py -v`
Expected: 전부 PASS

- [ ] **Step 5: 실제 트리에서 CLI가 도는지 확인한다**

```bash
.venv/Scripts/python.exe -m src.utils.tree_role --check; echo "exit=$?"
```

Expected: `.stock-role`이 아직 없으므로 `role=(없음) ok=False reason=ROLE_MISSING`, `exit=1`. **이 상태로는 워치독이 재기동을 거부한다** — Task 10에서 역할 파일을 만들기 전까지 운영을 죽이면 안 된다.

- [ ] **Step 6: 커밋**

```bash
git add scripts/watchdog_check.py tests/test_watchdog_check.py
git commit -m "feat(watchdog): refuse to revive a tree that is not at a release"
```

---

### Task 5: 계좌 단위 스크립트를 개발 트리에서 막는다

`scripts/paper_close_all.py`는 계좌 전체를 청산한다. 개발 트리에서 무심코 돌리면 운영 포지션이 사라진다.

**Files:**
- Modify: `scripts/paper_close_all.py`
- Create: `tests/test_paper_close_all_guard.py`

**Interfaces:**
- Consumes: `tree_role.read_role(root)`, `tree_role.ROLE_PROD` (Task 2)
- Produces: `paper_close_all.assert_prod_tree(root: Path) -> None` — `prod`가 아니면 `SystemExit(2)`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_paper_close_all_guard.py`:

```python
"""계좌 전체 청산은 운영 트리에서만 — 개발 실수로 운영 포지션을 날리지 않게."""

import importlib

import pytest


def _module():
    return importlib.import_module("scripts.paper_close_all")


def test_dev_tree_is_refused(tmp_path):
    (tmp_path / ".stock-role").write_text("dev", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        _module().assert_prod_tree(tmp_path)
    assert excinfo.value.code == 2


def test_missing_role_is_refused(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        _module().assert_prod_tree(tmp_path)
    assert excinfo.value.code == 2


def test_prod_tree_is_allowed(tmp_path):
    (tmp_path / ".stock-role").write_text("prod", encoding="utf-8")
    _module().assert_prod_tree(tmp_path)  # 예외가 나지 않으면 통과
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/Scripts/python.exe -m pytest tests/test_paper_close_all_guard.py -v`
Expected: FAIL — `AttributeError: module 'scripts.paper_close_all' has no attribute 'assert_prod_tree'`

- [ ] **Step 3: 가드를 넣는다**

`scripts/paper_close_all.py`의 import 블록 뒤(`KST = ZoneInfo("Asia/Seoul")` 위)에 추가:

```python
from src.utils import tree_role  # noqa: E402


def assert_prod_tree(root: Path = ROOT) -> None:
    """계좌 전체를 건드리는 스크립트는 운영 트리에서만 돈다.

    개발 트리는 운영과 모의계좌를 공유한다(설계 6절). 여기서 전량 청산을
    돌리면 운영이 들고 있는 포지션까지 팔린다.
    """
    role = tree_role.read_role(root)
    if role != tree_role.ROLE_PROD:
        print(
            f"[거부] 계좌 전체 청산은 운영 트리에서만 실행합니다. "
            f"현재 역할: {role or '(.stock-role 없음)'}"
        )
        raise SystemExit(2)
```

그리고 이 스크립트의 진입점(`if __name__ == "__main__":` 블록 또는 `main()` 첫 줄)에서 `assert_prod_tree()`를 가장 먼저 호출한다.

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/Scripts/python.exe -m pytest tests/test_paper_close_all_guard.py -v`
Expected: 3 passed

- [ ] **Step 5: 커밋**

```bash
git add scripts/paper_close_all.py tests/test_paper_close_all_guard.py
git commit -m "feat(ops): keep account-wide liquidation out of the dev tree"
```

---

### Task 6: `preflight.ps1` — 개발 트리 승격 전 검사

**Files:**
- Create: `scripts/preflight.ps1`

**Interfaces:**
- Consumes: `tree_role` CLI (Task 2)
- Produces: 콘솔 출력의 `FINGERPRINT=<12자리>` 줄. `promote.ps1`의 `-AcknowledgeFingerprint` 값이 여기서 나온다

- [ ] **Step 1: 스크립트를 쓴다**

`scripts/preflight.ps1`:

```powershell
<#
.SYNOPSIS
    개발 트리에서 승격 전 검사를 돌린다.
.DESCRIPTION
    ruff / mypy / pytest 를 순서대로 돌리고, 현재 전략 지문과 HEAD~1 대비
    변경 여부를 출력한다. 지문이 바뀌면 승격은 2단이며 증거 카운터가
    리셋된다 (설계 3절).

    설계: docs/superpowers/specs/2026-09-10-prod-dev-separation-design.md
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$py = Join-Path $repoRoot ".venv\Scripts\python.exe"

function Section([string]$t) { Write-Host ""; Write-Host "=== $t ===" -ForegroundColor Cyan }
function Fail([string]$t) { Write-Host "  [실패] $t" -ForegroundColor Red; exit 1 }

Section "역할 확인"
& $py -m src.utils.tree_role --check
if ($LASTEXITCODE -ne 0) { Fail "이 트리의 역할을 확인할 수 없습니다 (.stock-role)" }

Section "ruff"
& $py -m ruff check .
if ($LASTEXITCODE -ne 0) { Fail "ruff 위반이 있습니다" }
Write-Host "  [OK] 0건" -ForegroundColor Green

Section "mypy"
& $py -m mypy .
if ($LASTEXITCODE -ne 0) { Fail "mypy 오류가 있습니다" }
Write-Host "  [OK]" -ForegroundColor Green

Section "pytest"
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { Fail "테스트가 실패했습니다" }

Section "워킹트리"
$dirty = & git -C $repoRoot status --porcelain
if ($dirty) {
    Write-Host "  [경고] 커밋되지 않은 변경이 있습니다. 태그 전에 커밋하세요:" -ForegroundColor Yellow
    $dirty | ForEach-Object { Write-Host "    $_" }
} else {
    Write-Host "  [OK] 클린" -ForegroundColor Green
}

Section "전략 지문"
$fp = & $py -c "from src.release import strategy_fingerprint; print(strategy_fingerprint())"
if ($LASTEXITCODE -ne 0) { Fail "지문을 계산할 수 없습니다" }
Write-Host "FINGERPRINT=$fp"

$prev = & git -C $repoRoot stash list
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
```

- [ ] **Step 2: 실제로 돌려 통과를 확인한다**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\preflight.ps1
```

Expected: `.stock-role`이 아직 없으므로 **역할 확인에서 실패한다.** 확인용으로 임시 파일을 만들고 다시 돌린다:

```bash
echo dev > .stock-role
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/preflight.ps1; echo "exit=$?"
```

Expected: 전 구간 통과, 마지막에 `FINGERPRINT=<12자리>`, `exit=0`

`.stock-role`은 gitignore 대상이므로 그대로 두어도 커밋되지 않는다. Task 10에서 값을 확정한다.

- [ ] **Step 3: 커밋**

```bash
git add scripts/preflight.ps1
git commit -m "feat(ops): gate a release behind lint, types, tests and the fingerprint"
```

---

### Task 7: `promote.ps1` — 운영 트리 승격

**Files:**
- Create: `scripts/promote.ps1`

**Interfaces:**
- Consumes: `tree_role` CLI (Task 2), `scripts/restart_guard.py` (기존), `scripts/restart_main.ps1` (기존)
- Produces: 없음 (터미널 명령)

- [ ] **Step 1: 스크립트를 쓴다**

`scripts/promote.ps1`:

```powershell
<#
.SYNOPSIS
    운영 트리를 릴리스 태그로 승격한다.
.DESCRIPTION
    운영 트리에서만 실행된다. 태그를 fetch 해 지문 변화를 보여주고,
    포지션이 안전할 때만 체크아웃 후 재시작한다.

    지문이 바뀌는 승격은 -AcknowledgeFingerprint 로 예상 지문을 명시해야
    한다. 값은 개발 트리의 preflight.ps1 이 출력한다. 승격 후 실제 지문과
    다르면 중단한다 — 맹목적으로 붙일 수 있는 스위치가 아니다.

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
    $v = & $py -c "from src.release import strategy_fingerprint; print(strategy_fingerprint())"
    if ($LASTEXITCODE -ne 0) { Fail "지문을 계산할 수 없습니다" }
    return $v.Trim()
}

Section "역할 확인"
$role = (Get-Content (Join-Path $repoRoot ".stock-role") -ErrorAction SilentlyContinue)
if ($null -ne $role) { $role = $role.Trim().ToLower() }
if ($role -ne "prod") { Fail "승격은 운영 트리에서만 실행합니다. 현재 역할: $($role ?? '(없음)')" }
Write-Host "  [OK] prod" -ForegroundColor Green

Section "안전 상태 확인"
& $py (Join-Path $repoRoot "scripts\restart_guard.py")
if ($LASTEXITCODE -ne 0) { Fail "포지션이 안전 상태가 아닙니다. 청산 후 다시 시도하세요" }
Write-Host "  [OK] IDLE/CLOSED" -ForegroundColor Green

$before = Get-Fingerprint
Write-Host "  현재 지문: $before"

Section "태그 가져오기"
& git -C $repoRoot fetch origin --tags
if ($LASTEXITCODE -ne 0) { Fail "origin fetch 실패" }
& git -C $repoRoot rev-parse --verify "refs/tags/$Tag" | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "태그를 찾을 수 없습니다: $Tag" }

Section "변경 내역"
& git -C $repoRoot log --oneline HEAD.."refs/tags/$Tag"

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
        & git -C $repoRoot checkout --detach $before2 2>$null
        Fail @"
지문 변경을 확인하지 않았습니다.
  다시 실행:  scripts\promote.ps1 -Tag $Tag -AcknowledgeFingerprint $after
"@
    }
    Write-Host "  [확인됨] $AcknowledgeFingerprint" -ForegroundColor Green
} else {
    Write-Host "  지문 무변경 — 1단 승격입니다." -ForegroundColor Green
}

Section "재시작"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repoRoot "scripts\restart_main.ps1")
if ($LASTEXITCODE -ne 0) { Fail "재시작 실패" }

Write-Host ""
Write-Host "승격 완료: $Tag (지문 $after)" -ForegroundColor Green
exit 0
```

- [ ] **Step 2: 개발 트리에서 거부되는지 확인한다**

```bash
echo dev > .stock-role
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/promote.ps1 -Tag release/test; echo "exit=$?"
```

Expected: `[실패] 승격은 운영 트리에서만 실행합니다. 현재 역할: dev`, `exit=1`

- [ ] **Step 3: 롤백 경로의 결함을 고친다**

Step 1의 스크립트에는 의도적인 결함이 하나 있다 — 지문 미확인으로 중단할 때 `$before2`라는 정의되지 않은 변수로 되돌리려 한다. 체크아웃 **전에** 원래 위치를 저장해야 한다.

`Section "태그 가져오기"` 앞에 추가:

```powershell
$origin_ref = (& git -C $repoRoot rev-parse HEAD).Trim()
```

그리고 실패 경로의 `checkout --detach $before2`를 다음으로 바꾼다:

```powershell
        & git -C $repoRoot checkout --detach $origin_ref 2>$null
```

- [ ] **Step 4: ruff/pytest가 여전히 통과하는지 확인한다**

Run: `.venv/Scripts/python.exe -m pytest -q && .venv/Scripts/python.exe -m ruff check .`
Expected: 전부 PASS, ruff 0건

- [ ] **Step 5: 커밋**

```bash
git add scripts/promote.ps1
git commit -m "feat(ops): make promotion the only way code reaches production"
```

---

### Task 8: `start_main.ps1`에 역할 검사를 추가한다

수동 기동 경로. 워치독(Task 4)과 같은 판정을 쓴다.

**Files:**
- Modify: `scripts/start_main.ps1`

**Interfaces:**
- Consumes: `tree_role` CLI (Task 2)
- Produces: 없음

- [ ] **Step 1: 사전점검에 단계를 추가한다**

`scripts/start_main.ps1`에서 기존 사전점검 마지막 단계 뒤, `main.py`를 실행하기 직전에 삽입:

```powershell
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
```

`$venvPython`, `Write-Section`, `Write-Ok`, `Fail`은 이 스크립트에 이미 정의돼 있다.

- [ ] **Step 2: 개발 역할로 통과하는지 확인한다**

```bash
echo dev > .stock-role
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_main.ps1 -CheckOnly; echo "exit=$?"
```

Expected: 8단계까지 전부 `[OK]`, `exit=0` (`-CheckOnly`라 프로세스를 띄우지 않는다)

- [ ] **Step 3: 역할이 없으면 막히는지 확인한다**

```bash
rm .stock-role
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_main.ps1 -CheckOnly; echo "exit=$?"
echo dev > .stock-role
```

Expected: `[실패] 이 트리는 기동할 수 없는 상태입니다`, `exit=1`

- [ ] **Step 4: 커밋**

```bash
git add scripts/start_main.ps1
git commit -m "feat(ops): check the tree's role before the launcher starts anything"
```

---

**Phase 1 완료 지점.** 여기서 멈춰도 안전하다. 지문은 그대로이고, 아직 `.stock-role`이 운영에 없으므로 Task 4의 워치독 거부가 발동한다 — **Phase 3 전에 운영 프로세스를 죽이면 안 된다.** 운영이 계속 도는 한 문제없다.

---

# Phase 2 — 지문 변경 (2단 배포, 한 릴리스로 묶음)

Task 9와 10은 **반드시 같은 릴리스**로 올린다. 따로 올리면 지문이 두 번 갈린다.

---

### Task 9: 스케줄 시각 env 오버라이드

개발 트리가 운영의 진입 창(09:00~09:11) 밖에서 돌아야 한다.

**Files:**
- Modify: `src/schedule_times.py`
- Create: `tests/test_schedule_times_override.py`

**Interfaces:**
- Consumes: 없음
- Produces: `schedule_times.F1_H`, `F1_M`, `F2_H`, `F2_M`, `F3_H`, `F3_M`, `F3_S` — 기존 이름 그대로, 값만 env로 덮인다. `SCHEDULE_F1`, `SCHEDULE_F2`, `SCHEDULE_F3` 환경변수를 `HH:MM:SS`로 받는다

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_schedule_times_override.py`:

```python
"""스케줄 시각 오버라이드 — 개발 트리를 운영 진입 창 밖으로 밀어내기 위한 것."""

import importlib

from src import schedule_times


def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(schedule_times)


def test_defaults_match_the_production_schedule(monkeypatch):
    """오버라이드가 없으면 값이 바뀌면 안 된다 — 운영 동작 불변."""
    monkeypatch.delenv("SCHEDULE_F1", raising=False)
    monkeypatch.delenv("SCHEDULE_F2", raising=False)
    monkeypatch.delenv("SCHEDULE_F3", raising=False)
    st = importlib.reload(schedule_times)
    assert (st.F1_H, st.F1_M) == (9, 0)
    assert (st.F2_H, st.F2_M) == (9, 10)
    assert (st.F3_H, st.F3_M, st.F3_S) == (9, 10, 10)


def test_override_shifts_the_entry_chain(monkeypatch):
    st = _reload(monkeypatch, SCHEDULE_F1="09:15:00", SCHEDULE_F2="09:25:00",
                 SCHEDULE_F3="09:25:10")
    assert (st.F1_H, st.F1_M) == (9, 15)
    assert (st.F2_H, st.F2_M) == (9, 25)
    assert (st.F3_H, st.F3_M, st.F3_S) == (9, 25, 10)


def test_malformed_override_falls_back_to_the_default(monkeypatch):
    """잘못된 값으로 스케줄이 사라지면 하루를 통째로 잃는다. fail-safe."""
    st = _reload(monkeypatch, SCHEDULE_F1="아홉시")
    assert (st.F1_H, st.F1_M) == (9, 0)


def test_out_of_range_override_falls_back(monkeypatch):
    st = _reload(monkeypatch, SCHEDULE_F1="25:00:00")
    assert (st.F1_H, st.F1_M) == (9, 0)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/Scripts/python.exe -m pytest tests/test_schedule_times_override.py -v`
Expected: `test_override_shifts_the_entry_chain` FAIL — 값이 여전히 9:00

- [ ] **Step 3: 구현한다**

`src/schedule_times.py`의 `F1_H, F1_M = 9, 0` 위에 헬퍼를 넣고 세 상수를 바꾼다:

```python
import os


def _hhmmss(name: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    """`HH:MM:SS` env 오버라이드. 형식이 틀리면 기본값을 쓴다.

    개발 트리를 운영의 진입 창(09:00~09:11) 밖으로 밀어내기 위한 것이다.
    잘못된 값에 fail-closed 하면 그날 스케줄이 통째로 사라지므로,
    휴장 판정과 같은 fail-safe 원칙을 따른다 — 거래일을 잃는 쪽이 더 비싸다.
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    parts = raw.split(":")
    if len(parts) != 3:
        return default
    try:
        h, m, s = (int(p) for p in parts)
    except ValueError:
        return default
    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
        return default
    return h, m, s


F1_H, F1_M, _F1_S = _hhmmss("SCHEDULE_F1", (9, 0, 0))
```

`F2_H, F2_M = 9, 10`을 바꾼다:

```python
F2_H, F2_M, _F2_S = _hhmmss("SCHEDULE_F2", (9, 10, 0))
```

`F3_H, F3_M, F3_S = 9, 10, 10`을 바꾼다:

```python
F3_H, F3_M, F3_S = _hhmmss("SCHEDULE_F3", (9, 10, 10))
```

`F3_FILL_DEADLINE_H, F3_FILL_DEADLINE_M`과 `F5_*`는 **바꾸지 않는다.** 청산 시각은 KRX 연속매매 구간에 묶여 있고(파일 주석 참조), 체결 마감은 운영 기준선으로 남아야 개발 제외 규칙이 성립한다.

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/Scripts/python.exe -m pytest tests/test_schedule_times_override.py -v`
Expected: 4 passed

- [ ] **Step 5: 스케줄러가 깨지지 않았는지 확인한다**

Run: `.venv/Scripts/python.exe -m pytest tests/test_main_schedule_flow.py -q`
Expected: PASS

- [ ] **Step 6: 커밋하지 않는다**

Task 10과 한 커밋으로 묶는다. 지문 변경을 하나로 만들기 위함이다. 다음 작업으로 넘어간다.

---

### Task 10: 개발 트리가 계좌 보유종목을 후보에서 제외한다

`_rank_final_entry_candidates`에 이미 `exclude_tickers` 인자가 있다 (`src/modules/f3_entry.py:3146-3155`). 거기에 계좌 보유분을 합친다.

**Files:**
- Modify: `src/modules/f3_entry.py:3146-3155`
- Create: `tests/test_f3_held_ticker_exclusion.py`

**Interfaces:**
- Consumes: `kis_rest.get`, 기존 `_rank_final_entry_candidates(s, exclude_tickers)`
- Produces: `f3_entry._held_tickers_to_exclude() -> set[str]` — 플래그가 꺼져 있으면 빈 집합, 조회 실패도 빈 집합

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_f3_held_ticker_exclusion.py`:

```python
"""개발 트리 보유종목 제외 — 운영 F5가 개발분까지 파는 것을 막는다.

F5는 상태 파일이 아니라 브로커의 계좌 전체 hldg_qty로 매도 수량을 정한다
(f5_timeout.py:118). 두 트리가 같은 종목을 들면 먼저 청산하는 쪽이 상대의
물량까지 판다. 개발이 다음 순위로 내려가 그 상황 자체를 만들지 않는다.
"""

import pytest

from src.modules import f3_entry


async def test_flag_off_excludes_nothing(monkeypatch):
    monkeypatch.delenv("DEV_EXCLUDE_HELD_TICKERS", raising=False)
    assert await f3_entry._held_tickers_to_exclude() == set()


async def test_flag_on_returns_held_tickers(monkeypatch):
    monkeypatch.setenv("DEV_EXCLUDE_HELD_TICKERS", "1")

    async def fake_get(path, **kwargs):
        return {"rt_cd": "0", "output1": [
            {"pdno": "005930", "hldg_qty": "10"},
            {"pdno": "000660", "hldg_qty": "0"},
            {"pdno": "187660", "hldg_qty": "3"},
        ]}

    monkeypatch.setattr(f3_entry.kis_rest, "get", fake_get)
    assert await f3_entry._held_tickers_to_exclude() == {"005930", "187660"}


async def test_query_failure_excludes_nothing(monkeypatch):
    """조회 실패로 진입을 막으면 개발 트리가 아무것도 테스트하지 못한다.

    fail-open이 안전한 드문 경우다 — 최악의 결과가 '종목이 겹친다'이고,
    그 손해는 개발 물량(계좌의 30%)에 한정된다.
    """
    monkeypatch.setenv("DEV_EXCLUDE_HELD_TICKERS", "1")

    async def boom(path, **kwargs):
        raise RuntimeError("조회 실패")

    monkeypatch.setattr(f3_entry.kis_rest, "get", boom)
    assert await f3_entry._held_tickers_to_exclude() == set()
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/Scripts/python.exe -m pytest tests/test_f3_held_ticker_exclusion.py -v`
Expected: FAIL — `AttributeError: module has no attribute '_held_tickers_to_exclude'`

- [ ] **Step 3: 구현한다**

`src/modules/f3_entry.py`에서 `_rank_final_entry_candidates` **바로 위**에 추가:

```python
async def _held_tickers_to_exclude() -> set[str]:
    """계좌에 이미 보유 중인 종목. 개발 트리에서만 채워진다.

    운영과 개발이 모의계좌를 공유할 때(설계 6절), 같은 종목을 들면 먼저
    청산하는 쪽이 상대 물량까지 판다 — F5가 계좌 전체 hldg_qty로 수량을
    정하기 때문이다. 개발이 다음 순위로 내려가 충돌 자체를 없앤다.

    조회가 실패하면 빈 집합을 낸다. 여기서 fail-closed 하면 개발 트리가
    아무것도 테스트하지 못하는데, 실패의 대가는 종목 중복뿐이고 그 손해는
    개발 몫(계좌의 30%)에 한정된다.
    """
    if os.getenv("DEV_EXCLUDE_HELD_TICKERS", "0") != "1":
        return set()
    try:
        resp = await kis_rest.get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id=_BALANCE_TR[os.getenv("KIS_MODE", "PAPER")],
            params=kis_rest.balance_inquiry_params(),
        )
    except Exception as exc:  # noqa: BLE001 — 조회 실패가 진입을 막지 않는다
        log("DEV_HELD_QUERY_FAILED", level="WARN", error=repr(exc))
        return set()
    held = {
        str(row.get("pdno") or "")
        for row in (resp.get("output1") or [])
        if str(row.get("pdno") or "") and int(float(row.get("hldg_qty") or 0)) > 0
    }
    if held:
        log("DEV_HELD_TICKERS_EXCLUDED", level="INFO", tickers=sorted(held))
    return held
```

`_BALANCE_TR`이 이 모듈에 없으면 위 함수 앞에 추가한다:

```python
_BALANCE_TR = {"REAL": "TTTC8434R", "PAPER": "VTTC8434R"}
```

그리고 `_rank_final_entry_candidates`의 `exclude_tickers = exclude_tickers or set()` 줄을 바꾼다:

```python
    exclude_tickers = set(exclude_tickers or set()) | await _held_tickers_to_exclude()
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/Scripts/python.exe -m pytest tests/test_f3_held_ticker_exclusion.py -v`
Expected: 3 passed

- [ ] **Step 5: 운영 경로가 바뀌지 않았는지 확인한다**

Run: `.venv/Scripts/python.exe -m pytest tests/test_f3_entry.py -q`
Expected: 218건 전부 PASS. **플래그가 꺼진 상태에서 F3 동작이 조금이라도 달라지면 안 된다.**

- [ ] **Step 6: 전체 스위트와 린트**

Run: `.venv/Scripts/python.exe -m pytest -q && .venv/Scripts/python.exe -m ruff check . && .venv/Scripts/python.exe -m mypy .`
Expected: 전부 PASS

- [ ] **Step 7: Task 9와 함께 한 커밋으로 묶는다**

```bash
git add src/schedule_times.py src/modules/f3_entry.py \
        tests/test_schedule_times_override.py tests/test_f3_held_ticker_exclusion.py
git commit -m "feat(dev-tree): let a second tree trade the shared account safely

Two trees on one mock account collide two ways: F5 sizes its sell from the
account-wide hldg_qty, so whoever exits first sells the other's shares, and
both would fight the one-per-second limit inside the opening window.

The schedule override moves the dev tree's entry chain past production's fill
deadline, and the held-ticker exclusion drops anything already in the account
so the two never hold the same name. Both are off unless their env asks for
them, so production behaves exactly as before.

Bundled into one commit on purpose: each of these shifts the strategy
fingerprint, and separate releases would reset the evidence counter twice."
```

- [ ] **Step 8: 새 지문을 기록한다**

```bash
.venv/Scripts/python.exe -c "from src.release import strategy_fingerprint; print(strategy_fingerprint())"
```

출력값을 적어 둔다. Task 12의 `-AcknowledgeFingerprint`에 쓴다.

---

# Phase 3 — 마이그레이션 (수동, 장 마감 후 권장)

운영 프로세스를 옮긴다. **포지션이 없을 때만 한다** — 장 마감 후(15:30 이후) 또는 그날 진입이 차단된 날.

---

### Task 11: 운영 트리 구축

**Files:**
- 신규 디렉터리: `D:\Private\stock-prod`

- [ ] **Step 1: 릴리스 태그를 만들고 푸시한다**

개발 트리에서:

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/preflight.ps1
git tag release/$(date +%Y%m%d)-1
git push origin main --tags
```

Expected: preflight 전 구간 통과, 태그 푸시 성공

- [ ] **Step 2: 운영 트리를 clone 한다**

```bash
git clone https://github.com/hichang4u/stock.git /d/Private/stock-prod
cd /d/Private/stock-prod
git checkout --detach "refs/tags/$(git -C /d/Private/stock describe --tags --abbrev=0)"
```

- [ ] **Step 3: 데이터와 설정을 옮긴다**

```bash
cp -r /d/Private/stock/data /d/Private/stock-prod/data
cp /d/Private/stock/.env /d/Private/stock/.env.paper /d/Private/stock/.env.real /d/Private/stock-prod/
ls /d/Private/stock-prod/data | wc -l   # 원본과 같은지 확인
```

원본은 개발 트리에 그대로 둔다 — 롤백 경로다.

- [ ] **Step 4: venv를 만든다**

```powershell
cd D:\Private\stock-prod
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

- [ ] **Step 5: 역할 파일을 만든다**

```bash
echo prod > /d/Private/stock-prod/.stock-role
echo dev  > /d/Private/stock/.stock-role
```

- [ ] **Step 6: 운영 설정을 조정한다**

`D:\Private\stock-prod\.env`에서 `F3_ALLOC_RATIO`를 `0.70`으로 바꾼다. 없으면 추가한다. `.env.paper`도 같게 맞춘다. **`.env.real`은 건드리지 않는다.**

- [ ] **Step 7: 개발 설정을 조정한다**

`D:\Private\stock\.env`에서:

```dotenv
UI_PORT=8898
F3_ALLOC_RATIO=0.30
AUTH_DIR=D:/Private/stock-prod/data/auth
REPLAY_SOURCE_DIR=D:/Private/stock-prod/data
KIS_AUTH_READONLY=1
DEV_EXCLUDE_HELD_TICKERS=1
SCHEDULE_F1=09:15:00
SCHEDULE_F2=09:25:00
SCHEDULE_F3=09:25:10
```

- [ ] **Step 8: 개발 트리의 운영 이력을 비운다**

```bash
cd /d/Private/stock
mv data/logs data/logs.migrated && mkdir -p data/logs
mv data/state data/state.migrated && mkdir -p data/state
mv data/db data/db.migrated && mkdir -p data/db
```

`data/auth`는 지우지 않는다 — 개발이 운영 경로를 보므로 쓰이지 않지만, 롤백 시 필요하다. `.migrated` 디렉터리는 검증이 끝난 뒤 지운다.

- [ ] **Step 9: 양쪽이 기동 검사를 통과하는지 확인한다**

```bash
cd /d/Private/stock-prod && powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_main.ps1 -CheckOnly; echo "prod exit=$?"
cd /d/Private/stock      && powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_main.ps1 -CheckOnly; echo "dev exit=$?"
```

Expected: 둘 다 `exit=0`. 운영은 8단계에서 `AT_RELEASE_TAG`, 개발은 `DEV`.

---

### Task 12: Task Scheduler 전환과 검증

- [ ] **Step 1: 현재 운영 프로세스를 정지한다**

**포지션이 없음을 먼저 확인한다:**

```bash
cd /d/Private/stock && .venv/Scripts/python.exe scripts/restart_guard.py; echo "exit=$?"
```

Expected: `exit=0` (IDLE 또는 CLOSED). `exit != 0`이면 **여기서 멈추고 청산 후 다시 시작한다.**

안전하면 정지:

```powershell
Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
  Where-Object { $_.CommandLine -match 'main\.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

- [ ] **Step 2: 스케줄 작업 경로를 바꾼다**

```powershell
$new = "D:\Private\stock-prod"
$t = Get-ScheduledTask -TaskName 'StockBot_Watchdog'
$a = New-ScheduledTaskAction -Execute "$new\.venv\Scripts\pythonw.exe" `
       -Argument "$new\scripts\watchdog_check.py"
Set-ScheduledTask -TaskName 'StockBot_Watchdog' -Action $a

$t2 = Get-ScheduledTask -TaskName 'StockBot_Backfill'
$a2 = New-ScheduledTaskAction -Execute "powershell.exe" `
       -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$new\scripts\run_backfill.ps1`""
Set-ScheduledTask -TaskName 'StockBot_Backfill' -Action $a2

Get-ScheduledTask -TaskName 'StockBot_*' |
  Select-Object TaskName, @{n='Cmd';e={$_.Actions.Execute + ' ' + $_.Actions.Arguments}} |
  Format-List
```

Expected: 두 작업 모두 `stock-prod` 경로를 가리킨다

- [ ] **Step 3: 운영을 새 경로에서 띄운다**

```powershell
cd D:\Private\stock-prod
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_main.ps1
```

- [ ] **Step 4: 검증 6항목을 돌린다**

```bash
# 1. 운영 UI 응답
curl -s http://127.0.0.1:8899/api/status | head -c 200; echo

# 2. 개발 기동 (다른 포트)
cd /d/Private/stock && powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_main.ps1 -CheckOnly

# 3. 개발에서 REAL 시도 → 거부
cd /d/Private/stock && KIS_MODE=REAL powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_main.ps1 -CheckOnly; echo "REAL exit=$? (1이어야 정상)"

# 4. 운영 트리를 더럽히면 기동 거부
cd /d/Private/stock-prod && echo "//더러움" >> README.md
.venv/Scripts/python.exe -m src.utils.tree_role --check; echo "dirty exit=$? (1이어야 정상)"
git checkout README.md

# 5. 개발에서 승격 시도 → 거부
cd /d/Private/stock && powershell -NoProfile -ExecutionPolicy Bypass -File scripts/promote.ps1 -Tag release/test; echo "promote exit=$? (1이어야 정상)"

# 6. 개발에서 전량 청산 시도 → 거부
cd /d/Private/stock && .venv/Scripts/python.exe scripts/paper_close_all.py; echo "close_all exit=$? (2여야 정상)"
```

Expected: 1·2는 정상, 3·4·5는 exit 1, 6은 exit 2

- [ ] **Step 5: 토큰 공유를 확인한다**

```bash
cd /d/Private/stock
stat -c '%y' /d/Private/stock-prod/data/auth/token_cache.json
.venv/Scripts/python.exe -c "
import os; os.environ['AUTH_DIR']='D:/Private/stock-prod/data/auth'
from src.api import auth
c=auth._load_cache(); print('토큰 있음:', bool(c.get('access_token')), '만료:', c.get('expires_at'))
"
stat -c '%y' /d/Private/stock-prod/data/auth/token_cache.json
```

Expected: 앞뒤 mtime이 같다 (개발이 캐시를 쓰지 않았다)

- [ ] **Step 6: 다음 날 아침 확인**

08:00~08:05에 확인한다:

```bash
tail -5 /d/Private/stock-prod/data/logs/watchdog_$(date +%Y%m%d).log
head -3 /d/Private/stock-prod/data/logs/$(date +%Y%m%d).jsonl
ls /d/Private/stock/data/logs/   # 비어 있어야 한다
```

Expected: 운영 트리에 워치독·앱 로그가 쌓이고, **개발 트리 로그는 비어 있다**

- [ ] **Step 7: 문서를 갱신하고 커밋한다**

`docs/REAL_TRADING_CHECKLIST.md`에 설계 6-5절의 5개 항목을 추가한다:

```markdown
## 개발 트리 분리 관련 (2026-09-10 도입)

REAL 전환 시 아래를 조정한다. 근거는
`docs/superpowers/specs/2026-09-10-prod-dev-separation-design.md` 6-5절이다.

1. 운영 `F3_ALLOC_RATIO`를 REAL 정책값(초기 0.20 이하)으로 변경
2. 개발 `F3_ALLOC_RATIO`를 0.95로 복귀
3. 개발 `AUTH_DIR`을 자기 트리로 되돌리고 `KIS_AUTH_READONLY=0`
   — REAL 토큰과 PAPER 토큰은 발급 호스트가 달라 공유할 수 없다
4. `DEV_EXCLUDE_HELD_TICKERS` 해제 (선택 — 켜둬도 무해)
5. 앱키 분리 여부 결정. `.env.paper`와 `.env.real`이 같은 앱키를 쓰고
   있으므로(2026-09-10 확인), 분리하지 않으면 레이트 제한을 계속 공유한다.
   분리하면 개발 스케줄 오버라이드(`SCHEDULE_F*`)도 해제할 수 있다
```

```bash
cd /d/Private/stock
git add docs/REAL_TRADING_CHECKLIST.md
git commit -m "docs(real): carry the dev-tree settings into the REAL switch"
```

- [ ] **Step 8: 정리**

검증이 끝나고 하루가 무사히 지나면:

```bash
rm -rf /d/Private/stock/data/logs.migrated /d/Private/stock/data/state.migrated /d/Private/stock/data/db.migrated
```

---

## 롤백

Phase 3 중 무엇이 잘못되면:

```powershell
# 1. 새 운영 정지
Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
  Where-Object { $_.CommandLine -match 'stock-prod' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# 2. 스케줄 작업 경로를 되돌린다
$old = "D:\Private\stock"
Set-ScheduledTask -TaskName 'StockBot_Watchdog' -Action (
  New-ScheduledTaskAction -Execute "$old\.venv\Scripts\pythonw.exe" `
    -Argument "$old\scripts\watchdog_check.py")
```

```bash
# 3. 개발 트리의 데이터를 되돌리고 역할을 prod로 바꾼다
cd /d/Private/stock
rm -rf data/logs data/state data/db
mv data/logs.migrated data/logs; mv data/state.migrated data/state; mv data/db.migrated data/db
git checkout main
echo prod > .stock-role
```

원본 데이터가 개발 트리에 그대로 남아 있으므로 손실은 없다.

## Self-Review 결과

**스펙 커버리지:** 1절 구조 → Task 11. 2절 승격 경로 → Task 6·7. 3절 2단 게이트 → Task 6·7. 4절 기동 거부 → Task 4·8. 5절 런타임 격리 → Task 11 Step 6·7. 6절 계좌 규칙 → Task 9·10, Task 11 Step 6·7. 6-5절 모드별 범위 → Task 12 Step 7. 7절 지문 비용 → Task 10 Step 7 묶음 커밋. 8절 마이그레이션 → Task 11·12.

**미커버 항목:** 스펙 5절의 `REPLAY_SOURCE_DIR`은 값만 설정하고(Task 11 Step 7) 읽는 코드는 없다. 하위 프로젝트 B가 소비한다. 지금 소비자가 없는 설정을 넣는 것은 B의 착수를 위한 자리표시이며, 그 전까지 아무 동작에도 영향을 주지 않는다.

**타입 일관성:** `ReleaseState(ok, reason, detail)`이 Task 2에서 정의되어 Task 3·4에서 같은 필드명으로 쓰인다. `read_role`은 전 구간 `str | None`을 반환한다. `_held_tickers_to_exclude`는 `set[str]`을 반환해 `exclude_tickers`의 기존 타입과 맞는다.
