import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

pytest.importorskip("fastapi")

import src.api.server as server  # noqa: E402 — fastapi 미설치 시 모듈 스킵 이후 임포트
import src.api.status_logic as status_logic  # noqa: E402
import src.modules.f1_filter as f1_filter  # noqa: E402
import src.modules.f3_entry as f3_entry  # noqa: E402
import src.modules.f4_tracking as f4_tracking  # noqa: E402
from src import db  # noqa: E402
from src.schedule_times import (  # noqa: E402
    F5_EXEC_H,
    F5_EXEC_M,
    F5_PRECHECK_H,
    F5_PRECHECK_M,
    F5_PRECHECK_S,
)


def _tracking_stop_request(
    *,
    source: str | None = "dashboard",
    referer: str | None = "http://127.0.0.1:8899/",
) -> Request:
    headers = [(b"user-agent", b"pytest-browser")]
    if source is not None:
        headers.append((b"x-tracking-source", source.encode()))
    if referer is not None:
        headers.append((b"referer", referer.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/tracking/stop",
            "headers": headers,
            "client": ("127.0.0.1", 54321),
            "server": ("127.0.0.1", 8899),
            "scheme": "http",
            "query_string": b"",
        }
    )


def test_server_uses_f1_snapshot_dir_constant():
    assert server._F1_SNAPSHOT_DIR == Path(f1_filter.F1_SNAPSHOT_DIR)


def test_orders_ui_surfaces_uncertain_audit_as_immediate_action():
    root = Path(__file__).resolve().parents[1]
    app_js = (root / "docs/html/assets/app.js").read_text(encoding="utf-8")
    index_html = (root / "docs/html/index.html").read_text(encoding="utf-8")

    assert "MTS 즉시확인" in app_js
    assert "o.status === 'UNCERTAIN'" in app_js
    assert "entry_order_attempts' ? '진입 감사원장'" in app_js
    assert 'id="order-uncertain"' in index_html


def test_f1_snapshot_saved_is_only_weak_done_signal():
    logs = [
        {"event": "F1_SNAPSHOT_SAVED"},
        {"event": "F1_RETRY_WAIT"},
    ]

    status, last_event = server._f1_status_from_logs(logs)

    assert status == "RETRYING"
    assert last_event == logs[-1]


def test_f1_skipped_is_failed_status():
    status, last_event = server._f1_status_from_logs(
        [{"event": "F1_SKIPPED", "reason": "PROCESS_RESTART_DETECTED"}]
    )

    assert status == "FAILED"
    assert last_event["event"] == "F1_SKIPPED"


def test_paper_fast_selection_is_done_status():
    status, last_event = server._f1_status_from_logs(
        [{
            "event": "PAPER_FAST_PATH_SELECTED",
            "tickers": ["005930", "000660"],
        }]
    )

    assert status == "DONE"
    assert last_event["event"] == "PAPER_FAST_PATH_SELECTED"


def test_fast_candidates_can_be_recovered_from_selection_logs():
    logs = [
        {
            "event": "PAPER_FAST_PATH_SELECTED",
            "tickers": ["011790", "028050", "332570"],
        },
        {
            "event": "TARGET_LOCKED",
            "ticker": "028050",
            "name": "Samsung E&A",
            "target_tickers": ["028050", "332570"],
            "target_names": ["Samsung E&A", "PS Electronics"],
            "gap_pct": 4.88,
            "expected_price": 43000,
            "expected_amount": 1_514_761_000,
        },
    ]

    rows = server._fast_candidates_from_logs(logs)

    assert [row["ticker"] for row in rows] == ["011790", "028050", "332570"]
    assert rows[1]["name"] == "Samsung E&A"
    assert rows[1]["gap_pct"] == pytest.approx(0.0488)

    # 갭을 복구한 종목만 통과로 표시한다 — 나머지는 근거 없이 통과로 단정하지 않는다.
    assert rows[1]["gap_allowed"] is True
    assert status_logic.f1_verdict(rows[1]) == "통과"
    for row in (rows[0], rows[2]):
        assert "gap_allowed" not in row
        assert row["gap_reason"] == "GAP_UNVERIFIED"
        assert status_logic.f1_allowed(row) is False
        assert status_logic.f1_verdict(row) == "갭확인불가"


def test_fast_candidates_from_logs_without_lock_event_claims_no_gap_pass():
    """TARGET_LOCKED가 없으면 어떤 종목도 통과로 집계되지 않아야 한다."""
    rows = server._fast_candidates_from_logs(
        [{"event": "PAPER_FAST_PATH_SELECTED", "tickers": ["011790", "028050"]}]
    )

    assert [row["ticker"] for row in rows] == ["011790", "028050"]
    assert server._f1_summary_from_rows(rows)["gap_pass"] == 0


def test_selection_process_summarizes_f1_f2_f3():
    summary = {
        "selected": {
            "ticker": "006340", "name": "대원전선",
            "gap_pct": 0.0349, "expected_amount": 147_000_000},
        "liquidity_pass": 10,
        "gap_pass": 12,
        "candidates": [{"ticker": "006340"}, {"ticker": "036930"}],
    }
    logs = [
        {"event": "TARGET_LOCKED", "ticker": "006340", "name": "Daewon",
         "target_tickers": ["006340", "036930"],
         "target_names": ["Daewon", "Jusung"], "gap_pct": 3.49},
        {"event": "F3_FINAL_PICK", "ticker": "006340", "name": "Daewon",
         "checked_count": 2, "valid_count": 1, "expected_price": 10670},
    ]

    result = server._selection_process_from_logs(summary, logs)

    assert [row["phase"] for row in result] == ["F1 선정", "F2 잠금", "F3 최종"]
    assert result[0]["status"] == "완료"
    assert result[1]["status"] == "잠금"
    assert result[1]["detail"] == "006340 Daewon, 036930 Jusung"
    assert result[1]["name"] == "Daewon"
    assert result[1]["names"] == ["Daewon", "Jusung"]
    assert result[2]["name"] == "Daewon"
    assert result[2]["status"] == "최종"
    assert result[2]["detail"] == "1 / 2 재검증"

