"""ViWatch — 보유 중 VI(변동성완화장치) 감지 로직 유닛 테스트.

관측 전용 모듈: REST 백업 가격이 일정 시간 동결되면 VI 현황 API를 백그라운드
태스크로 확인한다. on_price는 절대 API 응답을 기다리지 않는다(P1) — WS stale
상황에서 REST 루프가 유일한 손절 감시 경로이기 때문이다.
발동 시각은 API의 bsop_date+cntg_vi_hour로 구성하고 감지 시각은 분리한다(P2).
"""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from src.modules.vi_watch import ViWatch, parse_vi_payload

KST = ZoneInfo("Asia/Seoul")
TICKER = "004310"

# 2026-07-16 실제 VI 현황 API 응답 (해제 완료 상태)
RELEASED_PAYLOAD = {
    "rt_cd": "0",
    "output": {
        "hts_kor_isnm": "현대약품", "mksc_shrn_iscd": "004310",
        "vi_cls_code": "N", "bsop_date": "20260716",
        "cntg_vi_hour": "091333", "vi_cncl_hour": "091550",
        "vi_kind_code": "1", "vi_prc": "7700", "vi_stnd_prc": "7000",
        "vi_dprt": "10.00", "vi_dmc_stnd_prc": "0", "vi_dmc_dprt": "0.00",
        "vi_count": "1",
    },
}

# 발동 중 상태 (해제 시각 없음)
ACTIVE_PAYLOAD = {
    "rt_cd": "0",
    "output": {
        "hts_kor_isnm": "현대약품", "mksc_shrn_iscd": "004310",
        "vi_cls_code": "Y", "bsop_date": "20260716",
        "cntg_vi_hour": "091333", "vi_cncl_hour": "",
        "vi_kind_code": "1", "vi_prc": "7700", "vi_stnd_prc": "7000",
        "vi_dprt": "10.00", "vi_count": "1",
    },
}

DETECT_AT = datetime(2026, 7, 16, 9, 13, 45, tzinfo=KST)
RELEASE_AT = datetime(2026, 7, 16, 9, 15, 50, tzinfo=KST)


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, sec: float) -> None:
        self.t += sec


def _watch(check_vi, clock, now=DETECT_AT, **kwargs) -> ViWatch:
    holder = {"now": now}
    w = ViWatch(TICKER, check_vi, freeze_sec=10.0, cooldown_sec=60.0,
                monotonic=clock, now_fn=lambda: holder["now"], **kwargs)
    # ViWatch에 없는 속성이다 — 테스트가 벽시계를 흔들려고 얹는 것이라
    # 타입 검사에는 보이지 않는다.
    w._test_now = holder  # type: ignore[attr-defined]
    return w


