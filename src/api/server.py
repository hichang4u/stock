"""FastAPI 웹 서버. UI에 실시간 데이터를 제공한다."""

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

from src import bars, db, indicators, live, readiness, state
from src import warmup as warmup_mod
from src.api import kis_rest
from src.api.status_logic import (
    f1_summary_from_rows as _f1_summary_from_rows,
)
from src.api.status_logic import (
    f3_detail_from_event as _f3_detail_from_event,
)
from src.api.status_logic import (
    latest_today_snapshot_path as _latest_today_snapshot_path,
)
from src.api.status_logic import (
    parse_asset_snapshot_response as _parse_asset_snapshot_response,
)
from src.api.status_logic import (
    pipeline_from_logs as _pipeline_from_logs,
)
from src.modules import f4_tracking, paper_fast_probe
from src.modules.f1_filter import (
    F1_DEADLINE_H,
    F1_DEADLINE_M,
    F1_EXPECTED_QUOTE_CONCURRENCY,
    F1_MARKET_INTERVAL_SEC,
    F1_MIN_CANDIDATES,
    F1_RETRY_INTERVAL_SEC,
    F1_SNAPSHOT_DIR,
    GAP_MAX,
    GAP_MIN,
    HIGH_GAP_MAX,
    HIGH_GAP_MIN_EXPECTED_AMOUNT,
    MIN_EXPECTED_AMOUNT,
)
from src.modules.f3_entry import (
    ALLOC_RATIO,
    F3_ENTRY_MAX_ATTEMPTS,
    F3_ENTRY_RETRY_DEADLINE,
    F3_ENTRY_RETRY_DELAY_SEC,
    F3_ENTRY_TOTAL_BUDGET_SEC,
    F3_FIRST_ORDER_AT,
    F3_LIMIT_FILL_TIMEOUT_SEC,
    F3_PRE_ORDER_QUIET_SEC,
    F3_PYRAMID_AT,
    F3_VI_CHECK_ENABLED,
    FIRST_RATIO,
    GAP_MAX_FILL,
    GAP_MAX_ORDER,
    PYRAMID_MIN_UP,
)
from src.modules.f4_tracking import (
    F4_POST_CLOSE_REST_BACKUP_ENABLED,
    F4_POST_CLOSE_REST_POLL_INTERVAL_SEC,
    F4_REST_BACKUP_ENABLED,
    F4_REST_ONLY_WHEN_WS_STALE,
    F4_REST_POLL_INTERVAL_SEC,
    F4_WS_STALE_SEC,
    FORCE_TRAILING_HOUR,
    FORCE_TRAILING_MINUTE,
    HARD_STOP_RATIO,
    STEP_SIZE,
    STEP_TRAIL,
    VI_CHECK_COOLDOWN_SEC,
    VI_FREEZE_SUSPECT_SEC,
    VI_WATCH_ENABLED,
)
from src.schedule_times import (
    F5_EXEC_H,
    F5_EXEC_M,
    F5_PRECHECK_H,
    F5_PRECHECK_M,
    F5_PRECHECK_S,
)
from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")
_MODE = os.getenv("KIS_MODE", "PAPER")
_LOG_DIR = Path(os.getenv("LOG_DIR", "data/logs"))
_F1_SNAPSHOT_DIR = Path(F1_SNAPSHOT_DIR)
_HTML_DIR = Path(__file__).parent.parent.parent / "docs" / "html"
_STATUS_LOG_LIMIT = 50
_F5_PRECHECK_TIME = f"{F5_PRECHECK_H:02d}:{F5_PRECHECK_M:02d}:{F5_PRECHECK_S:02d}"
_F5_EXEC_TIME = f"{F5_EXEC_H:02d}:{F5_EXEC_M:02d}"
_ASSET_CACHE_TTL_SEC = float(os.getenv("ASSET_CACHE_TTL_SEC", "60"))
_ASSET_CACHE: dict | None = None
_ASSET_CACHE_AT: float = 0.0
_ASSET_CACHE_LOCK = asyncio.Lock()
_ASSET_LAST_ERROR: dict | None = None
_BAL_TR = {"REAL": "TTTC8434R", "PAPER": "VTTC8434R"}


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """트랙 B 관측 계층의 기동·종료 배선.

    main.py가 아니라 여기에 두는 이유는 두 가지다. main.py는
    `src/release.py`의 _STRATEGY_FILES에 있어 손대면 트랙 A의 전략 지문이
    돌고(짝 성과 표본이 초기화된다) 이 파일은 그 목록 밖이다. 그리고
    main.py가 uvicorn을 loop="none"으로 봇의 이벤트 루프 안에서 태스크로
    돌리므로, 이 훅은 틱을 처리하는 것과 같은 루프에서 실행된다.
    """
    bars.install()
    bars.start()
    try:
        yield
    finally:
        bars.stop()


app = FastAPI(
    title="Daily1 Trading UI", docs_url=None, redoc_url=None, lifespan=_lifespan
)


# ─── 상태/선정 요약 헬퍼 ───────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(_HTML_DIR)), name="static")
app.mount("/assets", StaticFiles(directory=str(_HTML_DIR / "assets")), name="assets")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(_HTML_DIR / "index.html"))


def _today() -> str:
    return datetime.now(KST).strftime("%Y%m%d")


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default) == "1"


def _env_source(primary: str, fallback: str) -> str:
    if primary in os.environ:
        return primary
    if fallback in os.environ:
        return fallback
    return "unset"


def _env_float(name: str, default: float, errors: list[str]) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        errors.append(f"{name} 값은 숫자여야 합니다: {raw!r}")
        return default


def _db_path() -> str:
    return str(Path(os.getenv("DB_DIR", "data/db")) / "trading.db")


def _read_today_logs(limit: int | None = None) -> list[dict]:
    path = _LOG_DIR / f"{_today()}.jsonl"
    if not path.exists():
        return []

    raw_lines = path.read_text(encoding="utf-8").splitlines()
    if limit is not None and limit > 0:
        raw_lines = raw_lines[-limit:]

    result: list[dict] = []
    for line in raw_lines:
        try:
            result.append(json.loads(line))
        except Exception:
            pass
    return result


def _latest_f1_snapshot_path() -> Path | None:
    return _latest_today_snapshot_path(_F1_SNAPSHOT_DIR, _today())


def _read_f1_snapshot() -> tuple[Path | None, list[dict]]:
    path = _latest_f1_snapshot_path()
    if path is None:
        return None, []

    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return path, rows


def _fast_candidates_from_logs(logs: list[dict]) -> list[dict]:
    """Minimal last-resort recovery when the durable probe payload is unavailable."""
    selected_event = next(
        (entry for entry in reversed(logs) if entry.get("event") == "PAPER_FAST_PATH_SELECTED"),
        None,
    )
    if not selected_event:
        return []

    tickers = [str(ticker) for ticker in selected_event.get("tickers", []) if ticker]
    if not tickers:
        return []

    locked_event = next(
        (
            entry
            for entry in reversed(logs)
            if entry.get("event") == "TARGET_LOCKED"
            and any(
                str(ticker) in tickers
                for ticker in (entry.get("target_tickers") or [entry.get("ticker")])
                if ticker
            )
        ),
        None,
    )
    names: dict[str, str] = {}
    if locked_event:
        locked_tickers = list(locked_event.get("target_tickers") or [])
        locked_names = list(locked_event.get("target_names") or [])
        for index, ticker in enumerate(locked_tickers):
            if index < len(locked_names) and locked_names[index]:
                names[str(ticker)] = str(locked_names[index])
        if locked_event.get("ticker") and locked_event.get("name"):
            names[str(locked_event["ticker"])] = str(locked_event["name"])

    rows = []
    for ticker in tickers:
        candidate = {
            "ticker": ticker,
            "name": names.get(ticker),
            "gap_source": "fast.selection_log",
        }
        recovered_gap = None
        if locked_event and str(locked_event.get("ticker")) == ticker:
            if locked_event.get("gap_pct") is not None:
                recovered_gap = float(locked_event["gap_pct"]) / 100
                candidate["gap_pct"] = recovered_gap
            candidate["expected_price"] = locked_event.get("expected_price")
            candidate["expected_amount"] = locked_event.get("expected_amount")
        # 갭을 복구한 종목만 통과로 표시한다. 나머지는 선정된 것은 맞지만
        # 근거 수치가 없으므로 통과로 단정하지 않는다(대시보드 오독 방지).
        if recovered_gap is not None:
            candidate["gap_allowed"] = True
        else:
            candidate["gap_reason"] = "GAP_UNVERIFIED"
        rows.append(candidate)
    return rows


