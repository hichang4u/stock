"""진입 종목 교체 시 캡처 싱글턴 재바인딩.

2026-08-26 회귀: 09:01:10 TARGET_LOCKED(047040)로 붙은 캡처가 09:01:13 F3 최종
선정(215600)에서 재바인딩되지 않아 하루치 틱이 전량 폐기되고, 047040 파일이
0바이트·미최종화 상태로 남았다.
"""
import asyncio
import gzip
import json

import pytest

from src import db
from src.modules import tick_capture as tc

EXP = "baseline-x"
ENTRY_AT = "2026-08-26T09:01:19+09:00"


@pytest.fixture
async def mem():
    await db.init(":memory:")
    yield
    await db.close()


@pytest.fixture(autouse=True)
def _singleton(monkeypatch, tmp_path):
    # 프로덕션 싱글턴은 테스트에서 봉인돼 있다. 이 파일은 그 배선 자체가 대상이다.
    monkeypatch.setattr(tc, "_capture_allowed", lambda: True)
    monkeypatch.setattr(tc, "STRATEGY_TICK_DIR", str(tmp_path))
    tc._capture = None
    tc._switch_finalizers.clear()
    tc._pending_switch_by_ticker.clear()
    yield
    tc._capture = None
    tc._switch_finalizers.clear()
    tc._pending_switch_by_ticker.clear()


def _tick(ticker: str, price: float = 3095.0, second: str = "20") -> dict:
    ts = f"2026-08-26T09:01:{second}+09:00"
    return {
        "source_ts": ts,
        "received_at": ts,
        "price": price,
        "qty": 5,
        "source": "ws",
        "valid": True,
        "ticker": ticker,
    }


def _rows(tmp_path, ticker: str) -> list[dict]:
    rows: list[dict] = []
    for gzp in sorted((tmp_path / "20260826").glob(f"{ticker}.*.jsonl.gz")):
        with gzip.open(gzp, "rt", encoding="utf-8") as f:
            rows += [json.loads(line) for line in f if line.strip()]
    return rows


async def test_start_with_new_ticker_rebinds_active_capture(mem, tmp_path):
    assert tc.start("20260826", "047040", None, EXP, None) is True

    assert tc.start("20260826", "215600", 32, EXP, ENTRY_AT) is True
    assert tc.active_ticker() == "215600"

    await tc.finalize("COMPLETE", reached_expected_close=True)
    await tc.drain_switch_finalizers()


async def test_ticks_after_switch_land_in_the_traded_ticker_file(mem, tmp_path):
    tc.start("20260826", "047040", None, EXP, None)
    tc.start("20260826", "215600", 32, EXP, ENTRY_AT)

    tc.enqueue(_tick("215600", price=3095.0))
    await tc.finalize("COMPLETE", reached_expected_close=True)
    await tc.drain_switch_finalizers()

    assert [r["price"] for r in _rows(tmp_path, "215600")] == [3095.0]
    assert _rows(tmp_path, "047040") == []  # 낡은 종목 파일로 새지 않는다


async def test_switched_away_capture_is_finalized_as_incomplete(mem, tmp_path):
    tc.start("20260826", "047040", None, EXP, None)
    tc.start("20260826", "215600", 32, EXP, ENTRY_AT)
    await tc.drain_switch_finalizers()

    m = await db.get_price_path_manifest("20260826", "047040", EXP)
    assert m is not None
    assert m["data_complete"] == 0
    assert m["missing_reason"] == "TARGET_SWITCHED"

    await tc.finalize("COMPLETE", reached_expected_close=True)


async def test_same_ticker_start_stays_idempotent(mem, tmp_path):
    tc.start("20260826", "215600", None, EXP, None)
    first = tc._capture

    assert tc.start("20260826", "215600", 32, EXP, ENTRY_AT) is True
    assert tc._capture is first  # 재시작하면 seq·파일이 끊긴다

    await tc.finalize("COMPLETE", reached_expected_close=True)
    await tc.drain_switch_finalizers()


async def test_same_ticker_restart_adopts_the_trade_id(mem, tmp_path):
    """F4가 09:00에 trade_id=None으로 먼저 붙고 F3가 체결 뒤 같은 종목으로 다시
    start()하면, 인스턴스는 유지하되 체결 식별자는 받아들여야 한다. 2026-09-02
    이후 manifest.trade_id가 전부 NULL로 남은 원인."""
    tc.start("20260826", "215600", None, EXP, None)
    tc.start("20260826", "215600", 32, EXP, ENTRY_AT)

    assert tc._capture.trade_id == 32
    assert tc._capture.entry_at == ENTRY_AT

    await tc.finalize("COMPLETE", reached_expected_close=True)
    await tc.drain_switch_finalizers()
    m = await db.get_price_path_manifest("20260826", "215600", EXP)
    assert m["trade_id"] == 32


async def test_same_ticker_restart_never_overwrites_a_known_trade_id(mem, tmp_path):
    tc.start("20260826", "215600", 32, EXP, ENTRY_AT)
    tc.start("20260826", "215600", None, EXP, None)

    assert tc._capture.trade_id == 32
    assert tc._capture.entry_at == ENTRY_AT

    await tc.finalize("COMPLETE", reached_expected_close=True)
    await tc.drain_switch_finalizers()


