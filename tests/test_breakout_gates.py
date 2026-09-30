"""H5 판정 층 — 합성 표본만 쓴다."""

import math
import statistics

from scripts.breakout_gates import (
    COST,
    Sample,
    block_bootstrap_ci,
    block_bootstrap_lower,
    equity_curve_mdd,
    evaluate,
    recorded_metrics,
    samples_from_trades,
    trimmed_mean,
)
from scripts.breakout_rules import COMPLETE, INCOMPLETE, Outcome, Trade


def sample(entry, net=0.02, excess=0.01, excess_eq=0.0, ticker="000010", exit_="29991231"):
    return Sample(ticker, entry, exit_, 1000.0, 1000.0, net, excess, excess_eq)


def test_block_bootstrap_is_deterministic_and_needs_one_block():
    values = [0.01 * ((i * 7919) % 13 - 6) for i in range(400)]
    first = block_bootstrap_lower(values)
    # Pinned: a change in how random draws are consumed shows up before sealed run.
    assert first == -0.0007999999999999999
    assert first == block_bootstrap_lower(values)
    assert first < statistics.fmean(values)
    assert block_bootstrap_lower([1.0] * 100) == 1.0
    assert block_bootstrap_lower([1.0] * 19) == -math.inf


def test_trimmed_mean_drops_the_top_ceil_one_percent():
    assert trimmed_mean([float(v) for v in range(1, 101)]) == 50.0
    assert trimmed_mean([1.0, 2.0, 3.0]) == 1.5


def test_samples_from_trades_drops_incomplete_and_uncontrolled_and_sorts():
    done_late = Outcome(COMPLETE, "20120110", "20120207", 1000.0, 1100.0, "SCHEDULED")
    done_early = Outcome(COMPLETE, "20120105", "20120202", 1000.0, 900.0, "SCHEDULED")
    trades = [
        Trade("000010", "20120109", done_late, 0.05, 3, 0.02, 10),
        Trade("000020", "20120104", done_early, 0.00, 3, None, 0),
        Trade("000030", "20120104", Outcome(INCOMPLETE, "20120105"), 0.0, 3, 0.0, 10),
        Trade("000040", "20120104", done_early, None, 0, 0.0, 10),
    ]
    samples, dropped = samples_from_trades(trades)

    assert [s.ticker for s in samples] == ["000020", "000010"]
    late = samples[1]
    assert math.isclose(late.net, 0.1 - 0.0035)
    assert math.isclose(late.excess, 0.1 - 0.05)
    assert math.isclose(late.excess_eq, 0.1 - 0.02)
    assert samples[0].excess_eq is None
    assert dropped == {"incomplete": 1, "no_matched_control": 1}


def _even(n_per_period=20, **kw):
    out = []
    for year in ("2012", "2017", "2022"):
        out += [sample(f"{year}01{i + 1:02d}", **kw) for i in range(n_per_period)]
    return out


def test_evaluate_passes_when_every_gate_holds():
    result = evaluate(_even())
    assert result["gates"] == {"G1": True, "G2": True, "G3": True, "G4": True, "G5": True}
    assert result["passed"] is True
    assert result["n"] == 60
    assert math.isclose(result["lower_excess"], 0.01)


def test_evaluate_fails_g4_when_one_period_is_negative():
    samples = _even()
    samples = [s if not s.entry_date.startswith("2017") else sample(s.entry_date, excess=-0.01)
               for s in samples]
    result = evaluate(samples)
    assert result["gates"]["G4"] is False and result["passed"] is False


def test_evaluate_fails_g4_when_a_period_is_empty():
    samples = [s for s in _even() if not s.entry_date.startswith("2022")]
    assert evaluate(samples)["period_excess"]["2021-2026"] is None
    assert evaluate(samples)["gates"]["G4"] is False


def test_evaluate_fails_g5_below_fifty():
    assert evaluate(_even(n_per_period=16))["gates"]["G5"] is False


def test_evaluate_fails_g1_on_a_negative_mean_net():
    assert evaluate(_even(net=-0.001))["gates"]["G1"] is False


def test_evaluate_fails_g3_when_one_outlier_carries_the_mean():
    samples = [sample(f"2012{m:02d}{d:02d}", excess=-0.001)
               for m in range(1, 11) for d in range(1, 11)][:99]
    samples.append(sample("20121231", excess=0.5))
    assert evaluate(samples)["gates"]["G3"] is False


def test_recorded_metrics():
    samples = [sample("20120101", net=0.02, excess=0.01, excess_eq=0.03),
               sample("20130101", net=-0.01, excess=-0.02, excess_eq=-0.01)]
    m = recorded_metrics(samples)
    assert m["win_rate"] == 0.5
    assert math.isclose(m["median_net"], 0.005)
    assert math.isclose(m["control_gap"], 0.015)
    assert sorted(m["yearly"]) == ["2012", "2013"]
    assert m["yearly"]["2012"]["n"] == 1


def test_equity_curve_realizes_cost_on_exit():
    calendar = ["D1", "D2", "D3"]
    s = Sample("000010", "D1", "D3", 1000.0, 1000.0, -COST, 0.0, 0.0)
    got = equity_curve_mdd([s], lambda t, d: 1000.0, calendar)
    assert math.isclose(got["mdd"], COST / 20)
    assert got["excluded_trades"] == 0
    assert got["over_capacity_days"] == 0


