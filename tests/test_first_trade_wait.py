"""체결 확인 창을 그 종목의 첫 체결부터 센다 (진입 티켓 8.4절 가안, 2026-10-27).

빠른 경로 날 주문은 09:00:05 무렵 나가는데 첫 체결은 개장 7 ~ 27초 뒤에 온다. 2초 창이 첫 체결 전에
닫히면 체결될 수 없는 주문을 미체결로 보고 취소한다 — 10/16·10/23·10/27 진입 무산.
"""

import asyncio
import time
from datetime import datetime, timedelta

import pytest

from src import live, state
from src.modules import f3_entry as f3
from src.modules import f4_tracking as f4

KST = f3.KST
DAY = datetime(2026, 10, 27, tzinfo=KST)


def _at(hms: str, day: datetime = DAY) -> datetime:
    h, m, s = hms.split(":")
    sec = float(s)
    return day.replace(hour=int(h), minute=int(m), second=int(sec),
                       microsecond=round((sec - int(sec)) * 1_000_000))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    live._first_trades.clear()
    live._first_trade_events.clear()
    monkeypatch.setattr(f3, "F3_FIRST_TRADE_WAIT_UNTIL_SEC", 45.0)
    yield
    live._first_trades.clear()
    live._first_trade_events.clear()


@pytest.fixture
def events(monkeypatch):
    out = []
    monkeypatch.setattr(f3, "log", lambda event, **kw: out.append((event, kw)))
    return out


# ── live: 종목별 첫 체결 ──


def test_first_trade_ignores_pre_open_and_keeps_the_first():
    live.note_trade("005935", _at("08:59:58").isoformat(), 170_000.0)  # 장전 시각
    assert live.first_trade("005935", DAY.date()) is None
    live.note_trade("005935", _at("09:00:27.179").isoformat(), 170_600.0)
    live.note_trade("005935", _at("09:00:28").isoformat(), 170_700.0)
    first = live.first_trade("005935", DAY.date())
    assert first["at"] == _at("09:00:27.179")
    assert first["price"] == 170_600.0
    assert live.first_trade("396300", DAY.date()) is None  # 종목별


def test_first_trade_ignores_missing_or_naive_time():
    live.note_trade("005935", None, 170_600.0)
    live.note_trade("005935", "2026-10-27T09:00:27", 170_600.0)  # 오프셋 없음
    live.note_trade("005935", "not-a-time", 170_600.0)
    assert live.first_trade("005935", DAY.date()) is None


def test_first_trade_resets_on_a_new_day():
    live.note_trade("005935", _at("09:00:27").isoformat(), 170_600.0)
    yesterday_event = live.first_trade_event("005935", DAY.date())
    assert yesterday_event.is_set()
    next_day = DAY + timedelta(days=1)
    assert live.first_trade("005935", next_day.date()) is None
    assert not live.first_trade_event("005935", next_day.date()).is_set()
    live.note_trade("005935", _at("09:00:09", next_day).isoformat(), 171_000.0)
    assert live.first_trade("005935", next_day.date())["price"] == 171_000.0


# ── F4: 틱 처리에서 첫 체결을 남긴다 ──


@pytest.mark.asyncio
async def test_f4_price_tick_records_the_first_trade(monkeypatch):
    monkeypatch.setattr(f4, "_observation_should_continue", lambda ticker, now=None: True)
    state.get().position_status = "IDLE"
    accepted = await f4._handle_price_tick(
        170_600.0, "005935", None, source="ws",
        tick_meta={"source_ts": _at("09:00:27.179").isoformat(), "qty": 73517},
    )
    assert accepted is True
    assert live.first_trade("005935", DAY.date())["price"] == 170_600.0


# ── F3: 주문 뒤 첫 체결까지 기다린다 ──


@pytest.mark.asyncio
async def test_no_wait_after_the_cutoff(events):
    """레거시 경로(09:01 이후 주문)·재시도 등 09:00:45가 지났으면 지금과 같다."""
    started = time.perf_counter()
    await f3._wait_first_trade("059120", "0000003313", now=_at("09:01:40"))
    assert time.perf_counter() - started < 0.5
    assert events == []


