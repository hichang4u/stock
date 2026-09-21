"""C1 거래일 달력 공용 헬퍼 — 순수 함수와 파일 I/O 둘 다 검사한다."""

import json

from scripts.overnight_calendar import load_calendar, next_trading_date


def test_load_calendar_returns_none_when_file_absent(tmp_path):
    assert load_calendar(tmp_path) is None


def test_load_calendar_returns_none_when_file_is_malformed(tmp_path):
    d = tmp_path / "data" / "overnight"
    d.mkdir(parents=True)
    (d / "calendar.json").write_text("{not json", encoding="utf-8")
    assert load_calendar(tmp_path) is None


def test_load_calendar_returns_none_when_file_is_not_a_list(tmp_path):
    d = tmp_path / "data" / "overnight"
    d.mkdir(parents=True)
    (d / "calendar.json").write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    assert load_calendar(tmp_path) is None


def test_load_calendar_reads_a_sorted_or_unsorted_list(tmp_path):
    d = tmp_path / "data" / "overnight"
    d.mkdir(parents=True)
    (d / "calendar.json").write_text(json.dumps(["20260922", "20260921"]), encoding="utf-8")
    assert load_calendar(tmp_path) == ["20260922", "20260921"]


def test_next_trading_date_is_the_first_date_strictly_after():
    calendar = ["20260921", "20260922", "20260923"]
    assert next_trading_date(calendar, "20260921") == "20260922"
    assert next_trading_date(calendar, "20260922") == "20260923"


def test_next_trading_date_is_none_when_no_later_date_in_calendar():
    assert next_trading_date(["20260921"], "20260921") is None