def _f1_status_from_logs(logs: list[dict]) -> tuple[str, dict | None]:
    status = "IDLE"
    last_event: dict | None = None
    for entry in logs:
        event = entry.get("event")
        if not str(event).startswith("F1") and event not in {
            "NO_TARGET",
            "PAPER_FAST_PATH_SELECTED",
        }:
            continue
        last_event = entry
        if event == "PAPER_FAST_PATH_SELECTED":
            status = "DONE"
        elif event == "F1_API_ERROR":
            status = "FAILED"
        elif event == "F1_SKIPPED":
            status = "FAILED"
        elif event == "F1_RETRY_WAIT":
            status = "RETRYING"
        elif event in {"F1_FETCH_DONE", "F1_FILTER_EMPTY", "F1_EXPECTED_COMPARE"}:
            status = "RUNNING"
        elif event == "F1_DONE":
            status = "DONE"
        elif event == "NO_TARGET":
            status = "NO_TARGET"
        # F1_SNAPSHOT_SAVED is a weak completion signal. It should only mark
        # DONE when no fetch/filter/done/no-target event has been seen yet.
        elif event == "F1_SNAPSHOT_SAVED" and status == "IDLE":
            status = "DONE"
    return status, last_event


def _latest_entry_event(logs: list[dict]) -> dict | None:
    return next(
        (
            e
            for e in reversed(logs)
            if e.get("event") in {"ENTRY_EXECUTED", "DRY_RUN_ENTRY_EXECUTED"} and e.get("ticker")
        ),
        None,
    )


def _event_ts(entry: dict | None) -> str:
    return str(entry.get("ts") or "") if entry else ""


def _at_or_before(entry: dict, cutoff_ts: str) -> bool:
    if not cutoff_ts:
        return True
    ts = _event_ts(entry)
    return not ts or ts <= cutoff_ts


def _ticker_in_event(ticker: str | None, entry: dict | None) -> bool:
    if not ticker or not entry:
        return False
    tickers = [str(t) for t in (entry.get("target_tickers") or []) if t]
    if not tickers and entry.get("ticker"):
        tickers = [str(entry.get("ticker"))]
    return str(ticker) in tickers


def _summary_with_trade_anchor(summary: dict, logs: list[dict], current_state=None) -> dict:
    """Prefer the actually executed/restored trade over the latest F1 snapshot display."""
    entry_event = _latest_entry_event(logs)
    ticker = entry_event.get("ticker") if entry_event else None
    name = entry_event.get("name") if entry_event else None

    if not ticker and current_state is not None:
        has_trade = bool(
            getattr(current_state, "trade_id", 0)
            or getattr(current_state, "entry_qty", None)
            or getattr(current_state, "entry_price", None)
        )
        status_now = getattr(current_state, "position_status", None)
        if has_trade and status_now in {"ENTERING", "HOLDING", "EXITING", "CLOSED"}:
            ticker = getattr(current_state, "target_ticker", None)
            name = getattr(current_state, "target_name", None)

    if not ticker:
        return summary

    candidates = list(summary.get("candidates") or [])
    matched: dict = next((c for c in candidates if str(c.get("ticker")) == str(ticker)), {})
    selected = {**matched, "ticker": ticker}
    if name or matched.get("name"):
        selected["name"] = name or matched.get("name")

    return {
        **summary,
        "selected": selected,
        "selected_tickers": [ticker],
        "selection_source": "TRADE_EXECUTED" if entry_event else "STATE_TRADE",
    }


def _selection_process_from_logs(summary: dict, logs: list[dict]) -> list[dict]:
    selected = summary.get("selected") or {}
    f1_ticker = selected.get("ticker")
    f1_count = summary.get("liquidity_pass") or summary.get("gap_pass") or 0
    candidate_tickers = {
        str(c.get("ticker")) for c in summary.get("candidates", []) if c.get("ticker")
    }
    candidate_names = {
        str(c.get("ticker")): c.get("name")
        for c in summary.get("candidates", [])
        if isinstance(c, dict) and c.get("ticker") and c.get("name")
    }
    if f1_ticker and selected.get("name"):
        candidate_names[str(f1_ticker)] = selected.get("name")

    def stock_label(ticker: str | None, name: str | None = None) -> str:
        code = str(ticker) if ticker else ""
        label_name = name or candidate_names.get(code)
        if code and label_name:
            return f"{code} {label_name}"
        return code or (label_name or "")

    steps = [
        {
            "key": "f1",
            "phase": "F1 선정",
            "tickers": summary.get("selected_tickers") or ([f1_ticker] if f1_ticker else []),
            "ticker": f1_ticker,
            "name": selected.get("name"),
            "gap_pct": selected.get("gap_pct"),
            "expected_amount": selected.get("expected_amount"),
            "status": "완료" if f1_ticker else "대기",
            "detail": f"{f1_count}개 후보",
        }
    ]

    entry_event = _latest_entry_event(logs)
    entry_ticker = entry_event.get("ticker") if entry_event else None
    entry_ts = _event_ts(entry_event)
    f2_event = next(
        (
            e
            for e in reversed(logs)
            if e.get("event") == "TARGET_LOCKED"
            and _at_or_before(e, entry_ts)
            and (not entry_ticker or _ticker_in_event(str(entry_ticker), e))
        ),
        None,
    )
    f2_tickers = []
    if f2_event:
        f2_tickers = list(f2_event.get("target_tickers") or [])
        if not f2_tickers and f2_event.get("ticker"):
            f2_tickers = [f2_event.get("ticker")]
        if candidate_tickers and not (set(map(str, f2_tickers)) & candidate_tickers):
            f2_event = None
            f2_tickers = []
    f2_event_names = list(f2_event.get("target_names") or []) if f2_event else []
    f2_names = [
        (f2_event_names[idx] if idx < len(f2_event_names) else None)
        or candidate_names.get(str(ticker))
        for idx, ticker in enumerate(f2_tickers)
    ]
    steps.append(
        {
            "key": "f2",
            "phase": "F2 잠금",
            "tickers": f2_tickers,
            "ticker": f2_event.get("ticker") if f2_event else None,
            "names": f2_names,
            "name": (f2_event.get("name") or (f2_names[0] if f2_names else None))
            if f2_event
            else None,
            "gap_pct": (
                (float(f2_event["gap_pct"]) / 100)
                if f2_event and f2_event.get("gap_pct") is not None
                else None
            ),
            "expected_price": f2_event.get("expected_price") if f2_event else None,
            "expected_amount": f2_event.get("expected_amount") if f2_event else None,
            "status": "잠금" if f2_event else "대기",
            "detail": (
                ", ".join(
                    stock_label(ticker, f2_names[idx] if idx < len(f2_names) else None)
                    for idx, ticker in enumerate(f2_tickers)
                )
                if f2_tickers
                else "최대 3개 lock"
            ),
        }
    )

    f3_events = {
        "F3_FINAL_PICK",
        "ENTRY_ORDER_SENT",
        "ENTRY_EXECUTED",
        "F3_ENTRY_BLOCKED",
        "F3_SKIPPED",
        "GAP_CHANGED",
        "GAP_RECHECK_UNAVAILABLE",
        "BUYABLE_QTY_QUERY_FAILED",
        "BUYABLE_QTY_ZERO",
    }
    f3_event = entry_event or (
        next((e for e in reversed(logs) if e.get("event") in f3_events), None) if f2_event else None
    )
    f3_status = {
        "F3_FINAL_PICK": "최종",
        "ENTRY_ORDER_SENT": "주문전송",
        "ENTRY_EXECUTED": "체결",
        "F3_ENTRY_BLOCKED": "차단",
        "F3_SKIPPED": "생략",
        "GAP_CHANGED": "제외",
        "GAP_RECHECK_UNAVAILABLE": "차단",
        "BUYABLE_QTY_QUERY_FAILED": "차단",
        "BUYABLE_QTY_ZERO": "차단",
    }.get(str(f3_event.get("event") or "") if f3_event else "", "대기")
    f3_ticker = f3_event.get("ticker") if f3_event else None
    steps.append(
        {
            "key": "f3",
            "phase": "F3 최종",
            "tickers": [f3_ticker] if f3_ticker else [],
            "ticker": f3_ticker,
            "name": (f3_event.get("name") or candidate_names.get(str(f3_ticker)))
            if f3_event
            else None,
            "expected_price": f3_event.get("expected_price") if f3_event else None,
            "status": f3_status,
            "detail": _f3_detail_from_event(f3_event),
        }
    )
    return steps


