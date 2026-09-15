from src.modules import f1_selector


def test_default_policy_thresholds_are_intentional():
    assert f1_selector.GAP_MIN == 0.025
    assert f1_selector.GAP_CORE_MAX == 0.080
    assert f1_selector.GAP_HARD_MAX == 0.100
    assert f1_selector.MIN_EXPECTED_AMOUNT == 100_000_000
    assert f1_selector.HIGH_GAP_MIN_EXPECTED_AMOUNT == 1_000_000_000
    assert not hasattr(f1_selector, "MIN_VI_GAP")
    assert not hasattr(f1_selector, "SAFE_VI_GAP")
    assert f1_selector.BUY_PRESSURE_WEIGHT == 0
    assert f1_selector.SCORE_TARGET_MAX == 85.0


def _candidate(
    ticker: str,
    expected_amount: float,
    avg_amount_5d: float,
    gap_pct: float = 0.05,
    vi_gap: float = 0.04,
    buy_sell_ratio: float = 1.0,
) -> dict:
    return {
        "ticker": ticker,
        "gap_allowed": True,
        "gap_pct": gap_pct,
        "expected_amount": expected_amount,
        "avg_amount_5d": avg_amount_5d,
        "vi_gap": vi_gap,
        "buy_sell_ratio": buy_sell_ratio,
    }


def test_volume_surge_can_outrank_larger_absolute_amount():
    ranked = f1_selector.select_candidates([
        _candidate("BIG_NORMAL", expected_amount=5_000_000_000, avg_amount_5d=5_000_000_000),
        _candidate("SURGE", expected_amount=1_000_000_000, avg_amount_5d=100_000_000),
    ])

    assert ranked[0]["ticker"] == "SURGE"
    assert ranked[0]["volume_surge"] == 10
    assert ranked[0]["f1_score"] > ranked[1]["f1_score"]


def test_high_gap_requires_amount_but_not_static_vi_distance():
    ranked = f1_selector.select_candidates([
        _candidate(
            "LOW_AMOUNT", expected_amount=800_000_000,
            avg_amount_5d=100_000_000, gap_pct=0.085,
        ),
        _candidate(
            "VI_NEAR", expected_amount=6_000_000_000, avg_amount_5d=100_000_000,
            gap_pct=0.085, vi_gap=0.005,
        ),
    ])
    assert [candidate["ticker"] for candidate in ranked] == ["VI_NEAR"]


def test_empty_input_returns_empty_selection():
    assert f1_selector.select_candidates([]) == []


def test_illiquid_candidates_rejected_by_default_floor():
    """2026-07-20 인시던트: 예상 체결대금 수백만원짜리 후보(멤레이비티 3.2M,
    스타코링크 1.9M)가 통과해 미체결·유동성 제로 진입 시도로 이어졌다.
    저유동성 후보는 기본 하한(1억원)으로 걸러져야 한다."""
    ranked = f1_selector.select_candidates([
        _candidate("ILLIQUID", expected_amount=38_700_000, avg_amount_5d=10_000_000),
        _candidate("LIQUID", expected_amount=150_000_000, avg_amount_5d=100_000_000),
    ])

    assert [c["ticker"] for c in ranked] == ["LIQUID"]


def test_invalid_gap_is_rejected_but_vi_near_candidate_is_kept():
    ranked = f1_selector.select_candidates([
        _candidate(
            "LOW_GAP", expected_amount=10_000_000_000,
            avg_amount_5d=100_000_000, gap_pct=0.02,
        ),
        _candidate(
            "VI_TOO_NEAR", expected_amount=10_000_000_000,
            avg_amount_5d=100_000_000, vi_gap=0.005,
        ),
    ])

    assert [candidate["ticker"] for candidate in ranked] == ["VI_TOO_NEAR"]

def test_amount_score_is_never_negative():
    assert f1_selector._score_amount(100_000_000) == 0.0


def test_gap_score_uses_configured_core_max(monkeypatch):
    monkeypatch.setattr(f1_selector, "GAP_MIN", 0.030)
    monkeypatch.setattr(f1_selector, "GAP_CORE_MAX", 0.080)
    monkeypatch.setattr(f1_selector, "GAP_HARD_MAX", 0.100)

    assert f1_selector._score_gap(0.030) == 0.0
    assert f1_selector._score_gap(0.080) == 1.0
    assert 0.0 < f1_selector._score_gap(0.055) < 1.0


def test_removed_vi_score_is_renormalized_to_legacy_maximum():
    candidate = _candidate(
        "MAX_SCORE",
        expected_amount=30_000_000_000,
        avg_amount_5d=3_000_000_000,
        gap_pct=f1_selector.GAP_CORE_MAX,
    )

    assert f1_selector.f1_score(candidate) == 85.0
