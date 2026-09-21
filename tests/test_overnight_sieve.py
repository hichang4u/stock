"""C1 오버나이트 체 — 순수 함수만 검사한다. 파일·DB는 읽지 않는다."""

import json

from scripts.overnight_sieve import (
    BASE_ROUND_TRIP_COST_PCT,
    EARLY_STOP_N,
    HARD_STOP_SLIPPAGE_PCT,
    MIN_N,
    TRAILING_SLIPPAGE_PCT,
    apply_costs,
    load_candidates,
    run,
    simulate_overnight,
    summarize,
)


def _bar(time_: str, o: float, h: float, lo: float, c: float) -> dict:
    return {"date": "20260922", "time": time_, "open": o, "high": h, "low": lo, "close": c,
            "volume": 100.0}


def _session(first: dict, *rest: dict) -> list[dict]:
    """첫 봉 + 나머지 + 15:15 타임아웃 봉까지 채워 covers_session을 만족시킨다."""
    bars = [first, *rest]
    last_min = int(bars[-1]["time"][:2]) * 60 + int(bars[-1]["time"][2:4])
    close = bars[-1]["close"]
    for m in range(last_min + 1, 15 * 60 + 31):
        bars.append(_bar(f"{m // 60:02d}{m % 60:02d}00", close, close, close, close))
    return bars


def test_gap_down_beyond_hard_stop_exits_at_the_open():
    bars = _session(_bar("090000", 97.0, 99.0, 96.0, 98.0))
    r = simulate_overnight(bars, entry_price=100.0)
    assert r["exit_reason"] == "GAP_HARD_STOP" and r["exit_time"] == "090000"
    assert r["exit_price"] == 97.0 and r["gross_pct"] == -3.0
    assert r["gap_pct"] == -3.0 and r["bars_complete"] is True


def test_gap_up_then_trailing_uses_track_b_rule_low_first():
    # 시가 +1%, 09:01 고가 103(스텝 2.5%), 09:02 저가 100.4(스탑 100.5 이탈)
    bars = _session(
        _bar("090000", 101.0, 101.5, 100.8, 101.2),
        _bar("090100", 101.2, 103.0, 101.0, 102.8),
        _bar("090200", 102.8, 103.0, 100.4, 100.6),
    )
    r = simulate_overnight(bars, entry_price=100.0)
    assert r["gap_pct"] == 1.0
    assert r["exit_reason"] == "TRAILING" and r["exit_time"] == "090200"
    assert round(r["exit_price"], 2) == 100.5 and round(r["gross_pct"], 2) == 0.5


def test_hard_stop_inside_the_day_and_timeout():
    stopped = simulate_overnight(
        _session(_bar("090000", 100.5, 100.8, 97.9, 98.0)), entry_price=100.0
    )
    assert stopped["exit_reason"] == "HARD_STOP" and stopped["gross_pct"] == -2.0

    flat = simulate_overnight(_session(_bar("090000", 100.5, 100.8, 100.2, 100.5)),
                              entry_price=100.0)
    assert flat["exit_reason"] == "TIMEOUT" and flat["exit_time"] == "151500"


def test_incomplete_session_is_flagged_and_ends_with_data_end():
    bars = [_bar("090000", 100.5, 100.8, 100.2, 100.5), _bar("090100", 100.5, 100.6, 100.4, 100.5)]
    r = simulate_overnight(bars, entry_price=100.0)
    assert r["bars_complete"] is False and r["exit_reason"] == "DATA_END"


def test_costs_follow_the_improvement_plan_constants():
    assert (BASE_ROUND_TRIP_COST_PCT, HARD_STOP_SLIPPAGE_PCT, TRAILING_SLIPPAGE_PCT) == (
        0.18, 0.30, 0.15
    )
    c = apply_costs(2.5, "TRAILING")
    assert round(c["net_cost_pct"], 2) == 2.32 and round(c["net_slip_pct"], 2) == 2.17
    g = apply_costs(-3.0, "GAP_HARD_STOP")
    assert round(g["net_slip_pct"], 2) == -3.48   # 시가 하드스탑도 하드스탑 슬리피지
    t = apply_costs(0.0, "TIMEOUT")
    assert round(t["net_slip_pct"], 2) == -0.38


def _result(date: str, pct: float, rank: int = 1, reason: str = "TRAILING", gap: float = 0.5):
    return {"date": date, "rank": rank, "net_slip_pct": pct, "exit_reason": reason,
            "gap_pct": gap, "bars_complete": True}