def test_selection_process_derives_f3_name_from_snapshot_when_log_has_no_name():
    summary = {
        "selected": {
            "ticker": "006340", "name": "Daewon",
            "gap_pct": 0.0349, "expected_amount": 147_000_000},
        "liquidity_pass": 1,
        "gap_pass": 1,
        "candidates": [{"ticker": "006340", "name": "Daewon"}],
    }
    logs = [
        {"event": "TARGET_LOCKED", "ticker": "006340",
         "target_tickers": ["006340"], "gap_pct": 3.49},
        {"event": "F3_FINAL_PICK", "ticker": "006340", "checked_count": 1, "valid_count": 1},
    ]

    result = server._selection_process_from_logs(summary, logs)

    assert result[1]["detail"] == "006340 Daewon"
    assert result[1]["name"] == "Daewon"
    assert result[2]["name"] == "Daewon"

def test_selection_process_ignores_f2_from_different_f1_snapshot():
    summary = {
        "selected": {"ticker": "028050", "gap_pct": 0.0651, "expected_amount": 23_901_000_000},
        "liquidity_pass": 4,
        "gap_pass": 4,
        "candidates": [{"ticker": "028050"}, {"ticker": "073240"}],
    }
    logs = [
        {"event": "TARGET_LOCKED", "ticker": "006340",
         "target_tickers": ["006340"], "gap_pct": 3.49},
        {"event": "F3_FINAL_PICK", "ticker": "006340", "checked_count": 1, "valid_count": 1},
    ]

    result = server._selection_process_from_logs(summary, logs)

    assert result[0]["ticker"] == "028050"
    assert result[1]["ticker"] is None
    assert result[2]["ticker"] is None


@pytest.mark.asyncio
async def test_status_reads_only_recent_logs(monkeypatch):
    limits = []

    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: limits.append(limit) or [])

    await server.api_status()

    assert limits == [server._STATUS_LOG_LIMIT]
    assert server._STATUS_LOG_LIMIT == 50


@pytest.mark.asyncio
async def test_status_includes_asset_snapshot(monkeypatch):
    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: [])
    monkeypatch.setattr(
        server,
        "_ASSET_CACHE",
        {
            "cash": 1_000_000.0,
            "buyable_cash": 900_000.0,
            "stock_value": 500_000.0,
            "total_asset": 1_500_000.0,
            "pnl_amount": 12_000.0,
            "holdings_count": 1,
            "source": "KIS",
        },
    )

    resp = await server.api_status()
    body = resp.body.decode("utf-8")

    assert '"assets"' in body
    assert '"cash":1000000.0' in body


@pytest.mark.asyncio
async def test_status_does_not_fetch_asset_snapshot(monkeypatch):
    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: [])
    fetch = AsyncMock(return_value={"cash": 1.0})
    monkeypatch.setattr(server, "_asset_snapshot_safe", fetch)

    await server.api_status()

    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_exposes_post_close_tracking_state(monkeypatch):
    s = server.state.get()
    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: [])
    monkeypatch.setattr(s, "position_status", "CLOSED")
    monkeypatch.setattr(s, "post_close_tracking_stopped", False)
    monkeypatch.setattr(
        server.f4_tracking,
        "post_close_observation_active",
        lambda: True,
    )

    payload = json.loads((await server.api_status()).body.decode("utf-8"))

    assert payload["post_close_tracking_active"] is True
    assert payload["post_close_tracking_stopped"] is False


@pytest.mark.asyncio
async def test_stop_post_close_tracking_api_returns_result(monkeypatch):
    stop = AsyncMock(return_value={"ok": True, "persisted": True})
    events = []
    monkeypatch.setattr(server.f4_tracking, "stop_post_close_observation", stop)
    monkeypatch.setattr(
        server,
        "log",
        lambda event, **fields: events.append((event, fields)),
    )

    response = await server.api_stop_post_close_tracking(_tracking_stop_request())

    assert response.status_code == 200
    assert json.loads(response.body.decode("utf-8"))["ok"] is True
    stop.assert_awaited_once()
    assert events == [
        (
            "F4_POST_CLOSE_TRACKING_STOP_REQUESTED",
            {
                "level": "INFO",
                "request_source": "dashboard",
                "request_source_trusted": False,
                "client_host": "127.0.0.1",
                "client_port": 54321,
                "user_agent": "pytest-browser",
                "origin": "",
                "referer": "http://127.0.0.1:8899/",
                "sec_fetch_site": "",
            },
        )
    ]


