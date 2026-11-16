import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import src.api.status_logic as status_logic


def test_f1_verdict_has_current_high_gap_rejection_label():
    assert status_logic.f1_verdict({"gap_reason": "HIGH_GAP_REJECTED"}) == "고갭조건미달"


def test_f1_summary_uses_same_liquidity_selection_as_f1_filter():
    rows = [
        {
            "ticker": f"TICK{i:02d}",
            "gap_allowed": True,
            "gap_pct": 0.05,
            "avg_amount_5d": float(i) * 1e9,
        }
        for i in range(1, 21)
    ]

    summary = status_logic.f1_summary_from_rows(rows)

    assert summary["liquidity_pass"] == 10
    assert summary["selected"]["ticker"] == "TICK20"


def test_latest_today_snapshot_ignores_previous_day_files(tmp_path):
    old = tmp_path / "20260701_160253.jsonl"
    old.write_text("{}", encoding="utf-8")

    assert status_logic.latest_today_snapshot_path(tmp_path, "20260702") is None


def test_latest_today_snapshot_selects_newest_today_file(tmp_path):
    first = tmp_path / "20260702_084000.jsonl"
    second = tmp_path / "20260702_085000.jsonl"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")

    assert status_logic.latest_today_snapshot_path(tmp_path, "20260702") == second


def test_latest_today_snapshot_breaks_mtime_ties_by_filename(tmp_path):
    """Filenames sort chronologically; mtime alone can tie when writes land in the same tick."""
    first = tmp_path / "20260702_084000.jsonl"
    second = tmp_path / "20260702_085000.jsonl"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    tied_time = first.stat().st_mtime
    os.utime(second, (tied_time, tied_time))

    assert status_logic.latest_today_snapshot_path(tmp_path, "20260702") == second


def test_f1_candidates_display_pass_candidates_before_ranking_order():
    rows = [
        {
            "ticker": f"DROP{i:02d}",
            "gap_allowed": False,
            "gap_pct": 0.0,
            "expected_amount": float(100 - i) * 1e8,
        }
        for i in range(20)
    ]
    rows.append(
        {
            "ticker": "PASS01",
            "gap_allowed": True,
            "gap_pct": 0.05,
            "ranking_gap_pct": 0.0,
            "expected_api_gap_pct": 0.05,
            "expected_api_price": 10500,
            "expected_api_qty": 10,
            "expected_amount": 1e8,
            "avg_amount_5d": 1e8,
        }
    )

    summary = status_logic.f1_summary_from_rows(rows)

    assert summary["gap_pass"] == 1
    assert summary["candidates"][0]["ticker"] == "PASS01"


def test_pipeline_uses_today_logs_after_entry_fail_returns_to_idle():
    logs = [
        {"event": "F1_DONE"},
        {"event": "TARGET_LOCKED"},
        {"event": "ENTRY_ORDER_SENT"},
        {"event": "ENTRY_FAIL"},
    ]

    pipeline = status_logic.pipeline_from_logs(logs, "IDLE")

    assert pipeline == {"pipeline_stage": 2, "pipeline_failed": True}



def test_pipeline_marks_direct_f3_failure_events_failed():
    pipeline = status_logic.pipeline_from_logs([
        {"event": "F1_DONE"},
        {"event": "TARGET_LOCKED"},
        {"event": "BUYABLE_QTY_QUERY_FAILED"},
    ], "IDLE")

    assert pipeline == {"pipeline_stage": 2, "pipeline_failed": True}
def test_pipeline_live_position_status_takes_precedence():
    logs = [
        {"event": "F1_DONE"},
        {"event": "TARGET_LOCKED"},
        {"event": "ENTRY_FAIL"},
    ]

    pipeline = status_logic.pipeline_from_logs(logs, "HOLDING")

    assert pipeline == {"pipeline_stage": 3, "pipeline_failed": False}


