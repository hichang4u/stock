"""C1 그림자 성과 어댑터 — 트랙 A와 같은 형식(전략 자본 복리 CAGR·MDD)으로 낸다.

C1 손익은 사전등록 첫 점검일(2026-10-31) 전에는 보지 않는다. 그래서 이 테스트는 합성
데이터만 쓰고, main은 그 날짜 전에 실행을 거부한다.
"""

import json
import sqlite3

import pytest

import scripts.performance_report as pr
from scripts.overnight_sieve import apply_costs as sieve_costs
from scripts.overnight_sieve import run as sieve_run
from scripts.performance import annotate
from scripts.performance_report import (
    C1_FIRST_REVIEW,
    c1_review_allowed,
    c1_trades,
    main,
    same_period,
)
from tests.test_overnight_sieve import _bar, _session, _write_bars, _write_candidates


def _result(date, next_date, gross, reason, rank=1, complete=True, exit_time="091200",
            ticker="000001"):
    return {"date": date, "next_date": next_date, "ticker": ticker, "rank": rank,
            "gross_pct": gross, "exit_reason": reason, "exit_time": exit_time,
            "bars_complete": complete, **sieve_costs(gross, reason)}


def test_primary_rank1_results_become_standard_trades_from_close_to_next_day_exit():
    results = [_result("20261014", "20261015", 2.5, "TRAILING"),
               _result("20261014", "20261015", 1.0, "TRAILING", rank=2),
               _result("20261015", "20261016", -2.0, "HARD_STOP", complete=False),
               _result("20261016", "20261019", 40.0, "SUSPECT_CORPORATE_ACTION")]

    trades = c1_trades(results)

    assert [t["trade_id"] for t in trades] == ["C1-20261014-000001"]
    t = trades[0]
    assert t["entry_at"] == "2026-10-14T15:30:00+09:00"
    assert t["exit_at"] == "2026-10-15T09:12:00+09:00"
    assert (t["gross_pct"], t["exit_reason"], t["track"]) == (2.5, "TRAILING", "C1")


def test_c1_conservative_pnl_matches_the_sieve_judgement_value():
    """같은 자: 성과 보고서의 보수 손익이 C1 판정에 쓰는 슬리피지 차감값과 같아야 한다."""
    r = _result("20261014", "20261015", 2.5, "TRAILING")
    (row,) = annotate(c1_trades([r]), [])
    assert row["net_conservative_pct"] == pytest.approx(r["net_slip_pct"])


def test_same_period_keeps_trades_that_exit_inside_the_window():
    a = [{"trade_id": str(i), "entry_at": f"2026-10-{d}T09:01:00+09:00",
          "exit_at": f"2026-10-{d}T09:30:00+09:00"} for i, d in enumerate(("13", "15", "20"))]
    period = {"start": "2026-10-14", "end": "2026-10-16"}
    assert [t["trade_id"] for t in same_period(a, period)] == ["1"]
    assert same_period(a, None) == []


def test_review_is_blocked_before_the_first_preregistered_check():
    assert C1_FIRST_REVIEW == "20261031"
    assert c1_review_allowed("20261030") is False
    assert c1_review_allowed("20261031") is True


def test_main_refuses_c1_before_the_first_check_without_touching_files(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "_today", lambda: "20261013")
    rc = main(["--track", "C1", "--root", str(tmp_path), "--out-dir", str(tmp_path / "out")])
    assert rc == 3
    assert not (tmp_path / "out").exists()


def test_sieve_run_can_skip_writing_result_files(tmp_path):
    _write_candidates(tmp_path, "20260921", [
        {"date": "20260921", "ticker": "000001", "rank": 1, "close": 100.0}])
    _write_candidates(tmp_path, "20260922", [])
    _write_bars(tmp_path, "20260922", "000001", _session(_bar("090000", 97.0, 99.0, 96.0, 98.0)))

    results, _ = sieve_run(tmp_path, write_results=False)

    assert len(results) == 1
    assert not (tmp_path / "data" / "overnight" / "results").exists()


def test_main_c1_reports_alongside_track_a_in_the_same_period(tmp_path, monkeypatch, capsys):
    _write_candidates(tmp_path, "20260921", [
        {"date": "20260921", "ticker": "000001", "rank": 1, "close": 100.0}])
    _write_candidates(tmp_path, "20260922", [])
    _write_bars(tmp_path, "20260922", "000001", _session(_bar("090000", 97.0, 99.0, 96.0, 98.0)))
    d = tmp_path / "data" / "db"
    d.mkdir(parents=True)
    con = sqlite3.connect(d / "trading.db")
    con.execute("create table trades (id, date, track, ticker, name, entry_price, entry_at, "
                "exit_price, exit_at, close_reason, pnl_pct, pnl_amount, status, "
                "execution_mode, strategy_fingerprint, experiment_id)")
    for i, day in ((1, "20260901"), (2, "20260922")):
        iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        con.execute("insert into trades values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (i, day, "A", "000002", "x", 100.0, f"{iso}T09:01:00+09:00", 101.0,
                     f"{iso}T09:30:00+09:00", "TRAILING", 1.0, 1000.0, "CLOSED", "PAPER",
                     "f", "e"))
    con.commit()
    con.close()
    monkeypatch.setattr(pr, "_today", lambda: "20261031")

    rc = main(["--track", "C1", "--root", str(tmp_path), "--out-dir", str(tmp_path / "out")])

    assert rc == 0
    (run_dir,) = (tmp_path / "out" / "track_c1").iterdir()
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["overall"]["conservative"]["n"] == 1
    assert summary["track_a_same_period"]["conservative"]["n"] == 1   # 0901은 기간 밖
    assert any("그림자" in w for w in summary["warnings"])
    assert any("하루 넘게" in w for w in summary["warnings"])
    assert not (tmp_path / "data" / "overnight" / "results").exists()   # 운영 데이터에 안 쓴다
    assert "같은 기간 A" in capsys.readouterr().out