@pytest.mark.asyncio
async def test_stop_post_close_tracking_api_rejects_non_closed(monkeypatch):
    monkeypatch.setattr(
        server.f4_tracking,
        "stop_post_close_observation",
        AsyncMock(return_value={"ok": False, "reason": "POSITION_NOT_CLOSED"}),
    )

    response = await server.api_stop_post_close_tracking(_tracking_stop_request())

    assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "referer", "expected"),
    [
        (None, "http://127.0.0.1:8899/", "dashboard"),
        (None, None, "direct_api"),
        ("DaShBoArD", None, "dashboard"),
    ],
)
async def test_stop_post_close_tracking_classifies_request_source(
    monkeypatch,
    source,
    referer,
    expected,
):
    events = []
    monkeypatch.setattr(
        server.f4_tracking,
        "stop_post_close_observation",
        AsyncMock(return_value={"ok": True}),
    )
    monkeypatch.setattr(
        server,
        "log",
        lambda event, **fields: events.append((event, fields)),
    )

    response = await server.api_stop_post_close_tracking(
        _tracking_stop_request(source=source, referer=referer)
    )

    assert response.status_code == 200
    assert events[0][1]["request_source"] == expected
    assert events[0][1]["request_source_trusted"] is False


@pytest.mark.asyncio
async def test_status_includes_tick_history_while_holding_and_closed(monkeypatch):
    s = server.state.get()
    server.live.clear_tick_history()
    server.live._tick_history.append(
        {"ts": "2026-07-06T09:10:00+09:00", "ticker": "005930", "price": 75_000.0})
    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: [])
    monkeypatch.setattr(s, "position_status", "HOLDING")
    monkeypatch.setattr(s, "target_ticker", "005930")
    monkeypatch.setattr(s, "entry_price", 75_000.0)
    monkeypatch.setattr(s, "entry_qty", 1)
    monkeypatch.setattr(s, "remaining_qty", 1)
    monkeypatch.setattr(s, "high_price", 75_500.0)
    monkeypatch.setattr(s, "entry_at", "2026-07-06T09:10:01+09:00")
    server.live._tick_history.append(
        {"ts": "2026-07-06T09:10:01+09:00", "ticker": "005930", "price": 75_500.0})

    resp = await server.api_status()
    body = resp.body.decode("utf-8")

    assert '"tick_history"' in body
    assert '"trade_marks"' in body
    assert '"price":75500.0' in body
    assert '"price":75000.0' not in body

    # 청산 후에도 당일 리뷰용으로 tick 이력을 유지해 내려준다.
    monkeypatch.setattr(s, "position_status", "CLOSED")
    resp = await server.api_status()
    assert '"price":75500.0' in resp.body.decode("utf-8")

    monkeypatch.setattr(s, "position_status", "IDLE")
    resp = await server.api_status()
    body = resp.body.decode("utf-8")
    assert '"tick_history":[]' in body
    assert '"trade_marks":[]' in body

    server.live.clear_tick_history()


@pytest.mark.asyncio
async def test_status_exposes_observed_ticker_prices_while_idle(monkeypatch):
    # 2026-10-23: 진입 2회 미체결 뒤 F4는 1순위(observe_ticker)를 15:20까지 계속
    # 관측하는데, 화면에는 종목도 가격 흐름도 비어 있었다.
    s = server.state.get()
    server.live.clear_tick_history()
    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: [])
    monkeypatch.setattr(
        server, "_read_f1_snapshot",
        lambda: (Path("20261023_090000.jsonl"), [{"ticker": "065450", "name": "빅텍"}]),
    )
    monkeypatch.setattr(s, "position_status", "IDLE")
    monkeypatch.setattr(s, "target_ticker", None)
    monkeypatch.setattr(s, "target_name", None)
    monkeypatch.setattr(s, "observe_ticker", "065450")
    monkeypatch.setattr(s, "entry_at", None)
    server.live.push_tick(4_065.0, ticker="065450")
    server.live.push_tick(9_999.0, ticker="030960")

    payload = json.loads((await server.api_status()).body.decode("utf-8"))

    assert payload["ticker"] is None
    assert payload["observe_ticker"] == "065450"
    assert payload["observe_name"] == "빅텍"
    assert [row["price"] for row in payload["tick_history"]] == [4_065.0]
    assert [row["price"] for row in payload["minute_price_history"]] == [4_065.0]
    assert payload["trade_marks"] == []
    server.live.clear_tick_history()


@pytest.mark.asyncio
async def test_status_observed_prices_exclude_previous_days(monkeypatch):
    s = server.state.get()
    server.live.clear_tick_history()
    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: [])
    monkeypatch.setattr(server, "_read_f1_snapshot", lambda: (None, []))
    monkeypatch.setattr(s, "position_status", "IDLE")
    monkeypatch.setattr(s, "target_ticker", None)
    monkeypatch.setattr(s, "observe_ticker", "065450")
    server.live._tick_history.append(
        {"ts": "2026-07-06T09:10:00+09:00", "ticker": "065450", "price": 3_900.0})

    payload = json.loads((await server.api_status()).body.decode("utf-8"))

    assert payload["observe_ticker"] == "065450"
    assert payload["observe_name"] is None
    assert payload["tick_history"] == []
    server.live.clear_tick_history()


@pytest.mark.asyncio
async def test_status_has_no_observed_ticker_while_holding(monkeypatch):
    s = server.state.get()
    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: [])
    monkeypatch.setattr(s, "position_status", "HOLDING")
    monkeypatch.setattr(s, "target_ticker", "065450")
    monkeypatch.setattr(s, "observe_ticker", "065450")
    monkeypatch.setattr(s, "entry_at", "2026-10-23T09:00:20+09:00")

    payload = json.loads((await server.api_status()).body.decode("utf-8"))

    assert payload["ticker"] == "065450"
    assert payload["observe_ticker"] is None


