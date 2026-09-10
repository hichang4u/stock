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