async def _settle(w: ViWatch, price: float, source: str = "rest") -> list[dict]:
    """스폰된 백그라운드 확인 태스크를 완료시키고 다음 폴에서 결과를 소비한다."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return await w.on_price(price, source)


# ── parse_vi_payload ─────────────────────────────────────────────────

def test_parse_returns_none_for_released_vi():
    assert parse_vi_payload(RELEASED_PAYLOAD, TICKER) is None


def test_parse_returns_info_for_active_vi():
    info = parse_vi_payload(ACTIVE_PAYLOAD, TICKER)
    assert info is not None
    assert info["vi_prc"] == "7700"
    assert info["vi_stnd_prc"] == "7000"
    assert info["vi_kind_code"] == "1"
    assert info["cntg_vi_hour"] == "091333"
    assert info["bsop_date"] == "20260716"


def test_parse_ignores_other_ticker():
    assert parse_vi_payload(ACTIVE_PAYLOAD, "005930") is None


def test_parse_handles_list_output():
    payload = {"rt_cd": "0", "output": [ACTIVE_PAYLOAD["output"]]}
    assert parse_vi_payload(payload, TICKER) is not None


# ── 동결 감지 → 백그라운드 VI 확인 ──────────────────────────────────

async def test_no_check_before_freeze_threshold():
    clock = FakeClock()
    check = AsyncMock(return_value=ACTIVE_PAYLOAD)
    w = _watch(check, clock)

    assert await w.on_price(7690.0, source="rest") == []
    clock.advance(5)
    assert await w.on_price(7690.0, source="rest") == []
    check.assert_not_awaited()


async def test_frozen_price_detects_vi_with_actual_start_time():
    clock = FakeClock()
    check = AsyncMock(return_value=ACTIVE_PAYLOAD)
    w = _watch(check, clock)

    await w.on_price(7690.0, source="rest")
    clock.advance(10)
    assert await w.on_price(7690.0, source="rest") == []  # 스폰만, 대기 없음
    events = await _settle(w, 7690.0)

    check.assert_awaited_once()
    assert len(events) == 1
    assert events[0]["type"] == "VI_DETECTED"
    # [P2] ts = API 실제 발동 시각(09:13:33), 감지 시각은 detected_ts로 분리
    assert events[0]["ts"] == "2026-07-16T09:13:33+09:00"
    assert events[0]["detected_ts"] == DETECT_AT.isoformat()
    assert events[0]["vi_prc"] == "7700"
    assert events[0]["frozen_price"] == 7690.0
    assert w.vi_active is True


async def test_on_price_never_awaits_slow_check():
    """[P1 회귀] VI 조회가 아무리 느려도 on_price는 즉시 반환한다."""
    clock = FakeClock()
    gate = asyncio.Event()

    async def slow_check():
        await gate.wait()
        return ACTIVE_PAYLOAD

    w = _watch(slow_check, clock)
    await w.on_price(7690.0, source="rest")
    clock.advance(10)

    for _ in range(5):  # 조회가 막혀 있는 동안에도 폴은 계속 즉시 반환
        events = await asyncio.wait_for(w.on_price(7690.0, source="rest"), 0.1)
        assert events == []
    assert w.vi_active is False

    gate.set()
    events = await _settle(w, 7690.0)
    assert [e["type"] for e in events] == ["VI_DETECTED"]


async def test_price_change_resets_freeze_anchor():
    clock = FakeClock()
    check = AsyncMock(return_value=ACTIVE_PAYLOAD)
    w = _watch(check, clock)

    await w.on_price(7690.0, source="rest")
    clock.advance(8)
    await w.on_price(7700.0, source="rest")  # 가격 변동 → 동결 아님
    clock.advance(8)
    events = await w.on_price(7700.0, source="rest")  # 새 anchor 기준 8초뿐

    check.assert_not_awaited()
    assert events == []


async def test_ws_tick_resets_freeze_anchor():
    clock = FakeClock()
    check = AsyncMock(return_value=ACTIVE_PAYLOAD)
    w = _watch(check, clock)

    await w.on_price(7690.0, source="rest")
    clock.advance(8)
    await w.on_price(7690.0, source="ws")  # WS 틱 존재 → 동결 아님
    clock.advance(8)
    events = await w.on_price(7690.0, source="rest")

    check.assert_not_awaited()
    assert events == []


async def test_ws_tick_during_pending_check_discards_stale_result():
    """조회가 떠 있는 동안 WS 틱이 재개되면(동결 해소) 결과를 폐기한다."""
    clock = FakeClock()
    gate = asyncio.Event()

    async def slow_check():
        await gate.wait()
        return ACTIVE_PAYLOAD

    w = _watch(slow_check, clock)
    await w.on_price(7690.0, source="rest")
    clock.advance(10)
    await w.on_price(7690.0, source="rest")  # 스폰

    await w.on_price(7695.0, source="ws")  # 체결 재개 → 동결 아님
    gate.set()
    events = await _settle(w, 7695.0, source="ws")

    assert events == []
    assert w.vi_active is False


async def test_ws_gap_then_same_price_refreeze_does_not_reuse_stale_check():
    """[P2 회귀] 진행 중 조회가 WS 재개로 무효화된 뒤, WS가 다시 stale해지고
    같은 가격으로 재동결돼도 낡은 결과로 VI_DETECTED를 내면 안 된다."""
    clock = FakeClock()
    gate = asyncio.Event()
    calls = 0

    async def slow_check():
        nonlocal calls
        calls += 1
        await gate.wait()
        return ACTIVE_PAYLOAD

    w = _watch(slow_check, clock)
    await w.on_price(7690.0, source="rest")
    clock.advance(10)
    await w.on_price(7690.0, source="rest")  # 조회 #1 스폰 (pending)

    await w.on_price(7690.0, source="ws")    # 체결 재개 → 조회 #1 무효
    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)                   # 조회 #1 종료(무효 상태)

    # WS 다시 stale → REST가 같은 가격으로 재동결
    events = await w.on_price(7690.0, source="rest")
    assert events == []                      # 낡은 결과 소비 금지
    assert w.vi_active is False

    # 쿨다운 경과 후 새 동결 에피소드에서는 새 조회로 정상 감지
    clock.advance(61)
    await w.on_price(7690.0, source="rest")  # 새 조회 스폰
    events = await _settle(w, 7690.0)
    assert [e["type"] for e in events] == ["VI_DETECTED"]
    # 조회 #1은 실행 전에 취소됐으므로(스폰 직후 무효화) 실제 실행은 새 조회 1회뿐
    assert calls == 1


async def test_rest_price_move_during_pending_check_invalidates_it():
    """진행 중 조회 도중 REST 가격이 움직였다가 같은 가격으로 재동결돼도
    낡은 결과를 쓰지 않는다."""
    clock = FakeClock()
    gate = asyncio.Event()

    async def slow_check():
        await gate.wait()
        return ACTIVE_PAYLOAD

    w = _watch(slow_check, clock)
    await w.on_price(7690.0, source="rest")
    clock.advance(10)
    await w.on_price(7690.0, source="rest")  # 스폰 (pending)

    await w.on_price(7700.0, source="rest")  # 가격 변동 → 동결 깨짐, 조회 무효
    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    events = await w.on_price(7690.0, source="rest")  # 같은 가격 재동결
    assert events == []
    assert w.vi_active is False


# ── 해제 감지 ────────────────────────────────────────────────────────

async def _activate(w: ViWatch, clock: FakeClock, price: float = 7690.0):
    await w.on_price(price, source="rest")
    clock.advance(10)
    await w.on_price(price, source="rest")
    events = await _settle(w, price)
    assert events and events[0]["type"] == "VI_DETECTED"


async def test_rest_price_change_releases_vi_with_actual_duration():
    clock = FakeClock()
    w = _watch(AsyncMock(return_value=ACTIVE_PAYLOAD), clock)
    await _activate(w, clock)

    clock.advance(120)
    w._test_now["now"] = RELEASE_AT
    events = await w.on_price(7630.0, source="rest")

    assert len(events) == 1
    assert events[0]["type"] == "VI_RELEASED"
    assert events[0]["ts"] == RELEASE_AT.isoformat()
    assert events[0]["release_price"] == 7630.0
    # [P2] 정지 시간은 실제 발동(09:13:33)~해제(09:15:50) = 137초
    assert events[0]["duration_sec"] == 137.0
    assert w.vi_active is False


async def test_ws_tick_releases_vi_even_at_same_price():
    clock = FakeClock()
    w = _watch(AsyncMock(return_value=ACTIVE_PAYLOAD), clock)
    await _activate(w, clock)

    clock.advance(130)
    w._test_now["now"] = RELEASE_AT
    events = await w.on_price(7690.0, source="ws")  # 체결 재개 = 해제

    assert len(events) == 1
    assert events[0]["type"] == "VI_RELEASED"
    assert events[0]["duration_sec"] == 137.0
    assert w.vi_active is False


async def test_same_frozen_rest_price_while_active_emits_nothing():
    clock = FakeClock()
    w = _watch(AsyncMock(return_value=ACTIVE_PAYLOAD), clock)
    await _activate(w, clock)

    clock.advance(30)
    assert await w.on_price(7690.0, source="rest") == []
    assert w.vi_active is True


# ── 미발동·오류 처리 ─────────────────────────────────────────────────

async def test_negative_check_emits_once_and_cools_down():
    clock = FakeClock()
    check = AsyncMock(return_value=RELEASED_PAYLOAD)  # 발동 중 아님
    w = _watch(check, clock)

    await w.on_price(7690.0, source="rest")
    clock.advance(10)
    await w.on_price(7690.0, source="rest")
    events = await _settle(w, 7690.0)
    assert [e["type"] for e in events] == ["VI_CHECK_NEGATIVE"]

    clock.advance(30)  # 쿨다운(60초) 이내
    assert await w.on_price(7690.0, source="rest") == []
    assert check.await_count == 1

    clock.advance(31)  # 쿨다운 경과
    await w.on_price(7690.0, source="rest")
    events = await _settle(w, 7690.0)
    assert [e["type"] for e in events] == ["VI_CHECK_NEGATIVE"]
    assert check.await_count == 2


async def test_check_failure_emits_event_and_cools_down():
    clock = FakeClock()
    check = AsyncMock(side_effect=RuntimeError("api down"))
    w = _watch(check, clock)

    await w.on_price(7690.0, source="rest")
    clock.advance(10)
    await w.on_price(7690.0, source="rest")
    events = await _settle(w, 7690.0)

    assert [e["type"] for e in events] == ["VI_CHECK_FAILED"]
    assert "api down" in events[0]["error"]
    assert w.vi_active is False

    clock.advance(30)
    assert await w.on_price(7690.0, source="rest") == []
    assert check.await_count == 1


# ── fetch_vi_status — VI 현황 API 조회 (F3 진입 전 체크·F4 감시 공용) ──

async def test_fetch_vi_status_queries_vi_ranking_api(monkeypatch):
    from src.modules import vi_watch

    calls = {}

    async def fake_get(path, tr_id="", params=None, **kwargs):
        calls["path"] = path
        calls["tr_id"] = tr_id
        calls["params"] = params
        return {"rt_cd": "0", "output": []}

    monkeypatch.setattr(vi_watch.kis_rest, "get", fake_get)

    resp = await vi_watch.fetch_vi_status("072770")

    assert calls["path"] == "/uapi/domestic-stock/v1/quotations/inquire-vi-status"
    assert calls["tr_id"] == "FHPST01390000"
    assert calls["params"]["FID_INPUT_ISCD"] == "072770"
    assert resp["rt_cd"] == "0"


async def test_fetch_vi_status_raises_on_api_error(monkeypatch):
    import pytest

    from src.modules import vi_watch

    async def fake_get(path, tr_id="", params=None, **kwargs):
        return {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "오류"}

    monkeypatch.setattr(vi_watch.kis_rest, "get", fake_get)

    with pytest.raises(RuntimeError):
        await vi_watch.fetch_vi_status("072770")