async def test_status_includes_vi_events_while_holding_and_closed(monkeypatch):
    s = server.state.get()
    server.live.clear_tick_history()
    server.live.record_vi_detected({
        "ts": "2026-07-16T09:13:45+09:00", "frozen_price": 7690.0,
        "vi_kind_code": "1", "cntg_vi_hour": "091333",
        "vi_prc": "7700", "vi_stnd_prc": "7000", "vi_dprt": "10.00",
    })
    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: [])
    monkeypatch.setattr(s, "position_status", "HOLDING")
    monkeypatch.setattr(s, "target_ticker", "004310")
    monkeypatch.setattr(s, "entry_price", 6841.0)
    monkeypatch.setattr(s, "entry_at", "2026-07-16T09:01:16+09:00")

    body = (await server.api_status()).body.decode("utf-8")
    assert '"vi_events"' in body
    assert '"vi_prc":"7700"' in body

    monkeypatch.setattr(s, "position_status", "CLOSED")
    body = (await server.api_status()).body.decode("utf-8")
    assert '"vi_prc":"7700"' in body

    monkeypatch.setattr(s, "position_status", "IDLE")
    body = (await server.api_status()).body.decode("utf-8")
    assert '"vi_events":[]' in body

    server.live.clear_tick_history()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kis_mode", "expected_fallback"),
    [("PAPER", 1.1), ("REAL", 0.20)],
)
async def test_api_settings_survives_invalid_numeric_env(
    monkeypatch, kis_mode, expected_fallback
):
    monkeypatch.setenv("KIS_MODE", kis_mode)
    monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678")
    monkeypatch.setenv("KIS_APP_KEY", "key")
    monkeypatch.setenv("KIS_APP_SECRET", "secret")
    monkeypatch.setenv("KIS_RATE_INTERVAL_SEC", "not-a-number")
    monkeypatch.setenv("F2_RETRY_F1_INTERVAL_SEC", "also-not-a-number")

    resp = await server.api_settings()
    payload = json.loads(resp.body.decode("utf-8"))

    assert payload["valid"] is False
    assert any("KIS_RATE_INTERVAL_SEC" in err for err in payload["errors"])
    assert payload["safety"]["kis_rate_interval_sec"] == expected_fallback


@pytest.mark.asyncio
async def test_api_settings_returns_contract(monkeypatch):
    # main.py??load_dotenv()媛 ?섏쭛 ???ㅼ젣 .env瑜?濡쒕뱶?????덉뼱 ?곗꽑?쒖쐞 env ?쒓굅
    monkeypatch.delenv("KIS_ACCT_NO", raising=False)
    monkeypatch.delenv("KIS_ACCT_CD", raising=False)
    monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678")
    monkeypatch.setenv("KIS_APP_KEY", "key")
    monkeypatch.setenv("KIS_APP_SECRET", "secret")
    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("KIS_RATE_INTERVAL_SEC", "0.2")
    monkeypatch.setattr(server.live, "real_entry_enabled", True)

    resp = await server.api_settings()
    payload = json.loads(resp.body.decode("utf-8"))

    assert payload.items() >= {
        "mode": "PAPER",
        "dry_run": False,
        "auto_trading": None,
        "auto_trading_control": "read_only",
        "valid": True,
        "errors": [],
    }.items()
    assert payload["account"].items() >= {
        "configured": True,
        "account_source": "KIS_ACCOUNT_NO",
        "app_key_configured": True,
        "app_secret_configured": True,
    }.items()
    assert {"paths", "f1", "f2", "f3", "f4", "safety"} <= payload.keys()
    assert payload["f2"]["retry_f1_on_fail_supported"] is False
    assert payload["safety"]["kis_rate_interval_sec"] == 0.2
    assert payload["safety"]["real_entry_enabled"] is True
    assert payload["real_readiness"]["runtime_entry_latch_open"] is True


@pytest.mark.asyncio
async def test_api_settings_failure_preserves_runtime_entry_latch(monkeypatch):
    monkeypatch.setattr(server.live, "real_entry_enabled", True)
    monkeypatch.setattr(
        server.kis_rest,
        "account_no",
        lambda: (_ for _ in ()).throw(RuntimeError("settings failed")),
    )

    resp = await server.api_settings()
    payload = json.loads(resp.body.decode("utf-8"))

    assert payload["valid"] is False
    assert payload["real_readiness"]["runtime_entry_latch_open"] is True


@pytest.mark.asyncio
async def test_api_readiness_failure_preserves_runtime_entry_latch(monkeypatch):
    monkeypatch.setattr(server.live, "real_entry_enabled", True)
    monkeypatch.setattr(
        server.readiness,
        "calculate",
        AsyncMock(side_effect=RuntimeError("readiness failed")),
    )

    resp = await server.api_real_readiness()
    payload = json.loads(resp.body.decode("utf-8"))

    assert resp.status_code == 503
    assert payload["runtime_entry_latch_open"] is True


