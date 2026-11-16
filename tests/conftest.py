"""테스트 공통 설정."""
import logging
import os
import sys
from pathlib import Path

import pytest

# 테스트 모듈 수집 중 main.py가 개발 머신의 .env를 읽어 모듈 상수를
# 바꾸지 않도록, 어떤 테스트 모듈보다 먼저 dotenv 로드를 비활성화한다.
os.environ["STOCK_SKIP_DOTENV"] = "1"

# 체결통보는 기본으로 끈다. 기존 진입·F4 테스트가 주문을 장부에 등록하거나 가짜 소켓에 통보 구독을
# 보내 동작이 달라지지 않게 한다. 체결통보 테스트는 각자 monkeypatch로 켠다.
os.environ.setdefault("F3_FILL_NOTICE_ENABLED", "0")

# 개장 직후 첫 체결 대기도 기본으로 끈다. 실제 시계로 도는 진입 테스트가 09:00 ~ 09:00:45에
# 실행되면 첫 체결 신호를 기다리며 멈춘다. 대기 테스트(test_first_trade_wait.py)는 각자 켠다.
os.environ.setdefault("F3_FIRST_TRADE_WAIT_UNTIL_SEC", "0")

# 테스트 중 stock 로거 stdout/파일 출력 억제
_stock_log = logging.getLogger("stock")
_stock_log.handlers = []
_stock_log.propagate = False


def _clear_stock_log_handlers() -> None:
    """테스트가 설치한 파일/스트림 핸들러를 다음 테스트로 넘기지 않는다."""
    for handler in list(_stock_log.handlers):
        _stock_log.removeHandler(handler)
        handler.close()


@pytest.fixture(autouse=True)
def isolate_runtime_output_dirs(tmp_path_factory, monkeypatch):
    """모든 프로덕션 런타임 산출물을 테스트별 임시 루트로 격리한다."""
    runtime_dir = tmp_path_factory.mktemp("runtime-output")
    state_dir = runtime_dir / "state"
    probe_dir = runtime_dir / "paper-fast-probe"
    log_dir = runtime_dir / "logs"
    db_dir = runtime_dir / "db"
    auth_dir = runtime_dir / "auth"
    bars_dir = runtime_dir / "bars"
    for output_dir in (state_dir, probe_dir, log_dir, db_dir, auth_dir, bars_dir):
        output_dir.mkdir()

    monkeypatch.setenv("STATE_DIR", str(state_dir))
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(probe_dir))
    monkeypatch.setenv("LOG_DIR", str(log_dir))
    monkeypatch.setenv("DB_DIR", str(db_dir))
    monkeypatch.setenv("AUTH_DIR", str(auth_dir))
    monkeypatch.setenv("TRACK_B_BARS_DIR", str(bars_dir))

    # 수집 시점에 이미 import된 모듈의 경로 상수도 환경변수와 같은 임시
    # 루트로 맞춘다. fixture 이후 import되는 모듈은 위 env를 직접 읽는다.
    main_module = sys.modules.get("main")
    if main_module is not None:
        monkeypatch.setattr(main_module, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(main_module, "LOG_DIR", str(log_dir))
        monkeypatch.setattr(main_module, "DB_PATH", str(db_dir / "trading.db"))
    server_module = sys.modules.get("src.api.server")
    if server_module is not None:
        monkeypatch.setattr(server_module, "_LOG_DIR", Path(log_dir))
    # 1분 봉 파일도 격리한다. /api/status가 가격흐름 앞 구간을 저장 봉에서 읽으므로
    # (minute_price_seed) 이 줄이 없으면 status 테스트가 개발 트리의 data/bars를 읽고,
    # 봉을 쓰는 테스트는 거기에 쓴다. 파일 캐시는 테스트 사이에 넘기지 않는다.
    bars_module = sys.modules.get("src.bars")
    if bars_module is not None:
        monkeypatch.setattr(bars_module, "_BARS_DIR", bars_dir)
    seed_cache = getattr(server_module, "_PRICE_SEED_FILE_CACHE", None)
    if seed_cache is not None:
        seed_cache.clear()

    # logger.setup()을 호출한 테스트 뒤의 무관한 로그가 그 파일 핸들러로
    # 흘러가는 캐스케이드를 테스트 경계 양쪽에서 차단한다.
    _clear_stock_log_handlers()
    _stock_log.propagate = False
    yield runtime_dir
    if seed_cache is not None:
        seed_cache.clear()
    _clear_stock_log_handlers()