def test_pipeline_exiting_is_visible_as_unresolved_close():
    assert status_logic.pipeline_from_logs([], "EXITING") == {
        "pipeline_stage": 3,
        "pipeline_failed": True,
    }


def test_f3_detail_from_event_labels_internal_reasons():
    assert status_logic.f3_detail_from_event(
        {"event": "GAP_CHANGED", "reason": "BELOW_MIN"}
    ) == "갭 하한 미달"
    assert status_logic.f3_detail_from_event(
        {"event": "GAP_CHANGED", "reason": "ABOVE_MAX"}
    ) == "갭 상한 초과"
    assert status_logic.f3_detail_from_event(
        {"event": "F3_ENTRY_BLOCKED", "reason": "GAP_RECHECK_UNAVAILABLE"}
    ) == "진입 전 갭 재검증 불가"
    assert status_logic.f3_detail_from_event(
        {"event": "F3_ENTRY_BLOCKED", "reason": "BUYABLE_QTY_ZERO"}
    ) == "매수가능수량 0"
    assert status_logic.f3_detail_from_event(
        {"event": "F3_ENTRY_BLOCKED", "reason": "BUYABLE_QTY_QUERY_FAILED"}
    ) == "매수가능수량 조회 실패"


def test_f3_detail_from_event_summarizes_final_pick():
    assert (
        status_logic.f3_detail_from_event({
            "event": "F3_FINAL_PICK",
            "valid_count": 2,
            "checked_count": 3,
        })
        == "2 / 3 재검증"
    )


def test_parse_asset_snapshot_response_parses_kis_balance():
    result = status_logic.parse_asset_snapshot_response({
        "rt_cd": "0",
        "output1": [
            {"pdno": "005930", "hldg_qty": "2"},
            {"pdno": "000660", "hldg_qty": "0"},
        ],
        "output2": [{
            "dnca_tot_amt": "1,000,000",
            "ord_psbl_cash": "800000",
            "scts_evlu_amt": "500000",
            "tot_evlu_amt": "1500000",
            "evlu_pfls_smtl_amt": "12000",
        }],
    })

    assert result == {
        "cash": 1_000_000.0,
        "buyable_cash": 800_000.0,
        "buyable_cash_source": "ord_psbl_cash",
        "stock_value": 500_000.0,
        "total_asset": 1_500_000.0,
        "pnl_amount": 12_000.0,
        "holdings_count": 1,
        "holdings": [
            {
                "ticker": "005930",
                "name": "",
                "qty": 2,
                "orderable_qty": 0,
                "current_price": None,
                "avg_price": None,
                "purchase_amount": None,
                "evaluation_amount": None,
                "pnl_amount": None,
                "pnl_pct": None,
            }
        ],
        "source": "KIS",
    }


def test_parse_asset_snapshot_response_falls_back_to_cash_when_buyable_missing():
    result = status_logic.parse_asset_snapshot_response({
        "output1": [],
        "output2": [{
            "dnca_tot_amt": "1,000,000",
            "scts_evlu_amt": "500000",
            "tot_evlu_amt": "1500000",
            "evlu_pfls_smtl_amt": "12000",
        }],
    })

    assert result["cash"] == 1_000_000.0
    assert result["buyable_cash"] == 1_000_000.0
    assert result["buyable_cash_source"] == "dnca_tot_amt"


def test_parse_asset_snapshot_response_uses_settlement_amount_when_larger():
    result = status_logic.parse_asset_snapshot_response({
        "output1": [],
        "output2": [{
            "dnca_tot_amt": "120,543",
            "prvs_rcdl_excc_amt": "8,865,465",
            "scts_evlu_amt": "500000",
            "tot_evlu_amt": "1500000",
            "evlu_pfls_smtl_amt": "12000",
        }],
    })

    assert result["cash"] == 120_543.0
    assert result["buyable_cash"] == 8_865_465.0
    assert result["buyable_cash_source"] == "prvs_rcdl_excc_amt"


