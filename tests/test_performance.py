"""공통 성과 계산 — docs/superpowers/specs/2026-10-04-performance-report-design.md."""

import pytest

from scripts.performance import (
    OverlapError,
    apply_costs,
    compute_metrics,
    ordered,
    period_of,
)


def _t(tid, entry, exit_, pct, reason="TRAILING", excluded=None, fp=None):
    """보수 손익이 정확히 pct가 되도록 net_* 를 직접 채운 표준 거래."""
    return {"trade_id": tid, "ticker": "000001", "track": "A",
            "entry_at": f"{entry}T09:01:00+09:00", "exit_at": f"{exit_}T09:30:00+09:00",
            "gross_pct": pct, "exit_reason": reason, "excluded": excluded,
            "meta": {"strategy_fingerprint": fp},
            "net_cost_pct": pct, "net_conservative_pct": pct}


def test_apply_costs_conservative_uses_reason_slippage():
    assert apply_costs(2.0, "TRAILING") == pytest.approx(
        {"net_cost_pct": 1.82, "net_conservative_pct": 1.67})
    assert apply_costs(-2.0, "HARD_STOP")["net_conservative_pct"] == pytest.approx(-2.48)
    assert apply_costs(-3.0, "GAP_HARD_STOP")["net_conservative_pct"] == pytest.approx(-3.48)


def test_apply_costs_unknown_reason_uses_timeout_slippage():
    assert apply_costs(1.0, "SOMETHING")["net_conservative_pct"] == pytest.approx(0.62)


def test_metrics_compound_and_drawdown_from_the_starting_peak():
    trades = [_t("1", "2026-07-01", "2026-07-01", -10.0),
              _t("2", "2026-07-02", "2026-07-02", 20.0),
              _t("3", "2026-07-03", "2026-07-03", -5.0)]
    m = compute_metrics(trades, "net_conservative_pct")
    assert m["n"] == 3
    assert m["total_return"] == pytest.approx(0.9 * 1.2 * 0.95 - 1)
    assert m["mdd"] == pytest.approx(-0.10)   # 시작 1.0이 첫 고점
    assert m["win_rate"] == pytest.approx(1 / 3)
    assert m["mean_pct"] == pytest.approx(5 / 3)
    assert m["median_pct"] == pytest.approx(-5.0)


def test_cagr_is_annualised_and_flagged_under_a_year():
    trades = [_t("1", "2026-01-01", "2026-01-01", 10.0),
              _t("2", "2026-03-31", "2026-03-31", 0.0)]
    m = compute_metrics(trades, "net_conservative_pct")
    days = 90
    assert m["cagr"] == pytest.approx(1.10 ** (365.25 / days) - 1)
    assert m["cagr_reference_only"] is True


def test_cagr_over_a_year_is_not_reference_only():
    trades = [_t("1", "2025-01-01", "2025-01-01", 10.0),
              _t("2", "2026-01-01", "2026-01-01", 0.0)]
    m = compute_metrics(trades, "net_conservative_pct")
    assert m["cagr_reference_only"] is False
    assert m["cagr"] == pytest.approx(1.10 ** (365.25 / 366) - 1)


def test_metrics_with_no_trades_are_none():
    m = compute_metrics([], "net_conservative_pct")
    assert m["n"] == 0 and m["cagr"] is None and m["mdd"] is None


def test_excluded_trades_are_left_out():
    trades = [_t("1", "2026-07-01", "2026-07-01", 5.0),
              _t("2", "2026-07-02", "2026-07-02", -50.0, excluded="MANUAL")]
    assert compute_metrics(trades, "net_conservative_pct")["n"] == 1


def test_overlapping_positions_raise():
    a = _t("1", "2026-07-01", "2026-07-03", 1.0)
    b = _t("2", "2026-07-02", "2026-07-02", 1.0)
    with pytest.raises(OverlapError, match="1"):
        ordered([a, b])


def test_period_counts_calendar_and_trading_days():
    trades = [_t("1", "2026-07-01", "2026-07-01", 1.0),
              _t("2", "2026-07-03", "2026-07-03", 1.0)]
    assert period_of(trades) == {"start": "2026-07-01", "end": "2026-07-03",
                                 "days": 3, "trading_days": 2}
    assert period_of([]) is None
