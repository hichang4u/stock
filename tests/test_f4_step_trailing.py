"""F4 Step Trailing 로직 유닛 테스트."""
import asyncio
from datetime import datetime as _dt
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from src import state as _state_mod
from src.modules.f4_tracking import (
    FORCE_TRAILING_HOUR,
    FORCE_TRAILING_MINUTE,
    HARD_STOP_RATIO,
    STEP_SIZE,
    STEP_TRAIL,
    _execute_close,
    _get_observe_until,
    _handle_price_tick,
    _price_observation_active,
    _process_tick,
    _run_dry_ticks,
    _run_rest_price_backup,
)

KST = ZoneInfo("Asia/Seoul")
ENTRY = 10_000.0


# ── 헬퍼 ──────────────────────────────────────────────────────────────

def _kst(h: int, m: int) -> _dt:
    return _dt(2026, 6, 23, h, m, 0, tzinfo=KST)


def _spike_always_pass() -> MagicMock:
    sf = MagicMock()
    sf.is_valid.return_value = True
    return sf


async def _run_tick(
    price: float,
    *,
    hour: int = 9,
    minute: int = 30,
    set_closed_return: bool = False,
) -> AsyncMock:
    """_process_tick 실행, _execute_close mock 반환."""
    mock_close = AsyncMock()
    with (
        patch("src.modules.f4_tracking.datetime") as mock_dt,
        patch("src.modules.f4_tracking._execute_close", mock_close),
        patch("src.state.set_closed", new_callable=AsyncMock, return_value=set_closed_return),
    ):
        mock_dt.now.return_value = _kst(hour, minute)
        await _process_tick(price, _spike_always_pass())
        # 청산은 분리 태스크다(틱 경로를 막지 않는다). 테스트는 완료된 상태를 보므로
        # 패치가 살아 있는 동안 끝까지 기다린다.
        import src.modules.f4_tracking as f4

        if f4._closing_task is not None:
            await f4._closing_task
        await asyncio.sleep(0)
    return mock_close


