"""공통 pytest fixture가 모든 운영 산출물을 임시 경로로 격리하는지 검증한다."""

import logging
import os
from pathlib import Path
from uuid import uuid4

import pytest

import main
from src import db, state
from src.api import auth, server
from src.modules import paper_fast_probe
from src.utils import logger


def _tree_contains(root: Path, marker: bytes) -> bool:
    if not root.exists():
        return False
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if marker in path.read_bytes():
                return True
        except OSError:
            continue
    return False


@pytest.mark.asyncio
async def test_common_fixture_redirects_all_runtime_outputs(
    isolate_runtime_output_dirs, monkeypatch
):
    runtime_dir = Path(isolate_runtime_output_dirs)
    output_dirs = {
        "STATE_DIR": runtime_dir / "state",
        "PAPER_FAST_PROBE_DIR": runtime_dir / "paper-fast-probe",
        "LOG_DIR": runtime_dir / "logs",
        "DB_DIR": runtime_dir / "db",
        "AUTH_DIR": runtime_dir / "auth",
    }
    live_dirs = {
        key: (Path.cwd() / "data" / live_name).resolve()
        for key, live_name in {
            "STATE_DIR": "state",
            "PAPER_FAST_PROBE_DIR": "paper_fast_probe",
            "LOG_DIR": "logs",
            "DB_DIR": "db",
            "AUTH_DIR": "auth",
        }.items()
    }
    for key, output_dir in output_dirs.items():
        assert Path(os.environ[key]).resolve() == output_dir.resolve()
        assert output_dir.resolve() != live_dirs[key]

    assert Path(main.STATE_DIR).resolve() == output_dirs["STATE_DIR"].resolve()
    assert Path(main.LOG_DIR).resolve() == output_dirs["LOG_DIR"].resolve()
    assert Path(main.DB_PATH).resolve() == (output_dirs["DB_DIR"] / "trading.db").resolve()
    assert server._LOG_DIR.resolve() == output_dirs["LOG_DIR"].resolve()

    marker_text = f"PYTEST_RUNTIME_ISOLATION_{uuid4().hex}"
    marker = marker_text.encode()
    assert not any(_tree_contains(live_dir, marker) for live_dir in live_dirs.values())

    old_target_name = state.get().target_name
    try:
        state.get().target_name = marker_text
        await state.persist(os.environ["STATE_DIR"], "20991231")
    finally:
        state.get().target_name = old_target_name

    paper_fast_probe._append_record(marker_text, phase="TEST")
    # 이 테스트가 보려는 것은 AUTH_DIR 리디렉션이므로 쓰는 구성이어야 한다.
    # 개발 트리에서 돌면 실제 역할이 dev라 쓰기가 생략된다.
    monkeypatch.setattr(auth.tree_role, "read_role", lambda root: "prod")
    auth._save_cache(marker_text, "2099-12-31 23:59:59")
    logger.setup(os.environ["LOG_DIR"])
    logger.log(marker_text, level="INFO")

    await db.init(main.DB_PATH)
    try:
        await db.record_skip("20991231", "ENTRY_FAIL", marker_text)
    finally:
        await db.close()

    assert all(_tree_contains(output_dir, marker) for output_dir in output_dirs.values())
    assert not any(_tree_contains(live_dir, marker) for live_dir in live_dirs.values())


def test_common_fixture_starts_without_stock_logger_handlers():
    """이전 테스트의 logger.setup() 핸들러가 다음 테스트로 넘어오지 않는다."""
    stock_log = logging.getLogger("stock")

    assert stock_log.handlers == []
    assert stock_log.propagate is False
