import pytest

from src import live, state


def _clear_state() -> None:
    live.clear_tick_history()
    s = state.get()
    s.trading_date = None
    s.target_ticker = None
    s.observe_ticker = None
    s.target_name = None
    s.target_candidates = None
    s.entry_price = None
    s.entry_at = None
    s.entry_qty = None
    s.remaining_qty = None
    s.high_price = None
    s.position_status = "IDLE"
    s.close_reason = None
    s.order_id = None
    s.trailing_active = False
    s.highest_step = 0.0
    s.trade_id = 0
    s.daily_pnl_pct = 0.0
    s.day_skip = False
    s.pending_entry = None
    s.post_close_tracking_stopped = False


@pytest.fixture(autouse=True)
def clean_state():
    _clear_state()
    yield
    _clear_state()


async def test_new_trading_day_resets_daily_skip_state():
    await state.ensure_trading_day("20260627")
    s = state.get()
    s.day_skip = True
    s.target_ticker = "005930"
    s.close_reason = "NO_TARGET"
    s.position_status = "CLOSED"

    changed = await state.ensure_trading_day("20260628")

    assert changed is True
    assert s.trading_date == "20260628"
    assert s.day_skip is False
    assert s.target_ticker is None
    assert s.close_reason is None
    assert s.position_status == "IDLE"


async def test_new_trading_day_clears_tick_history():
    live.push_tick(75_000.0, ticker="005930")

    changed = await state.ensure_trading_day("20260703")

    assert changed is True
    assert live.tick_history() == []


async def test_new_trading_day_clears_observe_ticker():
    s = state.get()
    s.trading_date = "20260702"
    s.observe_ticker = "017900"

    assert await state.ensure_trading_day("20260703") is True

    assert state.get().observe_ticker is None


async def test_same_trading_day_does_not_clear_day_skip():
    await state.ensure_trading_day("20260629")
    s = state.get()
    s.day_skip = True

    changed = await state.ensure_trading_day("20260629")

    assert changed is False
    assert s.day_skip is True


async def test_new_trading_day_does_not_clear_active_position():
    await state.ensure_trading_day("20260630")
    s = state.get()
    s.position_status = "HOLDING"
    s.target_ticker = "005930"
    s.remaining_qty = 10

    changed = await state.ensure_trading_day("20260701")

    assert changed is False
    assert s.trading_date == "20260630"
    assert s.position_status == "HOLDING"
    assert s.target_ticker == "005930"
    assert s.remaining_qty == 10


async def test_verified_stale_entering_can_reset_for_new_trading_day():
    await state.ensure_trading_day("20260727")
    s = state.get()
    s.position_status = "ENTERING"
    s.target_ticker = "006340"
    s.day_skip = True

    changed = await state.reset_stale_entering_for_trading_day("20260728")

    assert changed is True
    assert s.trading_date == "20260728"
    assert s.position_status == "IDLE"
    assert s.target_ticker is None
    assert s.day_skip is False


async def test_stale_entering_with_pending_order_cannot_reset():
    await state.ensure_trading_day("20260727")
    s = state.get()
    s.position_status = "ENTERING"
    s.pending_entry = {"order_id": "0000000937"}

    changed = await state.reset_stale_entering_for_trading_day("20260728")

    assert changed is False
    assert s.trading_date == "20260727"
    assert s.position_status == "ENTERING"


@pytest.mark.parametrize("status", ["ENTERING", "HOLDING", "EXITING"])
async def test_verified_zero_holding_can_reset_any_stale_active_status(status):
    await state.ensure_trading_day("20260727")
    s = state.get()
    s.position_status = status
    s.target_ticker = "006340"
    s.pending_entry = {"order_id": "0000000937"} if status == "ENTERING" else None

    changed = await state.reset_stale_active_for_trading_day("20260728")

    assert changed is True
    assert s.trading_date == "20260728"
    assert s.position_status == "IDLE"
    assert s.pending_entry is None


async def test_set_holding_records_entry_at():
    s = state.get()
    s.position_status = "ENTERING"

    await state.set_holding(75_000.0, 10, "ORD001")

    assert s.position_status == "HOLDING"
    assert s.entry_at is not None
    assert state.datetime.fromisoformat(s.entry_at)

async def test_set_closed_keeps_tick_history_for_review():
    """청산 후에도 당일 가격흐름 차트를 보여주기 위해 tick 이력을 유지한다."""
    s = state.get()
    s.position_status = "HOLDING"
    s.remaining_qty = 10
    live.push_tick(75_000.0, ticker="005930")

    changed = await state.set_closed("TRAILING")

    assert changed is True
    assert s.position_status == "CLOSED"
    assert s.remaining_qty == 0
    assert len(live.tick_history()) == 1

    live.clear_tick_history()


