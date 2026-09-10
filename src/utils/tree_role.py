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


def require_prod(root: Path) -> None:
    """계좌 전체를 건드리는 스크립트는 운영 트리에서만 돈다.

    개발 트리는 운영과 모의계좌를 공유한다. 여기서 전량 청산 같은 계좌 단위
    작업을 돌리면 운영이 들고 있는 포지션까지 팔린다. 역할이 prod가 아니면
    (역할이 없어도 마찬가지로) 즉시 거부한다 — fail-closed.
    """
    role = read_role(root)
    if role != ROLE_PROD:
        print(
            f"[거부] 계좌 전체를 건드리는 작업은 운영 트리에서만 실행합니다. "
            f"현재 역할: {role or '(.stock-role 없음)'}"
        )
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    """`python -m src.utils.tree_role --check` — 통과 0, 실패 1."""
    root = Path(__file__).resolve().parents[2]
    role = read_role(root)
    state = check_release_state(root, role)
    print(f"role={role or '(없음)'} ok={state.ok} reason={state.reason} detail={state.detail}")
    return 0 if state.ok else 1


if __name__ == "__main__":
    sys.exit(main())