def test_equity_curve_marks_open_positions_to_market_with_one_slot_weight():
    calendar = ["D1", "D2", "D3"]
    s = Sample("000010", "D1", "D3", 1000.0, 1000.0, -0.0035, 0.0, 0.0)
    closes = {"D1": 1000.0, "D2": 500.0}
    got = equity_curve_mdd([s], lambda t, d: closes.get(d), calendar)
    assert math.isclose(got["mdd"], 0.5 / 20)


def test_equity_curve_excludes_trades_beyond_twenty_slots():
    calendar = ["D1", "D2", "D3"]
    samples = [Sample(f"{i:06d}", "D1", "D3", 1000.0, 1000.0, 0.0, 0.0, 0.0) for i in range(21)]
    got = equity_curve_mdd(samples, lambda t, d: 1000.0, calendar)
    assert (got["excluded_trades"], got["over_capacity_days"]) == (1, 1)


def test_g2_fails_when_block_bootstrap_ci_lower_is_negative():
    # 120 samples: 40 per period (2012, 2017, 2022), excess in runs of 20
    # at +0.05 then -0.048 (mean +0.001/period). Net constant 0.02.
    samples = []
    for year in ("2012", "2017", "2022"):
        for i in range(20):
            samples.append(
                sample(f"{year}01{i + 1:02d}", net=0.02, excess=0.05)
            )
        for i in range(20, 40):
            samples.append(
                sample(f"{year}01{i + 1:02d}", net=0.02, excess=-0.048)
            )
    result = evaluate(samples)
    assert result["gates"]["G1"] is True
    assert result["gates"]["G2"] is False
    assert result["gates"]["G3"] is True
    assert result["gates"]["G4"] is True
    assert result["gates"]["G5"] is True
    assert result["passed"] is False


def test_g5_boundary_at_fifty():
    # 49 samples (17/17/15) should fail G5
    samples49 = []
    for year, n in [("2012", 17), ("2017", 17), ("2022", 15)]:
        for i in range(n):
            samples49.append(
                sample(f"{year}01{i + 1:02d}", net=0.02, excess=0.01)
            )
    result49 = evaluate(samples49)
    assert result49["gates"]["G5"] is False

    # 50 samples (17/17/16) should pass G5
    samples50 = samples49 + [sample("20220116", net=0.02, excess=0.01)]
    result50 = evaluate(samples50)
    assert result50["gates"]["G5"] is True
    assert result50["gates"]["G1"] is True
    assert result50["gates"]["G2"] is True
    assert result50["gates"]["G3"] is True
    assert result50["gates"]["G4"] is True


def test_g1_fails_at_exactly_zero_mean():
    samples = _even(net=0.0)
    result = evaluate(samples)
    assert result["gates"]["G1"] is False
    assert result["mean_net"] == 0.0


def test_g1_passes_despite_negative_bootstrap_ci_lower():
    # Net follows the excess pattern: runs of 20 at +0.05 then -0.048
    # (mean +0.001/period). Excess constant 0.01.
    samples = []
    for year in ("2012", "2017", "2022"):
        for i in range(20):
            samples.append(
                sample(f"{year}01{i + 1:02d}", net=0.05, excess=0.01)
            )
        for i in range(20, 40):
            samples.append(
                sample(f"{year}01{i + 1:02d}", net=-0.048, excess=0.01)
            )
    result = evaluate(samples)
    assert result["gates"]["G1"] is True
    assert result["gates"]["G2"] is True
    assert result["gates"]["G3"] is True
    assert result["gates"]["G4"] is True
    assert result["gates"]["G5"] is True
    # But bootstrap CI lower for net should be negative
    m = recorded_metrics(samples)
    assert m["lower_net"] < 0


def test_g3_fails_from_net_outlier_only():
    # 100 samples spread over three periods, excess constant 0.01,
    # net 99 x -0.001 plus one 0.5
    samples = []
    dates_per_period = [34, 33, 33]  # Spread over three periods
    cumulative = 0
    for period_idx, (year, n) in enumerate(
        [("2012", dates_per_period[0]), ("2017", dates_per_period[1]),
         ("2022", dates_per_period[2])]
    ):
        for i in range(n):
            day = (i % 28) + 1
            month = (i // 28) + 1
            date_str = f"{year}{month:02d}{day:02d}"
            net_val = 0.5 if (cumulative + i == 99) else -0.001
            samples.append(
                sample(date_str, net=net_val, excess=0.01)
            )
        cumulative += n
    result = evaluate(samples)
    assert result["gates"]["G1"] is True
    assert result["gates"]["G2"] is True
    assert result["gates"]["G3"] is False
    assert result["gates"]["G4"] is True
    assert result["gates"]["G5"] is True
    assert result["passed"] is False


def test_block_bootstrap_ci_edge_cases():
    # Should return (-inf, inf) for 19 values (less than one block)
    lower, upper = block_bootstrap_ci([1.0] * 19)
    assert lower == -math.inf
    assert upper == math.inf

    # Should return (lower, upper) with lower <= upper for valid input
    values = [0.01 * ((i * 7919) % 13 - 6) for i in range(400)]
    lower, upper = block_bootstrap_ci(values)
    assert lower <= upper
    assert lower < statistics.fmean(values)
    assert upper > statistics.fmean(values)

    # Should raise ValueError for non-finite input
    try:
        block_bootstrap_ci([1.0, math.nan, 2.0])
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


def test_evaluate_raises_on_unsorted_entry_dates():
    # Entry dates must be non-decreasing for G2's block structure
    samples = [
        sample("20120115", net=0.02, excess=0.01),
        sample("20120110", net=0.02, excess=0.01),  # Out of order
    ]
    try:
        evaluate(samples)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "entry_date" in str(e).lower()
