"""화면 표시 모델: 거래 결과·거래일·연결 상태(읽기 전용 필드) 회귀.

2026-11-20 123690: DB 거래는 진입 6,650 → 청산 6,997(+5.22%, 비용 전)인데 화면 대표값은
청산 후 관측가 6,420(진입가 대비 −3.46%)이었다. 토요일에는 그 지난 거래가 오늘 것처럼 섞였다.
"""
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("fastapi")

import src.api.server as server  # noqa: E402
from src import db  # noqa: E402
from src.api.status_logic import (  # noqa: E402
    health_from_logs,
    session_view,
    trade_result_view,
)

KST = ZoneInfo("Asia/Seoul")


def _closed_row(**over):
    row = {
        "id": 79, "date": "20261120", "ticker": "123690", "name": "한국화장품",
        "status": "CLOSED", "entry_price": 6650.0, "entry_qty": 9,
        "entry_at": "2026-11-20T09:00:05.375899+09:00", "exit_price": 6997.0, "exit_qty": 9,
        "exit_at": "2026-11-20T09:01:53+09:00", "close_reason": "TRAILING",
        "pnl_amount": 3123.0, "pnl_pct": 5.22,
    }
    row.update(over)
    return row


# ── trade_result_view ────────────────────────────────────────────────────


def test_closed_trade_result_is_gross_and_carries_exit():
    view = trade_result_view(_closed_row())
    assert view["available"] is True
    assert view["closed"] is True
    assert view["exit_price"] == 6997.0
    assert view["pnl_pct"] == 5.22
    assert view["pnl_amount"] == 3123.0
    assert view["cost_basis"] == "GROSS"


def test_open_trade_has_no_exit_or_pnl():
    view = trade_result_view(_closed_row(status="OPEN", exit_price=None, pnl_pct=None))
    assert view["available"] is True
    assert view["closed"] is False
    assert view["exit_price"] is None
    assert view["pnl_pct"] is None


def test_missing_and_failed_results_are_not_zero():
    assert trade_result_view(None) == {"available": False, "reason": "NO_TRADE"}
    failed = trade_result_view(_closed_row(), query_failed=True)
    assert failed == {"available": False, "reason": "QUERY_FAILED"}
    assert "pnl_pct" not in failed


def test_non_finite_pnl_becomes_none():
    view = trade_result_view(_closed_row(pnl_pct=math.nan, pnl_amount=True))
    assert view["pnl_pct"] is None
    assert view["pnl_amount"] is None


# ── session_view ─────────────────────────────────────────────────────────


def test_saturday_marks_friday_trade_as_previous_day():
    now = datetime(2026, 11, 21, 10, 0, tzinfo=KST)
    view = session_view(
        now, trading_date="20261120", entry_at="2026-11-20T09:00:05.375899+09:00",
        late_open=False,
    )
    assert view["today"] == "20261121"
    assert view["today_weekday"] == "토"
    assert view["is_weekend"] is True
    assert view["data_date"] == "20261120"
    assert view["data_weekday"] == "금"
    assert view["is_previous_day"] is True
    assert view["holiday_known"] is False


def test_same_day_trade_is_not_previous():
    now = datetime(2026, 11, 20, 9, 30, tzinfo=KST)
    view = session_view(now, trading_date="20261120", entry_at=None, late_open=True)
    assert view["is_previous_day"] is False
    assert view["is_weekend"] is False
    assert view["late_open"] is True


def test_no_dates_means_no_data_date():
    now = datetime(2026, 11, 20, 9, 30, tzinfo=KST)
    view = session_view(now, trading_date=None, entry_at="not-a-time", late_open=False)
    assert view["data_date"] is None
    assert view["is_previous_day"] is False


# ── health_from_logs ─────────────────────────────────────────────────────


def test_health_takes_the_last_event_per_kind_and_none_when_absent():
    rows = [
        {"event": "TOKEN_LOADED_FROM_CACHE", "ts": "2026-11-20T08:30:00+09:00"},
        {"event": "TIME_SYNC_OK", "ts": "2026-11-20T08:30:01+09:00", "offset_ms": 12},
        {"event": "TIME_SYNC_WARN", "ts": "2026-11-20T08:40:01+09:00", "offset_ms": 230,
         "level": "WARN"},
        {"event": "TICK", "ts": "2026-11-20T09:00:00+09:00"},
    ]
    health = health_from_logs(rows)
    assert health["token"]["event"] == "TOKEN_LOADED_FROM_CACHE"
    assert health["ntp"]["event"] == "TIME_SYNC_WARN"
    assert health["ntp"]["offset_ms"] == 230
    assert health["ws"] is None