# ── 픽스처 ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def holding_state(monkeypatch):
    import src.modules.f4_tracking as f4

    s = _state_mod.get()
    s.position_status = "HOLDING"
    s.entry_price = ENTRY
    s.target_ticker = "005930"
    s.remaining_qty = 100
    s.high_price = ENTRY
    s.trailing_active = False
    s.highest_step = 0.0
    s.trade_id = 0
    s.pending_exit = None
    s.entry_at = None
    s.post_close_tracking_stopped = False
    # 청산은 분리 태스크라 앞 테스트의 done-callback 이 뒤늦게 전역을 만질 수 있다.
    # 매 테스트를 깨끗한 "청산 없음" 상태에서 시작한다.
    monkeypatch.setattr(f4, "_close_in_progress", False)
    monkeypatch.setattr(f4, "_close_in_progress_warned", False)
    monkeypatch.setattr(f4, "_closing_task", None)
    monkeypatch.setattr(f4.db, "update_order_submission", AsyncMock())
    monkeypatch.setattr(
        f4.db,
        "record_trailing_shadow_baseline",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        f4.db,
        "finalize_trailing_shadow_comparison",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(f4, "_shadow_baseline_recorded_trade_id", None)


# ── 스텝 갱신 정확성 ──────────────────────────────────────────────────

async def test_step_update_first_step():
    """2.6% 이익(1스텝 구간) → highest_step = 0.025, trailing_active 활성화.

    정확히 경계(2.5%)는 부동소수점 오차로 floor가 0이 되므로
    구간 안쪽(2.6%) 가격을 사용.
    """
    price = ENTRY * 1.026  # 10260, floor(0.026/0.025)=1 → step=0.025
    await _run_tick(price)
    s = _state_mod.get()
    assert s.highest_step == pytest.approx(STEP_SIZE)
    assert s.trailing_active is True


async def test_step_update_second_step():
    """5.1% 이익(2스텝 구간) → highest_step = 0.050."""
    price = ENTRY * 1.051  # 10510, floor(0.051/0.025)=2 → step=0.050
    await _run_tick(price)
    assert _state_mod.get().highest_step == pytest.approx(STEP_SIZE * 2)


async def test_step_update_third_step():
    """7.6% 이익(3스텝 구간) → highest_step = 0.075."""
    price = ENTRY * 1.076  # 10760, floor(0.076/0.025)=3 → step=0.075
    await _run_tick(price)
    assert _state_mod.get().highest_step == pytest.approx(STEP_SIZE * 3)


async def test_below_first_step_no_trailing():
    """2.4% 이익(스텝 미달) → trailing_active 미활성."""
    price = ENTRY * 1.024  # 10240, 스텝 미달
    await _run_tick(price)
    s = _state_mod.get()
    assert s.highest_step == 0.0
    assert s.trailing_active is False



async def test_process_tick_persists_state_immediately_when_step_advances(monkeypatch):
    import src.modules.f4_tracking as f4

    events = []
    persist = AsyncMock()
    s = _state_mod.get()
    s.trade_id = 123

    monkeypatch.setattr(f4.state, "persist", persist)
    monkeypatch.setattr(f4, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f4, "_last_state_persist_at", 0.0)

    await _run_tick(ENTRY * 1.026)

    persist.assert_awaited_once()
    assert persist.await_args.args[1] == "20260623"
    persisted = [kwargs for event, kwargs in events if event == "F4_STATE_PERSISTED"][-1]
    assert persisted["highest_step"] == pytest.approx(STEP_SIZE)
    assert persisted["trailing_active"] is True
    assert persisted["force"] is True



async def test_process_tick_updates_trade_progress_db(monkeypatch):
    import src.modules.f4_tracking as f4

    persist = AsyncMock()
    update_progress = AsyncMock()
    s = _state_mod.get()
    s.trade_id = 123

    monkeypatch.setattr(f4.state, "persist", persist)
    monkeypatch.setattr(f4.db, "update_trade_progress", update_progress)
    monkeypatch.setattr(f4, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f4, "_last_state_persist_at", 0.0)

    await _run_tick(ENTRY * 1.026)

    persist.assert_awaited_once()
    update_progress.assert_awaited_once_with(123, pytest.approx(ENTRY * 1.026), STEP_SIZE)


async def test_tracking_state_db_progress_survives_state_persist_error(monkeypatch):
    import src.modules.f4_tracking as f4

    events = []
    update_progress = AsyncMock()
    s = _state_mod.get()
    s.trade_id = 123

    monkeypatch.setattr(f4.state, "persist", AsyncMock(side_effect=OSError("disk full")))
    monkeypatch.setattr(f4.db, "update_trade_progress", update_progress)
    monkeypatch.setattr(f4, "log", lambda event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setattr(f4, "_last_state_persist_at", 0.0)

    saved = await f4._persist_tracking_state(force=True)

    assert saved is True
    update_progress.assert_awaited_once_with(123, ENTRY, 0.0)
    assert "F4_STATE_PERSIST_ERROR" in [event for event, _ in events]
    persisted = [kwargs for event, kwargs in events if event == "F4_STATE_PERSISTED"][-1]
    assert persisted["state_saved"] is False
    assert persisted["db_saved"] is True

async def test_late_trailing_close_does_not_persist_before_sell(monkeypatch):
    import src.modules.f4_tracking as f4

    persist = AsyncMock()
    s = _state_mod.get()
    s.trade_id = 123

    monkeypatch.setattr(f4.state, "persist", persist)
    monkeypatch.setattr(f4.db, "update_trade_progress", AsyncMock())
    monkeypatch.setattr(f4, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f4, "_last_state_persist_at", 0.0)

    mock_close = await _run_tick(ENTRY * 0.98, hour=10, minute=50, set_closed_return=True)

    mock_close.assert_awaited_once()
    persist.assert_not_awaited()

async def test_process_tick_throttles_high_price_only_state_persist(monkeypatch):
    import src.modules.f4_tracking as f4

    persist = AsyncMock()
    s = _state_mod.get()
    s.trade_id = 123

    monkeypatch.setattr(f4.state, "persist", persist)
    monkeypatch.setattr(f4, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f4, "F4_STATE_PERSIST_INTERVAL_SEC", 1.0)
    monkeypatch.setattr(f4, "_last_state_persist_at", 100.0)
    monkeypatch.setattr(f4.time, "monotonic", lambda: 100.5)

    await _run_tick(ENTRY * 1.001)

    persist.assert_not_awaited()


async def test_process_tick_persists_high_price_after_throttle_interval(monkeypatch):
    import src.modules.f4_tracking as f4

    persist = AsyncMock()
    s = _state_mod.get()
    s.trade_id = 123

    monkeypatch.setattr(f4.state, "persist", persist)
    monkeypatch.setattr(f4, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(f4, "F4_STATE_PERSIST_INTERVAL_SEC", 1.0)
    monkeypatch.setattr(f4, "_last_state_persist_at", 100.0)
    monkeypatch.setattr(f4.time, "monotonic", lambda: 101.1)

    await _run_tick(ENTRY * 1.001)

    persist.assert_awaited_once()

# ── Hard Stop ─────────────────────────────────────────────────────────

async def test_hard_stop_at_exact_boundary():
    """trailing 미활성 + 정확히 -2.0% → Hard Stop 발동."""
    price = ENTRY * (1 - HARD_STOP_RATIO)  # 9800.0
    mock_close = await _run_tick(price, set_closed_return=True)
    mock_close.assert_awaited_once_with(price, "HARD_STOP")


async def test_hard_stop_not_triggered_above_boundary():
    """trailing 미활성 + -1.99% → Hard Stop 미발동."""
    price = ENTRY * (1 - HARD_STOP_RATIO) + 1  # 9801
    mock_close = await _run_tick(price, set_closed_return=True)
    mock_close.assert_not_awaited()


async def test_hard_stop_skipped_when_trailing_active():
    """trailing 활성 구간에서 -2.0% → Hard Stop 체크 자체 건너뜀."""
    s = _state_mod.get()
    s.trailing_active = True
    s.highest_step = STEP_SIZE  # 1스텝 달성 후 하락 시나리오
    price = ENTRY * (1 - HARD_STOP_RATIO)  # 9800 — Hard Stop 조건이지만 trailing 우선
    # stop = ENTRY * (1 + 0.025 - 0.020) = 10050 → 9800 <= 10050 → TRAILING 발동
    mock_close = await _run_tick(price, set_closed_return=True)
    # TRAILING으로 닫혀야 함, HARD_STOP이 아님
    mock_close.assert_awaited_once_with(price, "TRAILING")


# ── Step Trailing ─────────────────────────────────────────────────────


async def test_close_trigger_does_not_mark_closed_before_sell_finishes(monkeypatch):
    import src.modules.f4_tracking as f4

    s = _state_mod.get()
    s.trailing_active = True
    s.highest_step = STEP_SIZE
    observed_statuses = []

    async def fake_execute_close(_price, _reason):
        observed_statuses.append(_state_mod.get().position_status)
        return False

    monkeypatch.setattr(f4, "_execute_close", fake_execute_close)

    stop = ENTRY * (1 + STEP_SIZE - STEP_TRAIL)
    await _process_tick(stop, _spike_always_pass())
    assert f4._closing_task is not None  # 분리 태스크로 띄워졌다
    await f4._closing_task

    assert observed_statuses == ["HOLDING"]
    assert _state_mod.get().position_status == "HOLDING"

async def test_step_trailing_triggers_at_stop():
    """trailing 활성 + stop 가격 이하 → TRAILING 발동."""
    s = _state_mod.get()
    s.trailing_active = True
    s.highest_step = STEP_SIZE  # 0.025
    # stop = ENTRY * (1 + 0.025 - 0.020) = 10050
    stop = ENTRY * (1 + STEP_SIZE - STEP_TRAIL)
    price = stop  # 정확히 stop (<=)
    mock_close = await _run_tick(price, set_closed_return=True)
    mock_close.assert_awaited_once_with(price, "TRAILING")


async def test_step_trailing_not_triggered_above_stop():
    """trailing 활성 + stop 가격 +1원 → 미발동."""
    s = _state_mod.get()
    s.trailing_active = True
    s.highest_step = STEP_SIZE
    stop = ENTRY * (1 + STEP_SIZE - STEP_TRAIL)
    price = stop + 1  # 10051
    mock_close = await _run_tick(price, set_closed_return=True)
    mock_close.assert_not_awaited()


async def test_shadow_records_legacy_exit_before_wider_current_stop():
    import src.modules.f4_tracking as f4

    s = _state_mod.get()
    s.trade_id = 123
    s.trailing_active = True
    s.highest_step = STEP_SIZE
    baseline_stop = ENTRY * (1 + STEP_SIZE - f4.TRAILING_SHADOW_BASELINE_TRAIL)
    recommended_stop = ENTRY * (1 + STEP_SIZE - STEP_TRAIL)
    price = (baseline_stop + recommended_stop) / 2

    mock_close = await _run_tick(price)

    mock_close.assert_not_awaited()
    f4.db.record_trailing_shadow_baseline.assert_awaited_once_with(
        123,
        baseline_step_trail=f4.TRAILING_SHADOW_BASELINE_TRAIL,
        recommended_step_trail=STEP_TRAIL,
        entry_price=ENTRY,
        highest_step=STEP_SIZE,
        baseline_stop_price=baseline_stop,
        recommended_stop_price=recommended_stop,
        baseline_exit_price=price,
    )


async def test_shadow_does_not_delay_current_stop_with_baseline_write():
    import src.modules.f4_tracking as f4

    s = _state_mod.get()
    s.trade_id = 123
    s.trailing_active = True
    s.highest_step = STEP_SIZE
    recommended_stop = ENTRY * (1 + STEP_SIZE - STEP_TRAIL)

    mock_close = await _run_tick(recommended_stop)

    mock_close.assert_awaited_once_with(recommended_stop, "TRAILING")
    f4.db.record_trailing_shadow_baseline.assert_not_awaited()


async def test_shadow_db_failure_is_attempted_only_once_per_trade(monkeypatch):
    """보고 DB 장애가 손절 임박 구간의 매 틱 쓰기 재시도로 번지지 않는다."""
    import src.modules.f4_tracking as f4

    s = _state_mod.get()
    s.trade_id = 123
    s.trailing_active = True
    s.highest_step = STEP_SIZE
    write = AsyncMock(side_effect=RuntimeError("database is locked"))
    monkeypatch.setattr(f4.db, "record_trailing_shadow_baseline", write)
    price = (
        ENTRY * (1 + STEP_SIZE - f4.TRAILING_SHADOW_BASELINE_TRAIL)
        + ENTRY * (1 + STEP_SIZE - STEP_TRAIL)
    ) / 2

    await _run_tick(price)
    await _run_tick(price)

    assert write.await_count == 1


# ── 청산 10분 전 강제 발동 ────────────────────────────────────────────
# 시각 리터럴을 쓰지 않는다 — F5 청산 시각이 바뀌면 이 테스트도 따라가야 한다.

_LATE_H = FORCE_TRAILING_HOUR
_LATE_M = FORCE_TRAILING_MINUTE
_BEFORE_LATE_H, _BEFORE_LATE_M = (
    (_LATE_H, _LATE_M - 1) if _LATE_M > 0 else (_LATE_H - 1, 59)
)


async def test_late_force_trailing_active():
    """강제 시각 이후 → highest_step 0이어도 trailing_active 강제 True."""
    price = ENTRY * 1.01  # 1% 이익, 스텝 미달
    await _run_tick(price, hour=_LATE_H, minute=_LATE_M)
    assert _state_mod.get().trailing_active is True


async def test_late_triggers_if_below_zero_step_stop():
    """강제 활성 후 stop(entry×0.980) 이하 → 청산 발동."""
    price = ENTRY * (1 - STEP_TRAIL) - 1
    mock_close = await _run_tick(
        price, hour=_LATE_H, minute=_LATE_M, set_closed_return=True
    )
    mock_close.assert_awaited_once_with(price, "TRAILING")


async def test_before_late_no_force():
    """강제 시각 1분 전 → 강제 발동 없음, trailing_active 여전히 False."""
    price = ENTRY * 1.01
    await _run_tick(price, hour=_BEFORE_LATE_H, minute=_BEFORE_LATE_M)
    assert _state_mod.get().trailing_active is False


# ── highest_step 단조 증가 ────────────────────────────────────────────

async def test_highest_step_does_not_decrease():
    """2스텝(0.05) 달성 후 가격 후퇴 → highest_step 감소하지 않음."""
    s = _state_mod.get()
    s.trailing_active = True
    s.highest_step = STEP_SIZE * 2  # 0.05
    # 4% 가격(current_step = 0.025) — stop보다 위라서 청산 없음
    # stop = ENTRY*(1+0.05-0.020) = 10300, price=10400 > 10300 → no close
    price = ENTRY * 1.04  # 10400
    await _run_tick(price)
    assert _state_mod.get().highest_step == pytest.approx(STEP_SIZE * 2)


async def test_highest_step_advances_to_new_high():
    """현재 highest_step 0.025 → 5% 신고가 도달 → 0.050으로 갱신."""
    s = _state_mod.get()
    s.trailing_active = True
    s.highest_step = STEP_SIZE  # 0.025
    price = ENTRY * (1 + STEP_SIZE * 2)  # 10500
    await _run_tick(price)
    assert _state_mod.get().highest_step == pytest.approx(STEP_SIZE * 2)


async def test_dry_run_execute_close_does_not_touch_order_db(monkeypatch):
    s = _state_mod.get()
    s.trade_id = 123
    s.highest_step = STEP_SIZE
    monkeypatch.setenv("DRY_RUN", "1")

    record_order = AsyncMock()
    update_order_fill = AsyncMock()
    close_trade = AsyncMock()
    persist = AsyncMock()

    monkeypatch.setattr("src.modules.f4_tracking.db.record_order", record_order)
    monkeypatch.setattr("src.modules.f4_tracking.db.update_order_fill", update_order_fill)
    monkeypatch.setattr("src.modules.f4_tracking.db.close_trade", close_trade)
    monkeypatch.setattr("src.modules.f4_tracking.state.persist", persist)
    monkeypatch.setattr("src.modules.f4_tracking.notifier.send", AsyncMock())

    await _execute_close(ENTRY * 1.01, "TRAILING")

    record_order.assert_not_awaited()
    update_order_fill.assert_not_awaited()
    close_trade.assert_not_awaited()
    persist.assert_awaited_once()


async def test_dry_run_ticks_finish_below_trailing_stop(monkeypatch):
    events = []
    s = _state_mod.get()
    s.entry_price = ENTRY
    s.position_status = "HOLDING"

    monkeypatch.setenv("DRY_RUN_STEP_DELAY", "0")
    monkeypatch.setattr(
        "src.modules.f4_tracking.log",
        lambda event, **kwargs: events.append((event, kwargs)),
    )
    monkeypatch.setattr("src.modules.f4_tracking._process_tick", AsyncMock())

    await _run_dry_ticks("005930", _spike_always_pass())

    start_event = [kwargs for event, kwargs in events if event == "DRY_RUN_F4_START"][0]
    prices = start_event["prices"]
    assert prices[-1] < ENTRY * (1 + STEP_SIZE - STEP_TRAIL)


@pytest.mark.asyncio
async def test_rest_backup_skips_poll_when_websocket_is_fresh(monkeypatch):
    fetch = AsyncMock(return_value=ENTRY)

    async def stop_after_sleep(_seconds):
        _state_mod.get().position_status = "CLOSED"

    monkeypatch.setattr("src.modules.f4_tracking._fetch_current_price", fetch)
    monkeypatch.setattr(
        "src.modules.f4_tracking.asyncio.sleep",
        AsyncMock(side_effect=stop_after_sleep),
    )
    monkeypatch.setattr("src.modules.f4_tracking.log", lambda *args, **kwargs: None)

    await _run_rest_price_backup("005930", _spike_always_pass(), lambda: False)

    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_rest_backup_polls_when_websocket_is_stale(monkeypatch):
    fetch = AsyncMock(return_value=ENTRY)
    process_tick = AsyncMock()

    async def stop_after_sleep(_seconds):
        _state_mod.get().position_status = "CLOSED"

    monkeypatch.setattr("src.modules.f4_tracking._fetch_current_price", fetch)
    monkeypatch.setattr("src.modules.f4_tracking._process_tick", process_tick)
    monkeypatch.setattr(
        "src.modules.f4_tracking.asyncio.sleep",
        AsyncMock(side_effect=stop_after_sleep),
    )
    monkeypatch.setattr("src.modules.f4_tracking.log", lambda *args, **kwargs: None)

    await _run_rest_price_backup("005930", _spike_always_pass(), lambda: True)

    fetch.assert_awaited_once_with(
        "005930",
        latency_context="F4_HOLDING",
        aggregate_latency=True,
    )
    process_tick.assert_awaited_once()


@pytest.mark.asyncio
async def test_rest_backup_survives_fetch_error(monkeypatch):
    events = []
    fetch = AsyncMock(side_effect=[RuntimeError("boom"), ENTRY])
    process_tick = AsyncMock()
    sleep_count = {"n": 0}

    async def stop_after_two_sleeps(_seconds):
        sleep_count["n"] += 1
        if sleep_count["n"] >= 2:
            _state_mod.get().position_status = "CLOSED"

    monkeypatch.setattr("src.modules.f4_tracking._fetch_current_price", fetch)
    monkeypatch.setattr("src.modules.f4_tracking._process_tick", process_tick)
    monkeypatch.setattr(
        "src.modules.f4_tracking.asyncio.sleep",
        AsyncMock(side_effect=stop_after_two_sleeps),
    )
    monkeypatch.setattr(
        "src.modules.f4_tracking.log",
        lambda event, **kwargs: events.append((event, kwargs)),
    )

    await _run_rest_price_backup("005930", _spike_always_pass(), lambda: True)

    assert fetch.await_count == 2
    process_tick.assert_awaited_once()
    assert "F4_REST_BACKUP_ERROR" in [event for event, _ in events]


@pytest.mark.asyncio
async def test_monitor_heartbeat_logs_liveness_only(monkeypatch):
    import src.modules.f4_tracking as f4

    events = []
    stale_values = iter([True, False])
    sleep_count = {"value": 0}

    async def advance(_seconds):
        sleep_count["value"] += 1
        if sleep_count["value"] == 2:
            f4.live.ws_connected = True
        elif sleep_count["value"] >= 3:
            _state_mod.get().position_status = "CLOSED"

    monkeypatch.setattr(f4, "F4_HEARTBEAT_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(f4.asyncio, "sleep", AsyncMock(side_effect=advance))
    monkeypatch.setattr(
        f4,
        "log",
        lambda event, **fields: events.append((event, fields)),
    )
    f4.live.ws_connected = False
    f4.live.last_tick_price = ENTRY

    await f4._run_monitor_heartbeat(
        "005930",
        lambda: next(stale_values),
        lambda: 250,
    )

    event_names = [event for event, _ in events]
    assert event_names.count("F4_HEARTBEAT") == 2
    assert "WS_STALE" not in event_names
    assert "WS_RECOVERED" not in event_names


@pytest.mark.asyncio
async def test_ws_health_monitor_detects_stale_and_recovery_independent_of_rest(monkeypatch):
    import src.modules.f4_tracking as f4

    events = []
    stale_values = iter([True, False])
    sleep_count = {"value": 0}

    async def advance(_seconds):
        sleep_count["value"] += 1
        if sleep_count["value"] >= 2:
            _state_mod.get().position_status = "CLOSED"

    monkeypatch.setattr(f4.asyncio, "sleep", AsyncMock(side_effect=advance))
    monkeypatch.setattr(f4, "F4_WS_HEALTH_LOG_COOLDOWN_SEC", 0.0)
    monkeypatch.setattr(
        f4,
        "log",
        lambda event, **fields: events.append((event, fields)),
    )

    await f4._run_ws_health_monitor(
        "005930",
        lambda: next(stale_values),
        lambda: {"last_ws_tick_age_ms": 2100, "rest_backup_enabled": False},
    )

    event_names = [event for event, _ in events]
    assert "WS_STALE" in event_names
    assert "WS_RECOVERED" in event_names
    assert all(fields["level"] == "INFO" for _, fields in events)


@pytest.mark.asyncio
async def test_ws_health_monitor_cools_down_repeated_tick_idle_logs(monkeypatch):
    import src.modules.f4_tracking as f4

    events = []
    stale_values = iter([True, False, True, False])
    sleep_count = 0

    async def advance(_seconds):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 4:
            _state_mod.get().position_status = "CLOSED"

    monkeypatch.setattr(f4, "F4_WS_HEALTH_LOG_COOLDOWN_SEC", 60.0)
    monkeypatch.setattr(f4.asyncio, "sleep", AsyncMock(side_effect=advance))
    monkeypatch.setattr(
        f4,
        "log",
        lambda event, **fields: events.append((event, fields)),
    )

    await f4._run_ws_health_monitor(
        "005930",
        lambda: next(stale_values),
        lambda: {"ws_connected": True, "last_ws_tick_age_ms": 2_100},
    )

    assert [event for event, _ in events] == ["WS_STALE", "WS_RECOVERED"]


@pytest.mark.asyncio
async def test_ws_health_suppression_counts_reset_after_logged_recovery(monkeypatch):
    import src.modules.f4_tracking as f4

    events = []
    stale_values = iter([True, False, True, False, True, False, True, False])
    event_times = iter([0.0, 1.0, 2.0, 3.0, 70.0, 71.0, 140.0, 141.0])
    sleep_count = 0

    async def advance(_seconds):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 8:
            _state_mod.get().position_status = "CLOSED"

    monkeypatch.setattr(f4, "F4_WS_HEALTH_LOG_COOLDOWN_SEC", 60.0)
    fake_time = MagicMock()
    fake_time.monotonic.side_effect = event_times
    monkeypatch.setattr(f4, "time", fake_time)
    monkeypatch.setattr(f4.asyncio, "sleep", AsyncMock(side_effect=advance))
    monkeypatch.setattr(
        f4,
        "log",
        lambda event, **fields: events.append((event, fields)),
    )

    await f4._run_ws_health_monitor(
        "005930",
        lambda: next(stale_values),
        lambda: {"ws_connected": True, "last_ws_tick_age_ms": 2_100},
    )

    assert [event for event, _ in events] == [
        "WS_STALE",
        "WS_RECOVERED",
        "WS_STALE",
        "WS_RECOVERED",
        "WS_STALE",
        "WS_RECOVERED",
    ]
    assert events[2][1]["suppressed_stale"] == 1
    assert events[2][1]["suppressed_recovered"] == 1
    assert events[3][1]["suppressed_stale"] == 1
    assert events[3][1]["suppressed_recovered"] == 1
    assert events[4][1]["suppressed_stale"] == 0
    assert events[4][1]["suppressed_recovered"] == 0


@pytest.mark.asyncio
async def test_ws_tick_idle_is_warn_when_rest_backup_is_disabled(monkeypatch):
    import src.modules.f4_tracking as f4

    events = []

    async def stop_after_sleep(_seconds):
        _state_mod.get().position_status = "CLOSED"

    monkeypatch.setattr(f4, "F4_REST_BACKUP_ENABLED", False)
    monkeypatch.setattr(f4.asyncio, "sleep", AsyncMock(side_effect=stop_after_sleep))
    monkeypatch.setattr(
        f4,
        "log",
        lambda event, **fields: events.append((event, fields)),
    )

    await f4._run_ws_health_monitor(
        "005930",
        lambda: True,
        lambda: {"ws_connected": True, "last_ws_tick_age_ms": 2_100},
    )

    assert events[0][0] == "WS_STALE"
    assert events[0][1]["level"] == "WARN"


@pytest.mark.asyncio
async def test_ws_stale_recovery_pair_survives_disconnect(monkeypatch):
    import src.modules.f4_tracking as f4

    events = []
    stale_values = iter([True, True, False])
    connected_values = iter([True, False, True])
    sleep_count = 0

    async def advance(_seconds):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 3:
            _state_mod.get().position_status = "CLOSED"

    monkeypatch.setattr(f4, "F4_REST_BACKUP_ENABLED", True)
    monkeypatch.setattr(f4.asyncio, "sleep", AsyncMock(side_effect=advance))
    monkeypatch.setattr(
        f4,
        "log",
        lambda event, **fields: events.append((event, fields)),
    )

    await f4._run_ws_health_monitor(
        "005930",
        lambda: next(stale_values),
        lambda: {
            "ws_connected": next(connected_values),
            "last_ws_tick_age_ms": 2_100,
        },
    )

    assert [event for event, _ in events] == ["WS_STALE", "WS_RECOVERED"]


@pytest.mark.asyncio
async def test_ws_health_monitor_does_not_duplicate_disconnect_warning(monkeypatch):
    import src.modules.f4_tracking as f4

    events = []

    async def stop_after_sleep(_seconds):
        _state_mod.get().position_status = "CLOSED"

    monkeypatch.setattr(f4.asyncio, "sleep", AsyncMock(side_effect=stop_after_sleep))
    monkeypatch.setattr(
        f4,
        "log",
        lambda event, **fields: events.append((event, fields)),
    )

    await f4._run_ws_health_monitor(
        "005930",
        lambda: True,
        lambda: {"ws_connected": False, "last_ws_tick_age_ms": 2_100},
    )

    assert events == []


@pytest.mark.asyncio
async def test_rest_wakeup_skips_poll_interval_sleep(monkeypatch):
    import src.modules.f4_tracking as f4

    wake_event = asyncio.Event()
    wake_event.set()
    sleep = AsyncMock()
    monkeypatch.setattr(f4.asyncio, "sleep", sleep)

    await f4._wait_for_rest_wakeup(1.0, wake_event)

    sleep.assert_not_awaited()
    assert not wake_event.is_set()


@pytest.mark.asyncio
async def test_run_starts_rest_backup_immediately_on_ws_disconnect(monkeypatch):
    import src.modules.f4_tracking as f4

    observed = asyncio.Event()
    s = _state_mod.get()
    s.target_ticker = "005930"
    s.position_status = "HOLDING"

    async def fake_subscribe(
        _ticker, _on_tick, *, stop_if=None, on_connection_change=None,
    ):
        assert on_connection_change is not None
        on_connection_change(False)
        await observed.wait()

    async def fake_rest_backup(
        _ticker, _spike_filter, should_poll_rest, *, vi_watch=None, wake_event=None,
    ):
        assert wake_event is not None
        await wake_event.wait()
        assert should_poll_rest() is True
        observed.set()
        s.position_status = "CLOSED"

    async def wait_forever(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(f4, "F4_WS_STALE_SEC", 999.0)
    monkeypatch.setattr(f4, "F4_REST_BACKUP_ENABLED", True)
    monkeypatch.setattr(f4, "F4_HEARTBEAT_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(f4.kis_ws, "subscribe", fake_subscribe)
    monkeypatch.setattr(f4, "_run_rest_price_backup", fake_rest_backup)
    monkeypatch.setattr(f4, "_run_ws_health_monitor", wait_forever)

    await asyncio.wait_for(f4.run(), 1)

    assert observed.is_set()


@pytest.mark.asyncio
async def test_ws_health_monitor_suppresses_tick_idle_warnings_after_close(monkeypatch):
    import src.modules.f4_tracking as f4

    events = []
    s = _state_mod.get()
    s.position_status = "CLOSED"
    s.entry_at = _kst(9, 1).isoformat()

    async def stop_after_sleep(_seconds):
        s.position_status = "IDLE"
        # 관측은 더 이상 A의 포지션 상태에 묶여 있지 않다. 종목 잠금이 풀려야
        # 관측 루프가 끝난다(일일 리셋 경로와 동일).
        s.target_ticker = None

    monkeypatch.setattr(f4, "_OBSERVE_UNTIL", (9, 10))
    monkeypatch.setattr(f4.asyncio, "sleep", AsyncMock(side_effect=stop_after_sleep))
    monkeypatch.setattr(
        f4,
        "log",
        lambda event, **fields: events.append((event, fields)),
    )

    with patch.object(f4, "datetime") as mock_dt:
        mock_dt.now.return_value = _kst(9, 9)
        await f4._run_ws_health_monitor(
            "005930",
            lambda: True,
            lambda: {"last_ws_tick_age_ms": 2_500},
        )

    assert "WS_STALE" not in [event for event, _ in events]


@pytest.mark.asyncio
async def test_run_starts_ws_health_monitor_when_rest_backup_is_disabled(monkeypatch):
    import src.modules.f4_tracking as f4

    health_started = asyncio.Event()

    async def fake_health(*_args, **_kwargs):
        health_started.set()
        await asyncio.Event().wait()

    async def fake_subscribe(
        _ticker, _on_tick, *, stop_if=None, on_connection_change=None,
    ):
        await health_started.wait()
        _state_mod.get().position_status = "CLOSED"

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(f4, "F4_REST_BACKUP_ENABLED", False)
    monkeypatch.setattr(f4, "F4_HEARTBEAT_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(f4, "_run_ws_health_monitor", fake_health)
    monkeypatch.setattr(f4.kis_ws, "subscribe", fake_subscribe)

    await asyncio.wait_for(f4.run(), 1)

    assert health_started.is_set()


@pytest.mark.asyncio
async def test_run_waits_for_close_before_cancelling_triggering_monitor(monkeypatch):
    """EXITING으로 다른 모니터가 끝나도 정상 청산 부모를 먼저 취소하지 않는다."""
    import src.modules.f4_tracking as f4

    events = []
    exiting = asyncio.Event()
    s = _state_mod.get()

    async def fake_execute_close(_price, reason):
        assert await f4.state.set_exiting(reason) is True
        exiting.set()
        # subscribe 태스크가 먼저 반환해 run()의 정리 경로가 시작되게 한다.
        await asyncio.sleep(0.02)
        assert await f4.state.set_closed(reason) is True
        return True

    async def fake_subscribe(
        _ticker, _on_tick, *, stop_if=None, on_connection_change=None,
    ):
        await exiting.wait()

    async def fake_rest_backup(*_args, **_kwargs):
        await f4._trigger_close(9_800.0, "TRAILING")

    async def wait_forever(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(f4, "F4_REST_BACKUP_ENABLED", True)
    monkeypatch.setattr(f4, "F4_HEARTBEAT_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(f4, "_close_in_progress", False)
    monkeypatch.setattr(f4, "_close_in_progress_warned", False)
    monkeypatch.setattr(f4, "_closing_task", None)
    monkeypatch.setattr(f4, "_active_monitor_tasks", set())
    monkeypatch.setattr(f4, "_execute_close", fake_execute_close)
    monkeypatch.setattr(f4, "_run_rest_price_backup", fake_rest_backup)
    monkeypatch.setattr(f4, "_run_ws_health_monitor", wait_forever)
    monkeypatch.setattr(f4.kis_ws, "subscribe", fake_subscribe)
    monkeypatch.setattr(
        f4,
        "log",
        lambda event, **fields: events.append((event, fields)),
    )

    await asyncio.wait_for(f4.run(), 1)

    assert s.position_status == "CLOSED"
    assert "F4_CLOSE_CANCEL_REQUESTED" not in [event for event, _ in events]


@pytest.mark.asyncio
async def test_run_does_not_log_ws_stale_before_first_tick_grace(monkeypatch):
    import src.modules.f4_tracking as f4

    events = []
    s = _state_mod.get()
    s.target_ticker = "005930"
    s.position_status = "HOLDING"

    async def fake_subscribe(
        _ticker, on_tick, *, stop_if=None, on_connection_change=None,
    ):
        await asyncio.sleep(0)
        await on_tick({"price": ENTRY})
        s.position_status = "CLOSED"

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(f4, "F4_REST_BACKUP_ENABLED", False)
    monkeypatch.setattr(f4, "F4_HEARTBEAT_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(f4, "F4_WS_STALE_SEC", 2.0)
    monkeypatch.setattr(f4, "_handle_price_tick", AsyncMock(return_value=True))
    monkeypatch.setattr(f4.kis_ws, "subscribe", fake_subscribe)
    monkeypatch.setattr(
        f4,
        "log",
        lambda event, **fields: events.append((event, fields)),
    )

    await asyncio.wait_for(f4.run(), 1)

    assert "WS_STALE" not in [event for event, _ in events]


def test_post_close_observation_stops_at_0910():
    s = _state_mod.get()
    s.position_status = "CLOSED"
    s.entry_at = _kst(9, 1).isoformat()

    assert _price_observation_active(_kst(9, 9)) is True
    assert _price_observation_active(_kst(9, 10)) is False
    assert _price_observation_active(_dt(2026, 6, 24, 9, 9, tzinfo=KST)) is False


def test_post_close_observation_stops_when_manually_disabled():
    s = _state_mod.get()
    s.position_status = "CLOSED"
    s.entry_at = _kst(9, 1).isoformat()
    s.post_close_tracking_stopped = True

    assert _price_observation_active(_kst(9, 9)) is False


@pytest.mark.asyncio
async def test_stop_post_close_observation_cancels_monitors_and_persists(monkeypatch):
    import src.modules.f4_tracking as f4

    s = _state_mod.get()
    s.position_status = "CLOSED"
    s.entry_at = _kst(9, 1).isoformat()
    monitor = asyncio.create_task(asyncio.sleep(60))
    persist = AsyncMock()
    monkeypatch.setattr(f4, "_closing_task", None)
    monkeypatch.setattr(f4, "_active_monitor_tasks", {monitor})
    monkeypatch.setattr(f4.state, "persist", persist)
    monkeypatch.setattr(f4, "log", lambda *args, **kwargs: None)

    result = await f4.stop_post_close_observation()
    await asyncio.gather(monitor, return_exceptions=True)

    assert result.items() >= {
        "ok": True,
        "already_stopped": False,
        "cancelled_tasks": 1,
        "persisted": True,
    }.items()
    assert s.post_close_tracking_stopped is True
    assert monitor.cancelled()
    persist.assert_awaited_once()


def test_price_observation_always_active_while_holding():
    s = _state_mod.get()
    s.position_status = "HOLDING"

    assert _price_observation_active(_kst(14, 0)) is True


def test_parse_observe_until_reports_fallback_on_invalid_value():
    import src.modules.f4_tracking as f4

    assert f4._parse_observe_until("09:10") == ((9, 10), False)
    assert f4._parse_observe_until("9:5") == ((9, 5), False)
    assert f4._parse_observe_until("0910") == ((9, 10), True)
    assert f4._parse_observe_until("9:75") == ((9, 10), True)
    assert f4._parse_observe_until("24:00") == ((9, 10), True)
    assert f4._parse_observe_until("") == ((9, 10), True)


def test_observe_until_invalid_warns_once_when_runtime_config_is_loaded(monkeypatch):
    import src.modules.f4_tracking as f4

    events = []
    monkeypatch.setattr(f4, "F4_POST_CLOSE_OBSERVE_UNTIL", "not-a-time")
    monkeypatch.setattr(f4, "_OBSERVE_UNTIL", None)
    monkeypatch.setattr(f4, "_observe_until_invalid_warned", False)
    monkeypatch.setattr(f4, "log", lambda event, **kwargs: events.append((event, kwargs)))

    assert _get_observe_until() == (9, 10)
    assert _get_observe_until() == (9, 10)

    warnings = [fields for event, fields in events if event == "F4_OBSERVE_UNTIL_INVALID"]
    assert warnings == [{"level": "WARN", "value": "not-a-time", "fallback": "09:10"}]


def test_invalid_entry_at_disables_observation_and_warns_once(monkeypatch):
    import src.modules.f4_tracking as f4

    s = _state_mod.get()
    s.position_status = "CLOSED"
    s.target_ticker = "005930"
    s.entry_at = "broken-entry-time"
    events = []
    monkeypatch.setattr(f4, "_invalid_entry_at_warned_value", None)
    monkeypatch.setattr(f4, "log", lambda event, **kwargs: events.append((event, kwargs)))

    assert _price_observation_active(_kst(9, 9)) is False
    assert _price_observation_active(_kst(9, 9)) is False

    warnings = [fields for event, fields in events if event == "F4_ENTRY_AT_INVALID"]
    assert warnings == [{
        "level": "WARN",
        "ticker": "005930",
        "entry_at": "broken-entry-time",
    }]


@pytest.mark.asyncio
async def test_ws_tick_after_close_only_records_price(monkeypatch):
    """CLOSED 관측 중 WS 틱은 차트에만 저장하고 스탑·주문 경로로 보내지 않는다."""
    import src.modules.f4_tracking as f4

    s = _state_mod.get()
    s.position_status = "CLOSED"
    process_tick = AsyncMock()
    push_tick = MagicMock()
    vi_watch = MagicMock()
    vi_watch.on_price = AsyncMock()

    monkeypatch.setattr(f4, "_price_observation_active", lambda *a, **k: True)
    monkeypatch.setattr(f4, "_process_tick", process_tick)
    monkeypatch.setattr(f4.live, "push_tick", push_tick)

    accepted = await _handle_price_tick(
        ENTRY + 100,
        "005930",
        _spike_always_pass(),
        source="ws",
        vi_watch=vi_watch,
    )

    assert accepted is True
    push_tick.assert_called_once_with(ENTRY + 100, ticker="005930")
    process_tick.assert_not_awaited()
    vi_watch.on_price.assert_not_awaited()


@pytest.mark.asyncio
async def test_ws_tick_outside_observation_is_ignored(monkeypatch):
    import src.modules.f4_tracking as f4

    process_tick = AsyncMock()
    push_tick = MagicMock()
    monkeypatch.setattr(f4, "_price_observation_active", lambda *a, **k: False)
    monkeypatch.setattr(f4, "_process_tick", process_tick)
    monkeypatch.setattr(f4.live, "push_tick", push_tick)

    accepted = await _handle_price_tick(
        ENTRY,
        "005930",
        _spike_always_pass(),
        source="ws",
    )

    assert accepted is False
    push_tick.assert_not_called()
    process_tick.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_routes_websocket_ticks_through_shared_handler(monkeypatch):
    import src.modules.f4_tracking as f4

    handle_tick = AsyncMock(return_value=True)

    async def fake_subscribe(
        ticker, on_tick, *, stop_if=None, on_connection_change=None,
    ):
        assert ticker == "005930"
        assert stop_if is not None
        await on_tick({"price": ENTRY + 50})

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(f4, "F4_REST_BACKUP_ENABLED", False)
    monkeypatch.setattr(f4, "_handle_price_tick", handle_tick)
    monkeypatch.setattr(f4, "_make_vi_watch", lambda _ticker: None)
    monkeypatch.setattr(f4.kis_ws, "subscribe", fake_subscribe)

    await f4.run()

    handle_tick.assert_awaited_once_with(
        ENTRY + 50,
        "005930",
        ANY,
        source="ws",
        vi_watch=None,
        tick_meta={"price": ENTRY + 50},
    )


@pytest.mark.asyncio
async def test_rest_backup_collects_after_close_without_running_stop_logic(monkeypatch):
    import src.modules.f4_tracking as f4

    fixed_now = _kst(9, 9)

    class FixedDateTime(_dt):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    s = _state_mod.get()
    s.position_status = "CLOSED"
    s.entry_at = _kst(9, 1).isoformat()
    fetch = AsyncMock(return_value=ENTRY + 100)
    process_tick = AsyncMock()
    push_tick = MagicMock()

    async def stop_after_sleep(_seconds):
        s.position_status = "IDLE"
        # 관측은 더 이상 A의 포지션 상태에 묶여 있지 않다. 종목 잠금이 풀려야
        # 관측 루프가 끝난다(일일 리셋 경로와 동일).
        s.target_ticker = None

    monkeypatch.setattr(f4, "datetime", FixedDateTime)
    monkeypatch.setattr(f4, "_OBSERVE_UNTIL", (9, 10))
    monkeypatch.setattr(f4, "F4_POST_CLOSE_REST_BACKUP_ENABLED", True)
    monkeypatch.setattr(f4, "F4_POST_CLOSE_REST_POLL_INTERVAL_SEC", 30.0)
    monkeypatch.setattr(f4, "_fetch_current_price", fetch)
    monkeypatch.setattr(f4, "_process_tick", process_tick)
    monkeypatch.setattr(f4.live, "push_tick", push_tick)
    monkeypatch.setattr(f4.asyncio, "sleep", AsyncMock(side_effect=stop_after_sleep))
    monkeypatch.setattr(f4, "log", lambda *args, **kwargs: None)

    await _run_rest_price_backup("005930", _spike_always_pass(), lambda: True)

    # 사후 관측 조회는 주문 경로와 경쟁하지 않도록 배경 우선순위를 사용한다.
    fetch.assert_awaited_once_with(
        "005930",
        latency_context="F4_POST_CLOSE",
        aggregate_latency=True,
        request_priority=f4.kis_rest.REQUEST_PRIORITY_BACKGROUND,
    )
    push_tick.assert_called_once_with(ENTRY + 100, ticker="005930")
    process_tick.assert_not_awaited()


@pytest.mark.asyncio
async def test_rest_backup_is_disabled_by_default_after_close(monkeypatch):
    import src.modules.f4_tracking as f4

    fixed_now = _kst(9, 9)

    class FixedDateTime(_dt):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    s = _state_mod.get()
    s.position_status = "CLOSED"
    s.entry_at = _kst(9, 1).isoformat()
    fetch = AsyncMock(return_value=ENTRY + 100)

    async def stop_after_sleep(seconds):
        assert seconds == 30.0
        s.position_status = "IDLE"
        # 관측은 더 이상 A의 포지션 상태에 묶여 있지 않다. 종목 잠금이 풀려야
        # 관측 루프가 끝난다(일일 리셋 경로와 동일).
        s.target_ticker = None

    monkeypatch.setattr(f4, "datetime", FixedDateTime)
    monkeypatch.setattr(f4, "_OBSERVE_UNTIL", (9, 10))
    monkeypatch.setattr(f4, "F4_POST_CLOSE_REST_BACKUP_ENABLED", False)
    monkeypatch.setattr(f4, "F4_POST_CLOSE_REST_POLL_INTERVAL_SEC", 30.0)
    monkeypatch.setattr(f4, "_fetch_current_price", fetch)
    monkeypatch.setattr(f4.asyncio, "sleep", AsyncMock(side_effect=stop_after_sleep))
    monkeypatch.setattr(f4, "log", lambda *args, **kwargs: None)

    await _run_rest_price_backup("005930", _spike_always_pass(), lambda: True)

    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_waits_for_close_task_when_sibling_monitor_finishes(monkeypatch):
    import asyncio as real_asyncio

    import src.modules.f4_tracking as f4

    s = _state_mod.get()
    s.target_ticker = "005930"
    s.trailing_active = True
    s.highest_step = STEP_SIZE

    close_started = real_asyncio.Event()
    release_close = real_asyncio.Event()

    async def fake_execute_close(_price, reason):
        close_started.set()
        await release_close.wait()
        await _state_mod.set_closed(reason)
        return True

    async def fake_subscribe(
        _ticker, on_tick, *, stop_if=None, on_connection_change=None,
    ):
        stop = ENTRY * (1 + STEP_SIZE - STEP_TRAIL)
        await on_tick({"price": stop})

    async def sibling_finishes(*_args, **_kwargs):
        await close_started.wait()

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(f4, "_close_in_progress", False)
    monkeypatch.setattr(f4, "_closing_task", None)
    monkeypatch.setattr(f4, "F4_REST_BACKUP_ENABLED", True)
    monkeypatch.setattr(f4, "_execute_close", fake_execute_close)
    monkeypatch.setattr(f4.kis_ws, "subscribe", fake_subscribe)
    monkeypatch.setattr(f4, "_run_rest_price_backup", sibling_finishes)
    monkeypatch.setattr(f4.notifier, "send", AsyncMock())
    monkeypatch.setattr(f4, "log", lambda *args, **kwargs: None)

    task = real_asyncio.create_task(f4.run())
    await real_asyncio.wait_for(close_started.wait(), 1)
    await real_asyncio.sleep(0.05)

    assert not task.done()
    assert _state_mod.get().position_status == "HOLDING"

    release_close.set()
    await real_asyncio.wait_for(task, 1)

    assert _state_mod.get().position_status == "CLOSED"


@pytest.mark.asyncio
async def test_run_cancellation_still_cleans_up_monitor_tasks(monkeypatch):
    import asyncio as real_asyncio

    import src.modules.f4_tracking as f4

    monitor_started = real_asyncio.Event()
    monitor_cleaned = real_asyncio.Event()
    release_close = real_asyncio.Event()
    shield_started = real_asyncio.Event()
    real_shield = real_asyncio.shield

    async def finishing_monitor(*_args, **_kwargs):
        monitor_started.set()

    async def pending_monitor(*_args, **_kwargs):
        try:
            await real_asyncio.Event().wait()
        finally:
            monitor_cleaned.set()

    async def pending_close():
        await release_close.wait()

    def observed_shield(awaitable):
        shield_started.set()
        return real_shield(awaitable)

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(f4, "F4_REST_BACKUP_ENABLED", False)
    monkeypatch.setattr(f4, "F4_HEARTBEAT_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(f4, "_active_monitor_tasks", set())
    monkeypatch.setattr(f4.kis_ws, "subscribe", finishing_monitor)
    monkeypatch.setattr(f4, "_run_ws_health_monitor", pending_monitor)
    monkeypatch.setattr(f4.asyncio, "shield", observed_shield)

    close_task = real_asyncio.create_task(pending_close())
    monkeypatch.setattr(f4, "_closing_task", close_task)
    f4.live.ws_connected = True
    task = real_asyncio.create_task(f4.run())
    await real_asyncio.wait_for(monitor_started.wait(), 1)
    # 한 모니터가 끝나 run()이 pending close를 shield로 기다리는 시점에 취소한다.
    await real_asyncio.wait_for(shield_started.wait(), 1)
    task.cancel()

    with pytest.raises(real_asyncio.CancelledError):
        await task

    assert monitor_cleaned.is_set()
    assert f4._active_monitor_tasks == set()
    assert f4.live.ws_connected is False

    # shield된 청산 자체는 취소되지 않아야 한다.
    assert not close_task.done()
    release_close.set()
    await real_asyncio.wait_for(close_task, 1)


@pytest.mark.asyncio
async def test_run_keeps_monitoring_and_alerts_when_backup_crashes(monkeypatch):
    import asyncio as real_asyncio

    import src.modules.f4_tracking as f4

    events = []
    notify = AsyncMock()
    ws_started = real_asyncio.Event()

    async def fake_subscribe(
        ticker, on_tick, *, stop_if=None, on_connection_change=None,
    ):
        ws_started.set()
        while not (stop_if and stop_if()):
            await real_asyncio.sleep(0.01)

    async def crash_backup(*args, **kwargs):
        raise RuntimeError("backup boom")

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(f4, "F4_REST_BACKUP_ENABLED", True)
    monkeypatch.setattr(f4.kis_ws, "subscribe", fake_subscribe)
    monkeypatch.setattr(f4, "_run_rest_price_backup", crash_backup)
    monkeypatch.setattr(f4.notifier, "send", notify)
    monkeypatch.setattr(f4, "log", lambda event, **kwargs: events.append((event, kwargs)))

    task = real_asyncio.create_task(f4.run())
    await real_asyncio.wait_for(ws_started.wait(), 1)
    await real_asyncio.sleep(0.05)

    # 백업 태스크가 죽어도 WS 모니터링은 계속되어야 한다
    assert not task.done()
    assert "F4_MONITOR_TASK_ERROR" in [event for event, _ in events]
    notify.assert_awaited()
    assert notify.await_args.args[0] == "F4_MONITOR_TASK_ERROR"

    _state_mod.get().position_status = "CLOSED"
    await real_asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_poll_fill_attempts_cover_timeout_window(monkeypatch):
    import src.modules.f4_tracking as f4

    get = AsyncMock(return_value={"output1": []})
    monkeypatch.setattr(f4.kis_rest, "get", get)
    monkeypatch.setattr(f4.kis_rest, "account_no", lambda: "12345678")
    monkeypatch.setattr(f4.kis_rest, "account_cd", lambda: "01")
    monkeypatch.setattr(f4, "F4_FILL_POLL_INTERVAL_SEC", 0.5)
    monkeypatch.setattr("src.modules.f4_tracking.asyncio.sleep", AsyncMock())

    result = await f4._poll_fill("ORD1", timeout_sec=3)

    assert result is None
    # 3초 창을 0.5초 간격으로 커버 → 6회 조회 (기존 버그: timeout_sec회 = 창 절반)
    assert get.await_count == 6
    assert all(
        call.kwargs["request_priority"]
        == f4.kis_rest.REQUEST_PRIORITY_ORDER_STATUS
        for call in get.await_args_list
    )


@pytest.mark.asyncio
async def test_poll_fill_waits_for_full_cumulative_quantity(monkeypatch):
    import src.modules.f4_tracking as f4

    get = AsyncMock(side_effect=[
        {
            "output1": [{
                "odno": "ORD1",
                "tot_ccld_qty": "30",
                "tot_ccld_amt": "1270500",
                "rmn_qty": "135",
            }],
        },
        {
            "output1": [{
                "odno": "ORD1",
                "tot_ccld_qty": "165",
                "tot_ccld_amt": "6985440",
                "rmn_qty": "0",
            }],
        },
    ])
    monkeypatch.setattr(f4.kis_rest, "get", get)
    monkeypatch.setattr(f4.kis_rest, "account_no", lambda: "12345678")
    monkeypatch.setattr(f4.kis_rest, "account_cd", lambda: "01")
    monkeypatch.setattr(f4, "F4_FILL_POLL_INTERVAL_SEC", 0.5)
    sleep = AsyncMock()
    monkeypatch.setattr("src.modules.f4_tracking.asyncio.sleep", sleep)

    result = await f4._poll_fill("ORD1", timeout_sec=3, expect_qty=165)

    assert result == {"fill_price": 42_336, "fill_qty": 165}
    assert get.await_count == 2
    sleep.assert_awaited_once_with(0.5)


@pytest.mark.asyncio
async def test_poll_fill_returns_latest_partial_only_after_timeout(monkeypatch):
    import src.modules.f4_tracking as f4

    get = AsyncMock(return_value={
        "output1": [{
            "odno": "ORD1",
            "tot_ccld_qty": "30",
            "tot_ccld_amt": "1270500",
            "rmn_qty": "135",
        }],
    })
    monkeypatch.setattr(f4.kis_rest, "get", get)
    monkeypatch.setattr(f4.kis_rest, "account_no", lambda: "12345678")
    monkeypatch.setattr(f4.kis_rest, "account_cd", lambda: "01")
    monkeypatch.setattr(f4, "F4_FILL_POLL_INTERVAL_SEC", 0.5)
    monkeypatch.setattr("src.modules.f4_tracking.asyncio.sleep", AsyncMock())

    result = await f4._poll_fill("ORD1", timeout_sec=1, expect_qty=165)

    assert result == {"fill_price": 42_350, "fill_qty": 30}
    assert get.await_count == 2


@pytest.mark.asyncio
async def test_poll_fill_returns_immediately_when_partial_order_is_terminal(monkeypatch):
    """부분체결 후 잔량 0(취소·소멸)이면 남은 폴링을 소진하지 않고 즉시 반환한다.

    끝까지 기다리면 F4_CLOSE_PENDING 알림과 잔량 기록이 폴링 창만큼 늦어진다.
    """
    import src.modules.f4_tracking as f4

    get = AsyncMock(return_value={
        "output1": [{
            "odno": "ORD1",
            "tot_ccld_qty": "30",
            "tot_ccld_amt": "1270500",
            "rmn_qty": "0",
        }],
    })
    monkeypatch.setattr(f4.kis_rest, "get", get)
    monkeypatch.setattr(f4.kis_rest, "account_no", lambda: "12345678")
    monkeypatch.setattr(f4.kis_rest, "account_cd", lambda: "01")
    monkeypatch.setattr(f4, "F4_FILL_POLL_INTERVAL_SEC", 0.5)
    sleep = AsyncMock()
    monkeypatch.setattr("src.modules.f4_tracking.asyncio.sleep", sleep)

    result = await f4._poll_fill("ORD1", timeout_sec=30, expect_qty=165)

    assert result == {"fill_price": 42_350, "fill_qty": 30}
    assert get.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_fill_keeps_polling_when_remaining_qty_missing(monkeypatch):
    """rmn_qty가 응답에 없으면 종료를 단정할 수 없으므로 기존대로 계속 폴링한다."""
    import src.modules.f4_tracking as f4

    get = AsyncMock(return_value={
        "output1": [{
            "odno": "ORD1",
            "tot_ccld_qty": "30",
            "tot_ccld_amt": "1270500",
        }],
    })
    monkeypatch.setattr(f4.kis_rest, "get", get)
    monkeypatch.setattr(f4.kis_rest, "account_no", lambda: "12345678")
    monkeypatch.setattr(f4.kis_rest, "account_cd", lambda: "01")
    monkeypatch.setattr(f4, "F4_FILL_POLL_INTERVAL_SEC", 0.5)
    monkeypatch.setattr("src.modules.f4_tracking.asyncio.sleep", AsyncMock())

    result = await f4._poll_fill("ORD1", timeout_sec=1, expect_qty=165)

    assert result == {"fill_price": 42_350, "fill_qty": 30}
    assert get.await_count == 2


@pytest.mark.asyncio
async def test_execute_close_sends_critical_alert_on_sell_error(monkeypatch):
    notify = AsyncMock()
    record_order = AsyncMock()
    persist = AsyncMock()

    monkeypatch.setenv("DRY_RUN", "0")
    _state_mod.get().trade_id = 123
    monkeypatch.setattr(
        "src.modules.f4_tracking._send_sell",
        AsyncMock(side_effect=RuntimeError("sell failed")),
    )
    monkeypatch.setattr("src.modules.f4_tracking.notifier.send", notify)
    monkeypatch.setattr(
        "src.modules.f4_tracking.exit_recovery.find_matching_order",
        AsyncMock(return_value=("NOT_FOUND", None)),
    )
    monkeypatch.setattr("src.modules.f4_tracking.db.record_order", record_order)
    monkeypatch.setattr("src.modules.f4_tracking.state.persist", persist)

    result = await _execute_close(ENTRY * 0.98, "HARD_STOP")

    assert result is False
    assert _state_mod.get().position_status == "EXITING"
    assert notify.await_args.args[0] == "F4_SELL_SUBMISSION_UNKNOWN"
    record_order.assert_awaited_once()
    assert persist.await_count >= 2


@pytest.mark.asyncio
async def test_execute_close_separates_trigger_price_and_measures_latency(monkeypatch):
    import src.modules.f4_tracking as f4

    _state_mod.get().trade_id = 123
    record_order = AsyncMock(return_value=9)
    update_order_fill = AsyncMock()
    close_trade = AsyncMock()
    poll_fill = AsyncMock(return_value={"fill_price": 9_750.0, "fill_qty": 100})

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(
        "src.modules.f4_tracking._send_sell",
        AsyncMock(return_value={"rt_cd": "0", "output": {"ODNO": "SELL001"}}),
    )
    monkeypatch.setattr(
        "src.modules.f4_tracking._poll_fill",
        poll_fill,
    )
    monkeypatch.setattr("src.modules.f4_tracking.db.record_order", record_order)
    monkeypatch.setattr("src.modules.f4_tracking.db.update_order_fill", update_order_fill)
    monkeypatch.setattr("src.modules.f4_tracking.db.close_trade", close_trade)
    monkeypatch.setattr("src.modules.f4_tracking.state.persist", AsyncMock())
    monkeypatch.setattr("src.modules.f4_tracking.notifier.send", AsyncMock())
    monkeypatch.setattr(
        "src.modules.f4_tracking.time.perf_counter",
        MagicMock(side_effect=[100.0, 100.321]),
    )

    result = await _execute_close(ENTRY * 0.98, "HARD_STOP")

    assert result is True
    poll_fill.assert_awaited_once_with("SELL001", timeout_sec=30, expect_qty=100)
    assert _state_mod.get().position_status == "CLOSED"
    assert _state_mod.get().remaining_qty == 0
    assert record_order.await_args.args[4] == 0.0
    assert record_order.await_args.kwargs["trigger_price"] == ENTRY * 0.98
    assert update_order_fill.await_args.args[1] == 9_750.0
    assert update_order_fill.await_args.args[3] == 321
    close_trade.assert_awaited_once_with(
        123,
        9_750.0,
        "HARD_STOP",
        -2.5,
        0.0,
        exit_qty=100,
        high_price=ENTRY,
    )
    f4.db.finalize_trailing_shadow_comparison.assert_awaited_once_with(
        123,
        baseline_step_trail=f4.TRAILING_SHADOW_BASELINE_TRAIL,
        recommended_step_trail=STEP_TRAIL,
        entry_price=ENTRY,
        exit_qty=100,
        highest_step=0.0,
        baseline_stop_price=None,
        recommended_stop_price=None,
        recommended_exit_price=ENTRY * 0.98,
        actual_exit_price=9_750.0,
        actual_pnl_pct=-2.5,
        close_reason="HARD_STOP",
    )


@pytest.mark.asyncio
async def test_execute_close_keeps_order_pending_when_fill_unconfirmed(monkeypatch):
    _state_mod.get().trade_id = 123
    record_order = AsyncMock(return_value=9)
    update_order_fill = AsyncMock()
    close_trade = AsyncMock()

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(
        "src.modules.f4_tracking._send_sell",
        AsyncMock(return_value={"rt_cd": "0", "output": {"ODNO": "SELL001"}}),
    )
    monkeypatch.setattr(
        "src.modules.f4_tracking._poll_fill", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("src.modules.f4_tracking.db.record_order", record_order)
    monkeypatch.setattr("src.modules.f4_tracking.db.update_order_fill", update_order_fill)
    monkeypatch.setattr("src.modules.f4_tracking.db.close_trade", close_trade)
    monkeypatch.setattr("src.modules.f4_tracking.state.persist", AsyncMock())
    notify = AsyncMock()
    monkeypatch.setattr("src.modules.f4_tracking.notifier.send", notify)

    result = await _execute_close(ENTRY * 0.98, "HARD_STOP")

    # 주문 기록은 남지만 체결 미확인이므로 FILLED로 갱신하지 않는다 —
    # 트리거가=체결가인 0%p 가짜 슬리피지 표본을 막는다.
    assert result is True
    assert _state_mod.get().position_status == "EXITING"
    assert _state_mod.get().remaining_qty == 100
    record_order.assert_awaited_once()
    update_order_fill.assert_not_awaited()
    close_trade.assert_not_awaited()
    assert notify.await_args.args[0] == "F4_CLOSE_PENDING"


@pytest.mark.asyncio
async def test_execute_close_keeps_partial_fill_in_exiting_state(monkeypatch):
    _state_mod.get().trade_id = 123
    record_order = AsyncMock(return_value=9)
    update_order_fill = AsyncMock()
    close_trade = AsyncMock()

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(
        "src.modules.f4_tracking._send_sell",
        AsyncMock(return_value={"rt_cd": "0", "output": {"ODNO": "SELL001"}}),
    )
    monkeypatch.setattr(
        "src.modules.f4_tracking._poll_fill",
        AsyncMock(return_value={"fill_price": 9_800.0, "fill_qty": 40}),
    )
    monkeypatch.setattr("src.modules.f4_tracking.db.record_order", record_order)
    monkeypatch.setattr("src.modules.f4_tracking.db.update_order_fill", update_order_fill)
    monkeypatch.setattr("src.modules.f4_tracking.db.close_trade", close_trade)
    monkeypatch.setattr("src.modules.f4_tracking.state.persist", AsyncMock())
    monkeypatch.setattr("src.modules.f4_tracking.notifier.send", AsyncMock())

    result = await _execute_close(ENTRY * 0.98, "HARD_STOP")

    assert result is True
    assert _state_mod.get().position_status == "EXITING"
    assert _state_mod.get().remaining_qty == 60
    assert update_order_fill.await_args.kwargs["status"] == "PARTIAL_FILL"
    close_trade.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_close_treats_rejected_sell_response_as_failure(monkeypatch):
    from src.modules.f4_tracking import _execute_close

    notify = AsyncMock()
    record_order = AsyncMock()
    close_trade = AsyncMock()
    persist = AsyncMock()

    monkeypatch.setattr(
        "src.modules.f4_tracking._send_sell",
        AsyncMock(return_value={
            "rt_cd": "1", "msg_cd": "EGW00001", "msg1": "rejected", "output": {},
            "_response_meta": {"http_status": 200, "request_sent": True},
        }),
    )
    monkeypatch.setattr("src.modules.f4_tracking.notifier.send", notify)
    monkeypatch.setattr("src.modules.f4_tracking.db.record_order", record_order)
    monkeypatch.setattr("src.modules.f4_tracking.db.close_trade", close_trade)
    monkeypatch.setattr("src.modules.f4_tracking.state.persist", persist)

    result = await _execute_close(ENTRY * 0.98, "HARD_STOP")

    assert result is False
    assert _state_mod.get().position_status == "HOLDING"
    notify.assert_awaited_once()
    record_order.assert_not_awaited()
    close_trade.assert_not_awaited()
    assert persist.await_count >= 2

@pytest.mark.asyncio
async def test_execute_close_logs_and_alerts_when_directly_cancelled(monkeypatch):
    from src.modules import f4_tracking as f4

    notify = AsyncMock()
    events = []

    async def cancelled_sell(*_args, **_kwargs):
        raise asyncio.CancelledError()

    _state_mod.get().trade_id = 123
    monkeypatch.setattr(f4.db, "record_order", AsyncMock(return_value=9))
    monkeypatch.setattr(f4.state, "persist", AsyncMock())
    monkeypatch.setattr(f4, "_send_sell", cancelled_sell)
    monkeypatch.setattr(f4.notifier, "send", notify)
    monkeypatch.setattr(f4, "log", lambda event, **kwargs: events.append((event, kwargs)))

    with pytest.raises(asyncio.CancelledError):
        await f4._execute_close(ENTRY * 0.98, "HARD_STOP")

    assert "F4_CLOSE_TASK_CANCELLED" in [event for event, _ in events]
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_close_still_sells_when_intent_db_recording_fails(monkeypatch):
    from src.modules import f4_tracking as f4

    notify = AsyncMock()
    persist = AsyncMock()
    close_trade = AsyncMock()
    events = []
    _state_mod.get().trade_id = 123

    monkeypatch.setattr(
        f4,
        "_send_sell",
        AsyncMock(return_value={"rt_cd": "0", "output": {"ODNO": "SELL001"}}),
    )
    monkeypatch.setattr(
        f4, "_poll_fill",
        AsyncMock(return_value={"fill_price": round(ENTRY * 0.98), "fill_qty": 100}),
    )
    monkeypatch.setattr(f4.db, "record_order", AsyncMock(side_effect=RuntimeError("db down")))
    monkeypatch.setattr(f4.db, "close_trade", close_trade)
    monkeypatch.setattr(f4.state, "persist", persist)
    monkeypatch.setattr(f4.notifier, "send", notify)
    monkeypatch.setattr(f4, "log", lambda event, **kwargs: events.append((event, kwargs)))

    result = await f4._execute_close(ENTRY * 0.98, "HARD_STOP")

    assert result is True
    assert _state_mod.get().position_status == "CLOSED"
    assert _state_mod.get().remaining_qty == 0
    assert "HARD_STOP" in [event for event, _ in events]
    notify.assert_awaited_once()
    f4._send_sell.assert_awaited_once()
    close_trade.assert_awaited_once()

@pytest.mark.asyncio
async def test_trigger_close_warns_only_once_while_close_is_in_progress(monkeypatch):
    from src.modules import f4_tracking as f4

    events = []
    monkeypatch.setattr(f4, "_close_in_progress", True)
    monkeypatch.setattr(f4, "_close_in_progress_warned", False)
    monkeypatch.setattr(f4, "log", lambda event, **kwargs: events.append((event, kwargs)))

    await f4._trigger_close(ENTRY * 0.98, "HARD_STOP")
    await f4._trigger_close(ENTRY * 0.97, "HARD_STOP")

    assert [event for event, _ in events] == ["F4_CLOSE_ALREADY_IN_PROGRESS"]
    assert f4._close_in_progress_warned is True


# ── run_forever: 거래일을 넘겨 사는 프로세스의 F4 재무장 ─────────────


@pytest.mark.asyncio
async def test_run_forever_tracks_new_day_after_restart_with_closed_state(monkeypatch):
    """[2026-07-16 인시던트 재발 방지] 전일 CLOSED 상태로 복원된 장기 실행
    프로세스에서도, 다음 거래일의 새 HOLDING에 F4가 다시 붙어야 한다."""
    import asyncio as real_asyncio
    import contextlib

    import src.modules.f4_tracking as f4

    s = _state_mod.get()
    s.trading_date = "20260715"
    s.position_status = "CLOSED"  # 전일 청산 상태로 프로세스 재시작

    subscribed = real_asyncio.Event()

    async def fake_subscribe(
        _ticker, on_tick, *, stop_if=None, on_connection_change=None,
    ):
        subscribed.set()
        while not (stop_if and stop_if()):
            await real_asyncio.sleep(0.01)

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(f4, "_close_in_progress", False)
    monkeypatch.setattr(f4, "_closing_task", None)
    monkeypatch.setattr(f4, "F4_REST_BACKUP_ENABLED", False)
    monkeypatch.setattr(f4, "_REARM_INTERVAL_SEC", 0.01, raising=False)
    monkeypatch.setattr(f4.kis_ws, "subscribe", fake_subscribe)
    monkeypatch.setattr(f4, "log", lambda *args, **kwargs: None)

    task = real_asyncio.create_task(f4.run_forever())
    try:
        await real_asyncio.sleep(0.1)
        assert not subscribed.is_set()  # CLOSED 동안은 구독하지 않는다

        await _state_mod.ensure_trading_day("20260716")  # 새 거래일 리셋(IDLE)
        s.position_status = "HOLDING"  # 새 진입
        s.target_ticker = "004310"
        await real_asyncio.wait_for(subscribed.wait(), 1)
    finally:
        s.position_status = "CLOSED"
        task.cancel()
        with contextlib.suppress(real_asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_forever_rearms_for_next_trading_day_after_close(monkeypatch):
    """당일 추적이 청산으로 끝난 뒤에도 다음 거래일 HOLDING을 다시 추적한다."""
    import asyncio as real_asyncio
    import contextlib

    import src.modules.f4_tracking as f4

    s = _state_mod.get()
    s.trading_date = "20260716"

    subscribe_count = 0
    subscribed = real_asyncio.Event()

    async def fake_subscribe(
        _ticker, on_tick, *, stop_if=None, on_connection_change=None,
    ):
        nonlocal subscribe_count
        subscribe_count += 1
        subscribed.set()
        while not (stop_if and stop_if()):
            await real_asyncio.sleep(0.01)

    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(f4, "_close_in_progress", False)
    monkeypatch.setattr(f4, "_closing_task", None)
    monkeypatch.setattr(f4, "F4_REST_BACKUP_ENABLED", False)
    monkeypatch.setattr(f4, "_REARM_INTERVAL_SEC", 0.01, raising=False)
    monkeypatch.setattr(f4.kis_ws, "subscribe", fake_subscribe)
    monkeypatch.setattr(f4, "log", lambda *args, **kwargs: None)

    task = real_asyncio.create_task(f4.run_forever())
    try:
        await real_asyncio.wait_for(subscribed.wait(), 1)  # 1일차 추적 시작
        subscribed.clear()

        s.position_status = "CLOSED"  # 1일차 청산
        await real_asyncio.sleep(0.05)

        await _state_mod.ensure_trading_day("20260717")  # 새 거래일 리셋(IDLE)
        s.position_status = "HOLDING"
        s.target_ticker = "005930"
        await real_asyncio.wait_for(subscribed.wait(), 1)  # 2일차 추적 시작
        assert subscribe_count == 2
    finally:
        s.position_status = "CLOSED"
        task.cancel()
        with contextlib.suppress(real_asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_forever_restarts_with_alert_after_run_exception(monkeypatch):
    """[P1 회귀] run()이 예외로 죽어도 상주 루프는 CRIT 로그·알림 후 재시작한다."""
    import asyncio as real_asyncio
    import contextlib

    import src.modules.f4_tracking as f4

    calls = 0
    recovered = real_asyncio.Event()
    events = []
    notify = AsyncMock()

    async def flaky_run():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("ws boom")
        recovered.set()

    monkeypatch.setattr(f4, "run", flaky_run)
    monkeypatch.setattr(f4, "_REARM_INTERVAL_SEC", 0.01, raising=False)
    monkeypatch.setattr(f4, "_REARM_HOLDING_INTERVAL_SEC", 0.01, raising=False)
    monkeypatch.setattr(f4, "_REARM_ERROR_INTERVAL_SEC", 0.01, raising=False)
    monkeypatch.setattr(f4.notifier, "send", notify)
    monkeypatch.setattr(f4, "log", lambda event, **kw: events.append((event, kw)))

    task = real_asyncio.create_task(f4.run_forever())
    try:
        await real_asyncio.wait_for(recovered.wait(), 1)
    finally:
        task.cancel()
        with contextlib.suppress(real_asyncio.CancelledError):
            await task

    assert calls >= 2
    assert "F4_RUN_FOREVER_ERROR" in [event for event, _ in events]
    notify.assert_awaited()
    assert notify.await_args.args[0] == "F4_RUN_FOREVER_ERROR"


@pytest.mark.asyncio
async def test_run_forever_survives_notifier_failure_on_run_exception(monkeypatch):
    """예외 알림 전송 자체가 실패해도 상주 루프는 죽지 않는다."""
    import asyncio as real_asyncio
    import contextlib

    import src.modules.f4_tracking as f4

    calls = 0
    recovered = real_asyncio.Event()

    async def flaky_run():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("ws boom")
        recovered.set()

    monkeypatch.setattr(f4, "run", flaky_run)
    monkeypatch.setattr(f4, "_REARM_INTERVAL_SEC", 0.01, raising=False)
    monkeypatch.setattr(f4, "_REARM_HOLDING_INTERVAL_SEC", 0.01, raising=False)
    monkeypatch.setattr(f4, "_REARM_ERROR_INTERVAL_SEC", 0.01, raising=False)
    monkeypatch.setattr(f4.notifier, "send", AsyncMock(side_effect=RuntimeError("telegram down")))
    monkeypatch.setattr(f4, "log", lambda *args, **kwargs: None)

    task = real_asyncio.create_task(f4.run_forever())
    try:
        await real_asyncio.wait_for(recovered.wait(), 1)
    finally:
        task.cancel()
        with contextlib.suppress(real_asyncio.CancelledError):
            await task

    assert calls >= 2


@pytest.mark.asyncio
async def test_run_forever_rearms_when_cycle_exits_while_holding(monkeypatch):
    """모니터 전원이 HOLDING 중 비정상 종료해도 루프가 사이클을 재시작한다."""
    import asyncio as real_asyncio
    import contextlib

    import src.modules.f4_tracking as f4

    calls = 0
    ran_twice = real_asyncio.Event()

    async def fake_run():
        nonlocal calls
        calls += 1
        if calls >= 2:
            ran_twice.set()

    monkeypatch.setattr(f4, "run", fake_run)
    monkeypatch.setattr(f4, "_REARM_INTERVAL_SEC", 0.01, raising=False)
    monkeypatch.setattr(f4, "_REARM_HOLDING_INTERVAL_SEC", 0.01, raising=False)

    task = real_asyncio.create_task(f4.run_forever())
    try:
        await real_asyncio.wait_for(ran_twice.wait(), 1)
    finally:
        task.cancel()
        with contextlib.suppress(real_asyncio.CancelledError):
            await task


# ── 청산 태스크는 틱 경로를 막지 않는다 (2026-09-17/18 WS 틱 정지 티켓) ──────────


@pytest.mark.asyncio
async def test_trigger_close_returns_before_the_close_finishes(monkeypatch):
    """스탑이 맞으면 청산 태스크를 띄우고 바로 돌아온다 — WS 리더가 계속 틱을 읽어야 한다.

    docs/F4_CLOSE_TICK_FREEZE_FOLLOWUP_20260917.md: 인라인 대기 때문에 매도~체결 확인
    (7~27초) 동안 틱을 한 건도 못 읽었고, 27초 날에는 큐 포화 → keepalive 단절 → 38초
    유실까지 번졌다.
    """
    import asyncio as real_asyncio

    from src.modules import f4_tracking as f4

    release = real_asyncio.Event()
    started = real_asyncio.Event()

    async def slow_close(_price, _reason):
        started.set()
        await release.wait()
        return True

    monkeypatch.setattr(f4, "_close_in_progress", False)
    monkeypatch.setattr(f4, "_close_in_progress_warned", False)
    monkeypatch.setattr(f4, "_closing_task", None)
    monkeypatch.setattr(f4, "_execute_close", slow_close)
    monkeypatch.setattr(f4, "log", lambda *a, **k: None)

    await real_asyncio.wait_for(f4._trigger_close(9_800.0, "TRAILING"), 0.2)

    await real_asyncio.wait_for(started.wait(), 1)
    assert f4._close_in_progress is True
    assert f4._closing_task is not None and not f4._closing_task.done()

    release.set()
    await real_asyncio.wait_for(f4._closing_task, 1)
    await real_asyncio.sleep(0)  # done-callback 실행
    assert f4._close_in_progress is False
    assert f4._closing_task is None


@pytest.mark.asyncio
async def test_close_now_waits_for_the_close_result(monkeypatch):
    """외부 호출(F3 비상가드 등)은 결과가 필요하므로 여전히 기다린다."""
    import asyncio as real_asyncio

    from src.modules import f4_tracking as f4

    async def close_then_mark(_price, reason):
        await real_asyncio.sleep(0.05)
        _state_mod.get().position_status = "CLOSED"
        return True

    _state_mod.get().position_status = "HOLDING"
    monkeypatch.setattr(f4, "_close_in_progress", False)
    monkeypatch.setattr(f4, "_close_in_progress_warned", False)
    monkeypatch.setattr(f4, "_closing_task", None)
    monkeypatch.setattr(f4, "_execute_close", close_then_mark)
    monkeypatch.setattr(f4, "log", lambda *a, **k: None)

    assert await f4.close_now(9_800.0, "MANUAL") is True
    assert f4._close_in_progress is False


@pytest.mark.asyncio
async def test_detached_close_task_error_is_logged_as_crit(monkeypatch):
    import asyncio as real_asyncio

    from src.modules import f4_tracking as f4

    events = []

    async def broken_close(_price, _reason):
        raise RuntimeError("boom")

    monkeypatch.setattr(f4, "_close_in_progress", False)
    monkeypatch.setattr(f4, "_close_in_progress_warned", False)
    monkeypatch.setattr(f4, "_closing_task", None)
    monkeypatch.setattr(f4, "_execute_close", broken_close)
    monkeypatch.setattr(f4, "log", lambda event, **k: events.append((event, k)))

    await f4._trigger_close(9_800.0, "HARD_STOP")
    for _ in range(5):
        await real_asyncio.sleep(0)

    assert [e for e, _ in events if e == "F4_CLOSE_TASK_ERROR"] == ["F4_CLOSE_TASK_ERROR"]
    assert f4._close_in_progress is False
