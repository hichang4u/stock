"""실제 별도 프로세스로 본 예산 장부·위험 중단의 경쟁과 강제 종료.

같은 프로세스 안의 두 연결 테스트(test_real_validation_risk.py)는 이미 있다. 여기서는 그것으로
확인되지 않는 것만 본다: 운영체제 프로세스끼리의 동시 예약, 트랜잭션 도중·직후의 강제 종료,
그 뒤 새 프로세스가 보는 상태.

임시 디렉터리의 합성 저장소만 쓴다. 운영 프로세스·운영 DB와 무관하다. 자식 프로세스는
시간 제한 안에 끝나지 않으면 죽이고 테스트를 실패시킨다.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.real_validation import gate, risk_halt, sizing
from src.real_validation.core import Verdict
from src.real_validation.risk_halt import HaltKind
from src.real_validation.store import Store
from tests import real_validation_proc_worker as worker
from tests import real_validation_support as s

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT_SEC = 60
RACERS = 6


def _spawn(*args: object) -> subprocess.Popen:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop(gate.ENV_DIR, None)
    return subprocess.Popen(
        [sys.executable, "-m", "tests.real_validation_proc_worker", *map(str, args)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def _finish(process: subprocess.Popen) -> dict:
    try:
        out, err = process.communicate(timeout=TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        pytest.fail("worker process did not finish in time")
    assert process.returncode == 0, err
    return json.loads(out.strip().splitlines()[-1])


def _run(*args: object) -> dict:
    return _finish(_spawn(*args))


def _race(commands: list[tuple]) -> list[dict]:
    """일꾼을 모두 띄워 저장소를 열게 한 뒤 신호 파일 하나로 동시에 출발시킨다."""
    processes = [_spawn(*command) for command in commands]
    go = Path(commands[0][-1])
    time.sleep(1.0)  # 인터프리터 기동·임포트가 끝나 신호를 기다리는 상태가 되도록
    go.write_bytes(b"go")
    return [_finish(process) for process in processes]


def _kill_when_ready(process: subprocess.Popen, ready: Path) -> None:
    deadline = time.monotonic() + TIMEOUT_SEC
    try:
        while not ready.exists():
            assert process.poll() is None, process.stderr.read() if process.stderr else ""
            assert time.monotonic() < deadline, "worker never became ready"
            time.sleep(0.01)
    finally:
        process.kill()
        process.communicate(timeout=TIMEOUT_SEC)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "gate.db"
    with Store.open(path, create=True):
        pass
    return path


# ── 동시 예약 ────────────────────────────────────────────────────────


def test_processes_racing_for_one_loss_budget_let_exactly_one_order_in(db, tmp_path):
    go = tmp_path / "go"

    results = _race([("reserve", db, f"buy-{i}", go) for i in range(RACERS)])

    assert Counter(r["result"] for r in results) == {"OK": 1, "LossBudgetExceeded": RACERS - 1}
    with Store.open(db) as store:
        states = sizing.orders(store)
        budget = s.risk_policy().per_trade_loss_budget()
        assert len(states) == 1
        assert sum(state.loss_in_use for state in states.values()) <= budget


def test_processes_racing_with_the_same_order_id_reserve_it_once(db, tmp_path):
    go = tmp_path / "go"

    results = _race([("reserve", db, "buy-same", go) for _ in range(RACERS)])

    assert Counter(r["result"] for r in results) == {"OK": 1, "DuplicateReservation": RACERS - 1}
    with Store.open(db) as store:
        assert [row["event"] for row in store.reservation_events()] == ["RESERVE"]


def test_processes_racing_to_release_one_order_release_it_once(db, tmp_path):
    with Store.open(db, create=True) as store:
        worker._reserve(store, "buy-1")
    go = tmp_path / "go"

    results = _race([("release", db, "buy-1", "CANCEL_CONFIRMED", go) for _ in range(RACERS)])

    assert Counter(r["result"] for r in results) == {True: 1, False: RACERS - 1}
    with Store.open(db) as store:
        assert [row["event"] for row in store.reservation_events()] == ["RESERVE", "RELEASE"]
        assert sizing.orders(store)["buy-1"].settled


# ── 강제 종료 ────────────────────────────────────────────────────────


def test_process_killed_inside_a_transaction_reserves_nothing(db, tmp_path):
    """예약을 쓰던 중 죽으면 그 예약은 없던 일이 된다 — 반쯤 잡힌 예산이 남지 않는다."""
    ready = tmp_path / "ready"

    _kill_when_ready(_spawn("hold-uncommitted", db, "buy-dead", ready), ready)

    # 읽기 전용 연결은 끊긴 트랜잭션을 되돌릴 수 없다. 그동안은 '확인 불가'로 막히거나(되돌릴
    # 저널이 남은 경우) 깨끗한 상태로 읽힌다. 어느 쪽이든 죽은 예약이 보이면 안 된다.
    finding = risk_halt.halt_finding(str(db))
    assert finding.verdict in (Verdict.PASS, Verdict.NOT_EVALUATED)

    with Store.open(db, create=True) as store:
        assert sizing.orders(store) == {}
        worker._reserve(store, "buy-after-crash")
    assert _run("budget", db)["orders"] == ["buy-after-crash"]
    assert risk_halt.halt_finding(str(db)).verdict is Verdict.PASS
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_process_killed_right_after_reserving_leaves_the_budget_held(db, tmp_path):
    """예약은 커밋됐는데 주문을 보내기 전에 죽었다. 재시작한 쪽이 예산을 또 쓰지 못한다."""
    ready = tmp_path / "ready"

    _kill_when_ready(_spawn("reserve-then-hang", db, "buy-1", ready), ready)

    held = _run("budget", db)
    assert held["orders"] == ["buy-1"] and held["loss_in_use"] > 0
    (tmp_path / "go").write_bytes(b"go")
    assert _run("reserve", db, "buy-2", tmp_path / "go") == {"result": "LossBudgetExceeded"}
    assert _run("reserve", db, "buy-1", tmp_path / "go") == {"result": "DuplicateReservation"}

    # 시간이 지났다고 풀리지 않는다. 주문이 나가지 않았음을 확인한 뒤에만, 한 번만 풀린다.
    released = _run("release", db, "buy-1", "NOT_SENT_CONFIRMED", tmp_path / "go")
    again = _run("release", db, "buy-1", "NOT_SENT_CONFIRMED", tmp_path / "go")
    assert (released, again) == ({"result": True}, {"result": False})
    assert _run("budget", db)["loss_in_use"] == 0
    assert _run("reserve", db, "buy-1", tmp_path / "go") == {"result": "DuplicateReservation"}
    assert _run("reserve", db, "buy-2", tmp_path / "go") == {"result": "OK"}


def test_halt_written_by_a_killed_process_blocks_a_fresh_process_but_not_exits(tmp_path):
    directory = tmp_path / "rv"
    evaluated_at = s.passing_directory(directory)
    now = (datetime.fromisoformat(evaluated_at) + timedelta(hours=1)).isoformat()
    before = _run("gate", directory, now)
    ready = tmp_path / "ready"

    _kill_when_ready(_spawn("halt-then-hang", directory / gate.STORE_FILE, ready), ready)
    after = _run("gate", directory, now)

    assert before["risk_not_halted"]["passed"] is True
    assert after["risk_not_halted"]["reasons"] == ["RISK_HALTED_DRAWDOWN"]
    assert after["eligible_for_real"] is False
    assert after["permits"] == {
        "NEW_BUY": False, "ADD_BUY": False,
        "PROTECTIVE_SELL": True, "CANCEL": True, "RECONCILE": True,
    }
    # 집행 미연결 상태(PRE_BUY_ENFORCEMENT_WIRED=False)를 건드리지 않았다 — 중단 전에도 닫혀 있다.
    assert before["eligible_for_real"] is False
    assert any("RUNTIME_PRE_BUY_ENFORCEMENT_NOT_WIRED" in b for b in before["blockers"])
    # 낙폭 중단은 거래일 전환으로 풀리지 않는다. 새 프로세스가 다음 날 시도해도 그대로다.
    go = tmp_path / "go"
    go.write_bytes(b"go")
    next_day = s.next_weekday(worker.DAY)
    assert _run("release-daily", directory / gate.STORE_FILE, next_day, go) == {"result": []}
    assert _run("gate", directory, now)["risk_not_halted"]["passed"] is False


def test_daily_halt_is_released_once_even_when_processes_race(db, tmp_path):
    with Store.open(db, create=True) as store:
        halt_id = risk_halt.raise_halt(
            store, kind=HaltKind.DAILY_LOSS, reason="synthetic",
            policy_hash=s.risk_policy().policy_hash(), trading_day=worker.DAY,
            at=s.ts(worker.DAY, "10:00"),
        )
    go = tmp_path / "go"
    next_day = s.next_weekday(worker.DAY)

    results = _race([("release-daily", db, next_day, go) for _ in range(RACERS)])

    assert sorted(map(tuple, (r["result"] for r in results))) == [()] * (RACERS - 1) + [
        (halt_id,)
    ]
    with Store.open(db) as store:
        assert [row["event"] for row in store.halt_events()] == ["HALT", "RESUME"]
        assert risk_halt.active_halts(store) == []
