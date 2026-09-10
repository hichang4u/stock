"""트리 역할 판정 — 역할 불명인 트리는 절대 통과시키지 않는다."""

import subprocess
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
