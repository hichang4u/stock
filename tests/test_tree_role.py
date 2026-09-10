"""트리 역할 판정 — 역할 불명인 트리는 절대 통과시키지 않는다."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_prod_accepts_a_release_tag_even_with_other_tags_on_the_same_commit(tmp_path):
    """릴리스 커밋에 메모용 태그가 같이 붙어도 release/* 를 찾아 승인해야 한다.

    `git describe --exact-match`는 여러 태그 중 임의의 하나를 골라 반환하고
    release/*를 우대하지 않는다 — 그 하나가 release/*가 아니면 코드는 그대로인데
    운영이 영구히 거부당한다(재현: 경량 태그 하나만 얹어도 거부로 뒤집힌다).
    """
    _git_repo(tmp_path)
    subprocess.run(["git", "tag", "release/20260911-1"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "tag", "hotfix-note"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "tag", "-a", "v1.0", "-m", "annotated"],
        cwd=str(tmp_path),
        check=True,
    )
    state = tree_role.check_release_state(tmp_path, "prod")
    assert state.ok is True
    assert state.reason == "AT_RELEASE_TAG"
    assert state.detail == "release/20260911-1"


def test_prod_reports_git_failed_distinct_from_no_release_tag(tmp_path):
    """git 명령 자체의 실패는 '태그가 없다'와 다른 사유로 남아야 한다.

    커밋이 하나도 없는 저장소는 `git status --porcelain`은 통과하지만(빈 출력),
    HEAD가 가리킬 커밋이 없어 `git tag --points-at HEAD`가 실패한다 — 이 실패를
    NOT_AT_RELEASE_TAG로 뭉개면 08:00 로그만 보는 운영자가 "릴리스 태그가 없다"와
    "git이 고장났다"를 구분할 수 없다.
    """
    run = lambda *a: subprocess.run(["git", *a], cwd=str(tmp_path), check=True,
                                    capture_output=True)
    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "test")
    state = tree_role.check_release_state(tmp_path, "prod")
    assert state.ok is False
    assert state.reason == "GIT_FAILED"


def _copy_tree_role_module_into(root: Path) -> None:
    """tree_role.main()은 root를 __file__ 위치(parents[2])로 구조적으로 유도한다.

    subprocess로 --check를 검증하려면, 실제 배포 트리처럼 모듈 파일 자체가
    트리 루트 아래 src/utils/tree_role.py에 있어야 한다. 그래서 임시 트리에
    src/utils 패키지 구조를 만들고 실제 모듈 내용을 그대로 복사해 넣는다.
    """
    utils_dir = root / "src" / "utils"
    utils_dir.mkdir(parents=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (utils_dir / "__init__.py").write_text("", encoding="utf-8")
    real_module = Path(tree_role.__file__)
    (utils_dir / "tree_role.py").write_bytes(real_module.read_bytes())


def _run_check(root: Path) -> subprocess.CompletedProcess:
    # PYTHONDONTWRITEBYTECODE: 이 호출이 __pycache__를 만들면 prod 트리
    # 케이스에서 그 자체가 untracked 파일이 되어 TREE_DIRTY로 오판된다
    # (커밋 이후에 생기므로 초기 커밋에 같이 넣을 수도 없다).
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, "-m", "src.utils.tree_role", "--check"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def test_main_check_exit_code_for_a_dev_tree(tmp_path):
    """main()의 계약(0=통과, 1=거부)을 서브프로세스 수준에서 고정한다.

    `return 0 if state.ok else 1`을 뒤집어도 나머지 1000여 개 테스트는 전부
    통과한다 — main()은 두 소비자(런처·사전검사)가 실제로 부르는 표면인데
    그 계약을 고정하는 테스트가 하나도 없었다. 서브프로세스로 돌리면
    cwd 독립성(I2)도 같이 고정된다: cwd=트리 루트일 때 모듈이 정상적으로
    임포트되고 그 트리의 .stock-role을 읽는다는 것 자체가 검증 대상이다.
    """
    _copy_tree_role_module_into(tmp_path)
    _role(tmp_path, "dev")
    result = _run_check(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_main_check_exit_code_for_a_missing_role_tree(tmp_path):
    _copy_tree_role_module_into(tmp_path)
    result = _run_check(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr


def test_main_check_exit_code_for_a_prod_tree(tmp_path):
    _copy_tree_role_module_into(tmp_path)
    # prod 판정은 워킹트리가 클린해야 하므로, .stock-role은 초기 커밋에
    # 같이 들어가도록 git init 전에 써 둔다 — 커밋 뒤에 쓰면 그 파일만
    # untracked로 남아 TREE_DIRTY가 되어 이 테스트의 의도(정상 통과)와
    # 어긋난다.
    _role(tmp_path, "prod")
    _git_repo(tmp_path)
    subprocess.run(["git", "tag", "release/20260911-1"], cwd=str(tmp_path), check=True)
    result = _run_check(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_require_prod_refuses_when_role_is_missing(tmp_path):
    """계좌 전체 청산은 운영 트리에서만 — 역할 파일이 없으면 거부."""
    with pytest.raises(SystemExit) as excinfo:
        tree_role.require_prod(tmp_path)
    assert excinfo.value.code == 2


def test_require_prod_refuses_dev_tree(tmp_path):
    _role(tmp_path, "dev")
    with pytest.raises(SystemExit) as excinfo:
        tree_role.require_prod(tmp_path)
    assert excinfo.value.code == 2


def test_require_prod_allows_prod_tree(tmp_path):
    _role(tmp_path, "prod")
    tree_role.require_prod(tmp_path)  # 예외가 나지 않으면 통과