async def test_post_close_tracking_stop_is_closed_only_and_persists(tmp_path):
    s = state.get()
    assert await state.stop_post_close_tracking() is False

    s.trading_date = "20260803"
    s.position_status = "CLOSED"
    assert await state.stop_post_close_tracking() is True
    assert s.post_close_tracking_stopped is True

    await state.persist(str(tmp_path), "20260803")
    _clear_state()
    state.restore_from(state.load(str(tmp_path)))

    assert state.get().post_close_tracking_stopped is True


async def test_new_holding_rearms_post_close_tracking():
    s = state.get()
    s.position_status = "ENTERING"
    s.post_close_tracking_stopped = True

    await state.set_holding(75_000.0, 10, "ORD001")

    assert s.post_close_tracking_stopped is False


async def test_exiting_blocks_daily_reset_until_reconciled():
    s = state.get()
    s.trading_date = "20260701"
    s.position_status = "HOLDING"
    s.remaining_qty = 10

    assert await state.set_exiting("HARD_STOP") is True
    assert s.position_status == "EXITING"
    assert await state.ensure_trading_day("20260702") is False
    assert s.trading_date == "20260701"
    assert s.remaining_qty == 10


async def test_target_candidates_persist_restore_round_trip(tmp_path):
    s = state.get()
    s.trading_date = "20260701"
    s.target_ticker = "005930"
    s.target_name = "삼성전자"
    s.entry_at = "2026-07-01T09:10:30+09:00"
    s.target_candidates = [
        {"ticker": "005930", "expected_amount": 10_000.0},
        {"ticker": "000660", "expected_amount": 9_000.0},
    ]

    await state.persist(str(tmp_path), "20260701")
    _clear_state()
    data = state.load(str(tmp_path))
    state.restore_from(data)

    restored = state.get()
    assert restored.target_ticker == "005930"
    assert restored.target_name == "삼성전자"
    assert restored.entry_at == "2026-07-01T09:10:30+09:00"
    assert restored.target_candidates == [
        {"ticker": "005930", "expected_amount": 10_000.0},
        {"ticker": "000660", "expected_amount": 9_000.0},
    ]


async def test_observe_ticker_persist_restore_round_trip(tmp_path):
    """재시작해도 그날 관측 종목이 이어져야 캡처가 이어쓰기된다."""
    s = state.get()
    s.trading_date = "20260915"
    s.observe_ticker = "017900"

    await state.persist(str(tmp_path), "20260915")
    _clear_state()
    assert state.get().observe_ticker is None
    state.restore_from(state.load(str(tmp_path)))

    assert state.get().observe_ticker == "017900"


async def test_reset_to_idle_keeps_observe_ticker():
    """ENTRY_FAIL → IDLE 전환은 관측을 끝내는 사유가 아니다."""
    s = state.get()
    s.target_ticker = "017900"
    s.observe_ticker = "017900"

    await state.reset_to_idle("ENTRY_FAIL")

    assert s.target_ticker is None
    assert s.observe_ticker == "017900"


async def test_pending_entry_persist_restore_round_trip(tmp_path):
    s = state.get()
    s.trading_date = "20260727"
    s.target_ticker = "006340"
    s.position_status = "ENTERING"
    pending = {
        "order_id": "0000000937",
        "org_no": "001",
        "ticker": "006340",
        "requested_qty": 48,
        "limit_price": 14_510,
        "anchor_price": 14_440,
        "prev_close": 13_730,
    }
    await state.set_pending_entry(pending)

    await state.persist(str(tmp_path), "20260727")
    _clear_state()
    state.restore_from(state.load(str(tmp_path)))

    assert state.get().position_status == "ENTERING"
    assert state.get().pending_entry == pending


async def test_set_holding_clears_pending_entry():
    s = state.get()
    s.position_status = "ENTERING"
    await state.set_pending_entry({"order_id": "0000000937"})

    await state.set_holding(14_500, 19, "0000000937")

    assert s.pending_entry is None


def test_restore_from_legacy_state_without_target_candidates():
    state.restore_from({
        "date": "20260701",
        "ticker": "005930",
        "position_status": "IDLE",
    })

    s = state.get()
    assert s.target_ticker == "005930"
    assert s.target_name is None
    assert s.target_candidates is None


async def test_reset_to_idle_clears_target_name():
    s = state.get()
    s.target_ticker = "005930"
    s.target_name = "삼성전자"
    s.position_status = "HOLDING"

    await state.reset_to_idle("TEST")

    assert s.target_ticker is None
    assert s.target_name is None