def test_parse_asset_snapshot_response_rejects_kis_error():
    try:
        status_logic.parse_asset_snapshot_response({
            "rt_cd": "1",
            "msg_cd": "EGW00123",
            "msg1": "token expired",
        })
    except RuntimeError as exc:
        assert "KIS balance error" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_parse_asset_snapshot_response_rejects_missing_output2():
    try:
        status_logic.parse_asset_snapshot_response({"rt_cd": "0", "output1": []})
    except RuntimeError as exc:
        assert "missing output2" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_parse_asset_snapshot_response_rejects_invalid_number():
    try:
        status_logic.parse_asset_snapshot_response({
            "rt_cd": "0",
            "output1": [],
            "output2": [{
                "dnca_tot_amt": "not-a-number",
                "ord_psbl_cash": "800000",
                "scts_evlu_amt": "500000",
                "tot_evlu_amt": "1500000",
                "evlu_pfls_smtl_amt": "12000",
            }],
        })
    except RuntimeError as exc:
        assert "invalid field dnca_tot_amt" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


# ── 가격흐름 앞 구간 보완(price_seed_from_bars) ─────────────────────────────
_SEED_KST = ZoneInfo("Asia/Seoul")
_SEED_DAY = datetime(2026, 11, 16, tzinfo=_SEED_KST)


def _seed_at(hhmm: str, second: int = 0) -> datetime:
    return _SEED_DAY.replace(hour=int(hhmm[:2]), minute=int(hhmm[2:]), second=second)


def _seed_bar(hhmm: str, close: object, date: str = "20261116") -> dict:
    return {"date": date, "time": f"{hhmm}00", "open": 1.0, "high": 1.0, "low": 1.0,
            "close": close, "volume": 1.0, "confirmed": True}


def _seed_day_bars() -> list[dict]:
    """정규장 하루치 390분(09:00~15:29). 종가는 분마다 1원씩 오른다."""
    start = _seed_at("0900")
    return [
        _seed_bar((start + timedelta(minutes=i)).strftime("%H%M"), 13_000.0 + i)
        for i in range(390)
    ]


def _seed(
    bars: list,
    *,
    trade_date: str = "20261116",
    since: datetime | None = _seed_at("0900", 28),
    before: datetime | None = None,
    now: datetime = _seed_at("1600"),
) -> tuple[list[dict], dict]:
    """기본값: 09:00:28 진입, 메모리 분 이력 없음, 장 마감 뒤(16:00)에 조회."""
    return status_logic.price_seed_from_bars(
        bars, trade_date=trade_date, since=since, before=before, now=now,
    )


def test_price_seed_fills_only_the_minutes_memory_has_dropped():
    # 2026-11-16 13:28: 메모리 분 이력은 10:28부터였고 저장 봉은 09:00부터 있었다.
    rows, meta = _seed(_seed_day_bars(), before=_seed_at("1028"), now=_seed_at("1328", 30))

    assert rows[0] == {"ts": "2026-11-16T09:00:00+09:00", "price": 13_000.0}
    assert rows[-1] == {"ts": "2026-11-16T10:27:00+09:00", "price": 13_087.0}
    assert len(rows) == 88
    assert meta == {
        "count": 88, "invalid": 0, "missing_minutes": 0,
        "first": "2026-11-16T09:00:00+09:00", "last": "2026-11-16T10:27:00+09:00",
    }


def test_price_seed_covers_a_full_session_across_noon_when_memory_is_empty():
    rows, meta = _seed(_seed_day_bars())

    assert len(rows) == 390
    assert rows[0]["ts"] == "2026-11-16T09:00:00+09:00"
    assert rows[-1]["ts"] == "2026-11-16T15:29:00+09:00"
    stamps = [row["ts"] for row in rows]
    assert stamps == sorted(stamps)
    assert "2026-11-16T11:59:00+09:00" in stamps and "2026-11-16T12:00:00+09:00" in stamps
    assert meta["missing_minutes"] == 0


