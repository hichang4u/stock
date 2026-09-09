import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src import bars
from src.api import kis_minute_bars as mb
from src.modules import tick_capture

KST = ZoneInfo("Asia/Seoul")

# isolated_bars가 ensure_worker를 no-op으로 스텁하기 전에 실물을 잡아 둔다.
# 워커 생애주기(지연 생성의 멱등성, 루프 없을 때의 조용한 반환, reset()의
# 취소)를 테스트하려면 스텁이 아니라 진짜 함수를 호출해야 한다.
_REAL_ENSURE_WORKER = bars.ensure_worker


def _tick(price, minute="0935", qty=10):
    ts = f"2026-08-27T09:{minute[2:]}:00+09:00"
    raw = [""] * 46
    raw[0], raw[2], raw[18] = "006340", str(price), "120.5"
    return {
        "source_ts": ts, "received_at": ts, "price": float(price),
        "qty": qty, "source": "ws", "valid": True, "ticker": "006340", "raw": raw,
    }


@pytest.fixture(autouse=True)
def isolated_bars(tmp_path, monkeypatch):
    monkeypatch.setattr(bars, "_BARS_DIR", tmp_path)
    # ensure_worker는 실행 중인 루프가 있으면 실제 60초 asyncio 태스크를
    # 만든다. 이 파일의 비동기 테스트마다 그 태스크가 살아남고, 동기인
    # reset()이 await 없이 cancel()만 하면 "Task was destroyed but it is
    # pending" 잡음과 flake로 이어진다. 워커 자체를 직접 테스트하는
    # test_worker_logs_and_exits_without_propagating만 예외로 둔다.
    monkeypatch.setattr(bars, "ensure_worker", lambda *a, **k: None)
    tick_capture.clear_tick_listeners()
    bars.reset()
    yield
    bars.reset()
    tick_capture.clear_tick_listeners()


def _official(time_, o, h, low, c, v):
    return {
        "stck_bsop_date": "20260827", "stck_cntg_hour": time_,
        "stck_oprc": str(o), "stck_hgpr": str(h), "stck_lwpr": str(low),
        "stck_prpr": str(c), "cntg_vol": str(v),
    }


def test_no_correction_inside_the_0900_0911_window():
    now = datetime(2026, 8, 27, 9, 5, tzinfo=KST)

    assert bars.should_correct(now, a_holding=False, ws_stale=False) is False


def test_no_correction_while_a_holds_and_the_socket_is_stale():
    now = datetime(2026, 8, 27, 10, 0, tzinfo=KST)

    assert bars.should_correct(now, a_holding=True, ws_stale=True) is False
    assert bars.should_correct(now, a_holding=True, ws_stale=False) is True
    assert bars.should_correct(now, a_holding=False, ws_stale=True) is True


async def test_correction_replaces_ohlcv_and_marks_the_bar_confirmed(monkeypatch):
    bars.on_tick(_tick(14570, qty=10))
    bars.drain()

    async def fake_fetch(ticker, *, hour_cursor=""):
        return {"rt_cd": "0", "output2": [_official("093500", 14500, 15200, 14400, 15100, 900)]}

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_minute_bars", fake_fetch)

    corrected = await bars.correct_once(
        "20260827", "006340", now=datetime(2026, 8, 27, 9, 40, tzinfo=KST)
    )

    row = bars.series("20260827", "006340")[0]
    assert corrected == 1
    assert (row["open"], row["high"], row["low"], row["close"]) == (14500.0, 15200.0, 14400.0, 15100.0)
    assert row["volume"] == 900.0
    assert row["confirmed"] is True


async def test_correction_preserves_tick_derived_and_counters(monkeypatch):
    bars.on_tick(_tick(14570, qty=10))
    bars.on_tick(_tick(14580, qty=10))
    bars.drain()

    async def fake_fetch(ticker, *, hour_cursor=""):
        return {"rt_cd": "0", "output2": [_official("093500", 14500, 15200, 14400, 15100, 900)]}

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_minute_bars", fake_fetch)
    await bars.correct_once("20260827", "006340", now=datetime(2026, 8, 27, 9, 40, tzinfo=KST))

    row = bars.series("20260827", "006340")[0]
    assert row["tick_count"] == 2
    assert row["tick_derived"]["cttr"] == 120.5
    assert row["tick_derived"]["corrected"] is False


async def test_correction_creates_bars_the_tick_stream_missed(monkeypatch):
    bars.on_tick(_tick(14570, minute="0935"))
    bars.drain()

    async def fake_fetch(ticker, *, hour_cursor=""):
        return {"rt_cd": "0", "output2": [
            _official("093500", 14500, 15200, 14400, 15100, 900),
            _official("093600", 15100, 15300, 15000, 15250, 400),
        ]}

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_minute_bars", fake_fetch)
    await bars.correct_once("20260827", "006340", now=datetime(2026, 8, 27, 9, 40, tzinfo=KST))

    rows = bars.series("20260827", "006340")
    assert [r["time"] for r in rows] == ["093500", "093600"]
    assert rows[1]["tick_derived"] is None      # 틱이 없던 봉 — 파생값 없음
    assert rows[1]["confirmed"] is True


