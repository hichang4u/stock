"""진입 체결을 실시간 통보로도 확인한다 — 조회 창·취소·재시도 규칙은 그대로.

설계: docs/superpowers/specs/2026-10-06-fill-notice-design.md §7
"""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from src.api import kis_notice
from src.api.kis_notice import Notice
from src.modules import f3_entry as f3


@pytest.fixture(autouse=True)
def _book(monkeypatch):
    kis_notice.reset()
    monkeypatch.setattr(f3, "F3_FILL_NOTICE_ENABLED", True)
    yield
    kis_notice.reset()


def _notice(qty=295, price=23500.0, order="1226"):
    return Notice(order, "348340", qty, price, "090008", "02")


@pytest.mark.asyncio
async def test_send_buy_registers_the_order_with_the_notice_book(monkeypatch):
    monkeypatch.setattr(f3.kis_rest, "post",
                        AsyncMock(return_value={"rt_cd": "0", "output": {"ODNO": "0000001226"}}))
    await f3._send_buy("348340", 295, "PAPER", limit_price=23700)
    assert kis_notice.event_for("0000001226") is not None


@pytest.mark.asyncio
async def test_send_buy_does_not_register_when_disabled(monkeypatch):
    monkeypatch.setattr(f3, "F3_FILL_NOTICE_ENABLED", False)
    monkeypatch.setattr(f3.kis_rest, "post",
                        AsyncMock(return_value={"rt_cd": "0", "output": {"ODNO": "0000001226"}}))
    await f3._send_buy("348340", 295, "PAPER", limit_price=23700)
    assert kis_notice.event_for("0000001226") is None


@pytest.mark.asyncio
async def test_snapshot_takes_the_notice_when_the_query_lags(monkeypatch):
    kis_notice.expect("0000001226", 295)
    kis_notice.record(_notice())
    monkeypatch.setattr(f3, "_query_order_fill_snapshot", AsyncMock(return_value=None))
    got = await f3._fetch_order_fill_snapshot("0000001226", expected_qty=295)
    assert got["fill_qty"] == 295 and got["status"] == "FILLED"


@pytest.mark.asyncio
async def test_snapshot_keeps_the_query_when_there_is_no_notice(monkeypatch):
    queried = {"status": "PARTIAL", "order_qty": 295, "fill_qty": 10,
               "remaining_qty": 285, "fill_price": 23500}
    monkeypatch.setattr(f3, "_query_order_fill_snapshot", AsyncMock(return_value=queried))
    assert await f3._fetch_order_fill_snapshot("0000001226", expected_qty=295) == queried


@pytest.mark.asyncio
async def test_poll_fill_returns_as_soon_as_the_notice_fills(monkeypatch):
    events = []
    monkeypatch.setattr(f3, "log", lambda e, **k: events.append((e, k)))
    kis_notice.expect("0000001226", 295)
    never = asyncio.Event()

    async def slow_query(order_id, **kwargs):
        await never.wait()

    monkeypatch.setattr(f3, "_query_order_fill_snapshot", slow_query)
    asyncio.get_running_loop().call_later(0.05, kis_notice.record, _notice())
    deadline = f3.datetime.now(f3.KST) + timedelta(seconds=5)

    got = await asyncio.wait_for(
        f3._poll_fill("0000001226", deadline=deadline, ticker="348340", expected_qty=295),
        timeout=2)

    assert got["fill_qty"] == 295
    stages = [k["stage"] for e, k in events if e == "ENTRY_FILL_CONFIRMED_BY_NOTICE"]
    assert stages == ["POLL_WAIT"]


@pytest.mark.asyncio
async def test_cancel_is_not_sent_when_the_notice_already_shows_a_full_fill(monkeypatch):
    events = []
    monkeypatch.setattr(f3, "log", lambda e, **k: events.append((e, k)))
    kis_notice.expect("0000001226", 295)
    kis_notice.record(_notice())
    cancel = AsyncMock(return_value={"rt_cd": "0"})
    monkeypatch.setattr(f3, "_cancel_order", cancel)

    outcome, fill = await f3._cancel_entry_order_confirmed(
        "0000001226", "00950", "PAPER", "348340", 1, 2, expected_qty=295)

    assert outcome == "FILLED" and fill["fill_qty"] == 295
    cancel.assert_not_awaited()
    assert [k["stage"] for e, k in events if e == "ENTRY_FILL_CONFIRMED_BY_NOTICE"] == [
        "BEFORE_CANCEL"]


@pytest.mark.asyncio
async def test_cancel_goes_ahead_when_disabled_even_with_a_full_notice(monkeypatch):
    monkeypatch.setattr(f3, "F3_FILL_NOTICE_ENABLED", False)
    monkeypatch.setattr(f3, "log", lambda e, **k: None)
    kis_notice.expect("0000001226", 295)
    kis_notice.record(_notice())
    cancel = AsyncMock(return_value={"rt_cd": "0"})
    monkeypatch.setattr(f3, "_cancel_order", cancel)
    monkeypatch.setattr(f3, "_query_order_fill_snapshot", AsyncMock(return_value=None))

    await f3._cancel_entry_order_confirmed(
        "0000001226", "00950", "PAPER", "348340", 1, 2, expected_qty=295)

    cancel.assert_awaited_once()
