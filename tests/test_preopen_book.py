"""장전 호가 수급 추출 — 프로브 원문 덤프에서 쌍별 잔량 비율을 뽑는다. 손익과 조인하지 않는다."""

import json

from scripts.preopen_book import book_features, label_pairs, parse_probe_lines, quantiles


def _multi(event: str, market: str, rows: list[dict]) -> str:
    return json.dumps({
        "ts": "2026-09-17T08:59:45+09:00", "event": event, "market": market,
        "response": {"rt_cd": "0", "output": rows},
    }, ensure_ascii=False)


def _row(ticker: str, bid: str, ask: str, **extra) -> dict:
    base = {
        "inter_shrn_iscd": ticker, "total_bidp_rsqn": bid, "total_askp_rsqn": ask,
        "shnu_rsqn": "10", "seln_rsqn": "5", "intr_antc_vol": "1000",
        "inter2_askp": "10100", "inter2_bidp": "10000", "hour_cls_code": "B",
    }
    base.update(extra)
    return base


# ── 파싱 ──────────────────────────────────────────────────────────────────


def test_parses_preopen_and_open_rows_by_phase_and_ticker():
    lines = [
        _multi("PAPER_FAST_PROBE_MULTI", "J", [_row("111111", "200", "100")]),
        _multi("PAPER_FAST_PROBE_MULTI", "Q", [_row("222222", "50", "100")]),
        _multi("PAPER_FAST_PROBE_OPEN_MULTI", "J", [_row("111111", "300", "100")]),
        json.dumps({"event": "PAPER_FAST_PROBE_PREOPEN_DONE", "elapsed_ms": 1}),
    ]
    parsed = parse_probe_lines(lines)
    assert set(parsed["PREOPEN"]) == {"111111", "222222"}
    assert set(parsed["OPEN"]) == {"111111"}
    assert parsed["OPEN"]["111111"]["total_bidp_rsqn"] == "300"


def test_rows_without_book_fields_are_ignored():
    """7/27처럼 잔량 필드가 없는 응답은 라벨 재료가 아니다."""
    lines = [
        _multi("PAPER_FAST_PROBE_MULTI", "J", [{"inter_shrn_iscd": "111111", "inter2_prpr": "100"}])
    ]
    assert parse_probe_lines(lines)["PREOPEN"] == {}


def test_unparseable_lines_are_skipped():
    lines = ["{not json", _multi("PAPER_FAST_PROBE_MULTI", "J", [_row("111111", "1", "1")])]
    assert set(parse_probe_lines(lines)["PREOPEN"]) == {"111111"}


# ── 피처 ──────────────────────────────────────────────────────────────────


def test_bid_ask_ratio_is_total_bid_over_total_ask():
    feats = book_features(_row("111111", "300", "100"))
    assert feats["bid_ask_ratio"] == 3.0
    assert feats["top1_bid_ask_ratio"] == 2.0
    assert feats["antc_vol"] == 1000
    assert feats["total_bid"] == 300 and feats["total_ask"] == 100


def test_zero_ask_side_gives_null_ratio_not_infinity():
    feats = book_features(_row("111111", "300", "0", seln_rsqn="0"))
    assert feats["bid_ask_ratio"] is None
    assert feats["top1_bid_ask_ratio"] is None


# ── 쌍 라벨 ───────────────────────────────────────────────────────────────


def test_label_pairs_reports_which_phases_covered_each_pair():
    probe = {
        "20260917": {
            "PREOPEN": {"111111": _row("111111", "200", "100")},
            "OPEN": {"111111": _row("111111", "300", "100"), "333333": _row("333333", "1", "1")},
        },
    }
    pairs = [
        {"date": "20260917", "ticker": "111111", "rank": 1},
        {"date": "20260917", "ticker": "333333", "rank": 2},
        {"date": "20260917", "ticker": "999999", "rank": 3},
        {"date": "20260901", "ticker": "111111", "rank": 1},
    ]
    rows = label_pairs(pairs, probe)
    by = {(r["date"], r["ticker"]): r for r in rows}
    assert by[("20260917", "111111")]["book_source"] == "BOTH"
    assert by[("20260917", "111111")]["preopen"]["bid_ask_ratio"] == 2.0
    assert by[("20260917", "111111")]["open"]["bid_ask_ratio"] == 3.0
    assert by[("20260917", "333333")]["book_source"] == "OPEN_ONLY"
    assert by[("20260917", "333333")]["preopen"] is None
    assert by[("20260917", "999999")]["book_source"] == "NONE"
    assert by[("20260901", "111111")]["book_source"] == "NO_PROBE_FILE"


# ── 분포 요약 (손익 없음) ─────────────────────────────────────────────────


def test_quantiles_skip_nulls():
    assert quantiles([3.0, None, 1.0, 2.0]) == {
        "n": 3, "min": 1.0, "p25": 1.5, "median": 2.0, "p75": 2.5, "max": 3.0,
    }
    assert quantiles([None]) == {
        "n": 0, "min": None, "p25": None, "median": None, "p75": None, "max": None,
    }


# ── H3 라벨 (문서 §2.2) ───────────────────────────────────────────────────


def test_h3_label_uses_the_preopen_total_ratio_at_the_natural_boundary():
    from scripts.preopen_book import h3_label

    assert h3_label({"preopen": {"bid_ask_ratio": 1.0}})["label"] == "BID_DOMINANT"
    assert h3_label({"preopen": {"bid_ask_ratio": 0.99}})["label"] == "ASK_DOMINANT"


def test_h3_label_is_none_without_a_preopen_row_or_with_an_empty_ask_side():
    from scripts.preopen_book import h3_label

    assert h3_label({"preopen": None})["label"] == "NONE"
    assert h3_label({"preopen": {"bid_ask_ratio": None}})["label"] == "NONE"


# ── 리뷰 반영: 비숫자 잔량은 0이 아니라 null, phase 필터 ─────────────────


def test_non_numeric_residual_quantity_gives_null_ratio_not_ask_dominant():
    from scripts.preopen_book import h3_label

    feats = book_features(_row("111111", "", "100"))
    assert feats["total_bid"] is None
    assert feats["bid_ask_ratio"] is None
    assert h3_label({"preopen": feats})["label"] == "NONE"


def test_multi_event_with_a_non_preopen_phase_is_ignored():
    line = json.loads(_multi("PAPER_FAST_PROBE_MULTI", "J", [_row("111111", "1", "1")]))
    line["phase"] = "OPEN"
    assert parse_probe_lines([json.dumps(line)])["PREOPEN"] == {}
