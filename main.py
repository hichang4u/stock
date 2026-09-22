"""진입점 — 스케줄러 부트스트랩 및 장기 실행 태스크 관리"""

import asyncio
import contextlib
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

if os.getenv("STOCK_SKIP_DOTENV", "0") != "1":
    load_dotenv()

if os.getenv("DRY_RUN", "0") == "1":
    os.environ["LOG_DIR"] = os.getenv("DRY_RUN_LOG_DIR", "data/dry_run/logs")
    os.environ["STATE_DIR"] = os.getenv("DRY_RUN_STATE_DIR", "data/dry_run/state")
    os.environ["DB_DIR"] = os.getenv("DRY_RUN_DB_DIR", "data/dry_run/db")

import uvicorn  # noqa: E402

from src import db, live, notifier, readiness, state  # noqa: E402
from src.api import auth, kis_rest, server  # noqa: E402
from src.modules import (  # noqa: E402
    baseline_experiment,
    exit_recovery,
    f1_filter,
    f2_lockup,
    f3_entry,
    f4_tracking,
    f5_timeout,
    paper_fast_probe,
    tick_capture,
)
from src.release import strategy_fingerprint  # noqa: E402
from src.scheduler import (  # noqa: E402
    F1_H,
    F1_M,
    F3_FILL_DEADLINE_H,
    F3_FILL_DEADLINE_M,
    build,
)
from src.utils import logger, time_sync  # noqa: E402

KST = ZoneInfo("Asia/Seoul")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


LOG_DIR = os.getenv("LOG_DIR", "data/logs")
STATE_DIR = os.getenv("STATE_DIR", "data/state")
NTP_SERVERS = [s.strip() for s in os.getenv("NTP_SERVER", "pool.ntp.org").split(",")]
DB_PATH = os.path.join(os.getenv("DB_DIR", "data/db"), "trading.db")
PID_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.pid")
# 개장 관측 전체(오프셋 0.3s + 허용 지각 2.5s + PREOPEN 대기 8s + 멀티시세 1콜 ~2s)를
# 덮어야 한다. 2.5s 였을 때는 paper_fast_probe 의 PREOPEN 대기가 여기서 잘려 죽은 코드였다
# (2026-09-22 리뷰). 값을 바꾸면 전략 지문이 바뀐다.
PAPER_FAST_PROBE_OPEN_TIMEOUT_SEC = max(
    0.1,
    _env_float("PAPER_FAST_PROBE_OPEN_TIMEOUT_SEC", 15.0),
)
PAPER_FAST_BALANCE_PREFETCH_CUTOFF = (8, 59, 58)
_BAL_TR = {"REAL": "TTTC8434R", "PAPER": "VTTC8434R"}
_BALANCE_MAX_PAGES = 10
_ACTIVE_POSITION_STATUSES = {"ENTERING", "HOLDING", "EXITING"}

# 국내휴장일조회 — 실전 전용 TR (모의투자 미지원)
_HOLIDAY_CHECK_PATH = "/uapi/domestic-stock/v1/quotations/chk-holiday"
_HOLIDAY_CHECK_TR_ID = "CTCA0903R"

# F1 결과를 F2에 전달하기 위한 세션 변수
_f1_result: list[dict] = []
_f2_done = False
_f3_started = False
# 휴장 확인된 날짜(YYYYMMDD). 날짜를 함께 저장해 당일 확인된 휴장에만 적용 —
# HOLDING으로 일일 리셋이 막히거나 휴장 API가 실패해도 다음 거래일을 막지 않는다.
_market_closed_date: str | None = None


def _today() -> str:
    return datetime.now(KST).strftime("%Y%m%d")


def _scheduled_at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime.now(KST).replace(hour=hour, minute=minute, second=second, microsecond=0)


def _is_trading_weekday() -> bool:
    return datetime.now(KST).weekday() < 5  # 월(0)~금(4)


def _is_market_closed_today() -> bool:
    return _market_closed_date == _today()


async def _ensure_trading_day() -> None:
    global _f1_result, _f2_done, _f3_started, _market_closed_date
    today = _today()
    reconciled = await _reconcile_runtime_stale_active(today)
    if reconciled or await state.ensure_trading_day(today):
        _f1_result = []
        _f2_done = False
        _f3_started = False
        _market_closed_date = None
        logger.log("DAILY_STATE_RESET", level="INFO", date=today)


async def _reconcile_runtime_stale_active(today: str) -> bool:
    """Clear a prior-day active state only after KIS confirms zero shares."""
    s = state.get()
    stale_date = s.trading_date
    if not stale_date or stale_date == today or s.position_status not in _ACTIVE_POSITION_STATUSES:
        return False

    stale_status = s.position_status
    ticker = s.target_ticker
    actual_qty = await _verified_holding_qty(ticker, "DAILY_ROLLOVER")
    if actual_qty != 0:
        logger.log(
            "STALE_ACTIVE_ROLLOVER_BLOCKED",
            level="CRIT",
            stale_date=stale_date,
            stale_status=stale_status,
            ticker=ticker,
            actual_qty=actual_qty,
            reason="BALANCE_VERIFY_FAILED" if actual_qty is None else "HOLDING_EXISTS",
            entry_blocked=True,
        )
        if actual_qty and actual_qty > 0:
            await notifier.send(
                "STALE_ACTIVE_ROLLOVER_BLOCKED",
                level="CRIT",
                message=(
                    f"전일 {stale_status} 상태의 실제 보유 {actual_qty}주가 "
                    f"확인되어 당일 진입을 차단했습니다: {ticker}"
                ),
                ticker=ticker,
            )
        return False

    if not state.backup_stale(STATE_DIR, stale_date):
        logger.log(
            "STALE_BACKUP_FAILED",
            level="CRIT",
            stale_date=stale_date,
            stale_status=stale_status,
        )
        return False

    changed = await state.reset_stale_active_for_trading_day(today)
    if not changed:
        return False
    state.discard(STATE_DIR)
    logger.log(
        "STALE_ACTIVE_RECONCILED",
        level="WARN",
        stale_date=stale_date,
        stale_status=stale_status,
        ticker=ticker,
        actual_qty=0,
        source="DAILY_ROLLOVER",
    )
    await notifier.send(
        "STALE_ACTIVE_RECONCILED",
        level="WARN",
        message=(
            f"전일 {stale_status} 잔류 상태를 잔고 0주 확인 후 " f"자동 정리했습니다: {ticker}"
        ),
        ticker=ticker,
    )
    return True