# ── /api/status · /api/f1 ────────────────────────────────────────────────


def _set_closed_state(monkeypatch, trade_id):
    s = server.state.get()
    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: [])
    for key, value in {
        "position_status": "CLOSED", "target_ticker": "123690", "target_name": "한국화장품",
        "entry_price": 6650.0, "entry_qty": 9, "remaining_qty": 0, "trade_id": trade_id,
        "entry_at": "2026-11-20T09:00:05.375899+09:00", "trading_date": "20261120",
    }.items():
        monkeypatch.setattr(s, key, value)
    monkeypatch.setattr(server.live, "last_tick_price", 6420.0)


@pytest.mark.asyncio
async def test_status_closed_keeps_observed_pnl_and_adds_trade_result(tmp_path, monkeypatch):
    await db.init(str(tmp_path / "closed.db"))
    try:
        trade_id = await db.open_trade("20261120", "123690", 6650.0, 9, name="한국화장품")
        await db.close_trade(trade_id, 6997.0, "TRAILING", 5.22, 0.05, exit_qty=9,
                             high_price=7010.0)
        _set_closed_state(monkeypatch, trade_id)

        payload = json.loads((await server.api_status()).body.decode("utf-8"))
    finally:
        await db.close()

    # 기존 계약(차트·SSE가 쓰는 지금 시세)은 그대로다.
    assert payload["current_price"] == 6420.0
    assert payload["pnl_pct"] == -3.46
    # 거래 결과는 별도 필드로, 비용 전임을 밝힌다.
    result = payload["trade_result"]
    assert result["available"] is True and result["closed"] is True
    assert result["exit_price"] == 6997.0
    assert result["pnl_pct"] == 5.22
    assert result["cost_basis"] == "GROSS"
    assert payload["session"]["data_date"] == "20261120"
    assert set(payload["health"]) >= {"token", "ntp", "ws"}


@pytest.mark.asyncio
async def test_status_trade_result_query_failure_is_reported(monkeypatch):
    _set_closed_state(monkeypatch, 79)

    def broken():
        raise RuntimeError("db not initialised")

    monkeypatch.setattr(server.db, "get", broken)
    payload = json.loads((await server.api_status()).body.decode("utf-8"))

    assert payload["trade_result"] == {"available": False, "reason": "QUERY_FAILED"}
    assert payload["pnl_pct"] == -3.46


@pytest.mark.asyncio
async def test_status_without_position_has_no_trade_result(monkeypatch):
    s = server.state.get()
    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: [])
    monkeypatch.setattr(s, "position_status", "IDLE")
    monkeypatch.setattr(s, "target_ticker", None)
    payload = json.loads((await server.api_status()).body.decode("utf-8"))
    assert payload["trade_result"] == {"available": False, "reason": "NO_POSITION"}


def test_health_snapshot_reads_latest_isolated_log(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_LOG_DIR", tmp_path)
    monkeypatch.setattr(server, "_today", lambda: "20261121")
    server._HEALTH_READER.reset()
    (tmp_path / "20261120.jsonl").write_bytes(
        b'{"event":"TIME_SYNC_OK","ts":"2026-11-20T08:30:01+09:00","offset_ms":3}\n'
        b'{"event":"TICK","ts":"2026-11-20T09:00:00+09:00"}\n'
    )
    (tmp_path / "20261122.jsonl").write_bytes(b'{"event":"TIME_SYNC_OK","ts":"future"}\n')

    snap = server._health_snapshot()

    assert snap["log_date"] == "20261120"
    assert snap["ntp"]["offset_ms"] == 3
    assert snap["token"] is None


@pytest.mark.asyncio
async def test_f1_marks_state_trade_from_previous_day(monkeypatch):
    s = server.state.get()
    monkeypatch.setattr(server, "_today", lambda: "20261121")
    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: [])
    monkeypatch.setattr(server, "_read_f1_snapshot", lambda: (None, []))
    monkeypatch.setattr(server.paper_fast_probe, "load_persisted_open_candidates",
                        lambda: (None, []))
    for key, value in {
        "position_status": "CLOSED", "target_ticker": "123690", "target_name": "한국화장품",
        "entry_at": "2026-11-20T09:00:05.375899+09:00", "trading_date": "20261120",
        "trade_id": 79, "entry_price": 6650.0,
    }.items():
        monkeypatch.setattr(s, key, value)

    payload = json.loads((await server.api_f1()).body.decode("utf-8"))

    assert payload["selection_source"] == "PREVIOUS_TRADE"
    assert payload["selection_trade_date"] == "20261120"
    assert payload["today"] == "20261121"
    assert payload["snapshot_date"] is None