def test_summarize_uses_rank1_only_for_the_primary_metric_and_counts_reasons():
    results = [_result("20260901", 1.0), _result("20260901", 9.0, rank=2),
               _result("20260902", -2.0, reason="HARD_STOP", gap=-1.0),
               _result("20260903", 3.0)]
    s = summarize(results, missing={"screen_failed": 1, "degraded": 0, "bars_incomplete": 0,
                                    "no_candidate": 2})
    assert s["n"] == 3
    assert round(s["mean"], 4) == round((1.0 - 2.0 + 3.0) / 3, 4)
    assert s["reasons"] == {"TRAILING": 2, "HARD_STOP": 1}
    assert s["rank_means"][2] == 9.0
    assert s["gap"]["share_positive"] == 2 / 3
    assert s["missing"]["screen_failed"] == 1 and s["missing"]["no_candidate"] == 2
    assert s["pass_conditions"]["evaluable"] is False  # n < 50


def test_summarize_top2_removed_and_drawdown():
    pcts = [5.0, 4.0] + [-0.5] * 10
    results = [_result(f"202609{i + 1:02d}", p) for i, p in enumerate(pcts)]
    s = summarize(results, missing={})
    assert round(s["mean_top2_removed"], 2) == -0.5
    assert s["max_drawdown_pct"] == -5.0          # 9.0 정점 뒤 -0.5×10
    assert s["pass_conditions"]["robust_top2"] is False


def test_early_stop_evaluates_only_at_thirty_and_triggers_on_mean():
    results = [_result(f"202609{i + 1:02d}", -1.5) for i in range(EARLY_STOP_N)]
    s = summarize(results, missing={})
    assert s["early_stop"] == {"evaluable": True, "triggered": True, "reason": "MEAN"}
    fewer = summarize(results[:-1], missing={})
    assert fewer["early_stop"]["evaluable"] is False


def test_pass_requires_all_three_conditions_at_min_n():
    results = [_result(f"2026{9 + i // 28:02d}{1 + i % 28:02d}", 1.0 + (i % 3) * 0.1)
               for i in range(MIN_N)]
    s = summarize(results, missing={})
    assert s["pass_conditions"]["evaluable"] is True
    assert s["pass_conditions"]["mean_positive"] and s["pass_conditions"]["ci_low_positive"]
    assert s["pass_conditions"]["robust_top2"] and s["pass_conditions"]["all"]


def _write_candidates(root, date, rows, summary=True):
    d = root / "data" / "overnight" / "candidates"
    d.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in rows]
    if summary:
        lines.append(json.dumps({"summary": True, "date": date, "candidates": len(rows)}))
    (d / f"{date}.jsonl").write_text("\n".join(lines), encoding="utf-8")


def _write_bars(root, date, ticker, bars):
    d = root / "data" / "backtest_bars"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{date}_{ticker}.json").write_text(json.dumps(bars), encoding="utf-8")


def test_load_candidates_keeps_ranked_rows_and_flags_dead_files(tmp_path):
    _write_candidates(tmp_path, "20260921", [
        {"date": "20260921", "ticker": "000001", "rank": 1, "close": 100.0},
        {"date": "20260921", "ticker": "000009", "rank": None, "rejected_reason": "X"},
    ])
    _write_candidates(tmp_path, "20260922", [{"date": "20260922", "ticker": "000002",
                                              "rank": 1, "close": 50.0}], summary=False)
    loaded, missing = load_candidates(tmp_path / "data" / "overnight" / "candidates")
    assert [r["ticker"] for r in loaded["20260921"]] == ["000001"]
    assert "20260922" not in loaded and missing["screen_died"] == 1


def test_run_replays_next_day_bars_and_accounts_for_missing(tmp_path):
    _write_candidates(tmp_path, "20260921", [
        {"date": "20260921", "ticker": "000001", "rank": 1, "close": 100.0},
        {"date": "20260921", "ticker": "000002", "rank": 2, "close": 100.0},   # 분봉 없음
    ])
    _write_candidates(tmp_path, "20260922", [])                                 # 후보 0
    _write_bars(tmp_path, "20260922", "000001",
                _session(_bar("090000", 97.0, 99.0, 96.0, 98.0)))
    results, missing = run(tmp_path)
    assert len(results) == 1
    r = results[0]
    assert (r["date"], r["next_date"], r["ticker"], r["rank"]) == ("20260921", "20260922",
                                                                   "000001", 1)
    assert r["exit_reason"] == "GAP_HARD_STOP" and round(r["net_slip_pct"], 2) == -3.48
    assert missing["bars_missing"] == 1 and missing["no_candidate"] == 1
    saved = json.loads((tmp_path / "data" / "overnight" / "results" / "20260921.json")
                       .read_text(encoding="utf-8"))
    assert saved[0]["ticker"] == "000001"