@pytest.mark.asyncio
async def test_api_settings_reports_empty_priority_account_env(monkeypatch):
    monkeypatch.setenv("KIS_ACCT_NO", "")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678")
    monkeypatch.setenv("KIS_ACCT_CD", "")
    monkeypatch.setenv("KIS_ACCOUNT_TYPE", "01")
    monkeypatch.setenv("KIS_APP_KEY", "key")
    monkeypatch.setenv("KIS_APP_SECRET", "secret")

    resp = await server.api_settings()
    payload = json.loads(resp.body.decode("utf-8"))

    assert payload["valid"] is False
    assert payload["account"]["configured"] is False
    assert payload["account"]["account_source"] == "KIS_ACCT_NO"
    assert any("KIS_ACCT_NO" in err for err in payload["errors"])
    assert any("KIS_ACCT_CD" in err for err in payload["errors"])


@pytest.mark.asyncio
async def test_api_settings_does_not_expose_unwired_f2_retry_flag(monkeypatch):
    monkeypatch.setenv("F2_RETRY_F1_ON_FAIL", "1")

    resp = await server.api_settings()
    payload = json.loads(resp.body.decode("utf-8"))

    assert "retry_f1_on_fail" not in payload["f2"]
    assert payload["f2"]["retry_f1_on_fail_supported"] is False
    assert not any("F2_RETRY_F1_ON_FAIL" in warning for warning in payload["warnings"])


def test_f5_times_come_from_shared_schedule_module():
    """F5 시각이 스케줄과 별도 문자열로 복제되면 안 된다 — 단일 출처 검증."""
    from src import schedule_times as st

    assert server._F5_EXEC_TIME == f"{st.F5_EXEC_H:02d}:{st.F5_EXEC_M:02d}"
    assert server._F5_PRECHECK_TIME == (
        f"{st.F5_PRECHECK_H:02d}:{st.F5_PRECHECK_M:02d}:{st.F5_PRECHECK_S:02d}"
    )


@pytest.mark.asyncio
async def test_api_settings_exposes_f4_timing_vi_and_rest_backup():
    resp = await server.api_settings()
    payload = json.loads(resp.body.decode("utf-8"))

    assert payload["f4"]["force_trailing_time"] == (
        f"{f4_tracking.FORCE_TRAILING_HOUR:02d}:{f4_tracking.FORCE_TRAILING_MINUTE:02d}"
    )
    assert payload["f3"]["total_budget_sec"] == f3_entry.F3_ENTRY_TOTAL_BUDGET_SEC
    assert payload["f3"]["total_budget_enforcement"] is False
    assert payload["f4"]["rest_backup"] == {
        "enabled": f4_tracking.F4_REST_BACKUP_ENABLED,
        "only_when_ws_stale": f4_tracking.F4_REST_ONLY_WHEN_WS_STALE,
        "ws_stale_sec": f4_tracking.F4_WS_STALE_SEC,
        "poll_interval_sec": f4_tracking.F4_REST_POLL_INTERVAL_SEC,
        "post_close_enabled": f4_tracking.F4_POST_CLOSE_REST_BACKUP_ENABLED,
        "post_close_poll_interval_sec": (
            f4_tracking.F4_POST_CLOSE_REST_POLL_INTERVAL_SEC
        ),
    }
    assert payload["f5"] == {
        "timeout_time": f"{F5_EXEC_H:02d}:{F5_EXEC_M:02d}",
        "precheck_time": f"{F5_PRECHECK_H:02d}:{F5_PRECHECK_M:02d}:{F5_PRECHECK_S:02d}",
    }
    # 청산은 장마감 동시호가(15:20) 전에 재시도까지 끝나야 한다.
    assert (F5_EXEC_H, F5_EXEC_M) < (15, 20)
    assert payload["vi"] == {
        "watch_enabled": f4_tracking.VI_WATCH_ENABLED,
        "freeze_suspect_sec": f4_tracking.VI_FREEZE_SUSPECT_SEC,
        "check_cooldown_sec": f4_tracking.VI_CHECK_COOLDOWN_SEC,
    }


@pytest.mark.asyncio
async def test_assets_refresh_fetches_asset_snapshot(monkeypatch):
    fetch = AsyncMock(return_value={"cash": 1_000_000.0})
    monkeypatch.setattr(server, "_asset_snapshot_safe", fetch)

    resp = await server.api_assets(refresh=1)
    body = resp.body.decode("utf-8")

    fetch.assert_awaited_once()
    assert '"cash":1000000.0' in body


@pytest.mark.asyncio
async def test_api_orders_returns_today_orders(tmp_path, monkeypatch):
    await db.init(str(tmp_path / "orders.db"))
    today = "20260702"
    monkeypatch.setattr(server, "_today", lambda: today)
    trade_id = await db.open_trade(today, "005930", 75_000.0, 10)
    order_id = await db.record_order(
        trade_id,
        "ORD001",
        "BUY",
        10,
        0.0,
        "FIRST_BUY",
        "005930",
        "삼성전자",
        trigger_price=75_000.0,
    )
    await db.update_order_fill(order_id, 75_100.0, 10, 120)
    await db.record_entry_order_attempt(
        today,
        "ORD-CANCELLED",
        "064400",
        3,
        75_300.0,
        74_500.0,
        1,
        2,
        "PAPER",
        name="LG씨엔에스",
        order_phase="PYRAMID_BUY",
        status="CANCELLED",
    )
    old_trade_id = await db.open_trade("20260701", "000660", 120_000.0, 1)
    await db.record_order(old_trade_id, "OLD001", "BUY", 1, 120_000.0, "FIRST_BUY", "000660")

    resp = await server.api_orders()
    body = resp.body.decode("utf-8")

    assert '"kis_order_id":"ORD001"' in body
    assert '"order_phase":"FIRST_BUY"' in body
    assert '"order_price":0.0' in body
    assert '"trigger_price":75000.0' in body
    assert '"fill_latency_ms":120' in body
    assert '"name":"삼성전자"' in body
    assert '"status":"FILLED"' in body
    assert '"kis_order_id":"ORD-CANCELLED"' in body
    assert '"order_phase":"PYRAMID_BUY"' in body
    assert '"audit_source":"entry_order_attempts"' in body
    assert '"status":"CANCELLED"' in body
    assert "OLD001" not in body
    await db.close()


