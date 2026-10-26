"""빠른 경로 진입 타이밍 정리 — 첫 체결과 1차 주문의 앞뒤, 주문별 갈래, 시가 대비 진입가.

진입 티켓(`docs/ENTRY_FILL_TIMEOUT_FOLLOWUP_20260903.md`) 8.2절 표와 8.10·8.11절 갈래 표를
매일 손으로 뽑던 것을 한 번에 낸다. 읽기 전용이다.
"""

import gzip
import json
from pathlib import Path

from scripts.entry_timing_report import analyze_day, branch_label, main

DATE = "20261026"


def _ev(hms: str, event: str, **kw) -> dict:
    return {"ts": f"2026-10-26T{hms}+09:00", "event": event, "level": "info", **kw}


def _write_log(root: Path, events: list[dict], date: str = DATE) -> None:
    d = root / "data" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n"
    (d / f"{date}.jsonl").write_bytes(body.encode("utf-8"))


def _rec(hms: str, price: int, qty: int, cum: int, ticker: str = "396300") -> list[str]:
    raw = ["0"] * 47
    raw[0], raw[1], raw[2], raw[12], raw[13] = ticker, hms, str(price), str(qty), str(cum)
    return raw


def _write_ticks(root: Path, rows: list[tuple[str, list[str]]], ticker: str = "396300",
                 truncate: bool = False) -> None:
    d = root / "data" / "strategy_ticks" / DATE
    d.mkdir(parents=True, exist_ok=True)
    body = "\n".join(
        json.dumps({"source": "ws", "received_at": f"2026-10-26T{at}+09:00",
                    "price": float(raw[2]), "raw": raw})
        for at, raw in rows
    ) + "\n"
    data = gzip.compress(body.encode("utf-8"))
    if truncate:
        data = data[:-8]  # 장중처럼 끝(trailer)이 아직 안 쓰인 gzip
    (d / f"{ticker}.09.jsonl.gz").write_bytes(data)


def _sent(hms: str, oid: str, attempt: int, ticker: str = "396300") -> dict:
    return _ev(hms, "ENTRY_ORDER_SENT", ticker=ticker, order_id=oid, entry_attempt=attempt,
               max_attempts=2, order_price=9570.0, order_qty=731)


def _order_api(hms: str, ms: int, wait: int) -> dict:
    return _ev(hms, "LATENCY_HIGH", api_endpoint="/uapi/domestic-stock/v1/trading/order-cash",
               latency_ms=ms, rate_wait_ms=wait)


def _timing(hms: str, source: str = "FAST_MULTI", total_ms: int = 9234) -> dict:
    return _ev(hms, "ENTRY_PIPELINE_TIMING", selection_source=source, total_ms=total_ms)


def test_order_after_first_trade_filled_in_window(tmp_path):
    """10/26: 주문 API가 느려 1차 주문이 첫 체결 뒤에 닿았다."""
    _write_log(tmp_path, [
        _order_api("09:00:07.822", 2843, 781),
        _sent("09:00:07.832", "0000002132", 1),
        _ev("09:00:09.238", "ENTRY_EXECUTED", ticker="396300", order_id="0000002132",
            fill_price=9565, fill_qty=731),
        _timing("09:00:09.238"),
    ])
    _write_ticks(tmp_path, [
        ("08:59:58.000", _rec("085958", 9450, 10, 929)),  # 장전 행은 첫 체결이 아니다
        ("09:00:07.101", _rec("090007", 9470, 64184, 65113)),
        ("09:00:07.101", _rec("090007", 9480, 1, 65114)),
    ])
    day = analyze_day(tmp_path, DATE)
    assert day["source"] == "FAST_MULTI"
    assert day["ticker"] == "396300"
    assert day["first_trade"]["at"] == "09:00:07.101"
    assert day["first_trade"]["price"] == 9470
    assert day["first_trade"]["qty"] == 64184
    [o] = day["orders"]
    assert o["api_ms"] == 2843 and o["rate_wait_ms"] == 781
    assert o["lead_sec"] == -0.731  # 음수 = 주문이 첫 체결 뒤
    assert o["outcome"] == "IN_WINDOW"
    assert day["result"] == "ENTERED"
    assert day["entry_ms"] == 9234
    assert day["vs_open_per_share"] == 95
    assert day["vs_open_amount"] == 69445
    assert branch_label(day) == "1차 첫 체결 뒤 · 창 안 체결"