async def test_the_minute_still_in_progress_is_never_merged(monkeypatch):
    """진행 중인 분의 공식 봉은 부분값이다 — 병합하면 거짓 확정 + 이중 계상.

    09:40:30에 0940을 부분 OHLCV로 덮고 confirmed=True로 찍으면, 그 분의
    남은 틱이 공식 거래량 위에 다시 더해진다. 장 마지막 분은 다음 폴링이
    없어 영구히 틀린 채로 남는다.
    """
    bars.on_tick(_tick(14570, minute="0939", qty=10))
    bars.on_tick(_tick(14600, minute="0940", qty=7))
    bars.drain()

    async def fake_fetch(ticker, *, hour_cursor=""):
        return {"rt_cd": "0", "output2": [
            _official("093900", 14500, 14700, 14450, 14650, 800),
            _official("094000", 14650, 14660, 14640, 14655, 12),   # 미완성
        ]}

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_minute_bars", fake_fetch)

    corrected = await bars.correct_once(
        "20260827", "006340", now=datetime(2026, 8, 27, 9, 40, 30, tzinfo=KST)
    )

    rows = {r["time"]: r for r in bars.series("20260827", "006340")}
    assert corrected == 1
    assert rows["093900"]["close"] == 14650.0
    assert rows["093900"]["confirmed"] is True
    # 진행 중인 분은 틱 집계값 그대로, 미확정 그대로.
    assert rows["094000"]["close"] == 14600.0
    assert rows["094000"]["volume"] == 7.0
    assert rows["094000"]["confirmed"] is False


async def test_restore_also_skips_the_minute_still_in_progress(monkeypatch):
    async def fake_day(ticker, *, max_pages=20):
        return [
            {"date": "20260827", "time": "093900",
             "open": 14500.0, "high": 14700.0, "low": 14450.0,
             "close": 14650.0, "volume": 800.0},
            {"date": "20260827", "time": "094000",
             "open": 14650.0, "high": 14660.0, "low": 14640.0,
             "close": 14655.0, "volume": 12.0},
        ], {"empty_bar": 0, "field_missing": 0}

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_day_bars", fake_day)

    restored = await bars.restore_day(
        "20260827", "006340", now=datetime(2026, 8, 27, 9, 40, 30, tzinfo=KST)
    )

    assert restored == 1
    assert [r["time"] for r in bars.series("20260827", "006340")] == ["093900"]


async def test_correcting_a_past_day_merges_every_minute(monkeypatch):
    # 오늘이 아닌 날짜에는 "진행 중인 분"이 없다.
    async def fake_fetch(ticker, *, hour_cursor=""):
        return {"rt_cd": "0", "output2": [
            _official("093900", 14500, 14700, 14450, 14650, 800),
            _official("094000", 14650, 14660, 14640, 14655, 12),
        ]}

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_minute_bars", fake_fetch)

    corrected = await bars.correct_once(
        "20260827", "006340", now=datetime(2026, 8, 28, 9, 40, 30, tzinfo=KST)
    )

    assert corrected == 2


async def test_a_failed_fetch_leaves_the_bars_untouched(monkeypatch):
    bars.on_tick(_tick(14570))
    bars.drain()

    async def boom(ticker, *, hour_cursor=""):
        raise mb.MinuteBarError("MINUTE_PRICE_FAILED")

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_minute_bars", boom)

    corrected = await bars.correct_once(
        "20260827", "006340", now=datetime(2026, 8, 27, 9, 40, tzinfo=KST)
    )

    assert corrected == 0
    assert bars.series("20260827", "006340")[0]["confirmed"] is False


async def test_correction_is_skipped_inside_the_forbidden_window(monkeypatch):
    called = {"n": 0}

    async def counting_fetch(ticker, *, hour_cursor=""):
        called["n"] += 1
        return {"rt_cd": "0", "output2": []}

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_minute_bars", counting_fetch)

    await bars.correct_once(
        "20260827", "006340", now=datetime(2026, 8, 27, 9, 5, tzinfo=KST)
    )

    assert called["n"] == 0