@pytest.mark.asyncio
async def test_today_trade_marks_returns_filled_orders_in_time_order(tmp_path, monkeypatch):
    await db.init(str(tmp_path / "marks.db"))
    today = "20260714"
    monkeypatch.setattr(server, "_today", lambda: today)
    trade_id = await db.open_trade(today, "005930", 75_000.0, 10)

    buy_id = await db.record_order(
        trade_id, "ORD-BUY", "BUY", 10, 75_000.0, "FIRST_BUY", "005930", "삼성전자")
    await db.update_order_fill(buy_id, 75_100.0, 10, 120)
    sell_id = await db.record_order(
        trade_id, "ORD-SELL", "SELL", 10, 76_000.0, "CLOSE_SELL", "005930", "삼성전자")
    await db.update_order_fill(sell_id, 76_050.0, 10, 90)
    # 미체결(PENDING) 주문은 마커에서 제외되어야 한다.
    await db.record_order(
        trade_id, "ORD-PENDING", "BUY", 5, 75_200.0, "PYRAMID_BUY", "005930", "삼성전자")
    # 부분체결 후 잔량이 취소된 주문(CANCELLED + fill_qty>0)은 마커에 포함되어야 한다.
    pc_id = await db.record_order(
        trade_id, "ORD-PARTIAL-CANCEL", "SELL", 10, 76_500.0, "TIMEOUT_SELL", "005930", "삼성전자")
    await db.update_order_fill(pc_id, 76_400.0, 3, 80, status="PARTIAL_FILL")
    await db.update_order_status(pc_id, "CANCELLED")
    # 무체결 취소 주문은 제외되어야 한다.
    nc_id = await db.record_order(
        trade_id, "ORD-NOFILL-CANCEL", "SELL", 5, 76_600.0, "TIMEOUT_SELL", "005930", "삼성전자")
    await db.update_order_status(nc_id, "CANCELLED")
    # 다른 날짜의 체결은 제외되어야 한다.
    old_trade_id = await db.open_trade("20260713", "000660", 120_000.0, 1)
    old_id = await db.record_order(
        old_trade_id, "ORD-OLD", "SELL", 1, 120_000.0, "TIMEOUT_SELL", "000660")
    await db.update_order_fill(old_id, 119_000.0, 1, 50)

    marks = await server._today_trade_marks()

    assert [m["order_type"] for m in marks] == ["BUY", "SELL", "SELL"]
    assert [m["order_phase"] for m in marks] == ["FIRST_BUY", "CLOSE_SELL", "TIMEOUT_SELL"]
    assert marks[0]["fill_price"] == 75_100.0
    assert marks[1]["fill_price"] == 76_050.0
    assert marks[2]["fill_price"] == 76_400.0    # 부분체결 후 취소된 주문도 포함
    assert all(m["ticker"] == "005930" for m in marks)
    assert all(m["filled_at"] for m in marks)
    await db.close()


@pytest.mark.asyncio
async def test_api_history_returns_recent_trade_contract(tmp_path):
    await db.init(str(tmp_path / "history.db"))
    old_trade_id = await db.open_trade("20260701", "000660", 120_000.0, 1)
    await db.close_trade(
        old_trade_id, 121_000.0, "TIMEOUT", 0.83, 0.0,
        exit_qty=1, high_price=121_000.0,
    )
    trade_id = await db.open_trade("20260702", "005930", 75_000.0, 10, name="삼성전자")
    await db.mark_pyramided(trade_id)
    await db.close_trade(
        trade_id, 78_750.0, "TRAILING", 5.0, 0.05,
        exit_qty=10, high_price=78_750.0,
    )

    resp = await server.api_history(limit=1)
    rows = json.loads(resp.body.decode("utf-8"))

    assert len(rows) == 1
    assert rows[0].items() >= {
        "date": "20260702",
        "ticker": "005930",
        "name": "삼성전자",
        "entry_price": 75_000.0,
        "exit_price": 78_750.0,
        "pnl_pct": 5.0,
        "close_reason": "TRAILING",
        "highest_step": 0.05,
        "pyramided": 1,
        "status": "CLOSED",
    }.items()
    await db.close()


