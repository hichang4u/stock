import time

import pytest

from src import bars, release
from src.modules import tick_capture


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(bars, "_BARS_DIR", tmp_path)
    tick_capture.clear_tick_listeners()
    bars.reset()
    yield
    bars.reset()
    tick_capture.clear_tick_listeners()


def _tick(i):
    ts = f"2026-08-27T09:35:{i % 60:02d}+09:00"
    raw = [""] * 46
    raw[0], raw[2] = "006340", str(14500 + (i % 50))
    return {
        "source_ts": ts, "received_at": ts, "price": 14500.0 + (i % 50),
        "qty": 10, "source": "ws", "valid": True, "ticker": "006340", "raw": raw,
    }


def _elapsed(ticks):
    started = time.perf_counter()
    for tick in ticks:
        tick_capture.enqueue(tick)
    return time.perf_counter() - started


def test_track_b_files_are_not_in_the_strategy_fingerprint():
    for name in (
        "src/bars.py",
        "src/indicators.py",
        "src/api/kis_minute_bars.py",
        "src/modules/tick_capture.py",
        "src/api/server.py",
    ):
        assert name not in release._STRATEGY_FILES


def test_the_listener_adds_no_measurable_cost_to_the_tick_path():
    ticks = [_tick(i) for i in range(20_000)]

    baseline = min(_elapsed(ticks) for _ in range(3))
    bars.install()
    with_listener = min(_elapsed(ticks) for _ in range(3))

    # 동기 경로에 더해지는 일은 deque.append 하나다. 5배(바닥 0.01s)는
    # 스케줄러 잡음에는 여유가 있으면서(측정값 대비 4배 이상 헤드룸) 대략
    # 10배 이상의 회귀 — 팬아웃 지점에서 집계·지표를 도는 것에 해당하는
    # 규모 — 는 잡아내는 상한이다.
    assert with_listener < max(baseline * 5, 0.01)


def test_stage_one_never_opens_a_database_connection():
    # 컨트롤러 판정: db._conn이 None이라는 절대 단언은 이 테스트 파일이
    # test_api_*, test_db_*보다 먼저 실행된다는 pytest 수집 순서에 암묵적으로
    # 의존한다. 이전에 실행된 어떤 테스트가 db.init()을 불렀다면 _conn이 이미
    # 채워져 있고, 이 테스트는 여기서 검증하려는 것과 무관한 이유로 실패한다.
    # 이 테스트가 잠그려는 주장은 "1단계는 DB를 건드리지 않는다"이지
    # "이 테스트보다 먼저 실행된 어떤 테스트도 커넥션을 열지 않았다"가 아니다.
    # 그래서 절대값 대신 틱 폭주 전후의 동일성을 비교한다.
    from src import db

    before = db._conn

    bars.install()
    for i in range(200):
        tick_capture.enqueue(_tick(i))
    bars.drain()

    # 봉 집계가 DB를 건드렸다면 커넥션 객체가 (열렸든 새로 열렸든) 달라졌을 것이다.
    assert db._conn is before


def test_the_bars_module_does_not_import_db():
    import ast
    import inspect

    def _modules_imported(source: str) -> set[str]:
        # 모듈-플러스-이름 문자열이 아니라 "참조된 모듈"의 집합을 모은다.
        # node.module 자체를 넣는 것이 핵심이다 — 그래야
        # `from src.db import open_trade`가 "src.db"를 직접 내놓는다.
        tree = ast.parse(source)
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    modules.add(node.module)
                    modules.update(
                        f"{node.module}.{alias.name}" for alias in node.names
                    )
        return modules

    def _touches_db(modules: set[str]) -> bool:
        return any(
            m == "db" or m == "src.db" or m.startswith("src.db.") for m in modules
        )

    # 검사기 자체를 먼저 검증한다: 세 가지 import 형태(직접 import, `from src
    # import db`, `from src.db import <이름>`)는 모두 걸려야 하고, db를
    # 언급만 하는 주석/독스트링은 걸리면 안 된다(부분 문자열 매칭 결함 없음을
    # 증명한다).
    assert _touches_db(_modules_imported("import src.db"))
    assert _touches_db(_modules_imported("from src import db"))
    assert _touches_db(_modules_imported("from src.db import open_trade"))
    assert not _touches_db(
        _modules_imported(
            "# this comment mentions db but does not import it\n"
            '"""docstring also mentions db here"""\n'
            "x = 1\n"
        )
    )

    assert not _touches_db(_modules_imported(inspect.getsource(bars)))