def test_cancel_then_retry_filled(tmp_path):
    """10/22 갈래: 1차 취소 성공 → 2차는 첫 체결 뒤라 창 안 체결."""
    _write_log(tmp_path, [
        _sent("09:00:05.504", "0000001876", 1),
        _ev("09:00:07.505", "ENTRY_FILL_POLL_TIMEOUT", order_id="0000001876"),
        _ev("09:00:09.053", "ENTRY_CANCEL_SENT", order_id="0000001876", rt_cd="0",
            msg_cd="40630000"),
        _sent("09:00:18.433", "0000002061", 2),
        _ev("09:00:19.745", "ENTRY_EXECUTED", order_id="0000002061", fill_price=9600,
            fill_qty=100),
        _timing("09:00:19.745", total_ms=19740),
    ])
    _write_ticks(tmp_path, [("09:00:10.194", _rec("090010", 9500, 1000, 1432))])
    day = analyze_day(tmp_path, DATE)
    first, second = day["orders"]
    assert first["outcome"] == "CANCELLED" and first["lead_sec"] == 4.69
    assert first["api_ms"] is None  # LATENCY_HIGH 임계 미만이면 기록이 없다
    assert second["outcome"] == "IN_WINDOW" and second["lead_sec"] < 0
    assert day["vs_open_per_share"] == 100
    assert branch_label(day) == "1차 첫 체결 전 · 취소 → 2차 첫 체결 뒤 · 창 안 체결"


def test_late_confirmation_counts_as_filled_at_the_open(tmp_path):
    """10/14 갈래: 창이 닫힌 뒤 체결돼 취소가 거부됐다."""
    _write_log(tmp_path, [
        _sent("09:00:05.195", "0000001767", 1),
        _ev("09:00:07.200", "ENTRY_FILL_POLL_TIMEOUT", order_id="0000001767"),
        _ev("09:00:07.516", "ENTRY_CANCEL_SENT", order_id="0000001767", rt_cd="1",
            msg_cd="40330000"),
        _ev("09:00:08.729", "ENTRY_CANCEL_REJECTED_FILLED", order_id="0000001767",
            fill_price=9470, fill_qty=731),
        _ev("09:00:08.738", "ENTRY_EXECUTED", order_id="0000001767", fill_price=9470,
            fill_qty=731),
        _timing("09:00:08.738", total_ms=8718),
    ])
    _write_ticks(tmp_path, [("09:00:07.248", _rec("090007", 9470, 91689, 92000))])
    day = analyze_day(tmp_path, DATE)
    [o] = day["orders"]
    assert o["outcome"] == "LATE_CONFIRM"
    assert day["vs_open_per_share"] == 0
    assert branch_label(day) == "1차 첫 체결 전 · 늦은 확인"