@pytest.mark.asyncio
async def test_api_stats_returns_strategy_breakdowns_contract(tmp_path):
    await db.init(str(tmp_path / "stats.db"))
    first_id = await db.open_trade("20260701", "005930", 75_000.0, 10)
    await db.mark_pyramided(first_id)
    await db.close_trade(
        first_id, 78_750.0, "TRAILING", 5.0, 0.05,
        exit_qty=10, high_price=78_750.0,
    )

    second_id = await db.open_trade("20260702", "000660", 120_000.0, 1)
    await db.close_trade(
        second_id, 118_800.0, "HARD_STOP", -1.0, 0.0,
        exit_qty=1, high_price=120_000.0,
    )

    third_id = await db.open_trade("20260703", "035420", 200_000.0, 1)
    await db.close_trade(
        third_id, 204_000.0, "TIMEOUT", 2.0, 0.075,
        exit_qty=1, high_price=204_000.0,
    )

    conn = db.get()
    await conn.execute(
        "UPDATE trades SET entry_at=? WHERE id=?", ("2026-07-01T09:10:00+09:00", first_id))
    await conn.execute(
        "UPDATE trades SET entry_at=? WHERE id=?", ("2026-07-02T10:10:00+09:00", second_id))
    await conn.execute(
        "UPDATE trades SET entry_at=? WHERE id=?", ("2026-07-03T09:20:00+09:00", third_id))
    await conn.commit()

    resp = await server.api_stats()
    payload = json.loads(resp.body.decode("utf-8"))

    assert payload["total"] == 3
    assert payload["wins"] == 2
    assert payload["losses"] == 1
    assert payload["by_reason"]["TRAILING"] == {"n": 1, "avg_pnl": 5.0}
    assert payload["by_reason"]["HARD_STOP"] == {"n": 1, "avg_pnl": -1.0}
    assert "by_pyramided" in payload
    assert "by_step" in payload
    assert payload["by_entry_hour"] == [
        {"hour": "09", "n": 2, "avg_pnl": 3.5},
        {"hour": "10", "n": 1, "avg_pnl": -1.0},
    ]
    assert sum(v["n"] for v in payload["by_pyramided"].values()) == 3
    assert sum(v["n"] for v in payload["by_step"].values()) == 3
    await db.close()


@pytest.mark.asyncio
async def test_fetch_asset_snapshot_parses_kis_balance(monkeypatch):
    async def fake_get(*args, **kwargs):
        return {
            "output1": [
                {"pdno": "005930", "prdt_name": "Samsung", "hldg_qty": "2",
                 "ord_psbl_qty": "2", "prpr": "70000", "pchs_avg_pric": "69000",
                 "pchs_amt": "138000", "evlu_amt": "140000",
                 "evlu_pfls_amt": "2000", "evlu_pfls_rt": "1.45"},
                {"pdno": "000660", "hldg_qty": "0"},
            ],
            "output2": [{
                "dnca_tot_amt": "1000000",
                "ord_psbl_cash": "800000",
                "scts_evlu_amt": "500000",
                "tot_evlu_amt": "1500000",
                "evlu_pfls_smtl_amt": "12000",
            }],
        }

    monkeypatch.setattr(server.kis_rest, "get", fake_get)

    result = await server._fetch_asset_snapshot()

    assert result.items() >= {
        "cash": 1_000_000.0,
        "buyable_cash": 800_000.0,
        "buyable_cash_source": "ord_psbl_cash",
        "stock_value": 500_000.0,
        "total_asset": 1_500_000.0,
        "pnl_amount": 12_000.0,
        "holdings_count": 1,
        "source": "KIS",
        "snapshot_source": "KIS",
    }.items()
    assert "captured_at" in result
    assert result["holdings"] == [
        {
            "ticker": "005930",
            "name": "Samsung",
            "qty": 2,
            "orderable_qty": 2,
            "current_price": 70000.0,
            "avg_price": 69000.0,
            "purchase_amount": 138000.0,
            "evaluation_amount": 140000.0,
            "pnl_amount": 2000.0,
            "pnl_pct": 1.45,
        }
    ]


@pytest.mark.asyncio
async def test_fetch_asset_snapshot_saves_to_db(tmp_path, monkeypatch):
    await db.init(str(tmp_path / "assets.db"))

    async def fake_get(*args, **kwargs):
        return {
            "rt_cd": "0",
            "output1": [{"pdno": "005930", "hldg_qty": "2"}],
            "output2": [{
                "dnca_tot_amt": "1000000",
                "ord_psbl_cash": "800000",
                "scts_evlu_amt": "500000",
                "tot_evlu_amt": "1500000",
                "evlu_pfls_smtl_amt": "12000",
            }],
        }

    monkeypatch.setattr(server.kis_rest, "get", fake_get)

    result = await server._fetch_asset_snapshot()
    conn = db.get()
    async with conn.execute("SELECT total_asset, raw_json FROM asset_snapshots") as cur:
        row = await cur.fetchone()

    assert result["asset_snapshot_id"] > 0
    assert "captured_at" in result
    assert row["total_asset"] == pytest.approx(1_500_000.0)
    assert '"rt_cd":"0"' in row["raw_json"]
    await db.close()


@pytest.mark.asyncio
async def test_assets_without_cache_falls_back_to_latest_db_snapshot(tmp_path, monkeypatch):
    await db.init(str(tmp_path / "assets.db"))
    await db.record_asset_snapshot({"total_asset": 2_000_000.0, "cash": 300_000.0, "source": "KIS"})
    monkeypatch.setattr(server, "_ASSET_CACHE", None)

    resp = await server.api_assets(refresh=0)
    body = resp.body.decode("utf-8")

    assert '"total_asset":2000000.0' in body
    assert '"snapshot_source":"DB"' in body
    assert '"captured_at"' in body
    await db.close()


@pytest.mark.asyncio
async def test_status_without_cache_falls_back_to_latest_db_snapshot(tmp_path, monkeypatch):
    await db.init(str(tmp_path / "assets.db"))
    await db.record_asset_snapshot({"total_asset": 2_000_000.0, "cash": 300_000.0, "source": "KIS"})
    monkeypatch.setattr(server, "_ASSET_CACHE", None)

    resp = await server.api_status()
    body = resp.body.decode("utf-8")

    assert '"total_asset":2000000.0' in body
    assert '"snapshot_source":"DB"' in body
    assert '"captured_at"' in body
    await db.close()