@pytest.mark.asyncio
async def test_no_wait_before_the_open(events):
    await f3._wait_first_trade("005935", "0000001932", now=_at("08:59:59"))
    assert events == []


@pytest.mark.asyncio
async def test_no_wait_when_the_first_trade_already_came(events):
    """10/26: 주문이 첫 체결 뒤에 닿은 날."""
    live.note_trade("396300", _at("09:00:07.101").isoformat(), 9_470.0)
    await f3._wait_first_trade("396300", "0000002132", now=_at("09:00:07.832"))
    assert events == []


@pytest.mark.asyncio
async def test_waits_until_the_first_trade(events):
    """10/27: 주문 09:00:05, 첫 체결 09:00:27 — 기다렸다가 창을 연다."""
    async def first_trade_later():
        await asyncio.sleep(0.2)
        live.note_trade("005935", _at("09:00:27.179").isoformat(), 170_600.0)

    trigger = asyncio.create_task(first_trade_later())
    started = time.perf_counter()
    await f3._wait_first_trade("005935", "0000001932", now=_at("09:00:05.050"))
    waited = time.perf_counter() - started
    await trigger
    assert 0.15 < waited < 2.0
    names = [e for e, _ in events]
    assert names == ["ENTRY_FIRST_TRADE_WAIT", "ENTRY_FIRST_TRADE_SEEN"]
    wait_kw = events[0][1]
    assert wait_kw["ticker"] == "005935" and wait_kw["order_id"] == "0000001932"
    assert wait_kw["wait_until"] == "09:00:45"
    seen = events[1][1]
    assert seen["via"] == "FIRST_TRADE"
    assert seen["first_trade_at"] == "09:00:27.179"
    assert seen["first_trade_price"] == 170_600.0
    assert seen["waited_ms"] >= 150


@pytest.mark.asyncio
async def test_wait_ends_at_the_cutoff(events):
    """첫 체결 신호가 끝내 없으면(WS 단절 등) 09:00:45에 지금 동작으로 돌아간다."""
    started = time.perf_counter()
    await f3._wait_first_trade("005935", "0000001932", now=_at("09:00:44.900"))
    assert time.perf_counter() - started < 1.0
    names = [e for e, _ in events]
    assert names == ["ENTRY_FIRST_TRADE_WAIT", "ENTRY_FIRST_TRADE_WAIT_TIMEOUT"]
    assert events[1][1]["level"] == "WARN"


@pytest.mark.asyncio
async def test_fill_notice_ends_the_wait(events, monkeypatch):
    """체결 통보가 먼저 오면 체결이 이미 일어난 것이다 — 바로 확인으로 넘어간다."""
    notice = asyncio.Event()
    monkeypatch.setattr(f3, "F3_FILL_NOTICE_ENABLED", True)
    monkeypatch.setattr(f3.kis_notice, "event_for", lambda order_id: notice)

    async def notice_later():
        await asyncio.sleep(0.1)
        notice.set()

    trigger = asyncio.create_task(notice_later())
    await f3._wait_first_trade("005935", "0000001932", now=_at("09:00:05"))
    await trigger
    assert [e for e, _ in events][-1] == "ENTRY_FIRST_TRADE_SEEN"
    assert events[-1][1]["via"] == "FILL_NOTICE"


@pytest.mark.asyncio
async def test_cutoff_zero_disables_the_wait(events, monkeypatch):
    monkeypatch.setattr(f3, "F3_FIRST_TRADE_WAIT_UNTIL_SEC", 0.0)
    await f3._wait_first_trade("005935", "0000001932", now=_at("09:00:05"))
    assert events == []


def test_new_events_have_labels():
    from src.utils.logger import event_label

    for event in ("ENTRY_FIRST_TRADE_WAIT", "ENTRY_FIRST_TRADE_SEEN",
                  "ENTRY_FIRST_TRADE_WAIT_TIMEOUT"):
        assert event_label(event) != f"{event}({event})"