async def _check_market_holiday() -> None:
    """
    KIS 휴장일 조회로 당일 개장 여부 확인. 휴장이면 당일 잡 전체를 스킵한다.
    조회 실패·미확정 응답값은 fail-open(개장 가정) — 거래일을 놓치는 쪽이 더 큰 손실.
    휴장 플래그는 확인된 날짜에 바인딩되므로(_market_closed_date) 실패 경로에서
    전일 플래그가 남아도 당일 잡을 막지 않는다.
    CTCA0903R은 모의투자 미지원이므로 PAPER 모드는 평일 가드만 적용한다.
    """
    global _market_closed_date
    if os.getenv("KIS_MODE", "PAPER") != "REAL":
        return

    today = _today()
    try:
        resp = await kis_rest.get(
            _HOLIDAY_CHECK_PATH,
            tr_id=_HOLIDAY_CHECK_TR_ID,
            params={"BASS_DT": today, "CTX_AREA_NK": "", "CTX_AREA_FK": ""},
        )
    except Exception as exc:
        logger.log("HOLIDAY_CHECK_FAILED", level="WARN", error=repr(exc))
        return

    if str(resp.get("rt_cd", "0")) != "0":
        logger.log(
            "HOLIDAY_CHECK_FAILED",
            level="WARN",
            msg_cd=resp.get("msg_cd"),
            msg1=resp.get("msg1"),
        )
        return

    rows = resp.get("output", [])
    row = next((r for r in rows if isinstance(r, dict) and r.get("bass_dt") == today), None)
    if row is None:
        logger.log("HOLIDAY_CHECK_FAILED", level="WARN", reason="TODAY_NOT_IN_RESPONSE")
        return

    opnd_yn = str(row.get("opnd_yn") or "").strip().upper()
    if opnd_yn == "Y":
        _market_closed_date = None
        return
    if opnd_yn != "N":
        # 미검증 스키마 대비 — 명시적 "N"만 휴장으로 인정하고 나머지는 fail-open
        logger.log(
            "HOLIDAY_CHECK_FAILED",
            level="WARN",
            reason="UNEXPECTED_OPND_YN",
            opnd_yn=row.get("opnd_yn"),
        )
        return

    await _mark_market_closed(today, source="HOLIDAY_API")


async def _mark_market_closed(today: str, *, source: str) -> None:
    """당일을 휴장으로 확정하고 잡 전체를 스킵시킨다. 중복 호출은 무시한다."""
    global _market_closed_date
    if _market_closed_date == today:
        return  # 기동 시 체크 후 08:29 잡 재확인 등 — 중복 로그·알림 방지

    _market_closed_date = today
    s = state.get()
    s.day_skip = True
    s.close_reason = s.close_reason or "MARKET_CLOSED"
    logger.log("MARKET_CLOSED", level="INFO", date=today, source=source)
    await notifier.send(
        "MARKET_CLOSED",
        level="INFO",
        message=f"휴장일 감지({today}). 당일 거래 없음.",
    )


# 당일 재시작 시 day_skip을 복원해 재진입을 막아야 하는 종료 스킵 사유.
# NO_TARGET은 주문·포지션이 없었던 경우라 catch-up이 F1을 다시 돌려 후보를
# 찾을 수 있어야 하므로 제외한다(기존 동작 보존). 오직 MARKET_CLOSED만
# 휴장 플래그를 세운다.
_REENTRY_BLOCKING_SKIP_REASONS = frozenset(
    {"MARKET_CLOSED", "VI_ACTIVE", "ENTRY_FAIL", "GAP_CHANGED", "SLIPPAGE_GUARD", "MANUAL"}
)


async def _restore_daily_skip_from_db() -> None:
    """
    재시작 복구: 당일 daily_skips에 재진입 차단 사유가 있으면 스킵 상태 복원.

    F3의 종료 결정(휴장·VI·진입실패·갭변동·슬리피지·수동)은 인메모리(day_skip)만
    세우고 상태 파일의 day_skip은 영속화되지 않는다. 예전에는 디스크의 잔여
    ENTERING이 우연히 재진입을 막았으나, 종료 상태를 IDLE로 durable하게 정정하면서
    그 우연한 방어가 사라졌다. 같은 날 재시작하면 catch-up이 F1~F3를 다시 돌려
    주문·알림이 반복(VI_ACTIVE는 해제가 추격 진입, ENTRY_FAIL은 중복 주문)될 수
    있으므로 DB 기록(유일한 재시작 생존 흔적)에서 시작 시 1회 복원한다.
    """
    global _market_closed_date
    today = _today()
    try:
        skip = await db.get_skip_by_date(today)
    except Exception as exc:
        logger.log("MARKET_CLOSED_RESTORE_FAILED", level="WARN", error=repr(exc))
        return
    reason = (skip or {}).get("reason")
    if reason not in _REENTRY_BLOCKING_SKIP_REASONS:
        return
    s = state.get()
    s.day_skip = True
    s.close_reason = s.close_reason or reason
    if reason == "MARKET_CLOSED":
        _market_closed_date = today
        logger.log("MARKET_CLOSED_RESTORED", level="INFO", date=today, source="DAILY_SKIPS")
    else:
        logger.log(
            "DAY_SKIP_RESTORED", level="INFO", date=today, reason=reason, source="DAILY_SKIPS"
        )


# ── 스케줄 작업 래퍼 ─────────────────────────────────────────────────


async def job_token_refresh() -> None:
    await _ensure_trading_day()
    await auth.refresh()
    await _check_market_holiday()


async def job_ntp_check() -> None:
    await _ensure_trading_day()
    time_sync.check_ntp(NTP_SERVERS)


async def _today_trade_exists() -> bool:
    try:
        return await db.get_trade_by_date(datetime.now(KST).strftime("%Y%m%d")) is not None
    except Exception as exc:
        logger.log("TRADE_EXISTENCE_CHECK_FAILED", level="WARN", error=repr(exc))
        return False


async def _skip_entry_pipeline_if_trade_exists(source: str) -> bool:
    """Once today's buy exists, never rerun F1/F2/F3 catch-up or scheduled entry."""
    global _f2_done, _f3_started
    if not await _today_trade_exists():
        return False
    _f2_done = True
    _f3_started = True
    state.get().day_skip = True
    state.get().close_reason = state.get().close_reason or "TRADE_ALREADY_EXISTS"
    logger.log("TRADE_ALREADY_EXISTS", level="WARN", source=source, action="SKIP_ENTRY_PIPELINE")
    return True