@pytest.mark.asyncio
async def test_fetch_asset_snapshot_rejects_kis_error_response(monkeypatch):
    async def fake_get(*args, **kwargs):
        return {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "token expired"}

    monkeypatch.setattr(server.kis_rest, "get", fake_get)

    with pytest.raises(RuntimeError, match="KIS balance error"):
        await server._fetch_asset_snapshot()


@pytest.mark.asyncio
async def test_fetch_asset_snapshot_rejects_missing_balance_summary(monkeypatch):
    async def fake_get(*args, **kwargs):
        return {"rt_cd": "0", "output1": []}

    monkeypatch.setattr(server.kis_rest, "get", fake_get)

    with pytest.raises(RuntimeError, match="missing output2"):
        await server._fetch_asset_snapshot()


@pytest.mark.asyncio
async def test_fetch_asset_snapshot_rejects_invalid_balance_number(monkeypatch):
    async def fake_get(*args, **kwargs):
        return {
            "rt_cd": "0",
            "output1": [],
            "output2": [{
                "dnca_tot_amt": "not-a-number",
                "ord_psbl_cash": "800000",
                "scts_evlu_amt": "500000",
                "tot_evlu_amt": "1500000",
                "evlu_pfls_smtl_amt": "12000",
            }],
        }

    monkeypatch.setattr(server.kis_rest, "get", fake_get)

    with pytest.raises(RuntimeError, match="invalid field dnca_tot_amt"):
        await server._fetch_asset_snapshot()


@pytest.mark.asyncio
async def test_asset_snapshot_safe_first_load_waits_for_inflight_refresh(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fake_fetch():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"cash": 7.0}

    monkeypatch.setattr(server, "_ASSET_CACHE", None)
    monkeypatch.setattr(server, "_ASSET_CACHE_AT", 0.0)
    monkeypatch.setattr(server, "_ASSET_CACHE_LOCK", asyncio.Lock())
    monkeypatch.setattr(server, "_fetch_asset_snapshot", fake_fetch)

    first = asyncio.create_task(server._asset_snapshot_safe())
    await started.wait()
    second = asyncio.create_task(server._asset_snapshot_safe())
    await asyncio.sleep(0)

    assert second.done() is False

    release.set()
    assert await asyncio.gather(first, second) == [{"cash": 7.0}, {"cash": 7.0}]
    assert calls == 1


@pytest.mark.asyncio
async def test_asset_snapshot_safe_records_failure_reason(monkeypatch):
    events = []

    async def fake_fetch():
        raise RuntimeError("KIS balance error rt_cd=1 msg_cd=EGW00123 msg1=token expired")

    monkeypatch.setattr(server, "_ASSET_CACHE", None)
    monkeypatch.setattr(server, "_ASSET_CACHE_AT", 0.0)
    monkeypatch.setattr(server, "_ASSET_LAST_ERROR", None)
    monkeypatch.setattr(server, "_ASSET_CACHE_LOCK", asyncio.Lock())
    monkeypatch.setattr(server, "_fetch_asset_snapshot", fake_fetch)
    monkeypatch.setattr(server, "log", lambda event, **kwargs: events.append((event, kwargs)))

    resp = await server.api_assets(refresh=1)
    body = resp.body.decode("utf-8")

    assert '"assets":null' in body
    assert "EGW00123" in body
    assert events[0][0] == "ASSET_SNAPSHOT_FAILED"
    assert events[0][1]["error_type"] == "RuntimeError"


def test_selection_process_prefers_executed_trade_over_later_restart_pick():
    summary = {
        "selected": {"ticker": "005930", "name": "Samsung", "gap_pct": 0.0396},
        "selected_tickers": ["005930"],
        "liquidity_pass": 3,
        "gap_pass": 3,
        "candidates": [
            {"ticker": "005930", "name": "Samsung"},
            {"ticker": "365660", "name": "Lemon"},
        ],
    }
    logs = [
        {
            "ts": "2026-07-09T09:01:24+09:00",
            "event": "TARGET_LOCKED",
            "ticker": "009150",
            "target_tickers": ["009150", "005930", "365660"],
            "target_names": ["SamsungElecParts", "Samsung", "Lemon"],
        },
        {"ts": "2026-07-09T09:01:36+09:00", "event": "ENTRY_EXECUTED",
         "ticker": "365660", "name": "Lemon"},
        {
            "ts": "2026-07-09T09:09:45+09:00",
            "event": "TARGET_LOCKED",
            "ticker": "005930",
            "target_tickers": ["005930", "005935", "365660"],
            "target_names": ["Samsung", "SamsungPref", "Lemon"],
        },
        {"ts": "2026-07-09T09:09:49+09:00", "event": "F3_FINAL_PICK",
         "ticker": "005930", "name": "Samsung"},
    ]

    anchored = server._summary_with_trade_anchor(summary, logs)
    result = server._selection_process_from_logs(anchored, logs)

    assert anchored["selected"]["ticker"] == "365660"
    assert anchored["selected"]["name"] == "Lemon"
    assert result[0]["ticker"] == "365660"
    assert result[1]["ticker"] == "009150"
    assert result[1]["tickers"] == ["009150", "005930", "365660"]
    assert result[2]["ticker"] == "365660"
    assert result[2]["status"] == "체결"