async def test_module_finalize_waits_for_switched_away_capture(mem, tmp_path, monkeypatch):
    """종료 경로가 tick_capture.finalize 하나만 부르므로, 전환으로 떼어낸 캡처의
    manifest도 그 안에서 확정돼야 DB close 전에 디스크·DB가 일치한다."""
    real = tc.TickCapture.finalize

    async def slow_for_detached(self, reason, *, reached_expected_close):
        if self.ticker == "047040":
            await asyncio.sleep(0.3)
        return await real(self, reason, reached_expected_close=reached_expected_close)

    monkeypatch.setattr(tc.TickCapture, "finalize", slow_for_detached)

    tc.start("20260826", "047040", None, EXP, None)
    tc.start("20260826", "215600", 32, EXP, ENTRY_AT)

    try:
        await tc.finalize("PROCESS_SHUTDOWN", reached_expected_close=False)

        m = await db.get_price_path_manifest("20260826", "047040", EXP)
        assert m is not None
        assert m["missing_reason"] == "TARGET_SWITCHED"
    finally:
        # 마감이 남아 있으면 DB가 닫힌 뒤 깨어나 테스트 세션이 멈춘다.
        await tc.drain_switch_finalizers()


async def test_switching_back_to_an_earlier_ticker_keeps_seq_unique(mem, tmp_path):
    """A→B→A. 되돌아온 캡처가 떼어낸 캡처의 flush 전에 복원 스캔을 돌면 seq가
    1부터 다시 매겨져 같은 chunk에 중복 행이 생기고, 그 파일이 완전으로 확정된다."""
    tc.start("20260826", "047040", None, EXP, None)
    tc.enqueue(_tick("047040", price=18_000.0, second="11"))

    tc.start("20260826", "215600", 32, EXP, ENTRY_AT)
    tc.enqueue(_tick("215600", price=3095.0, second="20"))

    tc.start("20260826", "047040", None, EXP, None)
    tc.enqueue(_tick("047040", price=18_100.0, second="30"))

    await tc.finalize("COMPLETE", reached_expected_close=True)
    await tc.drain_switch_finalizers()

    rows = _rows(tmp_path, "047040")
    seqs = [r["seq"] for r in rows]
    assert len(seqs) == len(set(seqs)), f"중복 seq: {seqs}"
    assert seqs == sorted(seqs)
    # 되돌아온 캡처가 이어쓰므로 앞 구간이 덮이지 않는다.
    assert [r["price"] for r in rows] == [18_000.0, 18_100.0]


async def test_target_switch_is_logged_at_warn_with_both_tickers(mem, tmp_path, monkeypatch):
    """조용한 거부가 하루치 데이터를 태웠다. 전환은 반드시 눈에 띄어야 한다."""
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(tc, "log", lambda event, **kw: events.append((event, kw)))

    tc.start("20260826", "047040", None, EXP, None)
    tc.start("20260826", "215600", 32, EXP, ENTRY_AT)

    switched = [kw for event, kw in events if event == "TICK_CAPTURE_TARGET_SWITCHED"]
    assert len(switched) == 1
    assert switched[0]["level"] == "WARN"
    assert switched[0]["previous_ticker"] == "047040"
    assert switched[0]["ticker"] == "215600"

    await tc.finalize("COMPLETE", reached_expected_close=True)


async def test_stuck_switch_finalize_does_not_wedge_shutdown(mem, tmp_path, monkeypatch):
    """종료 경로는 finalize()를 await한 뒤 DB를 닫고 PID를 지운다. 여기서 무한정
    기다리면 PID 락이 남아 다음 기동을 막는다. IN_PROGRESS manifest가 낫다."""
    real = tc.TickCapture.finalize
    reached = asyncio.Event()

    async def hang_for_detached(self, reason, *, reached_expected_close):
        if self.ticker == "047040":
            reached.set()
            await asyncio.sleep(3600)
        return await real(self, reason, reached_expected_close=reached_expected_close)

    monkeypatch.setattr(tc.TickCapture, "finalize", hang_for_detached)
    monkeypatch.setattr(tc, "SWITCH_DRAIN_TIMEOUT_SEC", 0.05)
    events: list[str] = []
    monkeypatch.setattr(tc, "log", lambda event, **kw: events.append(event))

    tc.start("20260826", "047040", None, EXP, None)
    detached = tc._capture
    tc.start("20260826", "215600", 32, EXP, ENTRY_AT)
    await asyncio.wait_for(reached.wait(), timeout=5)

    await asyncio.wait_for(
        tc.finalize("PROCESS_SHUTDOWN", reached_expected_close=False), timeout=5
    )

    assert "TICK_CAPTURE_SWITCH_DRAIN_TIMEOUT" in events
    assert not tc._switch_finalizers  # 매달린 태스크를 남기지 않는다

    # 실제 종료였다면 프로세스가 곧 사라진다. 테스트에서는 포기된 writer 루프를
    # 직접 닫아 다음 테스트에 경고가 새지 않게 한다.
    assert detached is not None and detached._task is not None
    detached._task.cancel()
