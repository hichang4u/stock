"""F2 후보 정렬·락업 유닛 테스트."""
from unittest.mock import AsyncMock, patch

import pytest

from src import state as _state_mod
from src.modules.f2_lockup import run

# ── 헬퍼 ──────────────────────────────────────────────────────────────

def _candidate(
    ticker: str,
    gap_pct: float,
    prev_close: float = 10_000.0,
    expected_amount: float = 1e12,
    buy_sell_ratio: float = 1.0,
) -> dict:
    ep = prev_close * (1 + gap_pct)
    return {
        "ticker": ticker,
        "expected_price": ep,
        "prev_close": prev_close,
        "gap_pct": gap_pct,
        "expected_amount": expected_amount,
        "buy_sell_ratio": buy_sell_ratio,
        "avg_amount_5d": expected_amount,
    }


async def _run(candidates: list[dict]) -> None:
    with patch("src.notifier.send", new_callable=AsyncMock):
        await run(candidates)


# ── 픽스처 ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_state():
    s = _state_mod.get()
    s.day_skip = False
    s.target_ticker = None
    s.observe_ticker = None
    s.target_candidates = None
    yield
    s.day_skip = False
    s.target_ticker = None
    s.observe_ticker = None
    s.target_candidates = None


# ── VI 근접 후보 유지 ─────────────────────────────────────────────────

async def test_vi_safe_candidate_locked():
    """일반 갭 후보를 타겟으로 확정한다."""
    await _run([_candidate("005930", gap_pct=0.05)])
    assert _state_mod.get().target_ticker == "005930"


async def test_lockup_pins_rank1_as_observe_ticker():
    """처음 잠긴 1순위가 그날의 관측 종목이다 — F3의 후보 교체·소진이
    target_ticker를 바꾸거나 지워도 이 값은 남아 F4가 15:20까지 찍는다."""
    await _run([_candidate("017900", gap_pct=0.07), _candidate("042510", gap_pct=0.04)])
    assert _state_mod.get().observe_ticker == "017900"


async def test_relock_keeps_first_observe_ticker():
    """같은 날 다시 잠가도(F1/F3 재시도) 관측 종목은 처음 것을 유지한다."""
    await _run([_candidate("017900", gap_pct=0.07)])
    await _run([_candidate("042510", gap_pct=0.04)])
    assert _state_mod.get().target_ticker == "042510"
    assert _state_mod.get().observe_ticker == "017900"


async def test_vi_near_candidate_is_kept_for_f3_runtime_check():
    """정적 VI 가격 근접은 F2 탈락 사유가 아니다."""
    await _run([_candidate("VI_NEAR", gap_pct=0.09)])
    s = _state_mod.get()
    assert s.day_skip is False
    assert s.target_ticker == "VI_NEAR"


async def test_hanwha_20260807_candidate_is_kept_without_static_vi_rejection():
    """당시 한화솔루션의 1.487% VI 이격은 F2 후보로 유지한다."""
    cand = {
        "ticker": "009830",
        "name": "한화솔루션",
        "expected_price": 32_950.0,
        "prev_close": 30_400.0,
        "gap_pct": 0.0839,
        "f1_score": 61.4641,
        "expected_amount": 6_323_731_050.0,
        "buy_sell_ratio": 0.0,
    }

    await _run([cand])

    assert _state_mod.get().target_ticker == "009830"


async def test_vi_near_candidates_preserve_ranking():
    """VI 근접 후보끼리도 원래 점수·거래대금 순위를 유지한다."""
    candidates = [
        _candidate("A", gap_pct=0.09, expected_amount=2e12),
        _candidate("B", gap_pct=0.095, expected_amount=1e12),
    ]
    await _run(candidates)
    assert _state_mod.get().target_ticker == "A"


# ── 복합 정렬 ─────────────────────────────────────────────────────────

async def test_sort_picks_highest_expected_amount():
    """expected_amount 높은 종목이 타겟 선정."""
    candidates = [
        _candidate("LOW_AMT",  gap_pct=0.05, expected_amount=1e11),
        _candidate("HIGH_AMT", gap_pct=0.05, expected_amount=1e12),
    ]
    await _run(candidates)
    assert _state_mod.get().target_ticker == "HIGH_AMT"


async def test_locks_up_to_three_targets_for_f3_failover():
    candidates = [
        _candidate("A", gap_pct=0.05, expected_amount=4e11),
        _candidate("B", gap_pct=0.05, expected_amount=3e11),
        _candidate("C", gap_pct=0.05, expected_amount=2e11),
        _candidate("D", gap_pct=0.05, expected_amount=1e11),
    ]

    await _run(candidates)

    s = _state_mod.get()
    assert s.target_ticker == "A"
    assert [c["ticker"] for c in s.target_candidates] == ["A", "B", "C"]


async def test_sort_tiebreak_by_buy_sell_ratio():
    """expected_amount 동일 → buy_sell_ratio 높은 종목 선정."""
    candidates = [
        _candidate("LOW_RATIO",  gap_pct=0.05, expected_amount=1e12, buy_sell_ratio=0.8),
        _candidate("HIGH_RATIO", gap_pct=0.05, expected_amount=1e12, buy_sell_ratio=2.5),
    ]
    await _run(candidates)
    assert _state_mod.get().target_ticker == "HIGH_RATIO"


async def test_f1_score_takes_priority_when_present():
    candidates = [
        {**_candidate("HIGH_AMOUNT", gap_pct=0.05, expected_amount=1e12), "f1_score": 40},
        {**_candidate("HIGH_SCORE", gap_pct=0.05, expected_amount=1e11), "f1_score": 80},
    ]

    await _run(candidates)

    assert _state_mod.get().target_ticker == "HIGH_SCORE"


async def test_sort_does_not_penalize_near_vi_candidate():
    """VI 근접이어도 원래 정렬 1위면 그대로 선택한다."""
    candidates = [
        _candidate("NEAR_VI",  gap_pct=0.09, expected_amount=9e12),
        _candidate("SAFE_LOW",  gap_pct=0.05, expected_amount=1e11),
        _candidate("SAFE_HIGH", gap_pct=0.05, expected_amount=5e11),
    ]
    await _run(candidates)
    assert _state_mod.get().target_ticker == "NEAR_VI"


# ── 엣지 케이스 ───────────────────────────────────────────────────────

async def test_empty_candidates_returns_early():
    """빈 candidates → target_ticker 없음, day_skip 유지."""
    await _run([])
    assert _state_mod.get().target_ticker is None
    assert _state_mod.get().day_skip is False


async def test_day_skip_returns_early():
    """day_skip=True 시 처리 없이 즉시 반환."""
    _state_mod.get().day_skip = True
    await _run([_candidate("SKIP", gap_pct=0.05)])
    # target_ticker 설정되지 않음
    assert _state_mod.get().target_ticker is None


async def test_missing_prev_close_is_deferred_to_f3_recheck():
    """시세 불완전 여부는 최신 데이터를 조회하는 F3가 판단한다."""
    cand = _candidate("BAD", gap_pct=0.05)
    cand["prev_close"] = 0.0
    await _run([cand])
    assert _state_mod.get().target_ticker == "BAD"
