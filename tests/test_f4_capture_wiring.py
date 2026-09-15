"""F4 ↔ tick_capture 배선: enqueue·15:15 관측·15:14 백업·수동중지 finalize."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src import state
from src.modules import f4_tracking, tick_capture
from src.utils.spike_filter import SpikeFilter

KST = ZoneInfo("Asia/Seoul")


@pytest.fixture(autouse=True)
def _reset_state():
    state._state = state.State()
    yield
    state._state = state.State()


def _set_closed_today(ticker="005930"):
    s = state.get()
    s.target_ticker = ticker
    s.entry_at = datetime.now(KST).isoformat()
    s.position_status = "CLOSED"
    s.trade_id = 27
    s.post_close_tracking_stopped = False


async def test_accepted_tick_is_enqueued_with_metadata_and_no_stop_when_closed(monkeypatch):
    _set_closed_today()
    captured: list[dict] = []
    monkeypatch.setattr(tick_capture, "enqueue", lambda tick: captured.append(tick))
    # 관측 창은 벽시계에 의존하므로 이 단위 테스트에서는 열림으로 고정한다.
    monkeypatch.setattr(f4_tracking, "_price_observation_active", lambda *a, **k: True)

    stop_calls = []
    monkeypatch.setattr(
        f4_tracking, "_process_tick",
        lambda *a, **k: stop_calls.append(a),
    )

    tick_meta = {
        "price": 10_500.0,
        "qty": 7,
        "source_ts": datetime(2026, 8, 13, 9, 30, 15, tzinfo=KST).isoformat(),
    }
    ok = await f4_tracking._handle_price_tick(
        10_500.0, "005930", SpikeFilter(), source="ws", tick_meta=tick_meta,
    )
    assert ok is True
    assert len(captured) == 1
    row = captured[0]
    assert row["price"] == 10_500.0
    assert row["source"] == "ws"
    assert row["qty"] == 7
    assert row["source_ts"] == tick_meta["source_ts"]
    assert row["valid"] is True
    # CLOSED에서는 스탑 계산을 절대 실행하지 않는다.
    assert stop_calls == []


async def test_naive_source_ts_is_marked_not_accepted(monkeypatch):
    _set_closed_today()
    captured: list[dict] = []
    monkeypatch.setattr(tick_capture, "enqueue", lambda tick: captured.append(tick))
    monkeypatch.setattr(f4_tracking, "_price_observation_active", lambda *a, **k: True)

    # 오프셋 없는 naive 시각은 받아들이지 않고 표시만 한다.
    tick_meta = {"price": 1.0, "qty": 3, "source_ts": "2026-08-13T09:30:15"}
    await f4_tracking._handle_price_tick(
        1.0, "005930", SpikeFilter(), source="ws", tick_meta=tick_meta,
    )
    assert captured[0]["source_ts"] is None
    assert captured[0]["valid"] is False


async def test_rest_tick_has_no_source_ts(monkeypatch):
    _set_closed_today()
    captured: list[dict] = []
    monkeypatch.setattr(tick_capture, "enqueue", lambda tick: captured.append(tick))
    monkeypatch.setattr(f4_tracking, "_price_observation_active", lambda *a, **k: True)

    await f4_tracking._handle_price_tick(1.0, "005930", SpikeFilter(), source="rest")
    assert captured[0]["source"] == "rest"
    assert captured[0]["source_ts"] is None
    assert captured[0]["valid"] is False


def test_note_ws_loss_marks_disconnect_before_1515(monkeypatch):
    _set_closed_today()
    marks = []
    monkeypatch.setattr(tick_capture, "is_active", lambda: True)
    monkeypatch.setattr(tick_capture, "active_ticker", lambda: "005930")
    monkeypatch.setattr(tick_capture, "mark_ws_disconnect", lambda: marks.append(1))
    base = datetime.now(KST).replace(second=0, microsecond=0)
    assert f4_tracking._note_ws_loss(base.replace(hour=11, minute=0)) is True
    assert marks == [1]
    # 15:15 이후에는 관측 창이 끝났으므로 표시하지 않는다.
    assert f4_tracking._note_ws_loss(base.replace(hour=15, minute=20)) is False
    assert marks == [1]


async def test_finalize_after_observation_when_only_observe_ticker_remains(monkeypatch):
    """소진일: target_ticker는 None, 캡처는 observe_ticker(1순위)에 활성.
    15:20 도달 시 COMPLETE로 최종화돼야 한다 — target_ticker만 보면 여기서
    빠져나가 프로세스 종료 때까지 INCOMPLETE로 남는다(2026-09-10 실측)."""
    s = state.get()
    s.target_ticker = None
    s.observe_ticker = "017900"
    s.position_status = "IDLE"
    s.trade_id = 0
    monkeypatch.setattr(tick_capture, "is_active", lambda: True)
    monkeypatch.setattr(tick_capture, "active_ticker", lambda: "017900")
    calls = []

    async def finalize(reason, *, reached_expected_close):
        calls.append((reason, reached_expected_close))

    monkeypatch.setattr(tick_capture, "finalize", finalize)
    fixed = datetime.now(KST).replace(hour=15, minute=20, second=0, microsecond=0)

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(f4_tracking, "datetime", _Now)

    await f4_tracking._finalize_capture_after_observation()

    assert calls == [("COMPLETE", True)]


async def test_observation_extends_to_1515_when_capture_active(monkeypatch):
    _set_closed_today()
    monkeypatch.setattr(tick_capture, "active_ticker", lambda: "005930")
    now = datetime.now(KST).replace(hour=14, minute=0, second=0, microsecond=0)

    monkeypatch.setattr(tick_capture, "is_active", lambda: True)
    assert f4_tracking._price_observation_active(now) is True

    # 캡처 비활성이면 기존 09:10 컷오프가 적용돼 14:00엔 관측이 꺼져 있다.
    monkeypatch.setattr(tick_capture, "is_active", lambda: False)
    assert f4_tracking._price_observation_active(now) is False


def test_capture_backup_active_stops_at_1514(monkeypatch):
    _set_closed_today()
    monkeypatch.setattr(tick_capture, "is_active", lambda: True)
    monkeypatch.setattr(tick_capture, "active_ticker", lambda: "005930")
    base = datetime.now(KST).replace(second=0, microsecond=0)
    assert f4_tracking._capture_backup_active(base.replace(hour=15, minute=13)) is True
    assert f4_tracking._capture_backup_active(base.replace(hour=15, minute=14)) is False
    assert f4_tracking._capture_backup_active(base.replace(hour=15, minute=20)) is False


def test_capture_backup_does_not_start_before_0935(monkeypatch):
    """계획 §사후 경로: 저우선 REST 백업은 09:35 이후에만 시작한다."""
    _set_closed_today()
    monkeypatch.setattr(tick_capture, "is_active", lambda: True)
    monkeypatch.setattr(tick_capture, "active_ticker", lambda: "005930")
    base = datetime.now(KST).replace(second=0, microsecond=0)
    assert f4_tracking._capture_backup_active(base.replace(hour=9, minute=12)) is False
    assert f4_tracking._capture_backup_active(base.replace(hour=9, minute=34)) is False
    assert f4_tracking._capture_backup_active(base.replace(hour=9, minute=35)) is True


def test_capture_backup_honours_its_own_kill_switch(monkeypatch):
    """캡처 백업은 일반 사후 폴링 스위치를 우회하므로 자체 스위치를 가진다."""
    _set_closed_today()
    monkeypatch.setattr(tick_capture, "is_active", lambda: True)
    monkeypatch.setattr(tick_capture, "active_ticker", lambda: "005930")
    monkeypatch.setattr(tick_capture, "REST_BACKUP_ENABLED", False)
    base = datetime.now(KST).replace(second=0, microsecond=0)
    assert f4_tracking._capture_backup_active(base.replace(hour=13, minute=0)) is False


async def test_manual_stop_finalizes_capture(monkeypatch):
    _set_closed_today()
    calls: list[tuple] = []

    async def fake_finalize(reason, *, reached_expected_close):
        calls.append((reason, reached_expected_close))

    monkeypatch.setattr(tick_capture, "is_active", lambda: True)
    monkeypatch.setattr(tick_capture, "active_ticker", lambda: "005930")
    monkeypatch.setattr(tick_capture, "finalize", fake_finalize)

    async def fake_persist(*a, **k):
        return None

    monkeypatch.setattr(state, "persist", fake_persist)

    result = await f4_tracking.stop_post_close_observation()
    assert result["ok"] is True
    assert calls == [("MANUAL_STOP", False)]
