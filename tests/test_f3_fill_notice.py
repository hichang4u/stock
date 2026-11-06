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


def _paper_frame(order, qty, price):
    """모의서버 모양의 체결 통보 프레임 — 23필드, 앞쪽 위치는 공식 사양과 같다."""
    fields = [""] * len(kis_notice.FIELDS)
    values = {"ODER_NO": order, "SELN_BYOV_CLS": "02", "STCK_SHRN_ISCD": "035720",
              "CNTG_QTY": str(qty), "CNTG_UNPR": str(price), "STCK_CNTG_HOUR": "090007",
              "RFUS_YN": "0", "CNTG_YN": "2", "ACPT_YN": "2"}
    for name, value in values.items():
        fields[kis_notice.FIELDS.index(name)] = value
    return "0|H0STCNI9|001|" + "^".join(fields[:23])


@pytest.mark.asyncio
async def test_split_paper_notices_confirm_the_entry_inside_the_window(monkeypatch):
    """2026-11-05 모양: 통보 4건으로 전량이 찼는데 조회가 늦는다. 프레임부터 확정까지 한 번에 돈다.

    그날은 통보가 2초 창 안에 왔지만 23필드라 장부에 오르지 않아 취소 시도와 재조회로 7.3초 늦었다
    (docs/ENTRY_FILL_TIMEOUT_FOLLOWUP_20260903.md 8.20절).
    """
    events = []
    monkeypatch.setattr(f3, "log", lambda e, **k: events.append((e, k)))
    monkeypatch.setattr(kis_notice, "log", lambda e, **k: None)
    kis_notice.expect("0000001836", 229)
    never = asyncio.Event()

    async def slow_query(order_id, **kwargs):
        await never.wait()

    monkeypatch.setattr(f3, "_query_order_fill_snapshot", slow_query)
    loop = asyncio.get_running_loop()
    for i, (qty, price) in enumerate(((100, 30450), (79, 30450), (40, 30450), (10, 30459))):
        loop.call_later(0.02 * (i + 1), kis_notice.handle_message,
                        _paper_frame("0000001836", qty, price))
    deadline = f3.datetime.now(f3.KST) + timedelta(seconds=5)

    got = await asyncio.wait_for(
        f3._poll_fill("0000001836", deadline=deadline, ticker="035720", expected_qty=229),
        timeout=2)

    assert got["fill_qty"] == 229 and got["fill_price"] == 30450 and got["status"] == "FILLED"
    assert [k["stage"] for e, k in events if e == "ENTRY_FILL_CONFIRMED_BY_NOTICE"] == ["POLL_WAIT"]
    assert "ENTRY_FILL_POLL_TIMEOUT" not in [e for e, _ in events]


@pytest.mark.asyncio
async def test_a_partial_paper_notice_does_not_confirm_the_entry(monkeypatch):
    """일부만 찬 통보로는 확정하지 않는다. 창이 닫히면 지금처럼 취소 경로로 간다."""
    monkeypatch.setattr(f3, "log", lambda e, **k: None)
    monkeypatch.setattr(kis_notice, "log", lambda e, **k: None)
    kis_notice.expect("0000001836", 229)
    kis_notice.handle_message(_paper_frame("0000001836", 100, 30450))
    monkeypatch.setattr(f3, "_query_order_fill_snapshot", AsyncMock(return_value=None))
    deadline = f3.datetime.now(f3.KST) + timedelta(seconds=0.05)

    got = await f3._poll_fill("0000001836", deadline=deadline, ticker="035720", expected_qty=229)

    assert got["fill_qty"] == 100 and got["status"] == "PARTIAL"


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
