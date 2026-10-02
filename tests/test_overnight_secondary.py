"""C1 부 지표 (스펙 §4.2) — 비용 민감도, A와 같은 날 상관, 틱 vs 분봉 재생 차이.

판정에 쓰지 않고 기록만 한다(스펙 §4.2). §7의 "2026-09-21 TODO"를 채운다.
"""

import gzip
import json
import sqlite3

import pytest

from scripts.overnight_sieve import (
    a_correlation,
    apply_costs,
    cost_sensitivity,
    load_a_pnl,
    load_tick_prices,
    run,
    simulate_overnight_ticks,
    summarize,
)


def _bar(time_: str, o: float, h: float, lo: float, c: float) -> dict:
    return {"date": "20260922", "time": time_, "open": o, "high": h, "low": lo, "close": c,
            "volume": 100.0}


def _session(first: dict, *rest: dict) -> list[dict]:
    bars = [first, *rest]
    last_min = int(bars[-1]["time"][:2]) * 60 + int(bars[-1]["time"][2:4])
    close = bars[-1]["close"]
    for m in range(last_min + 1, 15 * 60 + 31):
        bars.append(_bar(f"{m // 60:02d}{m % 60:02d}00", close, close, close, close))
    return bars


def _r(date, next_date, gross, reason, rank=1, complete=True):
    return {"date": date, "next_date": next_date, "ticker": "000001", "rank": rank,
            "gross_pct": gross, "exit_reason": reason, "bars_complete": complete,
            "gap_pct": 0.0, **apply_costs(gross, reason)}


# ── 비용 민감도 ──────────────────────────────────────────────────────────

def test_cost_sensitivity_grid_over_rank1_sample():
    results = [_r("20260921", "20260922", 1.0, "TRAILING"),
               _r("20260922", "20260923", -2.0, "HARD_STOP"),
               _r("20260922", "20260923", 5.0, "TRAILING", rank=2)]   # 랭크 2는 빠진다
    grid = cost_sensitivity(results)
    # 비용 0.18, 슬리피지 1.0배: (1.0-0.18-0.15) + (-2.0-0.18-0.30) = -1.81 → 평균 -0.905
    assert grid["cost_0.18_slip_1.0"] == pytest.approx(-0.905)
    # 비용 0.10, 슬리피지 0.5배: (1.0-0.10-0.075) + (-2.0-0.10-0.15) = -1.425 → -0.7125
    assert grid["cost_0.10_slip_0.5"] == pytest.approx(-0.7125)
    assert len(grid) == 9


def test_cost_sensitivity_matches_the_primary_metric_at_base_values():
    results = [_r("20260921", "20260922", 1.0, "TRAILING"),
               _r("20260922", "20260923", -2.0, "HARD_STOP")]
    summary = summarize(results, missing={})
    assert summary["cost_sensitivity"]["cost_0.18_slip_1.0"] == pytest.approx(summary["mean"])


def test_cost_sensitivity_is_empty_without_a_sample():
    assert cost_sensitivity([]) == {}


# ── A와 같은 날 상관 ─────────────────────────────────────────────────────

def _write_trades(root, rows):
    d = root / "data" / "db"
    d.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(d / "trading.db")
    db.execute("create table trades (date text, track text, ticker text, pnl_pct real)")
    db.executemany("insert into trades values (?,?,?,?)", rows)
    db.commit()
    db.close()


def test_load_a_pnl_reads_track_a_by_date(tmp_path):
    _write_trades(tmp_path, [("20260922", "A", "000009", 2.0),
                             ("20260923", "B", "000008", 9.0),
                             ("20260924", "A", "000007", -1.5)])
    assert load_a_pnl(tmp_path) == {"20260922": 2.0, "20260924": -1.5}


def test_load_a_pnl_without_a_database_is_empty(tmp_path):
    assert load_a_pnl(tmp_path) == {}


def test_a_correlation_pairs_c_rank1_on_its_exit_day_with_a_that_day():
    results = [_r("20260921", "20260922", 1.0, "TRAILING"),
               _r("20260922", "20260923", 2.0, "TRAILING"),
               _r("20260923", "20260924", 3.0, "TRAILING"),
               _r("20260924", "20260925", 9.0, "TRAILING")]          # A 거래 없는 날
    a_pnl = {"20260922": 10.0, "20260923": 20.0, "20260924": 30.0}
    out = a_correlation(results, a_pnl)
    assert out["n"] == 3
    assert out["pearson"] == pytest.approx(1.0)


def test_a_correlation_needs_three_pairs():
    results = [_r("20260921", "20260922", 1.0, "TRAILING")]
    assert a_correlation(results, {"20260922": 2.0}) == {"n": 1, "pearson": None}


def test_a_correlation_skips_rank2_incomplete_and_suspect_rows():
    results = [_r("20260921", "20260922", 1.0, "TRAILING", rank=2),
               _r("20260922", "20260923", 1.0, "TRAILING", complete=False),
               _r("20260923", "20260924", 40.0, "SUSPECT_CORPORATE_ACTION")]
    assert a_correlation(results, {"20260922": 1.0, "20260923": 1.0, "20260924": 1.0})["n"] == 0


# ── 틱 읽기 ──────────────────────────────────────────────────────────────

def _rec(hms: str, price: int, ticker="000001") -> list[str]:
    return [ticker, hms, str(price)] + ["0"] * 43 + ["2"]


def _write_ticks(root, date, ticker, rows, hour="09"):
    d = root / "data" / "strategy_ticks" / date
    d.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r) for r in rows) + "\n"
    (d / f"{ticker}.{hour}.jsonl.gz").write_bytes(gzip.compress(body.encode("utf-8")))


