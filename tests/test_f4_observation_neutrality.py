"""관측 계층 중립화 — 트랙 A의 포지션 상태에서 가격 관측을 분리한다.

지금은 `_price_observation_active()`가 `state.get()`(트랙 A)을 읽어 A가
HOLDING/CLOSED가 아니면 False를 반환한다. 그래서 A가 진입하지 않은 날에는
WS 구독도 틱 방송도 일어나지 않고, 하필 "A는 못 샀는데 B는 살 수 있는 날"의
데이터가 통째로 비어버린다.

관측을 종목 확정 시점부터 열되, 매매 판단과 유량 프로파일은 건드리지 않는다.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.modules import f4_tracking

KST = ZoneInfo("Asia/Seoul")

TODAY = "20260825"
NOON = datetime(2026, 8, 25, 12, 0, tzinfo=KST)
EVENING = datetime(2026, 8, 25, 16, 0, tzinfo=KST)


class _State:
    def __init__(self, **kw):
        self.trading_date = kw.get("trading_date", TODAY)
        self.target_ticker = kw.get("target_ticker")
        self.observe_ticker = kw.get("observe_ticker")
        self.position_status = kw.get("position_status", "IDLE")
        self.entry_at = kw.get("entry_at")
        self.post_close_tracking_stopped = kw.get("post_close_tracking_stopped", False)
        self.trade_id = kw.get("trade_id", 0)


@pytest.fixture(autouse=True)
def _no_capture(monkeypatch):
    """캡처 비활성 기본값 — cutoff가 F4_POST_CLOSE_OBSERVE_UNTIL을 타게 한다."""
    monkeypatch.setattr(f4_tracking.tick_capture, "is_active", lambda: False)
    monkeypatch.setattr(f4_tracking.tick_capture, "active_ticker", lambda: None)


def _use(monkeypatch, st):
    monkeypatch.setattr(f4_tracking.state, "get", lambda: st)


# ── (a) 관측 게이트 확장 ─────────────────────────────────────────────


def test_observes_after_target_locked_even_without_entry(monkeypatch):
    """A가 미진입(IDLE)이어도 종목이 확정됐으면 관측한다 — 이 파일의 핵심."""
    _use(monkeypatch, _State(target_ticker="005930", position_status="IDLE"))
    assert f4_tracking._price_observation_active(NOON) is True


def test_observes_while_entering(monkeypatch):
    """진입 시도 중에도 관측은 열려 있어야 한다."""
    _use(monkeypatch, _State(target_ticker="005930", position_status="ENTERING"))
    assert f4_tracking._price_observation_active(NOON) is True


def test_no_observation_without_target(monkeypatch):
    """종목이 없으면 관측할 대상이 없다."""
    _use(monkeypatch, _State(target_ticker=None, position_status="IDLE"))
    assert f4_tracking._price_observation_active(NOON) is False


def test_observes_rank1_after_candidates_exhausted(monkeypatch):
    """2026-09-10/15: 후보가 전부 탈락하면 F3가 target_ticker를 지우고 관측이
    그 자리에서 끝나 캡처 파일이 0바이트로 남았다 — 설계가 가장 원한 날인데.
    observe_ticker(처음 잠긴 1순위)는 소진과 무관하게 15:20까지 관측한다."""
    _use(monkeypatch, _State(target_ticker=None, observe_ticker="017900", position_status="IDLE"))
    assert f4_tracking._price_observation_active(NOON) is True
    assert f4_tracking._should_attach_capture(f4_tracking.state.get(), NOON) is True
    assert f4_tracking._observation_should_continue("017900", NOON) is True


def test_observe_ticker_respects_session_cutoff(monkeypatch):
    _use(monkeypatch, _State(target_ticker=None, observe_ticker="017900", position_status="IDLE"))
    assert f4_tracking._price_observation_active(EVENING) is False
    assert f4_tracking._observation_should_continue("017900", EVENING) is False


def test_target_ticker_wins_over_observe_ticker_while_set(monkeypatch):
    """후보 교체 중에는 현재 시도 종목을 따라간다(보유 시 손절 판정 종목과 일치)."""
    _use(
        monkeypatch,
        _State(target_ticker="042510", observe_ticker="017900", position_status="ENTERING"),
    )
    assert f4_tracking._observation_should_continue("042510", NOON) is True
    assert f4_tracking._observation_should_continue("017900", NOON) is False


def test_no_observation_after_session_cutoff(monkeypatch):
    """관측 창을 넘기면 미진입 상태에서도 닫힌다(밤새 도는 것 방지)."""
    _use(monkeypatch, _State(target_ticker="005930", position_status="IDLE"))
    assert f4_tracking._price_observation_active(EVENING) is False


def test_no_observation_for_stale_trading_date(monkeypatch):
    """지난 거래일 상태가 남아 있으면 관측하지 않는다."""
    _use(monkeypatch, _State(target_ticker="005930", position_status="IDLE",
                             trading_date="20260824"))
    assert f4_tracking._price_observation_active(NOON) is False


# ── 보존 불변식 (기존 동작을 깨뜨리지 않는다) ────────────────────────


def test_holding_observes_regardless_of_manual_stop(monkeypatch):
    """보유 중에는 수동 종료 플래그가 관측을 끄지 못한다 — 손절 추적 보호."""
    _use(monkeypatch, _State(target_ticker="005930", position_status="HOLDING",
                             post_close_tracking_stopped=True))
    assert f4_tracking._price_observation_active(NOON) is True


def test_manual_stop_closes_observation_when_not_holding(monkeypatch):
    """미보유 구간에서는 수동 종료가 관측을 닫는다."""
    _use(monkeypatch, _State(target_ticker="005930", position_status="IDLE",
                             post_close_tracking_stopped=True))
    assert f4_tracking._price_observation_active(NOON) is False


def test_closed_keeps_post_close_cutoff_when_capture_inactive(monkeypatch):
    """CLOSED 경로는 기존 사후 관측 컷오프(기본 09:10)를 그대로 쓴다.

    이 변경은 '미진입일 관측'만 연다. 청산 후 동작까지 바꾸면
    test_f4_capture_wiring의 기존 계약이 깨진다.
    """
    _use(monkeypatch, _State(target_ticker="005930", position_status="CLOSED",
                             entry_at="2026-08-25T09:05:00+09:00"))
    assert f4_tracking._price_observation_active(NOON) is False


def test_closed_after_cutoff_stops(monkeypatch):
    """CLOSED + 창 종료 후에는 닫힌다."""
    _use(monkeypatch, _State(target_ticker="005930", position_status="CLOSED",
                             entry_at="2026-08-25T09:05:00+09:00"))
    assert f4_tracking._price_observation_active(EVENING) is False


def test_post_close_observation_active_still_requires_closed(monkeypatch):
    """UI용 헬퍼는 CLOSED 전용이라는 의미를 유지해야 한다."""
    _use(monkeypatch, _State(target_ticker="005930", position_status="IDLE"))
    assert f4_tracking.post_close_observation_active(NOON) is False


# ── (c) 유량 가드 ────────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["IDLE", "ENTERING"])
def test_rest_backup_suppressed_without_position(monkeypatch, status):
    """보유가 없으면 보호할 손절이 없다. REST 폴링으로 유량을 태우지 않는다."""
    _use(monkeypatch, _State(target_ticker="005930", position_status=status))
    assert f4_tracking._rest_backup_allowed(status) is False


@pytest.mark.parametrize("status", ["HOLDING", "EXITING"])
def test_rest_backup_allowed_while_position_open(monkeypatch, status):
    """보유·청산 중에는 기존대로 백업이 살아 있어야 한다."""
    _use(monkeypatch, _State(target_ticker="005930", position_status=status))
    assert f4_tracking._rest_backup_allowed(status) is True


# ── (d) 거래 없는 날의 durable 캡처 ──────────────────────────────────


def test_capture_attaches_without_trade_id(monkeypatch):
    """거래가 없어도 종목이 확정됐으면 캡처를 붙인다 — 안 그러면 디스크에 안 남는다."""
    _use(monkeypatch, _State(target_ticker="005930", position_status="IDLE"))
    assert f4_tracking._should_attach_capture(f4_tracking.state.get(), NOON) is True


def test_capture_attaches_when_holding_with_trade(monkeypatch):
    """기존 경로(체결 후 부착)는 그대로 동작한다."""
    _use(monkeypatch, _State(target_ticker="005930", position_status="HOLDING",
                             trade_id=31))
    assert f4_tracking._should_attach_capture(f4_tracking.state.get(), NOON) is True


def test_capture_not_attached_without_target(monkeypatch):
    """종목이 없으면 붙일 대상이 없다."""
    _use(monkeypatch, _State(target_ticker=None, position_status="IDLE"))
    assert f4_tracking._should_attach_capture(f4_tracking.state.get(), NOON) is False


def test_capture_not_reattached_after_capture_window(monkeypatch):
    """캡처 창(15:20)이 끝난 뒤에는 캡처를 다시 붙이지 않는다.

    붙이면 `_price_observation_active()`의 컷오프가 `CAPTURE_UNTIL`로 뒤집혀
    관측이 즉시 끝나고, 최종화 → 재부착이 0.5초마다 반복된다. 2026-08-28
    실장에서 15:15~15:30 사이 약 900바퀴가 돌았고 그날 캡처가 RESTART_GAP으로
    오염됐다 — `F4_POST_CLOSE_OBSERVE_UNTIL`이 `CAPTURE_UNTIL`보다 늦으면
    언제든 재발한다.
    """
    _use(monkeypatch, _State(target_ticker="005930", position_status="CLOSED",
                             entry_at="2026-08-25T09:01:00+09:00"))
    after = datetime(2026, 8, 25, 15, 21, tzinfo=KST)
    assert f4_tracking._should_attach_capture(f4_tracking.state.get(), after) is False


def test_capture_not_reattached_exactly_at_capture_until(monkeypatch):
    """경계 15:20:00은 캡처 창 밖이다 — 그 시각의 최종화가 COMPLETE를 확정한다."""
    _use(monkeypatch, _State(target_ticker="005930", position_status="CLOSED",
                             entry_at="2026-08-25T09:01:00+09:00"))
    at_cutoff = datetime(2026, 8, 25, 15, 20, tzinfo=KST)
    assert f4_tracking._should_attach_capture(f4_tracking.state.get(), at_cutoff) is False


def test_capture_still_attached_during_the_closing_continuous_session(monkeypatch):
    """15:15~15:20은 연속매매 구간이다 — 캡처는 계속 붙어 있어야 한다.

    F5 청산이 15:15인 것은 시장가 매도가 마감 동시호가(15:20~)에 걸리지 않게
    하려는 주문 제약이지 관측 제약이 아니다. 캡처 창은 연속매매가 실제로
    끝나는 15:20까지 간다.
    """
    _use(monkeypatch, _State(target_ticker="005930", position_status="CLOSED",
                             entry_at="2026-08-25T09:01:00+09:00"))
    in_window = datetime(2026, 8, 25, 15, 17, tzinfo=KST)
    assert f4_tracking._should_attach_capture(f4_tracking.state.get(), in_window) is True


def test_capture_still_attached_before_capture_window_ends(monkeypatch):
    """캡처 창 안에서는 종전대로 붙는다 — 장중 재시작 복구가 이 경로다."""
    _use(monkeypatch, _State(target_ticker="005930", position_status="HOLDING",
                             trade_id=31))
    before = datetime(2026, 8, 25, 15, 14, tzinfo=KST)
    assert f4_tracking._should_attach_capture(f4_tracking.state.get(), before) is True


# ── 종목 교체 방어 ───────────────────────────────────────────────────
#
# 관측이 F2 잠금 시점부터 시작되면서, F3가 후보를 교체할 때
# (f3_entry.py:764 `s.target_ticker = picked["ticker"]`) 이미 떠 있는 구독이
# 낡은 종목을 가리키게 됐다. 낡은 구독을 그대로 두면 다른 종목의 가격으로
# 손절·트레일링을 판정한다. SpikeFilter는 ticker를 로깅에만 쓰므로 걸러주지
# 않는다.


def test_subscription_stops_when_target_switches(monkeypatch):
    """F3가 후보를 바꾸면 낡은 구독은 끝나야 한다(재구독 유도)."""
    _use(monkeypatch, _State(target_ticker="000660", position_status="ENTERING"))
    assert f4_tracking._observation_should_continue("005930", NOON) is False


def test_subscription_continues_for_current_target(monkeypatch):
    """종목이 그대로면 구독을 유지한다."""
    _use(monkeypatch, _State(target_ticker="005930", position_status="ENTERING"))
    assert f4_tracking._observation_should_continue("005930", NOON) is True


def test_subscription_stops_when_observation_window_closes(monkeypatch):
    """관측 창이 닫히면 종목이 같아도 끝난다."""
    _use(monkeypatch, _State(target_ticker="005930", position_status="IDLE"))
    assert f4_tracking._observation_should_continue("005930", EVENING) is False


async def test_stale_ticker_tick_never_reaches_stop_logic(monkeypatch):
    """낡은 구독의 틱은 청산 판정·차트·캡처 어디에도 들어가지 않는다."""
    calls = {"process": 0, "push": 0, "capture": 0}
    st = _State(target_ticker="000660", position_status="HOLDING")
    _use(monkeypatch, st)

    async def _process(*a, **k):
        calls["process"] += 1

    monkeypatch.setattr(f4_tracking, "_process_tick", _process)
    monkeypatch.setattr(f4_tracking.live, "push_tick",
                        lambda *a, **k: calls.__setitem__("push", calls["push"] + 1))
    monkeypatch.setattr(f4_tracking.tick_capture, "enqueue",
                        lambda *a, **k: calls.__setitem__("capture", calls["capture"] + 1))

    accepted = await f4_tracking._handle_price_tick(
        10_000.0, "005930", f4_tracking.SpikeFilter(), source="ws",
    )
    assert accepted is False
    assert calls == {"process": 0, "push": 0, "capture": 0}


async def test_current_ticker_tick_still_processed(monkeypatch):
    """정상 종목의 틱은 그대로 흘러야 한다 — 가드가 과하게 막으면 안 된다."""
    calls = {"process": 0, "push": 0}
    st = _State(target_ticker="005930", position_status="HOLDING")
    _use(monkeypatch, st)

    async def _process(*a, **k):
        calls["process"] += 1

    monkeypatch.setattr(f4_tracking, "_process_tick", _process)
    monkeypatch.setattr(f4_tracking.live, "push_tick",
                        lambda *a, **k: calls.__setitem__("push", calls["push"] + 1))
    monkeypatch.setattr(f4_tracking.tick_capture, "enqueue", lambda *a, **k: None)

    accepted = await f4_tracking._handle_price_tick(
        10_000.0, "005930", f4_tracking.SpikeFilter(), source="ws",
    )
    assert accepted is True
    assert calls["process"] == 1
    assert calls["push"] == 1