async def _fetch_asset_snapshot() -> dict:
    mode = os.getenv("KIS_MODE", "PAPER")
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/trading/inquire-balance",
        tr_id=_BAL_TR[mode],
        params=kis_rest.balance_inquiry_params(),
    )
    snapshot = {
        **_parse_asset_snapshot_response(resp),
        "captured_at": datetime.now(KST).isoformat(),
    }
    try:
        snapshot_id = await db.record_asset_snapshot(snapshot, raw=resp)
        snapshot = {**snapshot, "asset_snapshot_id": snapshot_id, "snapshot_source": "KIS"}
    except RuntimeError:
        snapshot = {**snapshot, "snapshot_source": "KIS"}
    except Exception as exc:
        log("ASSET_SNAPSHOT_SAVE_FAILED", level="WARN", error=repr(exc))
        snapshot = {**snapshot, "snapshot_source": "KIS"}
    return snapshot


async def _asset_snapshot_safe() -> dict | None:
    global _ASSET_CACHE, _ASSET_CACHE_AT, _ASSET_LAST_ERROR
    now = asyncio.get_running_loop().time()
    if _ASSET_CACHE is not None and now - _ASSET_CACHE_AT < _ASSET_CACHE_TTL_SEC:
        return _ASSET_CACHE
    # With a stale cache, concurrent refreshes return the stale value immediately.
    # On first load there is no safe value to show, so concurrent callers wait for
    # the in-flight request and then reuse its newly populated cache.
    if _ASSET_CACHE is not None and _ASSET_CACHE_LOCK.locked():
        return _ASSET_CACHE
    async with _ASSET_CACHE_LOCK:
        now = asyncio.get_running_loop().time()
        if _ASSET_CACHE is not None and now - _ASSET_CACHE_AT < _ASSET_CACHE_TTL_SEC:
            return _ASSET_CACHE
        try:
            _ASSET_CACHE = await _fetch_asset_snapshot()
            _ASSET_CACHE_AT = now
            _ASSET_LAST_ERROR = None
            return _ASSET_CACHE
        except Exception as exc:
            _ASSET_LAST_ERROR = {
                "type": type(exc).__name__,
                "message": str(exc) or repr(exc),
            }
            log(
                "ASSET_SNAPSHOT_FAILED",
                level="WARN",
                error_type=_ASSET_LAST_ERROR["type"],
                error=_ASSET_LAST_ERROR["message"],
                has_stale_cache=_ASSET_CACHE is not None,
            )
            return _ASSET_CACHE


def _asset_snapshot_cached() -> dict | None:
    return _ASSET_CACHE


async def _latest_asset_snapshot_from_db() -> dict | None:
    try:
        return await db.latest_asset_snapshot()
    except RuntimeError:
        return None
    except Exception as exc:
        log("ASSET_SNAPSHOT_LOAD_FAILED", level="WARN", error=repr(exc))
        return None


# ─── /api/status ──────────────────────────────────────────────────────