def test_load_tick_prices_expands_old_multi_record_frames(tmp_path):
    _write_ticks(tmp_path, "20260922", "000001", [
        {"source": "ws", "received_at": "2026-09-22T09:01:00.5+09:00", "price": 100.0,
         "raw": _rec("090100", 100) + _rec("090100", 101) + _rec("090101", 99)},
    ])
    assert load_tick_prices(tmp_path, "20260922", "000001") == [
        ("090100", 100.0), ("090100", 101.0), ("090101", 99.0)]


def test_load_tick_prices_reads_schema3_rows_and_rest_backup(tmp_path):
    _write_ticks(tmp_path, "20261002", "000001", [
        {"source": "ws", "received_at": "2026-10-02T09:01:00.5+09:00", "price": 100.0,
         "raw": _rec("090100", 100), "frame_count": 2, "frame_size": 2, "frame_index": 0},
        {"source": "ws", "received_at": "2026-10-02T09:01:00.5+09:00", "price": 102.0,
         "raw": _rec("090100", 102), "frame_count": 2, "frame_size": 2, "frame_index": 1},
        {"source": "rest", "received_at": "2026-10-02T09:01:05.2+09:00", "price": 98.0,
         "raw": None},
    ])
    assert load_tick_prices(tmp_path, "20261002", "000001") == [
        ("090100", 100.0), ("090100", 102.0), ("090105", 98.0)]


def test_load_tick_prices_without_capture_is_none(tmp_path):
    assert load_tick_prices(tmp_path, "20261002", "000001") is None


# ── 틱 재생 ──────────────────────────────────────────────────────────────

def test_tick_replay_uses_bars_before_the_first_tick_then_ticks():
    bars = _session(_bar("090000", 101.0, 101.5, 100.8, 101.2),
                    _bar("090100", 101.2, 101.4, 101.0, 101.3))
    # 틱은 09:02부터. 09:02:10에 103(스텝 2.5%), 09:02:30에 100.4(스탑 100.5 이탈)
    ticks = [("090205", 101.5), ("090210", 103.0), ("090230", 100.4)]
    r = simulate_overnight_ticks(bars, ticks, entry_price=100.0)
    assert r["exit_reason"] == "TRAILING" and r["exit_time"] == "090230"
    assert round(r["gross_pct"], 2) == 0.5


def test_tick_replay_keeps_the_gap_hard_stop_at_the_open():
    bars = _session(_bar("090000", 97.0, 99.0, 96.0, 98.0))
    r = simulate_overnight_ticks(bars, [("090105", 99.0)], entry_price=100.0)
    assert r["exit_reason"] == "GAP_HARD_STOP" and r["gross_pct"] == -3.0


# ── run / summarize 연결 ─────────────────────────────────────────────────

def _write_candidates(root, date, rows):
    d = root / "data" / "overnight" / "candidates"
    d.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in rows] + [json.dumps({"summary": True, "date": date})]
    (d / f"{date}.jsonl").write_text("\n".join(lines), encoding="utf-8")


def _write_bars(root, date, ticker, bars):
    d = root / "data" / "backtest_bars"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{date}_{ticker}.json").write_text(json.dumps(bars), encoding="utf-8")


def test_run_attaches_a_tick_replay_when_the_capture_exists(tmp_path):
    _write_candidates(tmp_path, "20260921", [
        {"date": "20260921", "ticker": "000001", "rank": 1, "close": 100.0}])
    # 분봉: 09:01 고가 103, 09:02 저가 100.4 → 분봉은 09:02 트레일
    _write_bars(tmp_path, "20260922", "000001", _session(
        _bar("090000", 101.0, 101.5, 100.8, 101.2),
        _bar("090100", 101.2, 103.0, 101.0, 102.8),
        _bar("090200", 102.8, 103.0, 100.4, 100.6)))
    # 틱은 09:01부터: 고점 103을 보지만 저점은 101.0까지만 → 틱은 트레일에 안 걸린다
    _write_ticks(tmp_path, "20260922", "000001", [
        {"source": "ws", "received_at": "2026-09-22T09:01:10+09:00", "price": 103.0,
         "raw": _rec("090110", 103)},
        {"source": "ws", "received_at": "2026-09-22T09:02:10+09:00", "price": 101.0,
         "raw": _rec("090210", 101)},
    ])
    results, _ = run(tmp_path)
    tick = results[0]["tick"]
    assert results[0]["exit_reason"] == "TRAILING"
    assert tick["exit_reason"] != "TRAILING" or tick["exit_time"] != results[0]["exit_time"]
    s = summarize(results, missing={})
    assert s["tick_vs_bar"]["n"] == 1
    assert s["tick_vs_bar"]["mean_diff_pct"] == pytest.approx(
        tick["gross_pct"] - results[0]["gross_pct"])


def test_run_without_capture_has_no_tick_field_and_summary_counts_zero(tmp_path):
    _write_candidates(tmp_path, "20260921", [
        {"date": "20260921", "ticker": "000001", "rank": 1, "close": 100.0}])
    _write_bars(tmp_path, "20260922", "000001",
                _session(_bar("090000", 97.0, 99.0, 96.0, 98.0)))
    results, _ = run(tmp_path)
    assert "tick" not in results[0]
    assert summarize(results, missing={})["tick_vs_bar"] == {"n": 0, "mean_diff_pct": None,
                                                              "reason_agree": 0, "rows": []}
