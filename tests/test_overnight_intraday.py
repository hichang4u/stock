"""C1 15:20 병행 기록 비교 (스펙 §6(a)) — 판정에 쓰지 않는 부 지표.

15:20 장중 값으로 같은 규칙을 돌린 후보가 15:32 종가 후보와 얼마나 같은지, 15:20 판
1순위를 15:20 가격에 샀다면 종가 대비 얼마나 비싸거나 쌌는지를 잰다.
"""

import json

from scripts.overnight_sieve import compare_intraday, load_screen_dir


def _row(ticker: str, close: float, rank: int | None = None, reason: str | None = None) -> dict:
    return {"ticker": ticker, "close": close, "rank": rank, "rejected_reason": reason}


def _summary(**kw) -> dict:
    return {"summary": True, **kw}


def _write(path, rows: list[dict], summary: dict | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in rows] + ([json.dumps(summary)] if summary else [])
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def test_same_rank1_counts_overlap_and_entry_gap_against_the_close():
    close = {"20261014": ([_row("000001", 10_000.0, 1), _row("000002", 5_000.0, 2),
                           _row("000003", 2_000.0, None, "CHANGE_PCT")], _summary())}
    intraday = {"20261014": ([_row("000001", 10_100.0, 1), _row("000003", 2_010.0, 2)],
                             _summary(status="OK"))}

    out = compare_intraday(close, intraday)

    assert out["compared"] == 1 and out["rank1_same"] == 1
    (day,) = out["rows"]
    assert day["close_rank1"] == "000001" and day["intraday_rank1"] == "000001"
    assert day["top5_overlap"] == 1                      # 000003은 종가 판에서 탈락
    assert round(day["entry_vs_close_pct"], 2) == 1.00   # 10,100에 사면 종가보다 1% 비쌈
    assert out["entry_vs_close_mean_pct"] == day["entry_vs_close_pct"]


def test_entry_gap_uses_the_close_of_the_intraday_pick_even_if_it_was_rejected_at_close():
    close = {"20261014": ([_row("000001", 10_000.0, 1),
                           _row("000009", 3_000.0, None, "CLOSE_POSITION")], _summary())}
    intraday = {"20261014": ([_row("000009", 2_970.0, 1)], _summary(status="OK"))}

    (day,) = compare_intraday(close, intraday)["rows"]

    assert day["rank1_same"] is False
    assert round(day["entry_vs_close_pct"], 2) == -1.00


def test_intraday_pick_missing_from_the_close_universe_has_no_entry_gap():
    close = {"20261014": ([_row("000001", 10_000.0, 1)], _summary())}
    intraday = {"20261014": ([_row("000777", 4_000.0, 1)], _summary(status="OK"))}

    out = compare_intraday(close, intraday)

    assert out["rows"][0]["entry_vs_close_pct"] is None and out["close_unknown"] == 1
    assert out["entry_vs_close_mean_pct"] is None


def test_days_with_no_candidates_on_both_sides_are_counted_apart():
    close = {"20261014": ([_row("000001", 10_000.0, None, "CHANGE_PCT")], _summary())}
    intraday = {"20261014": ([], _summary(status="OK"))}

    out = compare_intraday(close, intraday)

    assert out["rank1_both_empty"] == 1 and out["rank1_same"] == 0
    assert out["rows"][0]["entry_vs_close_pct"] is None and out["close_unknown"] == 0


def test_missing_and_no_bar_intraday_days_are_counted_from_the_first_intraday_date():
    close = {d: ([_row("000001", 10_000.0, 1)], _summary())
             for d in ("20261010", "20261014", "20261015", "20261016")}
    intraday = {"20261014": ([_row("000001", 10_000.0, 1)], _summary(status="OK")),
                "20261016": ([], _summary(status="NO_TODAY_BAR"))}

    out = compare_intraday(close, intraday)

    assert out["compared"] == 1
    assert out["intraday_missing"] == 1   # 1015. 1010은 병행 기록 시작 전이라 세지 않는다
    assert out["intraday_no_bar"] == 1    # 1016


def test_nothing_recorded_yet_gives_an_empty_comparison():
    out = compare_intraday({"20261014": ([_row("000001", 1.0, 1)], _summary())}, {})
    assert out["compared"] == 0 and out["rows"] == [] and out["intraday_missing"] == 0


def test_load_screen_dir_keeps_rows_and_summary_and_skips_files_without_a_summary(tmp_path):
    d = tmp_path / "candidates_1520"
    _write(d / "20261014.jsonl", [_row("000001", 10.0, 1)], _summary(status="OK"))
    _write(d / "20261015.jsonl", [_row("000001", 10.0, 1)], None)   # 중간에 죽은 파일

    loaded = load_screen_dir(d)

    assert list(loaded) == ["20261014"]
    rows, summary = loaded["20261014"]
    assert rows[0]["ticker"] == "000001" and summary["status"] == "OK"
    assert load_screen_dir(tmp_path / "nope") == {}