async def _today_trade_marks() -> list[dict]:
    """오늘 체결된 매수/매도 주문 목록 — UI 가격흐름 차트의 매매 마커용."""
    try:
        conn = db.get()
        async with conn.execute(
            """SELECT o.order_type, o.order_phase, o.ticker,
                      o.fill_price, o.fill_qty, o.filled_at
               FROM orders o
               JOIN trades t ON t.id = o.trade_id
               WHERE t.date = ? AND o.fill_price > 0
                 AND (o.status IN ('FILLED', 'PARTIAL_FILL')
                      OR (o.status = 'CANCELLED' AND o.fill_qty > 0))
               ORDER BY o.filled_at ASC, o.id ASC""",
            (_today(),),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        log("API_STATUS_TRADE_MARKS_FAILED", level="WARN", error=repr(exc))
        return []


@app.get("/api/status")
async def api_status() -> JSONResponse:
    s = state.get()
    logs = _read_today_logs(limit=_STATUS_LOG_LIMIT)
    assets = _asset_snapshot_cached() or await _latest_asset_snapshot_from_db()
    entry = s.entry_price or 0.0
    cur = live.last_tick_price
    pnl_pct = round((cur / entry - 1) * 100, 2) if (cur and entry) else None

    # 스탑 기준가 계산 — Hard Stop은 진입가 기준, Trail Stop은 활성 시 최고 스텝 기준
    hard_stop = round(entry * (1 - HARD_STOP_RATIO)) if entry else None
    trail_stop: float | None = None
    if s.trailing_active and entry and s.highest_step:
        trail_stop = round(entry * (1 + s.highest_step - STEP_TRAIL))

    return JSONResponse(
        {
            "mode": _MODE,
            "position_status": s.position_status,
            "ticker": s.target_ticker,
            "name": s.target_name,
            "entry_price": s.entry_price,
            "entry_at": s.entry_at,
            "entry_qty": s.entry_qty,
            "remaining_qty": s.remaining_qty,
            "high_price": s.high_price,
            "current_price": cur,
            "pnl_pct": pnl_pct,
            "trailing_active": s.trailing_active,
            "highest_step": s.highest_step,
            "hard_stop": hard_stop,
            "trail_stop": trail_stop,
            "ws_connected": live.ws_connected,
            "ntp_offset_ms": live.ntp_offset_ms,
            "ntp_level": live.ntp_level,
            "close_reason": s.close_reason,
            "post_close_tracking_active": f4_tracking.post_close_observation_active(),
            "post_close_tracking_stopped": s.post_close_tracking_stopped,
            "assets": assets,
            # 청산(CLOSED) 후에도 당일 리뷰용으로 tick 이력을 유지해 내려준다.
            "tick_history": (
                live.tick_history(s.target_ticker, since=s.entry_at)
                if s.position_status in ("HOLDING", "EXITING", "CLOSED")
                else []
            ),
            "minute_price_history": (
                live.minute_price_history(s.target_ticker, since=s.entry_at)
                if s.position_status in ("HOLDING", "EXITING", "CLOSED")
                else []
            ),
            "trade_marks": (
                await _today_trade_marks()
                if s.position_status in ("HOLDING", "EXITING", "CLOSED")
                else []
            ),
            "vi_events": (
                live.vi_events if s.position_status in ("HOLDING", "EXITING", "CLOSED") else []
            ),
            **_pipeline_from_logs(logs, s.position_status),
        }
    )


@app.post("/api/tracking/stop")
async def api_stop_post_close_tracking(request: Request) -> JSONResponse:
    """매도 완료 후 진행 중인 가격 관측만 수동 종료한다."""
    client = request.client
    explicit_source = str(request.headers.get("x-tracking-source") or "").strip().lower()
    referer = str(request.headers.get("referer") or "")[:300]
    log(
        "F4_POST_CLOSE_TRACKING_STOP_REQUESTED",
        level="INFO",
        request_source=(
            explicit_source[:80]
            if explicit_source
            else "dashboard" if referer else "direct_api"
        ),
        request_source_trusted=False,
        client_host=client.host if client else None,
        client_port=client.port if client else None,
        user_agent=str(request.headers.get("user-agent") or "")[:200],
        origin=str(request.headers.get("origin") or "")[:200],
        referer=referer,
        sec_fetch_site=str(request.headers.get("sec-fetch-site") or "")[:40],
    )
    result = await f4_tracking.stop_post_close_observation()
    if not result.get("ok"):
        return JSONResponse(result, status_code=409)
    return JSONResponse(result)


# ─── /api/logs ────────────────────────────────────────────────────────


@app.get("/api/settings")
async def api_settings() -> JSONResponse:
    try:
        errors: list[str] = []
        warnings: list[str] = []
        mode = os.getenv("KIS_MODE", "PAPER")
        dry_run = _env_flag("DRY_RUN")
        account_configured = bool(kis_rest.account_no())
        app_key_configured = bool(os.getenv("KIS_APP_KEY"))
        app_secret_configured = bool(os.getenv("KIS_APP_SECRET"))
        kis_rate_interval_sec = _env_float(
            "KIS_RATE_INTERVAL_SEC", kis_rest.default_rate_interval(), errors
        )
        try:
            readiness_report = await readiness.calculate()
        except Exception as readiness_error:
            log(
                "REAL_READINESS_FAILED",
                level="WARN",
                error=repr(readiness_error),
            )
            readiness_report = {
                "percent": 0,
                "eligible_for_real": False,
                "groups": [],
                "blockers": ["준비도 계산 실패"],
            }
        readiness_report = {
            **readiness_report,
            "runtime_entry_latch_open": bool(live.real_entry_enabled),
        }

        if "KIS_ACCT_NO" in os.environ and not os.getenv("KIS_ACCT_NO", ""):
            errors.append("KIS_ACCT_NO is set but empty.")
        if "KIS_ACCT_CD" in os.environ and not os.getenv("KIS_ACCT_CD", ""):
            errors.append("KIS_ACCT_CD is set but empty.")
        if mode == "REAL":
            warnings.append("REAL 모드입니다. 주문 전 계좌와 KIS URL을 확인하세요.")
        if not account_configured:
            errors.append("KIS 계좌번호가 설정되지 않았습니다.")
        if not app_key_configured or not app_secret_configured:
            errors.append("KIS API 키/시크릿이 모두 설정되지 않았습니다.")
        if dry_run:
            warnings.append("DRY_RUN이 켜져 있어 외부 주문/API 경로가 시뮬레이션 또는 우회됩니다.")

        return JSONResponse(
            {
                "mode": mode,
                "dry_run": dry_run,
                "auto_trading": None,
                "auto_trading_control": "read_only",
                "valid": not errors,
                "errors": errors,
                "warnings": warnings,
                "paths": {
                    "db": _db_path(),
                    "logs": os.getenv("LOG_DIR", "data/logs"),
                    "state": os.getenv("STATE_DIR", "data/state"),
                    "f1_snapshots": str(_F1_SNAPSHOT_DIR),
                },
                "account": {
                    "configured": account_configured,
                    "account_source": _env_source("KIS_ACCT_NO", "KIS_ACCOUNT_NO"),
                    "product_code_source": _env_source("KIS_ACCT_CD", "KIS_ACCOUNT_TYPE"),
                    "app_key_configured": app_key_configured,
                    "app_secret_configured": app_secret_configured,
                },
                "f1": {
                    "core_gap_pct": [round(GAP_MIN * 100, 2), round(GAP_MAX * 100, 2)],
                    "high_gap_pct": [round(GAP_MAX * 100, 2), round(HIGH_GAP_MAX * 100, 2)],
                    "high_gap_min_amount": HIGH_GAP_MIN_EXPECTED_AMOUNT,
                    "min_expected_amount": MIN_EXPECTED_AMOUNT,
                    "min_candidates": F1_MIN_CANDIDATES,
                    "retry_deadline": f"{F1_DEADLINE_H:02d}:{F1_DEADLINE_M:02d}",
                    "retry_interval_sec": F1_RETRY_INTERVAL_SEC,
                    "expected_quote_concurrency": F1_EXPECTED_QUOTE_CONCURRENCY,
                    "market_interval_sec": F1_MARKET_INTERVAL_SEC,
                },
                "f2": {
                    "lockup": "target selection only",
                    "retry_f1_on_fail_supported": False,
                },
                "f3": {
                    "alloc_ratio_pct": round(ALLOC_RATIO * 100, 2),
                    "first_ratio_pct": round(FIRST_RATIO * 100, 2),
                    "pyramid_ratio_pct": round((1 - FIRST_RATIO) * 100, 2),
                    "pyramid_min_up_pct": round(PYRAMID_MIN_UP * 100, 2),
                    "order_gap_max_pct": round(GAP_MAX_ORDER * 100, 2),
                    "fill_gap_max_pct": round(GAP_MAX_FILL * 100, 2),
                    "first_order_at": F3_FIRST_ORDER_AT,
                    "pyramid_at": F3_PYRAMID_AT,
                    "max_attempts": F3_ENTRY_MAX_ATTEMPTS,
                    "retry_delay_sec": F3_ENTRY_RETRY_DELAY_SEC,
                    "limit_fill_timeout_sec": F3_LIMIT_FILL_TIMEOUT_SEC,
                    "retry_deadline": F3_ENTRY_RETRY_DEADLINE,
                    "total_budget_sec": F3_ENTRY_TOTAL_BUDGET_SEC,
                    "total_budget_enforcement": False,
                    "pre_order_quiet_sec": F3_PRE_ORDER_QUIET_SEC,
                    "vi_check_enabled": F3_VI_CHECK_ENABLED,
                },
                "f4": {
                    "hard_stop_pct": round(HARD_STOP_RATIO * 100, 2),
                    "step_size_pct": round(STEP_SIZE * 100, 2),
                    "step_trail_pct": round(STEP_TRAIL * 100, 2),
                    "force_trailing_time": (
                        f"{FORCE_TRAILING_HOUR:02d}:{FORCE_TRAILING_MINUTE:02d}"
                    ),
                    "rest_backup": {
                        "enabled": F4_REST_BACKUP_ENABLED,
                        "only_when_ws_stale": F4_REST_ONLY_WHEN_WS_STALE,
                        "ws_stale_sec": F4_WS_STALE_SEC,
                        "poll_interval_sec": F4_REST_POLL_INTERVAL_SEC,
                        "post_close_enabled": F4_POST_CLOSE_REST_BACKUP_ENABLED,
                        "post_close_poll_interval_sec": (F4_POST_CLOSE_REST_POLL_INTERVAL_SEC),
                    },
                },
                "f5": {
                    "timeout_time": _F5_EXEC_TIME,
                    "precheck_time": _F5_PRECHECK_TIME,
                },
                "vi": {
                    "watch_enabled": VI_WATCH_ENABLED,
                    "freeze_suspect_sec": VI_FREEZE_SUSPECT_SEC,
                    "check_cooldown_sec": VI_CHECK_COOLDOWN_SEC,
                },
                "safety": {
                    "real_mode_warning": mode == "REAL",
                    "kis_rate_interval_sec": kis_rate_interval_sec,
                    "real_entry_enabled": bool(live.real_entry_enabled),
                },
                "real_readiness": readiness_report,
            }
        )
    except Exception as exc:
        log("API_SETTINGS_FAILED", level="WARN", error=repr(exc))
        return JSONResponse(
            {
                "mode": os.getenv("KIS_MODE", "PAPER"),
                "dry_run": _env_flag("DRY_RUN"),
                "auto_trading": None,
                "auto_trading_control": "read_only",
                "valid": False,
                "errors": [f"설정 API 처리 실패: {type(exc).__name__}"],
                "warnings": [],
                "paths": {},
                "account": {"configured": False},
                "f1": {},
                "f2": {"retry_f1_on_fail_supported": False},
                "f3": {},
                "f4": {},
                "f5": {},
                "vi": {},
                "safety": {"real_entry_enabled": bool(live.real_entry_enabled)},
                "real_readiness": {
                    "percent": 0,
                    "eligible_for_real": False,
                    "groups": [],
                    "blockers": ["설정 API 처리 실패"],
                    "runtime_entry_latch_open": bool(live.real_entry_enabled),
                },
            }
        )


@app.get("/api/readiness")
async def api_real_readiness() -> JSONResponse:
    try:
        report = await readiness.calculate()
        return JSONResponse(
            {
                **report,
                "runtime_entry_latch_open": bool(live.real_entry_enabled),
            }
        )
    except Exception as exc:
        log("REAL_READINESS_FAILED", level="WARN", error=repr(exc))
        return JSONResponse(
            {
                "percent": 0,
                "eligible_for_real": False,
                "blockers": [f"준비도 계산 실패: {type(exc).__name__}"],
                "runtime_entry_latch_open": bool(live.real_entry_enabled),
            },
            status_code=503,
        )


@app.get("/api/assets")
async def api_assets(refresh: int = 0) -> JSONResponse:
    assets = await _asset_snapshot_safe() if refresh else _asset_snapshot_cached()
    if assets is None and not refresh:
        assets = await _latest_asset_snapshot_from_db()
    return JSONResponse({"assets": assets, "error": None if assets else _ASSET_LAST_ERROR})


@app.get("/api/logs")
async def api_logs(n: int = 60) -> JSONResponse:
    lines = _read_today_logs(limit=n)
    lines.reverse()
    return JSONResponse(lines)


# ─── /api/orders ──────────────────────────────────────────────────────


@app.get("/api/orders")
async def api_orders(limit: int = 60) -> JSONResponse:
    try:
        conn = db.get()
        async with conn.execute(
            """SELECT * FROM (
                   SELECT o.id, o.kis_order_id, o.order_type, o.order_phase,
                          o.ticker, o.name, o.order_qty, o.order_price, o.trigger_price,
                          o.fill_price, o.fill_qty, o.fill_latency_ms,
                          o.status, o.ordered_at, o.filled_at,
                          o.error_code, o.error_msg, t.date,
                          'orders' AS audit_source
                     FROM orders o
                     JOIN trades t ON t.id = o.trade_id
                    WHERE t.date = ?
                   UNION ALL
                   SELECT -a.id AS id, a.kis_order_id, 'BUY' AS order_type,
                          a.order_phase,
                          a.ticker, a.name, a.order_qty, a.order_price, a.trigger_price,
                          a.fill_price, a.fill_qty, a.fill_latency_ms,
                          a.status, a.ordered_at,
                          CASE WHEN a.status IN ('FILLED','PARTIAL_FILL')
                               THEN a.updated_at ELSE NULL END AS filled_at,
                          NULL AS error_code, NULL AS error_msg, a.date,
                          'entry_order_attempts' AS audit_source
                     FROM entry_order_attempts a
                    WHERE a.date = ?
                      AND NOT EXISTS (
                          SELECT 1
                            FROM orders o2
                            JOIN trades t2 ON t2.id = o2.trade_id
                           WHERE t2.date = a.date
                             AND o2.kis_order_id = a.kis_order_id
                      )
               )
               ORDER BY ordered_at DESC, id DESC
               LIMIT ?""",
            (_today(), _today(), limit),
        ) as cur:
            rows = await cur.fetchall()
        return JSONResponse([dict(r) for r in rows])
    except Exception as exc:
        log("API_ORDERS_FAILED", level="WARN", error=repr(exc))
        return JSONResponse([])


# ─── /api/history ─────────────────────────────────────────────────────


@app.get("/api/f1")
async def api_f1() -> JSONResponse:
    logs = _read_today_logs(limit=500)
    status, last_event = _f1_status_from_logs(logs)
    snapshot_path, rows = _read_f1_snapshot()
    selection_data_source = "F1_SNAPSHOT" if rows else None
    if not rows:
        snapshot_path, rows = paper_fast_probe.load_persisted_open_candidates()
        if rows:
            selection_data_source = "PAPER_FAST_PROBE"
    if not rows:
        rows = _fast_candidates_from_logs(logs)
        if rows:
            selection_data_source = "PAPER_FAST_LOG"
    summary = _summary_with_trade_anchor(_f1_summary_from_rows(rows), logs, state.get())
    if rows and status in {"IDLE", "NO_TARGET"}:
        status = "DONE" if summary["gap_pass"] else "NO_TARGET"

    return JSONResponse(
        {
            "status": status,
            "last_event": last_event,
            "snapshot_name": snapshot_path.name if snapshot_path else None,
            "updated_at": (
                datetime.fromtimestamp(snapshot_path.stat().st_mtime, tz=KST).isoformat()
                if snapshot_path
                else None
            ),
            "selection_data_source": selection_data_source,
            "selection_process": _selection_process_from_logs(summary, logs),
            **summary,
        }
    )


_BARS_DATE_RE = re.compile(r"^\d{8}$")
_BARS_TICKER_RE = re.compile(r"^[A-Za-z0-9]{1,12}$")
# 정규장 1분 봉은 하루 381개다(연속매매 380 + 단일가 종가 1). 그 몇 배 위를 상한으로
# 둔다 — 이 위의 기간은 오타이지 의도가 아니다.
_BARS_MAX_PERIOD = 2000


def _valid_period(value: int) -> bool:
    return 1 <= value <= _BARS_MAX_PERIOD


# 차트가 그리는 이동평균. `sma` 파라미터와 별개다 — 그쪽은 전략이 쓰는 단일
# 설정값이고, 이쪽은 증권사 차트의 관례적 3종 세트다. 지표를 브라우저가 아니라
# 서버에서 계산하는 원칙(설계 §9)은 그대로 지킨다.
_BARS_MA_PERIODS = (5, 20, 60)
_BARS_VOL_MA_PERIODS = (5, 20)


def _bar_minute(value: object) -> int | None:
    """'HHMMSS' → 자정부터의 분. 형식이 아니면 None."""
    text = str(value or "")
    if len(text) < 4 or not text[:4].isdigit():
        return None
    hour, minute = int(text[:2]), int(text[2:4])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _bar_gaps(rows: list) -> list[dict]:
    """봉이 빠진 구간. 체결이 없던 분(VI 정지 등)이 여기로 드러난다.

    분봉 정정이 돌면 거래소가 0거래량 봉으로 메워 준다 — 2026-08-28에
    043200이 VI로 145초 멈췄을 때 09:03·09:04가 그렇게 채워졌다. 그런데 그
    정정은 09:00~09:11에 금지돼 있다. B가 판단해야 하는 바로 그 창에서만
    계열에 구멍이 남고, 09:11을 넘기면 같은 규칙이 다른 계열을 본다는 뜻이다.
    그래서 갭을 감추지 않고 사실로 내보낸다.

    `jump_pct`가 진단값이다. 단일가로 재개된 구간은 값이 크고, 그냥 체결이
    뜸했던 분은 0 근처다.
    """
    gaps: list[dict] = []
    previous_row: dict | None = None
    previous_minute: int | None = None
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        minute = _bar_minute(row.get("time"))
        if minute is None:
            # 시각을 못 읽는 봉. 봉은 있는데 자리를 모르는 것이므로 추적을
            # 끊는다 — 건너뛰고 앞뒤를 이으면 실재하는 봉을 없는 분으로
            # 세어 있지도 않은 갭을 만든다.
            previous_row = None
            previous_minute = None
            continue
        # 둘은 같이 세팅되고 같이 지워진다. 함께 좁혀야 타입이 따라온다.
        if previous_minute is not None and previous_row is not None:
            missing = minute - previous_minute - 1
            if missing > 0:
                close = previous_row.get("close")
                opening = row.get("open")
                jump = None
                if (
                    isinstance(close, (int, float))
                    and close
                    and isinstance(opening, (int, float))
                ):
                    jump = round((opening - close) / close * 100, 2)
                gaps.append({
                    "after": previous_row.get("time"),
                    "resume": row.get("time"),
                    # 재개 봉의 인덱스. 차트가 두 캔들 사이에 경계를 그릴 때
                    # 쓴다 — x축이 인덱스 기준이라 빠진 분에는 폭이 없다.
                    "index": index,
                    "missing": missing,
                    "jump_pct": jump,
                })
        previous_row = row
        previous_minute = minute
    return gaps


@app.get("/api/bars")
async def api_bars(
    track: str = "B",
    date: str = "",
    ticker: str = "",
    prev: str = "",
    sma: int = 20,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> JSONResponse:
    """트랙 B의 1분 봉과 지표.

    지표를 브라우저가 아니라 여기서 계산한다 — 전략 판정과 차트가 같은 순수
    함수를 타야 "차트는 신호인데 봇은 안 샀다"는 혼란이 없다.

    `date`·`ticker`는 파일 경로 조합에 쓰이고, 네 기간(`sma`/`fast`/`slow`/
    `signal`)은 indicators가 0 이하에서 ValueError를 올리므로 함께 검증한다 —
    검증 실패는 200과 빈 배열로 조용히 처리하되 `meta.source`를 "invalid"로
    남겨 원인을 드러낸다 (UI가 30초마다 폴링하므로 에러를 내면 안 된다).
    """
    trade_date = date or datetime.now(KST).strftime("%Y%m%d")
    target = ticker or (state.get().target_ticker or "")

    rows: list = []
    source = "empty"
    if (
        not _BARS_DATE_RE.match(trade_date)
        or (target and not _BARS_TICKER_RE.match(target))
        or not all(_valid_period(p) for p in (sma, fast, slow, signal))
    ):
        source = "invalid"
    elif target:
        rows = bars.series(trade_date, target)
        if rows:
            source = "memory"
        else:
            path = bars.bars_path(trade_date, target)
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
                source = "file"
            except (OSError, ValueError):
                rows = []
                source = "empty"

    # 지표 워밍업 — 전 거래일 봉을 앞에 붙여 09:00부터 증권사와 같은 값을 낸다.
    # 캔들은 당일 것만 돌려준다. 화면이 전일 봉을 그리면 안 된다.
    warm: list = []
    if rows and prev and _BARS_DATE_RE.match(prev) and target:
        try:
            warm = json.loads(
                bars.bars_path(prev, target).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            warm = []
    warmed_rows, offset = warmup_mod.combine(warmup_mod.usable(warm), rows)

    def _sma(period: int, field: str = "close") -> list:
        if not rows:
            return []
        return indicators.sma(warmed_rows, period, field=field)[offset:]

    return JSONResponse({
        "date": trade_date,
        "ticker": target,
        "track": track,
        "bars": rows,
        "indicators": {
            "sma": _sma(sma),
            "macd": (
                indicators.macd(warmed_rows, fast, slow, signal)[offset:]
                if rows else []
            ),
            "ma": {str(p): _sma(p) for p in _BARS_MA_PERIODS} if rows else {},
            "vol_ma": {
                str(p): _sma(p, field="volume") for p in _BARS_VOL_MA_PERIODS
            } if rows else {},
        },
        "warmup": warmup_mod.meta(warm, days=1 if warm else 0),
        "meta": {
            "bar_count": len(rows),
            "confirmed_count": sum(1 for r in rows if r.get("confirmed")),
            "spike_dropped": sum(int(r.get("spike_dropped") or 0) for r in rows),
            "tick_derived_missing": sum(1 for r in rows if r.get("tick_derived") is None),
            "gaps": _bar_gaps(rows),
            "sma_period": sma,
            "macd": {"fast": fast, "slow": slow, "signal": signal},
            "source": source,
        },
    })


@app.get("/api/history")
async def api_history(limit: int = 60, track: str = "A") -> JSONResponse:
    try:
        conn = db.get()
        async with conn.execute(
            """SELECT date, track, ticker, name, entry_price, exit_price,
                      pnl_pct, close_reason, highest_step, pyramided, status
               FROM trades
               WHERE track=?
               ORDER BY date DESC
               LIMIT ?""",
            (track, limit),
        ) as cur:
            rows = await cur.fetchall()
        result = [dict(r) for r in rows]
    except Exception as exc:
        log("API_HISTORY_FAILED", level="WARN", error=repr(exc))
        result = []
    return JSONResponse(result)


# ─── /api/stats ───────────────────────────────────────────────────────


@app.get("/api/stats")
async def api_stats(track: str = "A") -> JSONResponse:
    try:
        conn = db.get()

        # ????썹땟???꿔꺂???겸꼻??
        async with conn.execute(
            """SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
                AVG(pnl_pct) as avg_pnl,
                MIN(pnl_pct) as max_loss,
                MAX(pnl_pct) as max_gain
               FROM trades WHERE status='CLOSED' AND track=?""",
            (track,),
        ) as cur:
            # 집계 SELECT는 늘 한 행을 낸다 — 빈 테이블이면 값이 NULL인 행이다.
            agg_row = await cur.fetchone()
            agg = dict(agg_row) if agg_row is not None else {}

        # ????????????????????
        async with conn.execute(
            """SELECT close_reason,
                      COUNT(*) as n,
                      AVG(pnl_pct) as avg_pnl
               FROM trades WHERE status='CLOSED' AND track=?
               GROUP BY close_reason""",
            (track,),
        ) as cur:
            rows = await cur.fetchall()
        by_reason = {
            r["close_reason"]: {"n": r["n"], "avg_pnl": round(r["avg_pnl"] or 0, 2)} for r in rows
        }

        # ???됰Ŧ???????썹땟???????
        async with conn.execute(
            """SELECT substr(date,1,6) as ym,
                      COUNT(*) as n,
                      SUM(pnl_pct) as sum_pnl
               FROM trades WHERE status='CLOSED' AND track=?
               GROUP BY ym ORDER BY ym""",
            (track,),
        ) as cur:
            rows = await cur.fetchall()
        monthly = [
            {"ym": r["ym"], "n": r["n"], "sum_pnl": round(r["sum_pnl"] or 0, 2)} for r in rows
        ]

        async with conn.execute(
            """SELECT pyramided,
                      COUNT(*) as n,
                      AVG(pnl_pct) as avg_pnl
               FROM trades WHERE status='CLOSED' AND track=?
               GROUP BY pyramided""",
            (track,),
        ) as cur:
            rows = await cur.fetchall()
        by_pyramided = {
            ("피라미딩" if r["pyramided"] else "1차만"): {
                "n": r["n"],
                "avg_pnl": round(r["avg_pnl"] or 0, 2),
            }
            for r in rows
        }

        async with conn.execute(
            """SELECT
                  CASE
                    WHEN highest_step IS NULL OR highest_step <= 0 THEN '스텝 없음'
                    WHEN highest_step < 0.05 THEN '1스텝'
                    WHEN highest_step < 0.075 THEN '2스텝'
                    ELSE '3스텝 이상'
                  END as step_bucket,
                  COUNT(*) as n,
                  AVG(pnl_pct) as avg_pnl
               FROM trades WHERE status='CLOSED' AND track=?
               GROUP BY step_bucket""",
            (track,),
        ) as cur:
            rows = await cur.fetchall()
        by_step = {
            r["step_bucket"]: {"n": r["n"], "avg_pnl": round(r["avg_pnl"] or 0, 2)} for r in rows
        }

        async with conn.execute(
            """SELECT substr(entry_at,12,2) as hour,
                      COUNT(*) as n,
                      AVG(pnl_pct) as avg_pnl
               FROM trades
               WHERE status='CLOSED' AND entry_at IS NOT NULL AND track=?
               GROUP BY hour ORDER BY hour""",
            (track,),
        ) as cur:
            rows = await cur.fetchall()
        by_entry_hour = [
            {"hour": r["hour"], "n": r["n"], "avg_pnl": round(r["avg_pnl"] or 0, 2)} for r in rows
        ]

        total = agg.get("total") or 0
        wins = agg.get("wins") or 0
        return JSONResponse(
            {
                "total": total,
                "wins": wins,
                "losses": total - wins,
                "win_rate": round(wins / total * 100, 1) if total else 0,
                "avg_pnl": round(agg.get("avg_pnl") or 0, 2),
                "max_loss": round(agg.get("max_loss") or 0, 2),
                "max_gain": round(agg.get("max_gain") or 0, 2),
                "by_reason": by_reason,
                "monthly": monthly,
                "by_pyramided": by_pyramided,
                "by_step": by_step,
                "by_entry_hour": by_entry_hour,
            }
        )
    except Exception as exc:
        log("API_STATS_FAILED", level="WARN", error=repr(exc))
        return JSONResponse(
            {
                "total": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0,
                "avg_pnl": 0,
                "max_loss": 0,
                "max_gain": 0,
                "by_reason": {},
                "monthly": [],
                "by_pyramided": {},
                "by_step": {},
                "by_entry_hour": [],
            }
        )


# ─── /api/improve ─────────────────────────────────────────────────────

_BUY_PHASES = {"FIRST_BUY", "PYRAMID_BUY"}
_NEAR_MISS_MFE_PCT = 1.5  # 스텝1 근접 이탈로 보는 최소 고점 수익률(%)
_FAST_STOP_MIN = 10  # 진입 후 이 분수 이내 손절이면 '빠른 손절'


def _improve_params() -> dict:
    return {
        "step_size_pct": round(STEP_SIZE * 100, 2),
        "step_trail_pct": round(STEP_TRAIL * 100, 2),
        "hard_stop_pct": round(HARD_STOP_RATIO * 100, 2),
        "gap_max_order_pct": round(GAP_MAX_ORDER * 100, 2),
        "gap_max_fill_pct": round(GAP_MAX_FILL * 100, 2),
        "f1_gap_min_pct": round(GAP_MIN * 100, 2),
        "f1_gap_core_max_pct": round(GAP_MAX * 100, 2),
        "f1_gap_hard_max_pct": round(HIGH_GAP_MAX * 100, 2),
        "timeout_time": _F5_EXEC_TIME,
        "force_trailing_time": f"{FORCE_TRAILING_HOUR:02d}:{FORCE_TRAILING_MINUTE:02d}",
    }


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _minutes_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None
    return delta.total_seconds() / 60.0


def _improve_from_rows(
    trades: list[dict],
    orders: list[dict],
    skips: dict[str, int],
    skip_rows: list[dict] | None = None,
) -> dict:
    """개선 화면 집계. 수동 거래는 전략 파라미터 진단에서 제외한다."""
    source_closed_n = len(trades)
    trades = [t for t in trades if (t.get("close_reason") or "") != "MANUAL"]
    excluded_manual_n = source_closed_n - len(trades)
    total = len(trades)
    pnls = [(t.get("pnl_pct") or 0.0) for t in trades]
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p <= 0]
    win_rate = len(win_pnls) / total if total else 0.0
    avg_win = _avg(win_pnls)
    avg_loss = _avg(loss_pnls)
    payoff = abs(avg_win / avg_loss) if avg_loss < 0 else 0.0
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss

    max_streak = run = 0
    for p in pnls:
        run = run + 1 if p <= 0 else 0
        max_streak = max(max_streak, run)
    cur_streak = run

    mfe_rows: list[dict] = []
    near_miss_n = 0
    step1_n = 0
    step_observed_n = 0
    mfe_observed_n = 0
    hold_by_reason: dict[str, list[float]] = {}
    trailing_gb: list[float] = []
    trailing_pnl: list[float] = []
    trailing_stop_slip: list[float] = []
    timeout_pnl: list[float] = []
    timeout_mfe: list[float] = []
    hs_pnl: list[float] = []
    hs_minutes: list[float] = []
    fast_stop_n = 0

    for t in trades:
        entry, high = t.get("entry_price"), t.get("high_price")
        pnl = t.get("pnl_pct")
        reason = t.get("close_reason") or ""
        highest_step = t.get("highest_step") or 0.0
        mfe = round((high / entry - 1) * 100, 2) if entry and high else None
        giveback = round(mfe - pnl, 2) if mfe is not None and pnl is not None else None
        if mfe is not None:
            mfe_observed_n += 1
        mfe_rows.append(
            {
                "date": t.get("date"),
                "ticker": t.get("ticker"),
                "name": t.get("name"),
                "mfe_pct": mfe,
                "pnl_pct": pnl,
                "giveback_pp": giveback,
                "close_reason": reason,
            }
        )

        step_reached = highest_step >= STEP_SIZE
        if step_reached or mfe is not None:
            step_observed_n += 1
        if step_reached:
            step1_n += 1
        elif mfe is not None and mfe >= _NEAR_MISS_MFE_PCT and (pnl or 0.0) <= 0:
            near_miss_n += 1

        minutes = _minutes_between(t.get("entry_at"), t.get("exit_at"))
        if minutes is not None and reason:
            hold_by_reason.setdefault(reason, []).append(minutes)

        if reason == "TRAILING":
            if giveback is not None:
                trailing_gb.append(giveback)
            if pnl is not None:
                trailing_pnl.append(pnl)
            if pnl is not None and step_reached:
                active_stop_pnl = (highest_step - STEP_TRAIL) * 100
                trailing_stop_slip.append(active_stop_pnl - pnl)
        elif reason == "TIMEOUT":
            if pnl is not None:
                timeout_pnl.append(pnl)
            if mfe is not None:
                timeout_mfe.append(mfe)
        elif reason == "HARD_STOP":
            if pnl is not None:
                hs_pnl.append(pnl)
            if minutes is not None:
                hs_minutes.append(minutes)
                if minutes <= _FAST_STOP_MIN:
                    fast_stop_n += 1

    mfe_rows.reverse()

    avg_fill_pnl = _avg(hs_pnl)
    avg_slip_pp = max(0.0, -(avg_fill_pnl + HARD_STOP_RATIO * 100)) if hs_pnl else 0.0

    slip_acc: dict[str, dict[str, list]] = {}
    for o in orders:
        phase = o.get("order_phase") or ""
        trigger_price = o.get("trigger_price")
        if trigger_price is None:
            trigger_price = o.get("order_price")
        trigger_price = trigger_price or 0
        fill_price = o.get("fill_price")
        if not trigger_price or fill_price is None:
            continue
        pp = (fill_price - trigger_price) / trigger_price * 100
        if phase not in _BUY_PHASES:
            pp = -pp
        acc = slip_acc.setdefault(phase, {"pps": [], "lat": []})
        acc["pps"].append(pp)
        if o.get("fill_latency_ms") is not None:
            acc["lat"].append(o["fill_latency_ms"])

    def _slip_summary(pps: list[float], lat: list[float]) -> dict:
        return {
            "n": len(pps),
            "avg_pp": round(_avg(pps), 3),
            "max_pp": round(max(pps), 3) if pps else 0.0,
            "avg_latency_ms": round(_avg(lat)) if lat else 0,
        }

    by_phase = {ph: _slip_summary(a["pps"], a["lat"]) for ph, a in slip_acc.items()}
    buy_pps = [p for ph, a in slip_acc.items() if ph in _BUY_PHASES for p in a["pps"]]
    buy_lat = [v for ph, a in slip_acc.items() if ph in _BUY_PHASES for v in a["lat"]]
    sell_pps = [p for ph, a in slip_acc.items() if ph not in _BUY_PHASES for p in a["pps"]]
    sell_lat = [v for ph, a in slip_acc.items() if ph not in _BUY_PHASES for v in a["lat"]]
    fill_latency_measured_n = sum(
        1 for a in slip_acc.values() for v in a["lat"] if isinstance(v, (int, float)) and v > 0
    )

    trade_dates = {str(t.get("date")) for t in trades if t.get("date")}
    if skip_rows is None:
        no_target_days = skips.get("NO_TARGET", 0)
        market_closed_days = skips.get("MARKET_CLOSED", 0)
        operational_skip_n = sum(
            n for reason, n in skips.items() if reason not in {"NO_TARGET", "MARKET_CLOSED"}
        )
        overlap_days = 0
    else:
        no_target_dates = {
            str(r.get("date"))
            for r in skip_rows
            if r.get("date") and r.get("reason") == "NO_TARGET"
        }
        market_closed_dates = {
            str(r.get("date"))
            for r in skip_rows
            if r.get("date") and r.get("reason") == "MARKET_CLOSED"
        }
        all_skip_dates = {str(r.get("date")) for r in skip_rows if r.get("date")}
        no_target_days = len(no_target_dates - trade_dates)
        market_closed_days = len(market_closed_dates)
        operational_skip_n = sum(
            1 for r in skip_rows if r.get("reason") not in {"NO_TARGET", "MARKET_CLOSED"}
        )
        overlap_days = len(all_skip_dates & trade_dates)
    trade_days = len(trade_dates) if trade_dates else total
    evaluated_days = trade_days + no_target_days
    guard_n = skips.get("SLIPPAGE_GUARD", 0)
    stop_slip_avg = _avg(trailing_stop_slip)

    return {
        "params": _improve_params(),
        "overall": {
            "total": total,
            "wins": len(win_pnls),
            "win_rate": round(win_rate * 100, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "payoff_ratio": round(payoff, 2),
            "expectancy": round(expectancy, 2),
            "cur_loss_streak": cur_streak,
            "max_loss_streak": max_streak,
        },
        "hard_stop": {
            "n": len(hs_pnl),
            "share_pct": round(len(hs_pnl) / total * 100, 1) if total else 0.0,
            "avg_fill_pnl": round(avg_fill_pnl, 2),
            "avg_slip_pp": round(avg_slip_pp, 2),
            "fast_stop_n": fast_stop_n,
            "avg_min_to_stop": round(_avg(hs_minutes), 1) if hs_minutes else 0.0,
        },
        "step": {
            "step1_n": step1_n,
            "step1_rate": round(step1_n / step_observed_n * 100, 1) if step_observed_n else 0.0,
            "near_miss_n": near_miss_n,
            "observed_n": step_observed_n,
            "coverage_pct": round(step_observed_n / total * 100, 1) if total else 0.0,
        },
        "trailing": {
            "n": len(trailing_pnl),
            "avg_giveback_pp": round(_avg(trailing_gb), 2),
            "avg_pnl": round(_avg(trailing_pnl), 2),
        },
        "trailing_diagnostics": {
            "giveback_n": len(trailing_gb),
            "stop_eval_n": len(trailing_stop_slip),
            "avg_stop_slip_pp": round(max(0.0, stop_slip_avg), 2),
            "max_stop_slip_pp": (
                round(max(0.0, max(trailing_stop_slip)), 2) if trailing_stop_slip else 0.0
            ),
            "structural_giveback_min_pp": round(STEP_TRAIL * 100, 2),
            "structural_giveback_max_pp": round((STEP_SIZE + STEP_TRAIL) * 100, 2),
        },
        "slippage": {
            "buy": _slip_summary(buy_pps, buy_lat),
            "sell": _slip_summary(sell_pps, sell_lat),
            "by_phase": by_phase,
            "guard_n": guard_n,
        },
        "timeout_exit": {
            "n": len(timeout_pnl),
            "avg_pnl": round(_avg(timeout_pnl), 2),
            "avg_mfe": round(_avg(timeout_mfe), 2),
        },
        "candidates": {
            "skips": skips,
            "skip_days": sum(skips.values()),
            "trade_days": trade_days,
        },
        "candidate_supply": {
            "trade_days": trade_days,
            "no_target_days": no_target_days,
            "evaluated_days": evaluated_days,
            "operational_skip_n": operational_skip_n,
            "market_closed_days": market_closed_days,
            "overlap_days": overlap_days,
        },
        "data_quality": {
            "source_closed_n": source_closed_n,
            "strategy_trade_n": total,
            "excluded_manual_n": excluded_manual_n,
            "mfe_observed_n": mfe_observed_n,
            "mfe_coverage_pct": round(mfe_observed_n / total * 100, 1) if total else 0.0,
            "fill_latency_measured_n": fill_latency_measured_n,
            "params_versioned": False,
        },
        "mfe_rows": mfe_rows,
        "hold_time": {
            reason: {"n": len(mins), "avg_min": round(_avg(mins), 1)}
            for reason, mins in hold_by_reason.items()
        },
    }


@app.get("/api/improve")
async def api_improve(track: str = "A") -> JSONResponse:
    try:
        conn = db.get()
        async with conn.execute(
            """SELECT date, ticker, name, entry_price, high_price, highest_step,
                      pnl_pct, close_reason, entry_at, exit_at
               FROM trades WHERE status='CLOSED' AND track=? ORDER BY date""",
            (track,),
        ) as cur:
            trades = [dict(r) for r in await cur.fetchall()]
        async with conn.execute(
            """SELECT o.order_phase, o.order_price, o.trigger_price,
                      o.fill_price, o.fill_latency_ms
               FROM orders o
               JOIN trades t ON t.id = o.trade_id
               WHERE o.status IN ('FILLED', 'PARTIAL_FILL')
                 AND COALESCE(t.close_reason, '') != 'MANUAL'
                 AND t.track=?""",
            (track,),
        ) as cur:
            orders = [dict(r) for r in await cur.fetchall()]
        async with conn.execute(
            "SELECT date, reason FROM daily_skips WHERE track=? ORDER BY date, id",
            (track,),
        ) as cur:
            skip_rows = [dict(r) for r in await cur.fetchall()]
        skips: dict[str, int] = {}
        for row in skip_rows:
            reason = row["reason"]
            skips[reason] = skips.get(reason, 0) + 1
        return JSONResponse(_improve_from_rows(trades, orders, skips, skip_rows=skip_rows))
    except Exception as exc:
        log("API_IMPROVE_FAILED", level="WARN", error=repr(exc))
        return JSONResponse(_improve_from_rows([], [], {}))


# ─── /api/stream (SSE) ────────────────────────────────────────────────


@app.get("/api/stream")
async def api_stream(request: Request) -> StreamingResponse:
    queue = live.subscribe()

    async def event_gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            live.unsubscribe(queue)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