def test_price_seed_keeps_the_entry_minute_and_drops_every_bar_before_it():
    # live.minute_price_history와 같은 규칙: since를 분으로 내려 진입 분은 포함한다.
    rows, _ = _seed(_seed_day_bars(), since=_seed_at("0905", 28), before=_seed_at("0910"))

    assert [row["ts"][11:16] for row in rows] == ["09:05", "09:06", "09:07", "09:08", "09:09"]


def test_price_seed_never_returns_a_minute_that_has_not_finished():
    # 10:15:30 — 10:15 봉은 아직 종가가 바뀐다. 그 뒤 시각의 봉은 미래다.
    rows, _ = _seed(_seed_day_bars(), since=_seed_at("1010"), now=_seed_at("1015", 30))

    assert [row["ts"][11:16] for row in rows] == ["10:10", "10:11", "10:12", "10:13", "10:14"]
    exact, _ = _seed(_seed_day_bars(), since=_seed_at("1014"), now=_seed_at("1015"))
    assert [row["ts"][11:16] for row in exact] == ["10:14"]


def test_price_seed_drops_unusable_rows_and_counts_them():
    bars = [
        _seed_bar("0900", 13_000.0),
        _seed_bar("0901", 0),
        _seed_bar("0902", -5.0),
        _seed_bar("0903", float("nan")),
        _seed_bar("0904", float("inf")),
        _seed_bar("0905", "13000"),
        _seed_bar("0906", None),
        _seed_bar("0907", True),
        {"date": "20261116", "time": "xx0800", "close": 13_000.0},
        {"date": "20261116", "time": "2561", "close": 13_000.0},
        _seed_bar("0908", 13_008.0, date="20261113"),
        "not-a-row",
        _seed_bar("0909", 13_009),
    ]

    rows, meta = _seed(bars)

    assert rows == [
        {"ts": "2026-11-16T09:00:00+09:00", "price": 13_000.0},
        {"ts": "2026-11-16T09:09:00+09:00", "price": 13_009.0},
    ]
    assert meta["invalid"] == 11
    # 못 쓰는 분을 앞뒤 값으로 메우지 않는다 — 빈 분으로 드러낸다.
    assert meta["missing_minutes"] == 8


def test_price_seed_sorts_and_keeps_the_last_row_of_a_repeated_minute():
    bars = [
        _seed_bar("0903", 103.0),
        _seed_bar("0901", 101.0),
        _seed_bar("0902", 102.0),
        _seed_bar("0901", 111.0),
    ]

    rows, meta = _seed(bars)

    assert [(row["ts"][11:16], row["price"]) for row in rows] == [
        ("09:01", 111.0), ("09:02", 102.0), ("09:03", 103.0),
    ]
    assert meta["count"] == 3 and meta["invalid"] == 0


def test_price_seed_counts_missing_minutes_up_to_the_first_memory_minute():
    bars = [_seed_bar("0900", 100.0), _seed_bar("0901", 101.0), _seed_bar("0905", 105.0)]

    rows, meta = _seed(bars, before=_seed_at("0910"))

    assert len(rows) == 3
    # 09:02~09:04(3분) + 09:06~09:09(4분)
    assert meta["missing_minutes"] == 7


def test_price_seed_treats_naive_times_as_kst_and_rejects_a_bad_date():
    naive_since = datetime(2026, 11, 16, 9, 2, 10)
    naive_now = datetime(2026, 11, 16, 9, 4, 0)
    rows, _ = status_logic.price_seed_from_bars(
        _seed_day_bars(), trade_date="20261116", since=naive_since, before=None, now=naive_now,
    )
    assert [row["ts"][11:16] for row in rows] == ["09:02", "09:03"]

    empty, meta = _seed(_seed_day_bars(), trade_date="2026-11-16")
    assert empty == [] and meta["count"] == 0

    not_list, meta = _seed({"time": "090000"})  # type: ignore[arg-type]
    assert not_list == [] and meta["invalid"] == 0
