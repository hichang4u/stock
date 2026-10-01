"""WS 체결 프레임 점검 — 캡처 raw로 레코드 길이와 파서 손실을 잰다.

2026-09-14 KIS가 H0STCNT0 레코드에 필드를 하나 더했고(46 → 47), 파서는 46을 가정해
다건 프레임의 첫 체결만 썼다. 11거래일 동안 경보가 없었다. 이 점검은 그 두 가지
(필드 수 변화, 파서가 버린 체결)를 캡처에서 매일 확인한다.
"""

import gzip
import json
from pathlib import Path

from scripts.ws_frame_check import check_day, infer_record_length, main


def _rec(ticker: str, hms: str, price: int = 1000, n: int = 47) -> list[str]:
    return [ticker, hms, str(price)] + ["0"] * (n - 4) + ["2"]


def _frame(*records: list[str]) -> dict:
    raw = [v for r in records for v in r]
    return {"source": "ws", "received_at": "2026-10-01T09:00:00+09:00",
            "price": float(raw[2]), "raw": raw}


def _write(root: Path, date: str, ticker: str, frames: list[dict], hour: str = "09") -> Path:
    d = root / "data" / "strategy_ticks" / date
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{ticker}.{hour}.jsonl.gz"
    body = "\n".join(json.dumps(f, ensure_ascii=False) for f in frames) + "\n"
    path.write_bytes(gzip.compress(body.encode("utf-8")))
    return path


def _split47(body: str) -> list[list[str]]:
    values = body.split("^")
    return [values[i:i + 47] for i in range(0, len(values), 47)]


def _first_only(body: str) -> list[list[str]]:
    return [body.split("^")]


def test_infer_record_length_of_two_47_field_records():
    raw = _rec("005930", "090001") + _rec("005930", "090002")
    assert infer_record_length(raw, "005930") == 47


def test_infer_record_length_of_single_46_field_record():
    assert infer_record_length(_rec("005930", "090001", n=46), "005930") == 46


def test_infer_record_length_rejects_records_that_do_not_start_with_the_ticker():
    raw = _rec("005930", "090001") + _rec("000660", "090002")
    assert infer_record_length(raw, "005930") is None


def test_check_day_counts_trades_the_parser_dropped(tmp_path):
    _write(tmp_path, "20261001", "005930", [
        _frame(_rec("005930", "090001"), _rec("005930", "090001"), _rec("005930", "090002")),
        _frame(_rec("005930", "090003")),
    ])
    out = check_day(tmp_path, "20261001", split=_first_only)
    t = out["tickers"]["005930"]
    assert (t["frames"], t["records"], t["multi_frames"], t["parser_records"]) == (2, 4, 1, 2)
    assert "PARSER_DROPS" in out["issues"]


def test_check_day_is_clean_when_the_parser_splits_every_record(tmp_path):
    _write(tmp_path, "20261001", "005930", [
        _frame(_rec("005930", "090001"), _rec("005930", "090002")),
    ])
    out = check_day(tmp_path, "20261001", split=_split47)
    assert out["issues"] == []
    assert out["tickers"]["005930"]["record_lengths"] == {"47": 1}


def test_check_day_flags_a_field_count_change(tmp_path):
    _write(tmp_path, "20261001", "005930", [
        _frame(_rec("005930", "090001", n=48), _rec("005930", "090002", n=48)),
    ])
    out = check_day(tmp_path, "20261001", split=_split47)
    assert "FIELD_COUNT_CHANGED" in out["issues"]
    assert out["tickers"]["005930"]["record_lengths"] == {"48": 1}


def test_check_day_flags_frames_it_cannot_align(tmp_path):
    _write(tmp_path, "20261001", "005930", [
        _frame(_rec("005930", "090001"), _rec("000660", "090002")),
    ])
    out = check_day(tmp_path, "20261001", split=_split47)
    assert "UNALIGNED" in out["issues"]
    assert out["tickers"]["005930"]["unaligned_frames"] == 1


def test_check_day_reads_a_truncated_intraday_file_up_to_the_cut(tmp_path):
    path = _write(tmp_path, "20261001", "005930", [
        _frame(_rec("005930", "090001")) for _ in range(200)
    ])
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    out = check_day(tmp_path, "20261001", split=_split47)
    t = out["tickers"]["005930"]
    assert t["truncated"] is True
    assert 0 < t["frames"] < 200


def test_check_day_ignores_rest_backup_ticks(tmp_path):
    _write(tmp_path, "20261001", "005930", [
        {"source": "rest", "received_at": "2026-10-01T09:00:00+09:00",
         "price": 1000.0, "raw": None},
        _frame(_rec("005930", "090001")),
    ])
    out = check_day(tmp_path, "20261001", split=_split47)
    assert out["tickers"]["005930"]["frames"] == 1


def test_main_exits_nonzero_when_an_issue_is_found(tmp_path, capsys):
    _write(tmp_path, "20261001", "005930", [
        _frame(_rec("005930", "090001", n=48), _rec("005930", "090002", n=48)),
    ])
    assert main(["--root", str(tmp_path), "--date", "20261001"]) == 1
    assert "FIELD_COUNT_CHANGED" in capsys.readouterr().out


def test_main_exits_two_when_there_is_no_capture(tmp_path):
    assert main(["--root", str(tmp_path), "--date", "20261001"]) == 2
