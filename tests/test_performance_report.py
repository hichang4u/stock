"""트랙 A 성과 리포트 어댑터 — 운영 DB를 읽기 전용으로 읽어 표준 거래로 바꾼다."""

import json
import sqlite3

import pytest

from scripts.performance_report import (
    ACCOUNT_CAPITAL_KRW,
    TRACK_A_SEGMENTS,
    account_reference,
    load_track_a,
    main,
)

COLS = ("id, date, track, ticker, name, entry_price, entry_qty, entry_at, exit_price, "
        "exit_qty, exit_at, close_reason, pnl_pct, pnl_amount, status, execution_mode, "
        "strategy_fingerprint, experiment_id")


def _db(root, rows):
    d = root / "data" / "db"
    d.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(d / "trading.db")
    con.execute(f"create table trades ({COLS})")
    con.executemany(f"insert into trades ({COLS}) values ({','.join('?' * 18)})", rows)
    con.commit()
    con.close()


def _row(i, day, pnl, reason="TRAILING", amount=1000.0, status="CLOSED", track="A",
         fp="f1", entry=100.0, exit_=101.0):
    iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    return (i, day, track, "000001", "x", entry, 10, f"{iso}T09:01:00+09:00", exit_, 10,
            f"{iso}T09:30:00+09:00", reason, pnl, amount, status, "PAPER", fp, "e")


def test_load_track_a_maps_rows_and_excludes_manual_open_and_other_tracks(tmp_path):
    _db(tmp_path, [_row(1, "20260702", 2.0),
                   _row(2, "20260708", -1.7, reason="MANUAL"),
                   _row(3, "20260709", 5.0, status="HOLDING"),
                   _row(4, "20260710", 3.0, track="B"),
                   _row(5, "20260711", None, entry=100.0, exit_=103.0)])
    trades, source = load_track_a(tmp_path)
    assert [t["trade_id"] for t in trades] == ["1", "2", "5"]
    assert trades[0]["gross_pct"] == 2.0 and trades[0]["excluded"] is None
    assert trades[0]["meta"]["strategy_fingerprint"] == "f1"
    assert trades[1]["excluded"] == "MANUAL"
    assert trades[2]["gross_pct"] == pytest.approx(3.0)   # pnl_pct 없음 → 가격으로
    assert source["rows_read"] == 3 and source["rows_used"] == 2


def test_load_track_a_marks_no_pnl_and_respects_until(tmp_path):
    _db(tmp_path, [_row(1, "20260702", None, entry=None, exit_=None),
                   _row(2, "20260801", 1.0)])
    trades, _ = load_track_a(tmp_path, until="20260731")
    assert [t["excluded"] for t in trades] == ["NO_PNL"]


def test_load_track_a_without_db_fails_clearly(tmp_path):
    with pytest.raises(FileNotFoundError, match="trading.db"):
        load_track_a(tmp_path)


def test_account_reference_uses_krw_and_capital():
    trades = [{"exit_at": "2026-07-02T09:30:00+09:00", "excluded": None,
               "meta": {"pnl_amount": 1_000_000.0}},
              {"exit_at": "2026-07-03T09:30:00+09:00", "excluded": None,
               "meta": {"pnl_amount": -3_000_000.0}},
              {"exit_at": "2026-07-04T09:30:00+09:00", "excluded": "MANUAL",
               "meta": {"pnl_amount": -9_000_000.0}}]
    ref = account_reference(trades)
    assert ref["total_pnl_krw"] == -2_000_000.0
    assert ref["mdd_krw"] == -3_000_000.0
    assert ref["mdd"] == pytest.approx(-3_000_000 / ACCOUNT_CAPITAL_KRW)
    assert ref["capital_krw"] == ACCOUNT_CAPITAL_KRW


def test_segments_cover_from_the_first_trade_with_an_open_end():
    assert TRACK_A_SEGMENTS[0]["start"] == "20260702"
    assert TRACK_A_SEGMENTS[-1]["end"] is None
    for a, b in zip(TRACK_A_SEGMENTS, TRACK_A_SEGMENTS[1:]):
        assert a["end"] < b["start"]


def test_main_writes_a_run_and_returns_zero(tmp_path, capsys):
    _db(tmp_path, [_row(1, "20260702", 2.0), _row(2, "20261002", -1.0, reason="HARD_STOP")])
    out = tmp_path / "out"
    assert main(["--track", "A", "--root", str(tmp_path), "--out-dir", str(out)]) == 0
    runs = list((out / "track_a").iterdir())
    assert len(runs) == 1
    summary = json.loads((runs[0] / "summary.json").read_text("utf-8"))
    assert summary["account_reference"]["capital_krw"] == ACCOUNT_CAPITAL_KRW
    assert any(s["name"] == "파서 수정 후" and s["multiple_changes"] for s in summary["segments"])
    printed = capsys.readouterr().out
    assert "CAGR" in printed and "MDD" in printed and "(참고)" in printed


def test_main_returns_two_when_nothing_is_countable(tmp_path):
    _db(tmp_path, [_row(1, "20260708", -1.7, reason="MANUAL")])
    assert main(["--track", "A", "--root", str(tmp_path),
                 "--out-dir", str(tmp_path / "out")]) == 2


def test_main_survives_a_cp949_console(tmp_path, monkeypatch):
    """운영 PC 콘솔은 cp949다. 경고 문구의 em dash(—) 때문에 죽으면 안 된다."""
    import io
    import sys

    _db(tmp_path, [_row(1, "20260702", 2.0)])
    console = io.TextIOWrapper(io.BytesIO(), encoding="cp949")
    monkeypatch.setattr(sys, "stdout", console)
    assert main(["--track", "A", "--root", str(tmp_path),
                 "--out-dir", str(tmp_path / "out")]) == 0


def test_report_prints_a_dash_for_a_suppressed_cagr(tmp_path, capsys):
    _db(tmp_path, [_row(1, "20261002", 2.0)])
    main(["--track", "A", "--root", str(tmp_path), "--out-dir", str(tmp_path / "out")])
    printed = capsys.readouterr().out
    assert "CAGR — (30일 미만)" in printed