async def test_worker_logs_and_exits_without_propagating(monkeypatch):
    monkeypatch.setattr(bars, "_CORRECT_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(bars, "_IDLE_STOP_SEC", 0.05)

    def exploding_drain():
        raise RuntimeError("drain exploded")

    monkeypatch.setattr(bars, "drain", exploding_drain)

    await bars.worker("20260827", "006340")   # 예외가 새어 나오면 실패


def test_ensure_worker_without_a_running_loop_returns_quietly():
    _REAL_ENSURE_WORKER("20260827", "006340")

    assert bars._workers == {}


async def test_ensure_worker_is_idempotent(monkeypatch):
    async def fake_worker(date, ticker):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(bars, "worker", fake_worker)

    _REAL_ENSURE_WORKER("20260827", "006340")
    first = bars._workers[("20260827", "006340")]
    _REAL_ENSURE_WORKER("20260827", "006340")
    second = bars._workers[("20260827", "006340")]

    assert len(bars._workers) == 1
    assert first is second

    first.cancel()
    try:
        await first
    except asyncio.CancelledError:
        pass


async def test_reset_cancels_a_registered_worker_task(monkeypatch):
    async def fake_worker(date, ticker):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(bars, "worker", fake_worker)

    _REAL_ENSURE_WORKER("20260827", "006340")
    task = bars._workers[("20260827", "006340")]

    bars.reset()
    await asyncio.sleep(0)

    assert task.cancelled()
    assert bars._workers == {}
async def test_switching_tickers_cancels_the_abandoned_worker(monkeypatch):
    async def fake_worker(date, ticker):
        await asyncio.sleep(3600)

    monkeypatch.setattr(bars, "worker", fake_worker)

    bars._active = ("20260827", "041190")
    _REAL_ENSURE_WORKER("20260827", "041190")
    task = bars._workers[("20260827", "041190")]

    bars._close_previous(("20260827", "043200"))
    await asyncio.sleep(0)

    assert task.cancelled()
    assert ("20260827", "041190") not in bars._workers


async def test_the_abandoned_worker_stops_calling_the_minute_api(monkeypatch):
    """교체된 종목의 정정 폴링이 실제로 끊기는지 본다.

    태스크 취소만 확인하면 부족하다. 2026-08-28에 문제가 된 것은 워커가
    살아서 분봉 API를 계속 부른 것이었고, 그 호출이 멎는지가 요점이다.
    """
    calls: list[str] = []

    async def fake_correct(date, ticker, *, now=None):
        calls.append(ticker)
        return 0

    monkeypatch.setattr(bars, "correct_once", fake_correct)
    monkeypatch.setattr(bars, "_CORRECT_INTERVAL_SEC", 0.01)

    bars._active = ("20260827", "041190")
    _REAL_ENSURE_WORKER("20260827", "041190")
    await asyncio.sleep(0.06)
    assert calls, "워커가 정정을 한 번도 부르지 않았다면 이 테스트는 무의미하다"

    bars._close_previous(("20260827", "043200"))
    await asyncio.sleep(0)
    settled = len(calls)
    await asyncio.sleep(0.06)

    assert len(calls) == settled


async def test_closing_the_series_leaves_the_incoming_worker_alone(monkeypatch):
    """나가는 종목만 끊는다 — 들어오는 종목의 워커까지 죽이면 안 된다."""

    async def fake_worker(date, ticker):
        await asyncio.sleep(3600)

    monkeypatch.setattr(bars, "worker", fake_worker)

    bars._active = ("20260827", "041190")
    _REAL_ENSURE_WORKER("20260827", "041190")
    _REAL_ENSURE_WORKER("20260827", "043200")
    incoming = bars._workers[("20260827", "043200")]

    bars._close_previous(("20260827", "043200"))
    await asyncio.sleep(0)

    assert not incoming.done()
    assert bars._workers[("20260827", "043200")] is incoming

    # 살려 둔 태스크는 여기서 정리한다. reset()은 동기라 await 없이 cancel만
    # 하고, 루프가 닫힌 뒤에 그게 불리면 "Event loop is closed"가 난다.
    incoming.cancel()
    try:
        await incoming
    except asyncio.CancelledError:
        pass


async def test_a_late_tick_lands_on_a_bar_the_correction_created(monkeypatch):
    """정정이 만든 봉(tick_derived=None)에 뒤늦은 틱이 들어와도 파생값을 남긴다.

    WS가 끊긴 분을 분봉 API가 메꾸면 그 봉의 tick_derived는 None이다. 그 뒤
    재연결된 틱이 같은 분으로 도착하면 _apply가 None에 항목을 대입하려다
    TypeError로 죽는다 — 2026-08-31 실장에서 543건. OHLCV는 예외 직전에 이미
    갱신되므로 조용히 파생값만 사라진다.
    """
    bars.on_tick(_tick(14570, minute="0935"))
    bars.drain()

    async def fake_fetch(ticker, *, hour_cursor=""):
        return {"rt_cd": "0", "output2": [
            _official("093500", 14500, 15200, 14400, 15100, 900),
            _official("093600", 15100, 15300, 15000, 15250, 400),
        ]}

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_minute_bars", fake_fetch)
    await bars.correct_once("20260827", "006340", now=datetime(2026, 8, 27, 9, 40, tzinfo=KST))
    assert bars.series("20260827", "006340")[1]["tick_derived"] is None

    bars.on_tick(_tick(14600, minute="0936"))
    bars.drain()

    bar = bars.series("20260827", "006340")[1]
    assert bar["tick_count"] == 1
    assert bar["tick_derived"] is not None
    assert bar["tick_derived"]["cttr"] == 120.5


# ---------------------------------------------------------------------------
# 가드가 막을 때 흔적을 남긴다.
#
# 08-27 실장 이후 8거래일에서 "A 보유 + WS 끊김"이 동시에 성립한 적이 한 번도
# 없다. A가 매번 09:11 전에 청산했기 때문이다(최장 9분 13초). 그런데 전체
# 39건 중 13건은 09:11 이후까지 보유했으므로 죽은 분기가 아니다 -- 언젠가
# 발동한다. 조용히 return 0 하면 그날이 와도 우리는 모른다.
# ---------------------------------------------------------------------------


def test_block_reason_names_which_guard_fired():
    inside = datetime(2026, 9, 10, 9, 5, tzinfo=bars.KST)
    outside = datetime(2026, 9, 10, 10, 0, tzinfo=bars.KST)
    assert bars.correction_block_reason(
        inside, a_holding=False, ws_stale=False) == "ENTRY_WINDOW"
    assert bars.correction_block_reason(
        outside, a_holding=True, ws_stale=True) == "A_HOLDING_WS_DOWN"
    assert bars.correction_block_reason(
        outside, a_holding=True, ws_stale=False) is None
    assert bars.correction_block_reason(
        outside, a_holding=False, ws_stale=True) is None


def test_block_reason_puts_the_entry_window_first():
    """두 조건이 겹치면 금지창이 이유다 — 그쪽이 먼저 막는다."""
    inside = datetime(2026, 9, 10, 9, 5, tzinfo=bars.KST)
    assert bars.correction_block_reason(
        inside, a_holding=True, ws_stale=True) == "ENTRY_WINDOW"


def _capture_logs(monkeypatch):
    seen = []
    monkeypatch.setattr(bars, "log", lambda ev, **kw: seen.append((ev, kw)))
    return seen


async def test_a_holding_block_is_logged_once_per_episode(monkeypatch):
    seen = _capture_logs(monkeypatch)
    monkeypatch.setattr(bars.state, "get", lambda: type("S", (), {
        "position_status": "HOLDING"})())
    monkeypatch.setattr(bars.live, "ws_connected", False)
    now = datetime(2026, 9, 10, 10, 0, tzinfo=bars.KST)

    assert await bars.correct_once("20260910", "005930", now=now) == 0
    assert await bars.correct_once("20260910", "005930", now=now) == 0

    blocked = [kw for ev, kw in seen if ev == "TRACK_B_CORRECTION_BLOCKED"]
    assert len(blocked) == 1
    assert blocked[0]["reason"] == "A_HOLDING_WS_DOWN"
    assert blocked[0]["ticker"] == "005930"


async def test_the_entry_window_block_is_not_logged(monkeypatch):
    """매일 09:00~09:11에 걸린다. 남기면 소음이고 이미 알고 있는 사실이다."""
    seen = _capture_logs(monkeypatch)
    monkeypatch.setattr(bars.state, "get", lambda: type("S", (), {
        "position_status": "IDLE"})())
    monkeypatch.setattr(bars.live, "ws_connected", True)
    now = datetime(2026, 9, 10, 9, 5, tzinfo=bars.KST)

    assert await bars.correct_once("20260910", "005930", now=now) == 0
    assert [ev for ev, _ in seen if "BLOCKED" in ev] == []


async def test_recovery_is_logged_so_the_episode_can_be_measured(monkeypatch):
    seen = _capture_logs(monkeypatch)
    holding = type("S", (), {"position_status": "HOLDING"})()
    monkeypatch.setattr(bars.state, "get", lambda: holding)
    monkeypatch.setattr(bars.live, "ws_connected", False)
    now = datetime(2026, 9, 10, 10, 0, tzinfo=bars.KST)
    await bars.correct_once("20260910", "005930", now=now)

    async def fake_fetch(ticker, **kwargs):
        return {"output2": []}

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_minute_bars", fake_fetch)
    monkeypatch.setattr(bars.live, "ws_connected", True)
    await bars.correct_once("20260910", "005930", now=now)

    events = [ev for ev, _ in seen]
    assert "TRACK_B_CORRECTION_BLOCKED" in events
    assert "TRACK_B_CORRECTION_RESUMED" in events