async def job_f1() -> None:
    global _f1_result
    pipeline_started = time.perf_counter()
    await _ensure_trading_day()
    if _is_market_closed_today():
        return
    if await _skip_entry_pipeline_if_trade_exists("JOB_F1"):
        return
    fast_candidates: list[dict] = []
    probe_error_reason: str | None = None
    probe_started = time.perf_counter()
    try:
        fast_candidates = await asyncio.wait_for(
            paper_fast_probe.observe_open_boundary(),
            timeout=PAPER_FAST_PROBE_OPEN_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        probe_error_reason = "TIMEOUT"
        logger.log(
            "PAPER_FAST_PROBE_ERROR",
            level="WARN",
            phase="OPEN",
            reason="TIMEOUT",
            timeout_sec=PAPER_FAST_PROBE_OPEN_TIMEOUT_SEC,
        )
    except Exception as exc:
        probe_error_reason = "UNHANDLED"
        logger.log(
            "PAPER_FAST_PROBE_ERROR",
            level="WARN",
            phase="OPEN",
            reason="UNHANDLED",
            error=repr(exc),
        )
    probe_ms = round((time.perf_counter() - probe_started) * 1000)
    selection_started = time.perf_counter()
    fast_hybrid_enabled = paper_fast_probe.hybrid_enabled()
    fast_quality_ok = paper_fast_probe.open_quality_ok()
    if fast_hybrid_enabled and (not fast_quality_ok or not fast_candidates):
        quality = paper_fast_probe.get_last_open_quality()
        logger.log(
            "PAPER_FAST_PATH_FALLBACK",
            level="WARN",
            reason=(
                probe_error_reason
                or (quality.get("reason") if not fast_quality_ok else "NO_FAST_CANDIDATES")
                or "INCOMPLETE_PROBE"
            ),
            candidate_count=len(fast_candidates),
            tickers=[candidate.get("ticker") for candidate in fast_candidates],
            quality=quality,
        )
    if fast_hybrid_enabled and fast_candidates and fast_quality_ok:
        _f1_result = fast_candidates
        selection_source = "FAST_MULTI"
        f1_filter.save_candidate_snapshot(fast_candidates)
        logger.log(
            "PAPER_FAST_PATH_SELECTED",
            level="INFO",
            candidate_count=len(fast_candidates),
            tickers=[candidate.get("ticker") for candidate in fast_candidates],
        )
    else:
        preserve_fast = fast_hybrid_enabled and bool(fast_candidates)
        legacy_candidates = await f1_filter.run(terminal_on_empty=not preserve_fast)
        if preserve_fast:
            _f1_result = paper_fast_probe.merge_candidates(
                fast_candidates,
                legacy_candidates,
            )
            selection_source = "FAST_LEGACY_MERGED"
            f1_filter.save_candidate_snapshot(_f1_result)
            logger.log(
                "PAPER_FAST_PATH_MERGED",
                level="WARN",
                fast_count=len(fast_candidates),
                legacy_count=len(legacy_candidates),
                merged_count=len(_f1_result),
                tickers=[candidate.get("ticker") for candidate in _f1_result],
            )
        else:
            _f1_result = legacy_candidates
            selection_source = "LEGACY_SINGLE_QUOTE"
        try:
            paper_fast_probe.compare_with_legacy(_f1_result)
        except Exception as exc:
            logger.log(
                "PAPER_FAST_SHADOW_COMPARE_ERROR",
                level="WARN",
                reason="UNHANDLED",
                error=repr(exc),
            )
    selection_ms = round((time.perf_counter() - selection_started) * 1000)
    await _run_f2_f3_after_f1()
    logger.log(
        "ENTRY_PIPELINE_TIMING",
        level="INFO",
        selection_source=selection_source,
        probe_ms=probe_ms,
        selection_ms=selection_ms,
        total_ms=round((time.perf_counter() - pipeline_started) * 1000),
        candidate_count=len(_f1_result or []),
        position_status=state.get().position_status,
    )
    await asyncio.to_thread(paper_fast_probe.log_shadow_validation_progress)


async def job_paper_fast_probe() -> None:
    await _ensure_trading_day()
    if _is_market_closed_today():
        return
    try:
        await paper_fast_probe.prepare()
    except Exception as exc:
        logger.log(
            "PAPER_FAST_PROBE_ERROR",
            level="WARN",
            phase="PREOPEN",
            reason="UNHANDLED",
            error=repr(exc),
        )
        return
    # CTCA0903R(국내휴장일조회)은 모의투자 미지원이라 PAPER 는 평일 가드밖에 없다.
    # 장전 멀티시세가 '오늘 세션 없음'을 말하면 REAL 의 휴장 경로와 같은 처리를 한다.
    # 08:59:45 판정이라 F1 의 개장 후 조회가 시작되기 전에 막힌다.
    if paper_fast_probe.market_closed_detected():
        await _mark_market_closed(_today(), source="FAST_PROBE")


async def job_balance_snapshot_prefetch() -> None:
    """Prepare PAPER entry cash independently from the Fast Path probe."""
    await _ensure_trading_day()
    if _is_market_closed_today():
        return
    budget_sec = _paper_fast_balance_prefetch_budget_seconds()
    if budget_sec <= 0:
        logger.log(
            "BALANCE_SNAPSHOT_SKIPPED",
            level="WARN",
            phase="PREOPEN",
            reason="OPEN_GUARD",
            cutoff="08:59:58",
        )
        return
    try:
        await asyncio.wait_for(
            f3_entry.prepare_available_cash_snapshot(),
            timeout=budget_sec,
        )
    except asyncio.TimeoutError:
        logger.log(
            "BALANCE_SNAPSHOT_ERROR",
            level="WARN",
            phase="PREOPEN",
            reason="OPEN_GUARD_TIMEOUT",
            timeout_sec=round(budget_sec, 3),
            cutoff="08:59:58",
        )
    except Exception as exc:
        logger.log(
            "BALANCE_SNAPSHOT_ERROR",
            level="WARN",
            phase="PREOPEN",
            reason="UNHANDLED",
            error=repr(exc),
        )


def _paper_fast_balance_prefetch_budget_seconds(
    now: datetime | None = None,
) -> float:
    current = now or datetime.now(KST)
    cutoff = current.replace(
        hour=PAPER_FAST_BALANCE_PREFETCH_CUTOFF[0],
        minute=PAPER_FAST_BALANCE_PREFETCH_CUTOFF[1],
        second=PAPER_FAST_BALANCE_PREFETCH_CUTOFF[2],
        microsecond=0,
    )
    return max(0.0, (cutoff - current).total_seconds())


async def job_f2() -> None:
    await _ensure_trading_day()
    if _is_market_closed_today():
        return
    if await _skip_entry_pipeline_if_trade_exists("JOB_F2"):
        return
    # F1 completion can trigger F2/F3 immediately; this scheduled job is a safety net.
    if _f2_done:
        return
    if not _f1_result:
        return
    await _run_f2_f3_after_f1()


async def job_f3() -> None:
    global _f3_started
    await _ensure_trading_day()
    if _is_market_closed_today():
        return
    if await _skip_entry_pipeline_if_trade_exists("JOB_F3"):
        return
    # Keep the scheduled F3 job as a fallback when F2 locked a target but chaining did not start F3.
    if _f3_started:
        return
    if not state.get().target_ticker or state.get().day_skip:
        return
    _f3_started = True
    await f3_entry.run()


async def job_f5_precheck() -> None:
    await _ensure_trading_day()
    if _is_market_closed_today():
        return
    await f5_timeout.precheck()


async def job_f5_exec() -> None:
    await _ensure_trading_day()
    if _is_market_closed_today():
        return
    await f5_timeout.execute()


async def _run_f2_f3_after_f1() -> None:
    """
    Chain F2/F3 after F1 produced candidates.

    F1 자체가 deadline까지 데이터 재시도를 책임진다. F2는 필터가 없는 순위·락업
    단계이므로 같은 결과를 다시 F1에 넣는 별도 재시도 루프를 두지 않는다.
    """
    global _f1_result, _f2_done, _f3_started
    if not _f1_result or state.get().day_skip:
        return

    await f2_lockup.run(_f1_result)
    _f2_done = True

    if _f2_done and not _f3_started and state.get().target_ticker and not state.get().day_skip:
        _f3_started = True
        # 라이브 경로는 즉시 체이닝이어도 진입 마감을 우회하지 않는다.
        await f3_entry.run()


# ── F1 missed 보완 실행 ──────────────────────────────────────────────


async def _run_catchup() -> None:
    """
    F1 start 이후 F3 fill deadline 전에 기동하면 F1(~F3)이 missed 상태.
    F1 결과가 나오면 F2/F3 체인을 즉시 이어서 당일 파이프라인을 복구한다.
    F3 fill deadline 이후엔 진입 마감이 지났으므로 catchup 불가.
    """
    await _ensure_trading_day()
    dry_run = os.getenv("DRY_RUN", "0") == "1"
    force_catchup_requested = os.getenv("FORCE_CATCHUP", "0") == "1"
    if force_catchup_requested and not dry_run:
        logger.log(
            "FORCE_CATCHUP_BLOCKED",
            level="WARN",
            reason="LIVE_ENTRY_DEADLINE_CANNOT_BE_OVERRIDDEN",
        )
    force = dry_run

    if not force and not _is_trading_weekday():
        logger.log(
            "CATCHUP_SKIP_WEEKEND", level="INFO", message="주말(토·일) 기동 — 진입 catchup 생략"
        )
        return

    if not force and _is_market_closed_today():
        logger.log(
            "CATCHUP_SKIP_MARKET_CLOSED", level="INFO", message="휴장일 기동 — 진입 catchup 생략"
        )
        return

    if await _skip_entry_pipeline_if_trade_exists("CATCHUP"):
        return

    now = datetime.now(KST)
    f1_sched = _scheduled_at(F1_H, F1_M)
    f3_fill_deadline = _scheduled_at(F3_FILL_DEADLINE_H, F3_FILL_DEADLINE_M)

    if not force and not (f1_sched <= now < f3_fill_deadline):
        return

    logger.log(
        "CATCHUP_START",
        level="WARN",
        message=(
            f"{'[FORCE] ' if force else ''}F1 missed 감지. "
            f"보완 실행 ({now.strftime('%H:%M:%S')} 기동)"
        ),
    )
    await notifier.send(
        "CATCHUP_START",
        level="WARN",
        message=(
            f"{'[FORCE] ' if force else ''}F1 missed 감지 "
            f"({now.strftime('%H:%M:%S')} 기동). 보완 실행 중..."
        ),
    )

    global _f1_result
    _f1_result = await f1_filter.run()

    now = datetime.now(KST)
    await _run_f2_f3_after_f1()


# ── 재시작 복구 ──────────────────────────────────────────────────────


async def _recover_state() -> None:
    """
    프로세스 재시작 시 today_state.json을 우선 복구하고, 필요하면 DB OPEN trade를 보조로 복구한다.
    """
    data = state.load(STATE_DIR)
    today = datetime.now(KST).strftime("%Y%m%d")

    if data is not None and data.get("date") != today:
        stale_status = data.get("position_status")
        stale_active_reconciled = False
        if stale_status in _ACTIVE_POSITION_STATUSES and data.get("ticker"):
            actual_qty = await _verified_holding_qty(
                data.get("ticker"),
                "STALE_STATE_FILE",
            )
            if actual_qty == 0:
                if state.backup_stale(STATE_DIR, str(data.get("date"))):
                    state.discard(STATE_DIR)
                    stale_active_reconciled = True
                    logger.log(
                        "STALE_ACTIVE_RECONCILED",
                        level="WARN",
                        stale_date=data.get("date"),
                        stale_status=stale_status,
                        ticker=data.get("ticker"),
                        actual_qty=0,
                        source="STATE_FILE",
                    )
                    await notifier.send(
                        "STALE_ACTIVE_RECONCILED",
                        level="WARN",
                        message=(
                            f"전일 {stale_status} 상태 파일을 잔고 0주 확인 후 "
                            f"자동 정리했습니다: {data.get('ticker')}"
                        ),
                        ticker=data.get("ticker"),
                    )
                else:
                    logger.log(
                        "STALE_BACKUP_FAILED",
                        level="CRIT",
                        stale_date=data.get("date"),
                        stale_status=stale_status,
                    )

        if stale_active_reconciled:
            pass
        elif stale_status in ("CLOSED", "IDLE"):
            # 정상 종료된 지난 거래일 파일 — 잔류 위험이 없으므로 알림 없이
            # 폐기한다. 남겨두면 재시작마다 다시 처리된다.
            logger.log(
                "STALE_STATE_DISCARDED",
                level="INFO",
                stale_date=data.get("date"),
                stale_status=stale_status,
                ticker=data.get("ticker"),
            )
            state.discard(STATE_DIR)
        else:
            # HOLDING/ENTERING은 전일 미청산 포지션 의심, 그 외(누락·알 수 없는
            # 상태)는 파일 손상 의심 — 실계좌에 잔고가 남아 있을 수 있으므로
            # 운영자가 확인하기 전까지 당일 자동 진입(F1~F3)을 차단하고
            # 파일은 증거로 보존한다.
            # 해제: 계좌 확인 후 문제없으면 today_state.json 삭제 후 재시작.
            # 당일 DB OPEN 거래 복구가 이 파일을 덮어쓸 수 있으므로 증거 사본을 먼저 격리한다.
            # 사본 실패는 기록만 하고 계속한다 — 차단·알림·복구가 우선이다.
            if not state.backup_stale(STATE_DIR, str(data.get("date"))):
                logger.log(
                    "STALE_BACKUP_FAILED",
                    level="CRIT",
                    stale_date=data.get("date"),
                    stale_status=stale_status,
                )
            state.get().day_skip = True
            logger.log(
                "STALE_POSITION_DETECTED",
                level="CRIT",
                stale_date=data.get("date"),
                stale_status=stale_status,
                ticker=data.get("ticker"),
                entry_blocked=True,
            )
            await notifier.send(
                "STALE_POSITION_DETECTED",
                level="CRIT",
                message=(
                    f"전일 미청산 포지션 의심. date={data.get('date')} "
                    f"ticker={data.get('ticker')} status={stale_status}. "
                    "당일 자동 진입을 차단했습니다. 계좌 보유 수량과 미체결 주문을 "
                    "확인하고, 문제없으면 today_state.json 삭제 후 재시작하세요."
                ),
                ticker=data.get("ticker"),
            )
        data = None

    if data is not None and (
        data.get("position_status") == "EXITING" or isinstance(data.get("pending_exit"), dict)
    ):
        data = await exit_recovery.merge_db_intent(data, today)

    if data is not None and isinstance(data.get("pending_entry"), dict):
        state.restore_from(data)
        logger.log(
            "PROCESS_RESTART_DETECTED",
            level="CRIT",
            recovered_status="PENDING_ENTRY_RECONCILIATION",
            recovery_source="STATE_FILE",
            ticker=data.get("ticker"),
            order_id=data.get("pending_entry", {}).get("order_id"),
            entry_blocked=True,
        )
        await f3_entry.recover_pending_entry()
        return

    if data is not None and isinstance(data.get("pending_exit"), dict):
        state.restore_from(data)
        state.get().day_skip = True
        logger.log(
            "PROCESS_RESTART_DETECTED",
            level="CRIT",
            recovered_status="PENDING_EXIT_RECONCILIATION",
            recovery_source="STATE_FILE_DB_MERGED",
            ticker=data.get("ticker"),
            order_id=data.get("pending_exit", {}).get("order_id"),
            client_order_id=data.get("pending_exit", {}).get("client_order_id"),
            entry_blocked=True,
        )
        await f4_tracking.recover_pending_exit()
        return

    if data is not None and data.get("position_status") == "ENTERING":
        state.restore_from(data)
        state.get().day_skip = True
        logger.log(
            "PROCESS_RESTART_DETECTED",
            level="CRIT",
            recovered_status="ENTERING_WITHOUT_PENDING_ORDER",
            recovery_source="STATE_FILE",
            ticker=data.get("ticker"),
            entry_blocked=True,
        )
        await notifier.send(
            "PROCESS_RESTART_DETECTED",
            level="CRIT",
            message=(
                f"재시작 시 주문 식별자 없는 ENTERING 상태 발견: "
                f"{data.get('ticker')}. 계좌 주문/잔고 확인 필요"
            ),
            ticker=data.get("ticker"),
        )
        return

    if data is not None and data.get("position_status") == "EXITING":
        state.restore_from(data)
        state.get().day_skip = True
        logger.log(
            "PROCESS_RESTART_DETECTED",
            level="CRIT",
            recovered_status="EXITING_REQUIRES_RECONCILIATION",
            recovery_source="STATE_FILE",
            ticker=data.get("ticker"),
            remaining_qty=data.get("remaining_qty"),
            entry_blocked=True,
        )
        await notifier.send(
            "PROCESS_RESTART_DETECTED",
            level="CRIT",
            message=(
                f"재시작 시 청산 확인 대기 상태 발견: {data.get('ticker')} "
                f"잔여={data.get('remaining_qty')}주. 미체결 주문과 실제 잔고 확인 필요"
            ),
            ticker=data.get("ticker"),
        )
        return

    if data is not None and data.get("position_status") == "HOLDING":
        actual_qty = await _verified_holding_qty(data.get("ticker"), "STATE_FILE")
        if actual_qty and actual_qty > 0:
            data = dict(data)
            data["entry_qty"] = actual_qty
            data["remaining_qty"] = actual_qty
            state.restore_from(data)
            logger.log(
                "PROCESS_RESTART_DETECTED",
                level="CRIT",
                recovered_status="HOLDING_RESUMED",
                recovery_source="STATE_FILE",
                actual_qty=actual_qty,
            )
            await notifier.send(
                "PROCESS_RESTART_DETECTED",
                level="CRIT",
                message=f"재시작 감지. 포지션 복구: {data.get('ticker')} {actual_qty}주",
                ticker=data.get("ticker"),
            )
            return
        if actual_qty is None:
            logger.log(
                "PROCESS_RESTART_DETECTED",
                level="CRIT",
                recovered_status="HOLDING_VERIFY_FAILED_SKIP_RESTORE",
                recovery_source="STATE_FILE",
                actual_qty=actual_qty,
            )
        else:
            logger.log(
                "PROCESS_RESTART_DETECTED",
                level="WARN",
                recovered_status="NO_ACTUAL_HOLDING",
                recovery_source="STATE_FILE",
                actual_qty=actual_qty,
            )
        return

    if data is not None and _is_terminal_state(data):
        if data.get("position_status") == "CLOSED":
            # 당일 청산 완료 상태 복원 — 재시작 후에도 UI가 청산 차트와 매수/매도
            # 마커를 유지하고, CLOSED 상태가 당일 재진입(set_entering)도 막는다.
            state.restore_from(data)
        logger.log(
            "PROCESS_RESTART_DETECTED",
            level="WARN",
            recovered_status="STATE_FILE_TERMINAL_SKIP_DB_FALLBACK",
            recovery_source="STATE_FILE",
            position_status=data.get("position_status"),
            remaining_qty=data.get("remaining_qty"),
        )
        return

    await _recover_open_trade_from_db(today)


async def _recover_state_safely() -> bool:
    """재시작 대사 장애를 런타임 시작 실패와 분리한다."""
    try:
        await _recover_state()
        return True
    except Exception as exc:
        s = state.get()
        # 대사 중 예외가 상태 복원 전에 발생했으면 마지막 내구 상태를 최소한
        # 메모리에 되살린다. HOLDING은 F4/F5가 계속 보호하고, EXITING은 중복
        # 매도를 피하기 위해 그대로 보존한다.
        try:
            fallback = state.load(STATE_DIR)
            if isinstance(fallback, dict) and fallback.get("position_status") in {
                "ENTERING",
                "HOLDING",
                "EXITING",
            }:
                state.restore_from(fallback)
                s = state.get()
        except Exception as fallback_exc:
            logger.log(
                "STARTUP_RECOVERY_FALLBACK_FAILED",
                level="WARN",
                error=repr(fallback_exc),
            )
        s.day_skip = True
        logger.log(
            "STARTUP_RECOVERY_FAILED",
            level="CRIT",
            ticker=s.target_ticker,
            position_status=s.position_status,
            error=repr(exc),
            entry_blocked=True,
        )
        try:
            await notifier.send(
                "STARTUP_RECOVERY_FAILED",
                level="CRIT",
                message=(
                    f"재시작 대사 실패. 런타임과 F4/F5는 계속 실행합니다: {exc!r}. "
                    "KIS 주문·잔고를 수동 확인하세요."
                ),
                ticker=s.target_ticker,
            )
        except Exception as notify_exc:
            logger.log(
                "STARTUP_RECOVERY_ALERT_FAILED",
                level="WARN",
                error=repr(notify_exc),
            )
        return False


def _is_terminal_state(data: dict) -> bool:
    status = data.get("position_status")
    remaining_qty = data.get("remaining_qty")
    return status == "CLOSED" or (remaining_qty is not None and int(float(remaining_qty)) <= 0)


async def _verified_holding_qty(ticker: str | None, recovery_source: str) -> int | None:
    if not ticker:
        logger.log(
            "PROCESS_RESTART_DETECTED",
            level="CRIT",
            recovered_status="HOLDING_VERIFY_SKIPPED",
            recovery_source=recovery_source,
            reason="MISSING_TICKER",
        )
        return None

    mode = os.getenv("KIS_MODE", "PAPER")
    params = kis_rest.balance_inquiry_params()
    seen_keys: set[tuple[str, str]] = set()
    tr_cont = ""

    for page in range(1, _BALANCE_MAX_PAGES + 1):
        try:
            resp = await kis_rest.get(
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                tr_id=_BAL_TR[mode],
                params=params,
                tr_cont=tr_cont,
                include_response_meta=True,
            )
        except Exception as exc:
            await _log_holding_verify_failure(
                ticker,
                recovery_source,
                page=page,
                error=repr(exc),
            )
            return None

        if str(resp.get("rt_cd", "0")) != "0":
            await _log_holding_verify_failure(
                ticker,
                recovery_source,
                page=page,
                rt_cd=resp.get("rt_cd"),
                msg_cd=resp.get("msg_cd"),
                msg1=resp.get("msg1"),
            )
            return None

        output = resp.get("output1")
        if not isinstance(output, list):
            await _log_holding_verify_failure(
                ticker,
                recovery_source,
                page=page,
                reason="INVALID_OUTPUT1",
            )
            return None

        for item in output:
            if isinstance(item, dict) and item.get("pdno") == ticker:
                try:
                    qty = int(float(item.get("hldg_qty") or 0))
                except (TypeError, ValueError):
                    await _log_holding_verify_failure(
                        ticker,
                        recovery_source,
                        page=page,
                        reason="INVALID_HOLDING_QTY",
                    )
                    return None
                if qty < 0:
                    await _log_holding_verify_failure(
                        ticker,
                        recovery_source,
                        page=page,
                        reason="INVALID_HOLDING_QTY",
                    )
                    return None
                return qty

        response_meta = resp.get("_response_meta")
        next_flag = (
            str(response_meta.get("tr_cont") or "").upper()
            if isinstance(response_meta, dict)
            else ""
        )
        if next_flag not in {"M", "F"}:
            return 0

        fk = str(resp.get("ctx_area_fk100") or "")
        nk = str(resp.get("ctx_area_nk100") or "")
        continuation_key = (fk, nk)
        if not (fk or nk) or continuation_key in seen_keys:
            await _log_holding_verify_failure(
                ticker,
                recovery_source,
                page=page,
                reason="INVALID_CONTINUATION_KEY",
                tr_cont=next_flag,
            )
            return None
        seen_keys.add(continuation_key)
        params = {
            **params,
            "CTX_AREA_FK100": fk,
            "CTX_AREA_NK100": nk,
        }
        tr_cont = "N"

    await _log_holding_verify_failure(
        ticker,
        recovery_source,
        page=_BALANCE_MAX_PAGES,
        reason="MAX_PAGES_EXCEEDED",
    )
    return None


async def _log_holding_verify_failure(
    ticker: str,
    recovery_source: str,
    **fields,
) -> None:
    logger.log(
        "PROCESS_RESTART_DETECTED",
        level="CRIT",
        recovered_status="HOLDING_VERIFY_FAILED",
        recovery_source=recovery_source,
        ticker=ticker,
        **fields,
    )
    detail = " ".join(
        str(value)
        for value in (fields.get("msg_cd"), fields.get("msg1"), fields.get("reason"))
        if value
    )
    await notifier.send(
        "PROCESS_RESTART_DETECTED",
        level="CRIT",
        message=(
            f"재시작 복구 전 전체 잔고 확인 실패: {ticker}"
            f"{f'. {detail}' if detail else ''}. 수동 확인 필요."
        ),
        ticker=ticker,
    )


async def _recover_open_trade_from_db(today: str) -> None:
    """상태 파일이 없을 때 당일 DB 거래에서 CLOSED/HOLDING 상태를 복원한다."""
    try:
        trade = await db.get_trade_by_date(today)
    except RuntimeError:
        return
    if not trade:
        return

    if trade.get("status") == "CLOSED":
        restore_data = {
            "date": today,
            "ticker": trade.get("ticker"),
            "name": trade.get("name"),
            "target_candidates": [],
            "entry_price": trade.get("entry_price"),
            "entry_at": trade.get("entry_at"),
            "entry_qty": trade.get("entry_qty"),
            "remaining_qty": 0,
            "high_price": trade.get("high_price") or trade.get("entry_price"),
            "trailing_active": bool(trade.get("highest_step")),
            "highest_step": float(trade.get("highest_step") or 0.0),
            "trade_id": int(trade.get("id") or 0),
            "position_status": "CLOSED",
            "close_reason": trade.get("close_reason"),
            "post_close_tracking_stopped": True,
        }
        state.restore_from(restore_data)
        await state.persist(STATE_DIR, today)
        logger.log(
            "PROCESS_RESTART_DETECTED",
            level="WARN",
            recovered_status="DB_CLOSED_TRADE_RESTORED",
            recovery_source="DB_CLOSED_TRADE",
            trade_id=restore_data["trade_id"],
            ticker=restore_data["ticker"],
        )
        return

    if trade.get("status") != "OPEN":
        return

    entry_price = float(trade.get("entry_price") or 0)
    entry_qty = int(trade.get("entry_qty") or 0)
    if entry_price <= 0 or entry_qty <= 0:
        logger.log(
            "PROCESS_RESTART_DETECTED",
            level="WARN",
            recovered_status="DB_OPEN_TRADE_INCOMPLETE",
            recovery_source="DB_OPEN_TRADE",
            trade_id=trade.get("id"),
            ticker=trade.get("ticker"),
        )
        return

    actual_qty = await _verified_holding_qty(trade.get("ticker"), "DB_OPEN_TRADE")
    if not actual_qty or actual_qty <= 0:
        logger.log(
            "PROCESS_RESTART_DETECTED",
            level="CRIT",
            recovered_status="DB_OPEN_TRADE_NO_ACTUAL_HOLDING",
            recovery_source="DB_OPEN_TRADE",
            trade_id=trade.get("id"),
            ticker=trade.get("ticker"),
            db_entry_qty=entry_qty,
            actual_qty=actual_qty,
            pyramided=trade.get("pyramided"),
        )
        await notifier.send(
            "PROCESS_RESTART_DETECTED",
            level="CRIT",
            message=(
                f"DB OPEN trade가 있지만 실제 보유 수량이 없습니다: "
                f"{trade.get('ticker')}. 자동 복구 중단."
            ),
            ticker=trade.get("ticker"),
        )
        return

    # entry_qty는 누적 진입량, remaining_qty는 현재 증권사 잔고다. 부분 매도 뒤
    # 상태 파일이 유실된 경우 actual_qty를 entry_qty에도 복사하면 이미 체결된
    # 매도량과 잔고를 잘못 비교해 살아 있는 잔여분을 CLOSED 처리할 수 있다.
    # trades.entry_qty는 구버전에서 피라미딩 전 수량일 수 있으므로 확인된 BUY
    # 체결 합계와 현재 잔고까지 하한으로 삼아 누적 진입량을 복원한다.
    confirmed_entry_qty = int(trade.get("confirmed_entry_qty") or 0)
    # actual_qty에는 같은 종목의 수동 매수가 섞일 수 있다. 그 경우 기준량이
    # 커져 정상 청산 뒤에도 EXITING이 남을 수 있지만, 잔여 보유를 CLOSED로
    # 오판하는 것보다 안전한 방향이므로 운영자 대사 대상으로 보존한다.
    recovered_entry_qty = max(entry_qty, confirmed_entry_qty, actual_qty)
    highest_step = float(trade.get("highest_step") or 0.0)
    restore_data = {
        "date": today,
        "ticker": trade.get("ticker"),
        "name": trade.get("name"),
        "target_candidates": [],
        "entry_price": entry_price,
        "entry_at": trade.get("entry_at"),
        "entry_qty": recovered_entry_qty,
        "remaining_qty": actual_qty,
        "high_price": trade.get("high_price") or entry_price,
        "trailing_active": highest_step >= f4_tracking.STEP_SIZE,
        "highest_step": highest_step,
        "trade_id": int(trade.get("id") or 0),
        "position_status": "HOLDING",
        "close_reason": None,
    }
    merged = await exit_recovery.merge_db_intent(restore_data, today)
    has_pending_exit = isinstance(merged.get("pending_exit"), dict)
    # F4가 이미 상주 중이므로 DB 의도가 있으면 HOLDING을 한 번 노출하지 않고
    # 첫 메모리 복원·영속화부터 EXITING으로 원자적으로 공개한다.
    state.restore_from(merged if has_pending_exit else restore_data)
    if has_pending_exit:
        state.get().day_skip = True
    await state.persist(STATE_DIR, today)
    if has_pending_exit:
        logger.log(
            "PROCESS_RESTART_DETECTED",
            level="CRIT",
            recovered_status="DB_EXIT_INTENT_RESTORED",
            recovery_source="DB_OPEN_TRADE_AND_ORDER",
            trade_id=restore_data["trade_id"],
            ticker=restore_data["ticker"],
            client_order_id=merged["pending_exit"].get("client_order_id"),
        )
        await f4_tracking.recover_pending_exit()
        return
    logger.log(
        "PROCESS_RESTART_DETECTED",
        level="CRIT",
        recovered_status="HOLDING_RESUMED",
        recovery_source="DB_OPEN_TRADE",
        trade_id=restore_data["trade_id"],
        actual_qty=actual_qty,
        db_entry_qty=entry_qty,
        confirmed_entry_qty=confirmed_entry_qty,
        recovered_entry_qty=recovered_entry_qty,
        pyramided=trade.get("pyramided"),
        high_price=restore_data["high_price"],
        highest_step=highest_step,
    )
    await notifier.send(
        "PROCESS_RESTART_DETECTED",
        level="CRIT",
        message=f"DB 기준 포지션 복구: {trade.get('ticker')} {actual_qty}주",
        ticker=trade.get("ticker"),
    )


# ── PID 파일 관리 ────────────────────────────────────────────────────

_pid_lock_file = None


def _read_pid(path: str = PID_PATH) -> int | None:
    try:
        raw = open(path, encoding="utf-8").read().strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _try_lock_pid_file(handle) -> bool:
    try:
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _unlock_pid_file(handle) -> None:
    try:
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _write_pid() -> bool:
    global _pid_lock_file
    if _pid_lock_file is not None:
        return True

    path = PID_PATH
    current_pid = os.getpid()
    try:
        handle = open(path, "a+", encoding="utf-8")
    except OSError as e:
        logger.log("PID_WRITE_ERROR", level="WARN", path=path, error=repr(e))
        return False

    if not _try_lock_pid_file(handle):
        existing_pid = _read_pid(path)
        logger.log(
            "PROCESS_ALREADY_RUNNING",
            level="CRIT",
            path=path,
            existing_pid=existing_pid,
            current_pid=current_pid,
        )
        handle.close()
        return False

    try:
        handle.seek(0)
        existing_raw = handle.read().strip()
        existing_pid = int(existing_raw) if existing_raw else None
    except ValueError:
        existing_pid = None

    if existing_pid and existing_pid != current_pid:
        logger.log(
            "STALE_PID_REPLACED",
            level="WARN",
            path=path,
            stale_pid=existing_pid,
            current_pid=current_pid,
        )

    try:
        handle.seek(0)
        handle.truncate()
        handle.write(str(current_pid))
        handle.flush()
        os.fsync(handle.fileno())
        _pid_lock_file = handle
        return True
    except OSError as e:
        _unlock_pid_file(handle)
        handle.close()
        logger.log("PID_WRITE_ERROR", level="WARN", path=path, error=repr(e))
        return False


def _clear_pid() -> None:
    global _pid_lock_file
    handle = _pid_lock_file
    if handle is None:
        return
    _pid_lock_file = None
    _unlock_pid_file(handle)
    handle.close()


# ── 메인 ─────────────────────────────────────────────────────────────


async def main() -> None:
    dry_run = os.getenv("DRY_RUN", "0") == "1"
    # 같은 인터프리터에서 main()이 다시 호출돼도 이전 REAL 승인을 재사용하지 않는다.
    live.real_entry_enabled = False
    logger.setup(LOG_DIR)
    # 디스크가 장중 배포로 바뀌어도 이 프로세스가 실제로 시작한 코드 지문을
    # 모든 당일 거래에 기록하도록 스케줄/주문 시작 전에 캐시를 고정한다.
    fingerprint = strategy_fingerprint()
    logger.log("STRATEGY_FINGERPRINT_LOCKED", level="INFO", fingerprint=fingerprint)
    if not _write_pid():
        raise SystemExit(2)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    await db.init(DB_PATH)
    # 현재 지문의 canonical 기준선 실험·설정을 등록한다. 실패해도 런타임과
    # 주문 안전 경로를 막지 않는다(등록은 통계 기준선용, 주문 판단과 무관).
    try:
        baseline_experiment_id = await baseline_experiment.ensure_baseline_experiment()
        logger.log(
            "BASELINE_EXPERIMENT_REGISTERED",
            level="INFO",
            experiment_id=baseline_experiment_id,
        )
    except Exception as exc:
        logger.log("BASELINE_EXPERIMENT_REGISTER_FAILED", level="WARN", error=repr(exc))
    if os.getenv("KIS_MODE", "PAPER") == "REAL" and not dry_run:
        real_readiness = await readiness.calculate()
        if not real_readiness["eligible_for_real"]:
            logger.log(
                "REAL_START_BLOCKED",
                level="CRIT",
                readiness_percent=real_readiness["percent"],
                blockers=real_readiness["blockers"],
            )
            await db.close()
            _clear_pid()
            raise SystemExit(3)
        live.real_entry_enabled = True
    await _ensure_trading_day()
    # 휴장·종료 스킵(F3)은 인메모리라 같은 날 재시작 시 DB에서 복원해 재진입을 막는다
    await _restore_daily_skip_from_db()

    if dry_run:
        logger.log(
            "DRY_RUN_START",
            level="WARN",
            message="DRY_RUN=1: external auth, NTP, orders, and WebSocket are simulated",
        )
    else:
        await auth.load_or_refresh()
        time_sync.check_ntp(NTP_SERVERS)
        # 휴장일에 기동해도 catchup·스케줄 잡이 돌지 않도록 시작 시점에 1회 확인
        await _check_market_holiday()

    # F4: 상주 루프 — 거래일마다 HOLDING을 기다렸다가 추적한다.
    # 일회성 run()을 직접 띄우면 CLOSED 상태로 복원된 뒤 영구 종료되어
    # 다음 거래일 포지션을 아무도 추적하지 않는다 (2026-07-16 인시던트).
    f4_task = asyncio.create_task(f4_tracking.run_forever(), name="f4_tracking")
    # 대사 API/SQLite 장애가 나도 이미 F4 보호 루프가 살아 있어야 하며,
    # 실패는 자동 진입 차단+CRIT 알림으로 격리하고 나머지 런타임을 시작한다.
    await _recover_state_safely()

    # Telegram 알림 워커
    notifier_task = None
    if not dry_run:
        notifier_task = asyncio.create_task(notifier.worker(), name="notifier")

    # Web UI exposes account assets; bind to localhost unless explicitly opened.
    ui_port = int(os.getenv("UI_PORT", "8080"))
    ui_host = os.getenv("UI_HOST", "127.0.0.1")
    config = uvicorn.Config(
        server.app, host=ui_host, port=ui_port, log_level="warning", loop="none"
    )
    uvi = uvicorn.Server(config)
    uvi.install_signal_handlers = lambda: None  # uvicorn의 시그널 핸들러 비활성화
    ui_task = asyncio.create_task(uvi.serve(), name="ui_server")

    scheduler = None
    try:
        # Catchup은 F3 진입(실주문)까지 인라인 수행할 수 있으므로 F4 손절 추적·알림·UI가
        # 살아있는 상태에서 실행한다. 스케줄러보다는 먼저 완료해 예약 잡과의 중복 실행을 막는다.
        await _run_catchup()

        if not dry_run:
            scheduler = build(
                token_refresh=job_token_refresh,
                ntp_check=job_ntp_check,
                paper_fast_probe=job_paper_fast_probe,
                balance_snapshot_prefetch=job_balance_snapshot_prefetch,
                f1=job_f1,
                f2=job_f2,
                f3=job_f3,
                f5_precheck=job_f5_precheck,
                f5_exec=job_f5_exec,
            )
            scheduler.start()

        # UI 태스크가 바인딩 실패 등으로 끝나면 프로세스도 실패 처리해야 PID 잠금만
        # 남은 반쪽짜리 프로세스가 다음 재시작을 막지 않는다.
        await ui_task
        raise RuntimeError(f"UI server stopped unexpectedly: {ui_host}:{ui_port}")
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        uvi.should_exit = True  # uvicorn graceful 종료 신호
        if not ui_task.done():
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(ui_task, timeout=2.0)
        tasks = [task for task in (f4_task, notifier_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks)
        await f3_entry.drain_entry_audit_tasks()
        # 아직 진행 중인 가격 경로 캡처를 마감한다(15:15 미도달 → 불완전).
        # F4가 이미 정상 최종화했으면 no-op이다. DB close 전에 실행한다.
        try:
            await tick_capture.finalize(
                "PROCESS_SHUTDOWN", reached_expected_close=False
            )
        except Exception:  # noqa: BLE001 — 종료 정리가 실패해도 나머지 종료 진행
            pass
        await kis_rest.close_client()
        await db.close()
        _clear_pid()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
