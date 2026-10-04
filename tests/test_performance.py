"""공통 성과 계산 — docs/superpowers/specs/2026-10-04-performance-report-design.md."""

from datetime import date

import pytest

from scripts.performance import (
    OverlapError,
    annotate,
    apply_costs,
    compute_metrics,
    ordered,
    period_of,
    segment_of,
    summarize,
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


# ── 구간·연도 요약 ───────────────────────────────────────────────────────

SEGMENTS = [
    {"name": "S1", "start": "20260701", "end": "20260731", "changed": ["시작"]},
    {"name": "S2", "start": "20260801", "end": None, "changed": ["A 변경", "B 변경"]},
]


def _raw(tid, day, gross, reason="TRAILING", excluded=None, fp=None):
    return {"trade_id": tid, "ticker": "000001", "track": "A",
            "entry_at": f"{day}T09:01:00+09:00", "exit_at": f"{day}T09:30:00+09:00",
            "gross_pct": gross, "exit_reason": reason, "excluded": excluded,
            "meta": {"strategy_fingerprint": fp}}


def test_segment_of_uses_inclusive_bounds_and_open_end():
    assert segment_of(date(2026, 7, 31), SEGMENTS) == "S1"
    assert segment_of(date(2026, 8, 1), SEGMENTS) == "S2"
    assert segment_of(date(2026, 6, 30), SEGMENTS) is None


def test_annotate_adds_costs_segment_and_year_but_no_costs_for_excluded():
    rows = annotate([_raw("1", "2026-07-02", 2.0),
                     _raw("2", "2026-07-03", None, reason="MANUAL", excluded="MANUAL")],
                    SEGMENTS)
    assert rows[0]["net_conservative_pct"] == pytest.approx(1.67)
    assert (rows[0]["segment"], rows[0]["year"]) == ("S1", 2026)
    assert "net_conservative_pct" not in rows[1] and rows[1]["segment"] == "S1"


def test_summarize_restarts_equity_per_segment_and_year():
    rows = annotate([_raw("1", "2026-07-02", 10.0 + 0.18 + 0.15, fp="f1"),   # 보수 +10%
                     _raw("2", "2026-08-03", -10.0 + 0.18 + 0.15, fp="f2")],  # 보수 -10%
                    SEGMENTS)
    s = summarize(rows, SEGMENTS)
    s1, s2 = s["segments"]
    assert s1["conservative"]["total_return"] == pytest.approx(0.10)
    assert s2["conservative"]["total_return"] == pytest.approx(-0.10)
    assert s2["conservative"]["mdd"] == pytest.approx(-0.10)   # 구간 시작 1.0에서 다시
    assert s["overall"]["conservative"]["total_return"] == pytest.approx(1.1 * 0.9 - 1)
    assert s["years"][0]["year"] == 2026 and s["years"][0]["conservative"]["n"] == 2
    assert s["overall"]["period"]["start"] == "2026-07-02"


def test_multiple_fingerprints_or_changes_flag_the_segment():
    rows = annotate([_raw("1", "2026-07-02", 1.0, fp="f1"),
                     _raw("2", "2026-07-03", 1.0, fp="f2"),
                     _raw("3", "2026-08-03", 1.0, fp="f3")], SEGMENTS)
    s1, s2 = summarize(rows, SEGMENTS)["segments"]
    assert s1["multiple_changes"] is True and s1["fingerprints"] == ["f1", "f2"]
    assert s2["multiple_changes"] is True and s2["fingerprints"] == ["f3"]   # changed 2개


def test_missing_fingerprints_are_not_counted():
    one = [{"name": "S1", "start": "20260701", "end": None, "changed": ["x"]}]
    rows = annotate([_raw("1", "2026-07-02", 1.0), _raw("2", "2026-07-03", 1.0)], one)
    seg = summarize(rows, one)["segments"][0]
    assert seg["fingerprints"] == [] and seg["multiple_changes"] is False


def test_summarize_counts_exclusions_unknown_reasons_and_warns():
    rows = annotate([_raw("1", "2026-07-02", 1.0, reason="WEIRD"),
                     _raw("2", "2026-07-03", None, reason="MANUAL", excluded="MANUAL")],
                    SEGMENTS)
    s = summarize(rows, SEGMENTS, holding_overnight=True, extra_warnings=("추가 경고",))
    assert s["basis"] == "strategy_capital" and s["headline"] == "conservative"
    assert s["excluded"] == {"MANUAL": 1}
    assert s["unknown_reasons"] == {"WEIRD": 1}
    text = " ".join(s["warnings"])
    assert "1년 미만" in text and "보유 중 평가손" in text and "추가 경고" in text
    assert "복수 변경" in text   # S2의 changed 2개