def test_attempts_exhausted_is_a_failed_entry(tmp_path):
    """10/23 갈래: 두 번 다 첫 체결 전에 취소 → 무산. 진입가 비교는 없다."""
    _write_log(tmp_path, [
        _sent("09:00:05.045", "0000001813", 1),
        _ev("09:00:07.063", "ENTRY_FILL_POLL_TIMEOUT", order_id="0000001813"),
        _ev("09:00:08.545", "ENTRY_CANCEL_SENT", order_id="0000001813", rt_cd="0"),
        _sent("09:00:17.478", "0000001998", 2),
        _ev("09:00:19.498", "ENTRY_FILL_POLL_TIMEOUT", order_id="0000001998"),
        _ev("09:00:19.997", "ENTRY_CANCEL_SENT", order_id="0000001998", rt_cd="0"),
        _ev("09:00:21.171", "ENTRY_FAIL", order_id="0000001998", reason="UNFILLED"),
        _timing("09:00:21.770", total_ms=21757),
    ])
    _write_ticks(tmp_path, [("09:00:24.129", _rec("090024", 3905, 270218, 272194))],
                 truncate=True)
    day = analyze_day(tmp_path, DATE)
    assert day["result"] == "FAILED"
    assert [o["outcome"] for o in day["orders"]] == ["CANCELLED", "CANCELLED"]
    assert day["vs_open_per_share"] is None
    assert branch_label(day) == "1차 첫 체결 전 · 취소 → 2차 첫 체결 전 · 취소 → 무산"


def test_missing_ticks_and_legacy_path(tmp_path):
    """레거시 경로 날은 캡처가 종목 고정 뒤부터라 첫 체결·시가 비교를 하지 않는다."""
    _write_log(tmp_path, [
        _sent("09:01:40.466", "0000003313", 1),
        _ev("09:01:42.143", "ENTRY_EXECUTED", order_id="0000003313", fill_price=6761,
            fill_qty=1020),
        _timing("09:01:42.144", source="LEGACY_SINGLE_QUOTE", total_ms=102136),
    ])
    # 레거시 날 캡처는 종목 고정(09:01 이후)부터라 첫 행이 그날 첫 체결이 아니다.
    _write_ticks(tmp_path, [("09:01:36.738", _rec("090136", 6780, 15, 900000))])
    day = analyze_day(tmp_path, DATE)
    assert day["source"] == "LEGACY_SINGLE_QUOTE"
    assert day["first_trade"] is None
    assert day["orders"][0]["lead_sec"] is None
    assert day["vs_open_per_share"] is None
    assert branch_label(day) == "레거시 경로"


def test_fast_path_without_ticks_leaves_first_trade_unknown(tmp_path):
    """10/16: 관측 대상이 1순위로 돌아가 시도 종목 틱이 없었다."""
    _write_log(tmp_path, [
        _sent("09:00:04.990", "0000001700", 1, ticker="179880"),
        _ev("09:00:07.000", "ENTRY_FILL_POLL_TIMEOUT", order_id="0000001700"),
        _ev("09:00:08.000", "ENTRY_CANCEL_SENT", order_id="0000001700", rt_cd="0"),
        _ev("09:00:21.000", "ENTRY_FAIL", order_id="0000001700", reason="UNFILLED"),
        _timing("09:00:21.500", total_ms=21500),
    ])
    _write_ticks(tmp_path, [("09:00:10.000", _rec("090010", 5000, 10, 100))])  # 다른 종목 틱
    day = analyze_day(tmp_path, DATE)
    assert day["first_trade"] is None
    assert branch_label(day) == "1차 첫 체결 모름 · 취소 → 무산"


def test_no_log_returns_none(tmp_path):
    assert analyze_day(tmp_path, DATE) is None


def test_main_prints_a_table_over_a_range(tmp_path, capsys):
    _write_log(tmp_path, [
        _sent("09:00:07.832", "0000002132", 1),
        _ev("09:00:09.238", "ENTRY_EXECUTED", order_id="0000002132", fill_price=9565,
            fill_qty=731),
        _timing("09:00:09.238"),
    ])
    _write_ticks(tmp_path, [("09:00:07.101", _rec("090007", 9470, 64184, 65113))])
    rc = main(["--root", str(tmp_path), "--since", "20261020", "--until", "20261031"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "| 1026 | 396300 |" in out
    assert "09:00:07.101" in out and "9,470" in out
    assert "+95" in out and "추정" in out
    assert "1차 첫 체결 뒤 · 창 안 체결" in out
