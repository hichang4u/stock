"""F4. 장중 추적 스탑 모듈 (09:00:00 ~ F5 청산 직전) — PRD §F4"""

import asyncio
import math
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from src import db, live, notifier, state
from src.api import kis_rest, kis_ws
from src.modules import exit_recovery, tick_capture
from src.modules import vi_watch as vi_watch_mod
from src.modules.vi_watch import ViWatch
from src.utils.logger import log
from src.utils.spike_filter import SpikeFilter

KST = ZoneInfo("Asia/Seoul")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


# 청산 10분 전 — F5_EXEC(15:15)에 연동한다. 스텝 미달 포지션도 마감 직전에는
# trailing을 켜서 F5 시장가 청산 전에 스탑 기회를 준다.
FORCE_TRAILING_HOUR = 15
FORCE_TRAILING_MINUTE = 5

STEP_SIZE      = 0.025   # 스텝 간격 +2.5% (params.json 로드 예정)
STEP_TRAIL     = 0.020   # 스텝 기준 하락폭 -2.0%
HARD_STOP_RATIO = 0.020  # Hard Stop -2.0% (trailing 미활성 구간 전용)
TRAILING_SHADOW_ENABLED = os.getenv("TRAILING_SHADOW_ENABLED", "1") == "1"
TRAILING_SHADOW_BASELINE_TRAIL = _env_float(
    "TRAILING_SHADOW_BASELINE_TRAIL",
    0.015,
)
F4_REST_BACKUP_ENABLED = os.getenv("F4_REST_BACKUP_ENABLED", "1") == "1"
F4_REST_ONLY_WHEN_WS_STALE = os.getenv("F4_REST_ONLY_WHEN_WS_STALE", "1") == "1"
F4_WS_STALE_SEC = max(0.0, _env_float("F4_WS_STALE_SEC", 2.0))
F4_REST_POLL_INTERVAL_SEC = max(
    0.0,
    _env_float("F4_REST_POLL_INTERVAL_SEC", 1.0),
)
# CLOSED 상태의 관측은 차트 보강용일 뿐 주문 안전과 무관하다. 기본값은
# WS-only로 두어 장시간 REST 폴링과 stale 경고 폭주를 만들지 않는다.
F4_POST_CLOSE_REST_BACKUP_ENABLED = (
    os.getenv("F4_POST_CLOSE_REST_BACKUP_ENABLED", "0") == "1"
)
F4_POST_CLOSE_REST_POLL_INTERVAL_SEC = max(
    0.1,
    _env_float("F4_POST_CLOSE_REST_POLL_INTERVAL_SEC", 30.0),
)
F4_FILL_POLL_INTERVAL_SEC = max(
    0.0,
    _env_float("F4_FILL_POLL_INTERVAL_SEC", 0.5),
)
F4_STATE_PERSIST_INTERVAL_SEC = max(
    0.0,
    _env_float("F4_STATE_PERSIST_INTERVAL_SEC", 1.0),
)
F4_HEARTBEAT_INTERVAL_SEC = max(
    0.0,
    _env_float("F4_HEARTBEAT_INTERVAL_SEC", 30.0),
)
F4_WS_HEALTH_LOG_COOLDOWN_SEC = max(
    0.0,
    _env_float("F4_WS_HEALTH_LOG_COOLDOWN_SEC", 60.0),
)
VI_WATCH_ENABLED = os.getenv("VI_WATCH_ENABLED", "1") == "1"
VI_FREEZE_SUSPECT_SEC = float(os.getenv("VI_FREEZE_SUSPECT_SEC", "10"))
VI_CHECK_COOLDOWN_SEC = float(os.getenv("VI_CHECK_COOLDOWN_SEC", "60"))
# Keep the existing price-flow buffer alive briefly after an early close so
# the sell marker can be compared with the subsequent market path. This does
# not run stop logic or send orders while CLOSED. With the normal 09:10:10
# entry schedule the cutoff has already passed, so it only affects early or
# manually-triggered experiments.
F4_POST_CLOSE_OBSERVE_UNTIL = os.getenv(
    "F4_POST_CLOSE_OBSERVE_UNTIL",
    "09:10",
)


def _parse_observe_until(raw: str) -> tuple[tuple[int, int], bool]:
    """HH:MM을 파싱하고 기본값 사용 여부를 함께 반환한다."""
    try:
        hour, minute = (int(v) for v in raw.split(":", 1))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(raw)
    except (TypeError, ValueError):
        return (9, 10), True
    return (hour, minute), False


_OBSERVE_UNTIL: tuple[int, int] | None = None
_observe_until_invalid_warned = False
_invalid_entry_at_warned_value: str | None = None

_SELL_TR = {"REAL": "TTTC0011U", "PAPER": "VTTC0011U"}
_CCLD_TR = {"REAL": "TTTC0081R", "PAPER": "VTTC0081R"}
_last_state_persist_at = 0.0
_close_in_progress = False
_close_in_progress_warned = False
_closing_task: asyncio.Task | None = None
_active_monitor_tasks: set[asyncio.Task] = set()
_shadow_baseline_recorded_trade_id: int | None = None

_REARM_INTERVAL_SEC = 0.5
_REARM_HOLDING_INTERVAL_SEC = 5.0
_REARM_ERROR_INTERVAL_SEC = 5.0


def _get_observe_until() -> tuple[int, int]:
    """로거 초기화 이후 처음 필요할 때 설정을 파싱하고 오타를 1회 경고한다."""
    global _OBSERVE_UNTIL, _observe_until_invalid_warned
    if _OBSERVE_UNTIL is None:
        _OBSERVE_UNTIL, used_fallback = _parse_observe_until(
            F4_POST_CLOSE_OBSERVE_UNTIL
        )
        if used_fallback and not _observe_until_invalid_warned:
            log(
                "F4_OBSERVE_UNTIL_INVALID",
                level="WARN",
                value=F4_POST_CLOSE_OBSERVE_UNTIL,
                fallback="09:10",
            )
            _observe_until_invalid_warned = True
    return _OBSERVE_UNTIL


def _price_observation_active(now: datetime | None = None) -> bool:
    """확정된 종목의 가격을 계속 수집해야 하는지 반환한다.

    관측은 트랙 A의 포지션이 아니라 "종목이 확정됐고 관측 창 안인가"로
    결정된다. A가 진입하지 않은 날에도 가격 경로가 남아야 다른 전략 트랙과
    사후 분석이 그날을 쓸 수 있다 — 하필 "A는 못 샀는데 B는 살 수 있는 날"이
    두 전략을 비교하는 가장 의미 있는 날이다.

    매매 판단은 이 함수와 무관하다. 청산 판정(_process_tick)은 호출부의
    HOLDING 게이트 뒤에 그대로 남아 있다.
    """
    global _invalid_entry_at_warned_value
    s = state.get()
    if s.position_status == "HOLDING":
        return True  # 보유 중엔 무조건 — 수동 종료도 손절 추적을 끄지 못한다
    if s.post_close_tracking_stopped:
        return False
    if not _observed_ticker(s):
        return False

    now = now or datetime.now(KST)

    if s.position_status != "CLOSED":
        # 미진입·진입중·청산중. 사후 관측 설정(F4_POST_CLOSE_OBSERVE_UNTIL)은
        # "청산 이후"를 다루는 값이므로 여기 적용하지 않고 세션 종료까지 본다.
        if s.trading_date and s.trading_date != now.strftime("%Y%m%d"):
            return False  # 지난 거래일 잔여 상태로 관측을 되살리지 않는다
        end_h, end_m = tick_capture.CAPTURE_UNTIL
        return now < now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

    # ── 이하 CLOSED 사후 관측: 기존 판정을 그대로 보존한다 ──
    if not s.entry_at:
        return False
    try:
        entry_at = datetime.fromisoformat(s.entry_at)
        if entry_at.tzinfo is None:
            entry_at = entry_at.replace(tzinfo=KST)
    except (TypeError, ValueError):
        invalid_value = str(s.entry_at)
        if _invalid_entry_at_warned_value != invalid_value:
            log(
                "F4_ENTRY_AT_INVALID",
                level="WARN",
                ticker=s.target_ticker,
                entry_at=invalid_value[:100],
            )
            _invalid_entry_at_warned_value = invalid_value
        return False
    _invalid_entry_at_warned_value = None
    # 캡처가 이 체결 종목에 활성이면 사후 관측을 고정 15:20까지 연장해 durable
    # 가격 경로가 실제 청산 이후에도 계속 기록되게 한다. 캡처 비활성이면 기존
    # 조기·수동 진입 동작(F4_POST_CLOSE_OBSERVE_UNTIL, 기본 09:10)을 보존한다.
    if tick_capture.is_active() and tick_capture.active_ticker() == _observed_ticker(s):
        cutoff_hm = tick_capture.CAPTURE_UNTIL
    else:
        cutoff_hm = _get_observe_until()
    cutoff = now.replace(
        hour=cutoff_hm[0], minute=cutoff_hm[1], second=0, microsecond=0
    )
    return entry_at.astimezone(KST).date() == now.date() and now < cutoff


def _observed_ticker(s: state.State) -> str | None:
    """지금 관측해야 할 종목. 현재 시도 중인 target_ticker가 있으면 그것 —
    보유 시 손절 판정 종목과 같아야 한다. F3가 후보를 소진해 target_ticker를
    지운 뒤에는 처음 잠긴 1순위(observe_ticker)로 돌아가 15:20까지 찍는다."""
    return s.target_ticker or s.observe_ticker


def _observation_should_continue(ticker: str, now: datetime | None = None) -> bool:
    """이 종목의 구독을 유지할지 여부.

    관측이 F2 잠금 시점부터 시작되면서 F3의 후보 교체
    (f3_entry.py의 ``s.target_ticker = picked["ticker"]``)가 이미 떠 있는
    구독보다 나중에 일어날 수 있게 됐다. 낡은 구독을 그대로 두면 **다른 종목의
    가격으로 손절·트레일링을 판정한다** — SpikeFilter는 ticker를 로깅에만
    쓰므로 걸러주지 않는다. 종목이 바뀌면 구독을 끝내고 run_forever가 새 종목으로
    다시 붙게 한다.
    """
    return _price_observation_active(now) and _observed_ticker(state.get()) == ticker


def _rest_backup_allowed(position_status: str) -> bool:
    """미보유 구간에서 REST 백업 폴링을 억제한다.

    REST 백업의 목적은 WS 장애 시 손절 추적을 보호하는 것이다. 보유가 없으면
    보호할 손절이 없다. 관측 창이 종목 확정 시점부터 열리면서 IDLE/ENTERING이
    폴링 루프에 도달할 수 있게 됐는데, 그대로 두면 WS 장애 시 보유 등급
    간격(F4_REST_POLL_INTERVAL_SEC=1.0)으로 폴링해 PAPER 초당 1건 예산을
    통째로 소모하고 그날 A의 진입까지 막는다.
    """
    return position_status not in ("IDLE", "ENTERING")


def _should_attach_capture(s: state.State, now: datetime | None = None) -> bool:
    """durable 캡처를 붙일지 여부. 거래가 없는 날도 대상이다.

    캡처는 trade_id를 Optional로 받고 price_path_manifests.trade_id도
    nullable이므로, 체결이 없어도 (거래일, 종목, experiment_id)로 식별된다.
    이 조건이 trade_id를 요구하면 A가 진입하지 않은 날의 가격 경로가 디스크에
    전혀 남지 않는다.

    캡처 창(CAPTURE_UNTIL, 15:20)이 끝난 뒤에는 붙지 않는다. 붙이면
    `_price_observation_active()`의 컷오프가 `F4_POST_CLOSE_OBSERVE_UNTIL`에서
    `CAPTURE_UNTIL`로 뒤집혀 관측이 그 자리에서 끝나고, 최종화 → 재부착이
    `_REARM_INTERVAL_SEC`마다 반복된다. 실장에서 두 값이 어긋난 채로
    (관측 15:30 > 캡처 15:15) 하루 약 900바퀴가 돌았고, 재부착이 디스크의
    기존 청크를 재시작으로 읽어 그날 캡처를 RESTART_GAP으로 오염시켰다.
    2026-08-14/19/27/28/31 다섯 거래일이 이렇게 남았다 — 틱 자체는 온전하다.
    자세한 내역은 docs/DEV_ENV.md의 "F4 청산 후 관측과 EXITING 운영".
    그 시각 이후에 붙인 캡처는 어차피 즉시 최종화되므로 남길 것도 없다.
    """
    if not _observed_ticker(s):
        return False
    now = now or datetime.now(KST)
    return (now.hour, now.minute) < tick_capture.CAPTURE_UNTIL


def post_close_observation_active(now: datetime | None = None) -> bool:
    """UI/API용: 현재 CLOSED 거래의 사후 가격 관측이 진행 중인지 반환한다."""
    return state.get().position_status == "CLOSED" and _price_observation_active(now)


def _capture_backup_active(now: datetime | None = None) -> bool:
    """캡처가 이 종목에 활성이고 09:35~15:14 창이면 사후 저우선 REST 백업을 허용한다.

    이 경로는 일반 사후 폴링 스위치(F4_POST_CLOSE_REST_BACKUP_ENABLED, 기본 0)를
    우회하므로, 우회 자체를 별도 스위치(STRATEGY_TICK_REST_BACKUP_ENABLED)로 명시해
    운영자가 끈 설정을 캡처가 조용히 되살리지 않게 한다. 09:35 이전에는 시작하지
    않고, 15:14에 멈춰 F5 precheck(15:14:50)·exec(15:15:00)이 항상 우선하게 한다.
    """
    if not tick_capture.REST_BACKUP_ENABLED:
        return False
    if not (
        tick_capture.is_active()
        and tick_capture.active_ticker() == _observed_ticker(state.get())
    ):
        return False
    now = now or datetime.now(KST)
    now_hm = (now.hour, now.minute)
    return tick_capture.CAPTURE_BACKUP_START <= now_hm < tick_capture.CAPTURE_BACKUP_STOP


def _note_ws_loss(now: datetime | None = None) -> bool:
    """캡처 창(15:20) 이전 WS 단절을 캡처에 실제 증거로 기록한다.

    재연결하더라도 그 사이 구간이 비어 완전 커버가 깨지므로, 캡처는 이 거래를
    ``data_complete=0``/``missing_reason=WS_LOSS``로 최종화한다.
    """
    if not (
        tick_capture.is_active()
        and tick_capture.active_ticker() == _observed_ticker(state.get())
    ):
        return False
    now = now or datetime.now(KST)
    if (now.hour, now.minute) >= tick_capture.CAPTURE_UNTIL:
        return False
    tick_capture.mark_ws_disconnect()
    return True


async def _finalize_capture_after_observation() -> None:
    """관측 창이 끝난 뒤 캡처를 최종화한다(정상 청산 경로 전용).

    포지션이 아직 HOLDING이면(모니터 비정상 종료 후 재무장) 캡처를 계속 두고
    최종화하지 않는다. 15:20에 도달했으면 COMPLETE, 이전이면 불완전으로 남긴다.
    """
    if not (
        tick_capture.is_active()
        and tick_capture.active_ticker() == _observed_ticker(state.get())
    ):
        return
    if state.get().position_status == "HOLDING":
        return
    now = datetime.now(KST)
    reached = (now.hour, now.minute) >= tick_capture.CAPTURE_UNTIL
    await tick_capture.finalize(
        "COMPLETE" if reached else "INCOMPLETE_BEFORE_1515",
        reached_expected_close=reached,
    )


async def stop_post_close_observation() -> dict:
    """매도 완료 후 가격 관측만 종료하고 그 선택을 상태 파일에 보존한다."""
    s = state.get()
    if s.position_status != "CLOSED":
        return {"ok": False, "reason": "POSITION_NOT_CLOSED"}

    closing = _closing_task
    if closing is not None and not closing.done():
        await asyncio.shield(closing)

    already_stopped = s.post_close_tracking_stopped
    if not await state.stop_post_close_tracking():
        return {"ok": False, "reason": "POSITION_NOT_CLOSED"}

    cancelled = 0
    for task in tuple(_active_monitor_tasks):
        if task is closing or task.done():
            continue
        task.cancel()
        cancelled += 1

    # 수동 중지는 캡처 창 이전 종료이므로 캡처를 불완전(MANUAL_STOP)으로 최종화한다.
    if tick_capture.is_active() and tick_capture.active_ticker() == _observed_ticker(s):
        await tick_capture.finalize("MANUAL_STOP", reached_expected_close=False)

    persisted = True
    try:
        await state.persist(
            os.getenv("STATE_DIR", "data/state"),
            datetime.now(KST).strftime("%Y%m%d"),
        )
    except Exception as exc:
        persisted = False
        log(
            "F4_POST_CLOSE_TRACKING_STOP_PERSIST_ERROR",
            level="WARN",
            ticker=s.target_ticker,
            error=repr(exc),
        )

    log(
        "F4_POST_CLOSE_TRACKING_STOPPED",
        level="INFO",
        ticker=s.target_ticker,
        already_stopped=already_stopped,
        cancelled_tasks=cancelled,
        persisted=persisted,
    )
    return {
        "ok": True,
        "already_stopped": already_stopped,
        "cancelled_tasks": cancelled,
        "persisted": persisted,
    }


async def run_forever() -> None:
    """F4 상주 진입점 — main.py에서 asyncio.create_task로 구동.

    run()은 한 사이클(HOLDING 대기 → 추적 → 청산)만 수행하고 반환하므로,
    거래일을 넘겨 살아 있는 프로세스에서는 이 루프가 다음 거래일의
    HOLDING을 다시 기다린다. HOLDING인 채로 사이클이 끝나는 경우는
    모니터 전원 비정상 종료뿐이므로, 재구독 폭주를 막는 백오프를 둔다.

    run()이 예외로 죽어도 루프는 유지한다 — main()은 이 태스크를 감시하지
    않으므로 여기서 전파되면 손절 감시가 영구히 사라진다. CancelledError는
    Exception이 아니므로 잡히지 않고 그대로 전파된다(종료 경로).
    """
    _get_observe_until()
    while True:
        try:
            await run()
        except Exception as e:
            log("F4_RUN_FOREVER_ERROR", level="CRIT", error=repr(e))
            try:
                await notifier.send(
                    "F4_RUN_FOREVER_ERROR",
                    level="CRIT",
                    message=(
                        f"F4 사이클 비정상 종료 — "
                        f"{_REARM_ERROR_INTERVAL_SEC:.0f}초 후 재시작: {e!r}"
                    ),
                )
            except Exception:
                pass  # 알림 실패가 상주 루프를 죽여선 안 된다
            await asyncio.sleep(_REARM_ERROR_INTERVAL_SEC)
            continue
        if state.get().position_status == "HOLDING":
            await asyncio.sleep(_REARM_HOLDING_INTERVAL_SEC)
        else:
            await asyncio.sleep(_REARM_INTERVAL_SEC)


async def run() -> None:
    """
    F4 단일 사이클 — WebSocket 구독 → 실패 시 REST 폴링 fallback.
    시작 시점 상태가 CLOSED면(당일 거래 종료) 즉시 반환한다.
    """
    s = state.get()
    # 종목이 확정되거나 포지션이 열릴 때까지 대기. 종목만 잠겨도 관측을 시작해
    # A가 진입하지 않는 날의 가격 경로를 확보한다. 후보 소진으로 target_ticker가
    # 지워진 뒤에도 observe_ticker(1순위)로 관측을 이어간다.
    while not (_observed_ticker(s) or s.position_status in ("HOLDING", "CLOSED")):
        await asyncio.sleep(0.5)
        s = state.get()

    ticker = _observed_ticker(s)
    if not ticker or not _price_observation_active():
        return
    spike_filter = SpikeFilter()

    if os.getenv("DRY_RUN", "0") == "1":
        if s.position_status == "CLOSED":
            return
        await _run_dry_ticks(ticker, spike_filter)
        return

    # durable 가격 경로 캡처에 idempotent하게 붙는다. F3가 체결 확정 시 이미
    # 시작했으면 no-op이고, DB 복구된 HOLDING이면 여기서 이어쓴다. 체결이 없는
    # 날에도 종목이 잠겼으면 붙어서 그날 가격 경로를 남긴다(trade_id=None).
    if _should_attach_capture(s):
        try:
            from src.modules import baseline_experiment

            tick_capture.attach_or_resume(
                datetime.now(KST).strftime("%Y%m%d"),
                ticker,
                s.trade_id or None,
                baseline_experiment.active_experiment_id(),
                s.entry_at,
            )
        except Exception:  # noqa: BLE001 — 캡처 부착 실패가 추적을 막지 않는다
            pass

    live.ws_connected = False
    last_ws_tick_at = 0.0
    ws_watch_started_at = time.monotonic()
    ws_transport_known = False
    rest_wakeup = asyncio.Event()

    def is_ws_stale() -> bool:
        now_mono = time.monotonic()
        if ws_transport_known and not live.ws_connected:
            return True
        if last_ws_tick_at <= 0:
            return (now_mono - ws_watch_started_at) >= F4_WS_STALE_SEC
        if not live.ws_connected:
            return True
        return (now_mono - last_ws_tick_at) >= F4_WS_STALE_SEC

    def should_poll_rest() -> bool:
        if not _rest_backup_allowed(state.get().position_status):
            return False
        if state.get().position_status == "CLOSED":
            if _capture_backup_active():
                # 캡처용 저우선 백업(15:14까지). 주문 경로보다 항상 뒤로 밀린다.
                return not F4_REST_ONLY_WHEN_WS_STALE or is_ws_stale()
            return F4_POST_CLOSE_REST_BACKUP_ENABLED and (
                not F4_REST_ONLY_WHEN_WS_STALE or is_ws_stale()
            )
        return not F4_REST_ONLY_WHEN_WS_STALE or is_ws_stale()

    def ws_tick_age_ms() -> int | None:
        if last_ws_tick_at <= 0:
            return None
        return max(0, round((time.monotonic() - last_ws_tick_at) * 1000))

    def ws_status_fields() -> dict:
        return {
            "ws_connected": bool(live.ws_connected),
            "last_ws_tick_age_ms": ws_tick_age_ms(),
            "last_price": live.last_tick_price,
            "high_price": state.get().high_price,
            "remaining_qty": state.get().remaining_qty,
            "position_status": state.get().position_status,
            "trade_id": state.get().trade_id,
            "rest_backup_enabled": F4_REST_BACKUP_ENABLED,
        }

    vi_watch = _make_vi_watch(ticker)

    async def on_tick(tick: dict) -> None:
        nonlocal last_ws_tick_at
        accepted = await _handle_price_tick(
            tick["price"],
            ticker,
            spike_filter,
            source="ws",
            vi_watch=vi_watch,
            tick_meta=tick,
        )
        if not accepted:
            return
        live.ws_connected = True
        last_ws_tick_at = time.monotonic()

    def on_connection_change(connected: bool) -> None:
        nonlocal ws_transport_known
        ws_transport_known = True
        live.ws_connected = connected
        # 캡처 창 이전 WS 단절을 캡처에 실제 증거로 남긴다(재연결해도 불완전).
        if not connected:
            try:
                _note_ws_loss()
            except Exception:  # noqa: BLE001 — 관측 전용, 전파 금지
                pass
        # Re-evaluate REST fallback immediately instead of waiting for the
        # normal polling interval after a transport disconnect.
        rest_wakeup.set()

    ws_task = asyncio.create_task(kis_ws.subscribe(
        ticker, on_tick,
        stop_if=lambda: not _observation_should_continue(ticker),
        on_connection_change=on_connection_change,
    ))
    ws_health_task = asyncio.create_task(
        _run_ws_health_monitor(ticker, is_ws_stale, ws_status_fields)
    )
    tasks = [ws_task, ws_health_task]
    if F4_REST_BACKUP_ENABLED:
        tasks.append(asyncio.create_task(
            _run_rest_price_backup(
                ticker,
                spike_filter,
                should_poll_rest,
                vi_watch=vi_watch,
                wake_event=rest_wakeup,
            )
        ))
    if F4_HEARTBEAT_INTERVAL_SEC > 0:
        tasks.append(
            asyncio.create_task(
                _run_monitor_heartbeat(ticker, is_ws_stale, ws_tick_age_ms)
            )
        )
    _active_monitor_tasks.update(tasks)
    try:
        # 모니터 태스크 하나가 예외로 죽어도 나머지 태스크는 유지한다.
        # 정상 종료(HOLDING 해제)만 감시 종료로 취급.
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            crashed = [t for t in done if not t.cancelled() and t.exception() is not None]
            for task in crashed:
                log(
                    "F4_MONITOR_TASK_ERROR",
                    level="CRIT",
                    ticker=ticker,
                    error=repr(task.exception()),
                    remaining_monitors=len(pending),
                )
            if len(crashed) < len(done):
                break
            if crashed and state.get().position_status == "HOLDING":
                await notifier.send(
                    "F4_MONITOR_TASK_ERROR",
                    level="CRIT",
                    message=(
                        f"F4 모니터 태스크 비정상 종료. {ticker} "
                        f"잔여 모니터={len(pending)}개"
                    ),
                    ticker=ticker,
                )
        # 관측 창(15:20)이 끝나 모니터가 정상 종료했으면 캡처를 최종화한다.
        # 취소(shutdown) 시에는 여기 도달하지 않고 main.py finally가
        # PROCESS_SHUTDOWN으로 마감한다. 캡처는 F4 청산 모니터가 소유·취소하지
        # 않으므로 정상 청산이 writer를 죽이지 않는다.
        await _finalize_capture_after_observation()
    finally:
        closing = _closing_task
        # EXITING 전환으로 WS/health 태스크가 먼저 끝나더라도, 청산을 호출한
        # 모니터 태스크를 취소하기 전에 보호된 청산 태스크부터 완료한다.
        # 순서가 반대면 _trigger_close()의 부모가 정상 청산 중 취소되어
        # F4_CLOSE_CANCEL_REQUESTED가 거짓 CRIT로 기록된다.
        try:
            if closing is not None and not closing.done():
                await asyncio.shield(closing)
        finally:
            # shield()는 내부 청산을 보호하지만 run() 자체가 취소되면 여기서
            # CancelledError를 던진다. 정리는 별도 finally에 두어 모든 모니터를
            # 회수하고 연결 상태를 반드시 초기화한다.
            for task in tasks:
                if task is closing:
                    continue
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            _active_monitor_tasks.difference_update(tasks)
            live.ws_connected = False


async def _run_monitor_heartbeat(
    ticker: str,
    is_ws_stale,
    ws_tick_age_ms,
) -> None:
    """Emit monitor liveness without persisting state."""
    while _price_observation_active():
        await asyncio.sleep(F4_HEARTBEAT_INTERVAL_SEC)
        if not _price_observation_active():
            return
        stale = bool(is_ws_stale())
        fields = {
            "ws_connected": bool(live.ws_connected),
            "ws_stale": stale,
            "last_ws_tick_age_ms": ws_tick_age_ms(),
            "last_price": live.last_tick_price,
            "high_price": state.get().high_price,
            "remaining_qty": state.get().remaining_qty,
            "position_status": state.get().position_status,
            "trade_id": state.get().trade_id,
            "rest_backup_enabled": F4_REST_BACKUP_ENABLED,
        }
        log("F4_HEARTBEAT", level="DEBUG", ticker=ticker, **fields)


async def _run_ws_health_monitor(
    ticker: str,
    is_ws_stale,
    ws_status_fields,
) -> None:
    """Detect WS stale/recovery independently from the optional REST backup."""
    stale_reported = False
    tick_idle_episode = False
    stale_event_logged = False
    last_log_at = float("-inf")
    suppressed_stale = 0
    suppressed_recovered = 0
    interval_sec = min(
        1.0,
        max(0.1, F4_WS_STALE_SEC / 2),
    )
    while _price_observation_active():
        # 청산 후에는 주문 안전과 무관한 차트 관측만 수행한다. 실제 소켓
        # 단절은 kis_ws.subscribe()가 WS_DISCONNECTED로 계속 기록하므로,
        # 2초 체결 공백을 WARN/복구 쌍으로 반복 기록하지 않는다.
        if state.get().position_status == "CLOSED":
            stale_reported = False
            await asyncio.sleep(interval_sec)
            continue
        stale = bool(is_ws_stale())
        fields = {**ws_status_fields(), "ws_stale": stale}
        if not bool(fields.get("ws_connected", True)):
            # kis_ws emits WS_DISCONNECTED/WS_CONNECTED for transport state.
            # Avoid duplicating that warning as a no-tick health transition.
            stale_reported = stale
            await asyncio.sleep(interval_sec)
            continue
        event = None
        if stale and not stale_reported:
            event = "WS_STALE"
            stale_reported = True
            tick_idle_episode = True
        elif not stale and stale_reported:
            if tick_idle_episode:
                event = "WS_RECOVERED"
            stale_reported = False
            tick_idle_episode = False
        if event is not None:
            now_mono = time.monotonic()
            if event == "WS_STALE" and (
                now_mono - last_log_at >= F4_WS_HEALTH_LOG_COOLDOWN_SEC
            ):
                log(
                    event,
                    level="INFO" if F4_REST_BACKUP_ENABLED else "WARN",
                    ticker=ticker,
                    suppressed_stale=suppressed_stale,
                    suppressed_recovered=suppressed_recovered,
                    **fields,
                )
                last_log_at = now_mono
                stale_event_logged = True
            elif event == "WS_STALE":
                suppressed_stale += 1
                stale_event_logged = False
            elif stale_event_logged:
                log(
                    event,
                    level="INFO",
                    ticker=ticker,
                    suppressed_stale=suppressed_stale,
                    suppressed_recovered=suppressed_recovered,
                    **fields,
                )
                stale_event_logged = False
                suppressed_stale = 0
                suppressed_recovered = 0
            else:
                suppressed_recovered += 1
        await asyncio.sleep(interval_sec)


async def _run_rest_price_backup(
    ticker: str,
    spike_filter: SpikeFilter,
    should_poll_rest=None,
    vi_watch: ViWatch | None = None,
    wake_event: asyncio.Event | None = None,
) -> None:
    """REST backup while holding or during the short post-close observation."""
    log(
        "F4_REST_BACKUP_START",
        level="INFO",
        ticker=ticker,
        interval_sec=F4_REST_POLL_INTERVAL_SEC,
        only_when_ws_stale=F4_REST_ONLY_WHEN_WS_STALE,
        ws_stale_sec=F4_WS_STALE_SEC,
        post_close_enabled=F4_POST_CLOSE_REST_BACKUP_ENABLED,
        post_close_interval_sec=F4_POST_CLOSE_REST_POLL_INTERVAL_SEC,
    )
    while _price_observation_active():
        is_post_close = state.get().position_status == "CLOSED"
        should_poll = (
            bool(should_poll_rest())
            if should_poll_rest is not None
            else True
        )
        if (
            is_post_close
            and not F4_POST_CLOSE_REST_BACKUP_ENABLED
            and not _capture_backup_active()
        ):
            should_poll = False
        interval_sec = (
            F4_POST_CLOSE_REST_POLL_INTERVAL_SEC
            if is_post_close
            else F4_REST_POLL_INTERVAL_SEC
        )
        if not should_poll:
            await _wait_for_rest_wakeup(interval_sec, wake_event)
            continue
        try:
            if is_post_close:
                price = await _fetch_current_price(
                    ticker,
                    latency_context="F4_POST_CLOSE",
                    aggregate_latency=True,
                    request_priority=kis_rest.REQUEST_PRIORITY_BACKGROUND,
                )
            else:
                price = await _fetch_current_price(
                    ticker,
                    latency_context="F4_HOLDING",
                    aggregate_latency=True,
                )
            if price > 0:
                await _handle_price_tick(
                    price,
                    ticker,
                    spike_filter,
                    source="rest",
                    vi_watch=vi_watch,
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 백업 폴러가 죽으면 WS까지 함께 취소되므로 절대 전파하지 않는다
            log("F4_REST_BACKUP_ERROR", level="WARN", ticker=ticker, error=repr(e))
        await _wait_for_rest_wakeup(interval_sec, wake_event)


async def _wait_for_rest_wakeup(
    interval_sec: float,
    wake_event: asyncio.Event | None,
) -> None:
    if wake_event is None:
        await asyncio.sleep(interval_sec)
        return
    if wake_event.is_set():
        wake_event.clear()
        return
    try:
        await asyncio.wait_for(wake_event.wait(), timeout=interval_sec)
    except TimeoutError:
        pass
    finally:
        wake_event.clear()


def _offset_aware(ts: str | None) -> bool:
    if not ts:
        return False
    try:
        parsed = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


async def _handle_price_tick(
    price: float,
    ticker: str,
    spike_filter: SpikeFilter,
    *,
    source: str,
    vi_watch: ViWatch | None = None,
    tick_meta: dict | None = None,
) -> bool:
    """WS/REST 공용 가격 처리.

    관측 창이 열려 있으면 가격은 저장하되, 주문 가능 로직과 VI 처리는
    HOLDING 상태에서만 실행한다. 처리했으면 True, 관측 종료면 False.
    """
    # 관측 창과 종목 일치를 함께 본다. 낡은 구독의 틱이 다른 종목의 손절
    # 판정·차트·캡처로 흘러들지 않게 한다.
    if not _observation_should_continue(ticker):
        return False

    live.push_tick(price, ticker=ticker)
    # 진입~15:20 durable 가격 경로에 적재한다. 논블로킹·격리 — 캡처 실패가
    # 스탑 추적·주문 경로를 흔들면 안 된다. CLOSED 구간에서도 가격만 기록한다.
    # 거래소 시각(source_ts)·체결량(qty)·출처를 그대로 보존하고, naive/무효
    # 시각은 소리 없이 받아들이지 않고 source_ts=None·valid=False로 표시한다.
    try:
        meta = tick_meta or {}
        raw_source_ts = meta.get("source_ts")
        ts_valid = _offset_aware(raw_source_ts)
        tick_capture.enqueue({
            "source_ts": raw_source_ts if ts_valid else None,
            "received_at": datetime.now(KST).isoformat(),
            "price": price,
            "qty": meta.get("qty"),
            "source": source,
            "valid": ts_valid,
            "ticker": ticker,
            # 미해석 원시 필드(WS만 존재, REST 백업은 None). 관측 전용이며
            # 청산 판단은 이 값을 읽지 않는다.
            "raw": meta.get("raw"),
        })
    except Exception:  # noqa: BLE001 — 캡처는 관측 전용, 절대 전파하지 않는다
        pass
    if state.get().position_status != "HOLDING":
        return True

    await _process_tick(price, spike_filter)
    if state.get().position_status != "HOLDING" or vi_watch is None:
        return True

    try:
        await _handle_vi_events(await vi_watch.on_price(price, source), ticker)
    except Exception as e:
        # 관측 전용 — VI 감시 오류가 스탑 추적을 깨선 안 된다
        log(
            "VI_WATCH_ERROR",
            level="WARN",
            ticker=ticker,
            source=source,
            error=repr(e),
        )
    return True


# ── VI 감지 (관측 전용 — PRD 외 가시성 기능) ─────────────────────────

def _make_vi_watch(ticker: str) -> ViWatch | None:
    if not VI_WATCH_ENABLED:
        return None
    return ViWatch(
        ticker,
        lambda: _fetch_vi_status(ticker),
        freeze_sec=VI_FREEZE_SUSPECT_SEC,
        cooldown_sec=VI_CHECK_COOLDOWN_SEC,
    )


async def _fetch_vi_status(ticker: str) -> dict:
    """변동성완화장치(VI) 현황 조회 — vi_watch 공용 구현 위임."""
    return await vi_watch_mod.fetch_vi_status(ticker)


async def _handle_vi_events(events: list[dict], ticker: str) -> None:
    """ViWatch 이벤트를 로그·텔레그램·live(UI)로 전파한다."""
    for ev in events:
        etype = ev.get("type")
        fields = {k: v for k, v in ev.items() if k not in ("type", "ts")}
        if etype == "VI_DETECTED":
            log("VI_DETECTED", level="WARN", ticker=ticker, **fields)
            live.record_vi_detected(ev)
            await notifier.send(
                "VI_DETECTED",
                level="WARN",
                message=(
                    f"VI 발동 — 발동가 {ev.get('vi_prc')}원, "
                    f"기준가 {ev.get('vi_stnd_prc')}원({ev.get('vi_dprt')}%). "
                    f"2분 단일가 정지, 해제 시 재알림."
                ),
                ticker=ticker,
            )
        elif etype == "VI_RELEASED":
            log("VI_RELEASED", level="INFO", ticker=ticker, **fields)
            live.record_vi_released(ev)
            duration = ev.get("duration_sec") or 0
            await notifier.send(
                "VI_RELEASED",
                level="INFO",
                message=(
                    f"VI 해제 — 재개가 {ev.get('release_price')}원, "
                    f"정지 {duration:.0f}초. 스탑 감시 계속."
                ),
                ticker=ticker,
            )
        elif etype == "VI_CHECK_FAILED":
            log("VI_CHECK_FAILED", level="WARN", ticker=ticker, **fields)
        elif etype == "VI_CHECK_NEGATIVE":
            # 가격 동결인데 VI 아님 — 한산 or 실제 WS 장애 후보. 로그만 남긴다.
            log("VI_CHECK_NEGATIVE", level="INFO", ticker=ticker, **fields)


async def _process_tick(price: float, spike_filter: SpikeFilter) -> None:
    """단일 체결 틱 처리. 우선순위: Hard Stop > Step Trailing (상호 배타적)."""
    s = state.get()
    if s.position_status != "HOLDING":
        return
    if not spike_filter.is_valid(price, s.target_ticker):
        return

    entry = s.entry_price or 0.0
    before_high_price = s.high_price
    before_highest_step = s.highest_step
    before_trailing_active = s.trailing_active
    state.update_high_price(price)

    now = datetime.now(KST)
    late = (now.hour, now.minute) >= (FORCE_TRAILING_HOUR, FORCE_TRAILING_MINUTE)

    # 스텝 갱신 (highest_step은 단조 증가)
    pnl = price / entry - 1
    current_step = max(math.floor(pnl / STEP_SIZE) * STEP_SIZE, 0.0)
    if current_step > s.highest_step:
        s.highest_step = current_step
    if s.highest_step >= STEP_SIZE:
        s.trailing_active = True

    # 청산 10분 전 강제 활성화 (스텝 미달성이어도 trailing 발동)
    if late and not s.trailing_active:
        s.trailing_active = True

    high_changed = s.high_price != before_high_price
    step_changed = s.highest_step != before_highest_step
    trailing_activated = s.trailing_active and not before_trailing_active

    # [우선순위 1] Hard Stop (-2.0%): trailing 미활성 구간에서만 유효
    if not s.trailing_active and price <= entry * (1 - HARD_STOP_RATIO):
        await _trigger_close(price, "HARD_STOP")
        return

    # [우선순위 2] Step Trailing
    if s.trailing_active:
        baseline_stop, recommended_stop = _trailing_shadow_stop_prices(
            entry,
            s.highest_step,
        )
        recommended_hit = price <= recommended_stop
        # 기존 1.5% 선만 먼저 맞은 경우를 저장한다. 현재 2.0% 선도 같은
        # 틱에 맞았다면 청산을 지연시키지 않고 최종화 단계에서 동일 틱으로
        # 비교 행을 만든다.
        if price <= baseline_stop and not recommended_hit:
            await _record_trailing_shadow_baseline(
                price,
                baseline_stop=baseline_stop,
                recommended_stop=recommended_stop,
            )
        if recommended_hit:
            await _trigger_close(price, "TRAILING")
            return

    if high_changed or step_changed or trailing_activated:
        await _persist_tracking_state(force=step_changed or trailing_activated)


def _trailing_shadow_stop_prices(
    entry_price: float,
    highest_step: float,
) -> tuple[float, float]:
    return (
        entry_price * (1 + highest_step - TRAILING_SHADOW_BASELINE_TRAIL),
        entry_price * (1 + highest_step - STEP_TRAIL),
    )


def _trailing_shadow_config_valid() -> bool:
    return (
        TRAILING_SHADOW_ENABLED
        and 0 < TRAILING_SHADOW_BASELINE_TRAIL < STEP_TRAIL
    )


async def _record_trailing_shadow_baseline(
    price: float,
    *,
    baseline_stop: float,
    recommended_stop: float,
) -> None:
    """Record the first legacy-rule exit without affecting the live rule."""
    global _shadow_baseline_recorded_trade_id
    s = state.get()
    if (
        not _trailing_shadow_config_valid()
        or not s.trade_id
        or _shadow_baseline_recorded_trade_id == s.trade_id
    ):
        return
    # 보고 전용 DB 장애가 손절 임박 가격대의 매 틱 쓰기로 번지지 않도록
    # 성공 여부와 무관하게 이 프로세스에서는 거래당 한 번만 시도한다.
    _shadow_baseline_recorded_trade_id = s.trade_id
    try:
        inserted = await db.record_trailing_shadow_baseline(
            s.trade_id,
            baseline_step_trail=TRAILING_SHADOW_BASELINE_TRAIL,
            recommended_step_trail=STEP_TRAIL,
            entry_price=float(s.entry_price or 0),
            highest_step=s.highest_step,
            baseline_stop_price=baseline_stop,
            recommended_stop_price=recommended_stop,
            baseline_exit_price=price,
        )
        if inserted:
            entry = float(s.entry_price or price)
            log(
                "TRAILING_SHADOW_BASELINE_EXIT",
                level="INFO",
                ticker=s.target_ticker,
                trade_id=s.trade_id,
                baseline_step_trail_pct=round(
                    TRAILING_SHADOW_BASELINE_TRAIL * 100,
                    2,
                ),
                recommended_step_trail_pct=round(STEP_TRAIL * 100, 2),
                highest_step=s.highest_step,
                baseline_stop_price=round(baseline_stop, 0),
                recommended_stop_price=round(recommended_stop, 0),
                baseline_exit_price=price,
                baseline_pnl_pct=round((price / entry - 1) * 100, 4),
            )
    except Exception as exc:
        # 보고 전용 shadow가 현재 청산 판단을 흔들면 안 된다.
        log(
            "TRAILING_SHADOW_BASELINE_RECORD_ERROR",
            level="WARN",
            ticker=s.target_ticker,
            trade_id=s.trade_id,
            error=repr(exc),
        )


async def finalize_trailing_shadow(
    *,
    trigger_price: float,
    actual_exit_price: float,
    exit_qty: int,
    actual_pnl_pct: float,
    close_reason: str,
) -> dict | None:
    """Finalize and log the per-trade legacy-vs-current exit comparison."""
    s = state.get()
    if not _trailing_shadow_config_valid() or not s.trade_id:
        return None
    entry = float(s.entry_price or 0)
    decision_exit = trigger_price if trigger_price > 0 else actual_exit_price
    if entry <= 0 or decision_exit <= 0 or actual_exit_price <= 0 or exit_qty <= 0:
        return None

    baseline_stop: float | None = None
    recommended_stop: float | None = None
    if s.trailing_active:
        baseline_stop, recommended_stop = _trailing_shadow_stop_prices(
            entry,
            s.highest_step,
        )
    try:
        comparison = await db.finalize_trailing_shadow_comparison(
            s.trade_id,
            baseline_step_trail=TRAILING_SHADOW_BASELINE_TRAIL,
            recommended_step_trail=STEP_TRAIL,
            entry_price=entry,
            exit_qty=exit_qty,
            highest_step=s.highest_step,
            baseline_stop_price=baseline_stop,
            recommended_stop_price=recommended_stop,
            recommended_exit_price=decision_exit,
            actual_exit_price=actual_exit_price,
            actual_pnl_pct=actual_pnl_pct,
            close_reason=close_reason,
        )
        log(
            "TRAILING_SHADOW_FINAL",
            level="INFO",
            ticker=s.target_ticker,
            trade_id=s.trade_id,
            close_reason=close_reason,
            baseline_stop_price=comparison.get("baseline_stop_price"),
            recommended_stop_price=comparison.get("recommended_stop_price"),
            baseline_exit_price=comparison.get("baseline_exit_price"),
            recommended_exit_price=comparison.get("recommended_exit_price"),
            actual_exit_price=comparison.get("actual_exit_price"),
            baseline_pnl_pct=comparison.get("baseline_pnl_pct"),
            recommended_pnl_pct=comparison.get("recommended_pnl_pct"),
            actual_pnl_pct=comparison.get("actual_pnl_pct"),
            pnl_delta_pct=comparison.get("pnl_delta_pct"),
            pnl_delta_amount=comparison.get("pnl_delta_amount"),
        )
        return comparison
    except Exception as exc:
        log(
            "TRAILING_SHADOW_FINALIZE_ERROR",
            level="WARN",
            ticker=s.target_ticker,
            trade_id=s.trade_id,
            error=repr(exc),
        )
        return None

async def _trigger_close(price: float, reason: str) -> None:
    """Run a close once; state becomes CLOSED only after sell/DB/persist succeeds."""
    global _close_in_progress, _close_in_progress_warned, _closing_task
    if _close_in_progress:
        if not _close_in_progress_warned:
            log(
                "F4_CLOSE_ALREADY_IN_PROGRESS", level="WARN",
                ticker=state.get().target_ticker, reason=reason,
            )
            _close_in_progress_warned = True
        return

    _close_in_progress = True
    _close_in_progress_warned = False
    close_task = asyncio.create_task(
        _execute_close(price, reason),
        name=f"f4_execute_close_{reason.lower()}",
    )
    _closing_task = close_task
    try:
        await asyncio.shield(close_task)
    except asyncio.CancelledError:
        log(
            "F4_CLOSE_CANCEL_REQUESTED", level="CRIT",
            ticker=state.get().target_ticker, reason=reason,
        )
        try:
            await notifier.send(
                "F4_CLOSE_CANCEL_REQUESTED",
                level="CRIT",
                message=(
                    f"F4 청산 태스크 취소 요청 감지: {state.get().target_ticker} "
                    f"{reason}. 청산 완료까지 대기합니다."
                ),
                ticker=state.get().target_ticker,
            )
        finally:
            await asyncio.shield(close_task)
            raise
    finally:
        if close_task.done():
            _close_in_progress = False
            _close_in_progress_warned = False
            if _closing_task is close_task:
                _closing_task = None


async def close_now(price: float, reason: str) -> bool:
    """F3 비상가드 등 외부 모듈이 동일한 확인 청산 경로를 사용한다."""
    await _trigger_close(price, reason)
    return state.get().position_status in {"EXITING", "CLOSED"}


async def _persist_tracking_state(force: bool = False) -> bool:
    """Persist in-trade trailing progress with a small throttle."""
    global _last_state_persist_at
    s = state.get()
    if s.position_status != "HOLDING" or not s.trade_id:
        return False

    now_mono = time.monotonic()
    if (
        not force
        and F4_STATE_PERSIST_INTERVAL_SEC > 0
        and now_mono - _last_state_persist_at < F4_STATE_PERSIST_INTERVAL_SEC
    ):
        return False

    state_saved = False
    db_saved = False
    try:
        await state.persist(
            os.getenv("STATE_DIR", "data/state"),
            datetime.now(KST).strftime("%Y%m%d"),
        )
        state_saved = True
    except Exception as exc:
        log(
            "F4_STATE_PERSIST_ERROR",
            level="WARN",
            ticker=s.target_ticker,
            error=repr(exc),
        )

    try:
        await db.update_trade_progress(s.trade_id, s.high_price, s.highest_step)
        db_saved = True
    except Exception as exc:
        log(
            "F4_DB_PROGRESS_ERROR",
            level="WARN",
            ticker=s.target_ticker,
            trade_id=s.trade_id,
            error=repr(exc),
        )

    if not (state_saved or db_saved):
        return False

    _last_state_persist_at = now_mono
    log(
        "F4_STATE_PERSISTED",
        level="DEBUG",
        ticker=s.target_ticker,
        high_price=s.high_price,
        highest_step=s.highest_step,
        trailing_active=s.trailing_active,
        state_saved=state_saved,
        db_saved=db_saved,
        force=force,
    )
    return True

async def _execute_close(price: float, reason: str) -> bool:
    try:
        return await _execute_close_impl(price, reason)
    except asyncio.CancelledError:
        s = state.get()
        log("F4_CLOSE_TASK_CANCELLED", level="CRIT", ticker=s.target_ticker, reason=reason)
        try:
            await asyncio.shield(notifier.send(
                "F4_CLOSE_TASK_CANCELLED",
                level="CRIT",
                message=(
                    f"F4 청산 태스크 직접 취소 감지: {s.target_ticker} "
                    f"{reason}. KIS 체결/잔고 확인 필요"
                ),
                ticker=s.target_ticker,
            ))
        except Exception:
            pass
        raise


async def recover_pending_exit() -> bool:
    """재시작 시 내구적으로 저장된 매도 의도를 KIS 주문과 대사한다."""
    s = state.get()
    pending = dict(s.pending_exit or {})
    if s.position_status != "EXITING" or not pending:
        return False
    mode = os.getenv("KIS_MODE", "PAPER")
    outcome, snapshot = await exit_recovery.reconcile_pending_intent(mode)
    if outcome == "RECONCILED" and snapshot is not None:
        if s.trade_id:
            summary = await db.sell_fill_summary(s.trade_id)
            total_qty = int(summary.get("fill_qty") or 0)
            total_amt = float(summary.get("fill_amt") or 0)
            close_target_qty = int(s.entry_qty or 0)
            remaining_qty = max(0, close_target_qty - total_qty)
        else:
            # SQLite 강등 포지션은 누적 체결 원장이 없으므로 이 pending 주문의
            # 요청량과 KIS 누적 체결/잔량만으로 판단한다.
            total_qty = int(snapshot.get("fill_qty") or 0)
            total_amt = float(snapshot.get("fill_amt") or 0)
            close_target_qty = int(pending.get("requested_qty") or 0)
            remaining_qty = int(
                snapshot.get("remaining_qty")
                if snapshot.get("remaining_qty") is not None
                else max(0, close_target_qty - total_qty)
            )
        if (
            close_target_qty > 0
            and total_qty >= close_target_qty
            and remaining_qty <= 0
        ):
            exit_price = round(total_amt / total_qty) if total_qty else 0
            entry = float(s.entry_price or 0)
            pnl_pct = round((exit_price / entry - 1) * 100, 2) if entry else 0.0
            reason = str(pending.get("reason") or s.close_reason or "TIMEOUT")
            if s.trade_id:
                await db.close_trade(
                    s.trade_id,
                    exit_price,
                    reason,
                    pnl_pct,
                    s.highest_step,
                    exit_qty=total_qty,
                    high_price=s.high_price,
                )
                await finalize_trailing_shadow(
                    trigger_price=float(pending.get("trigger_price") or 0),
                    actual_exit_price=exit_price,
                    exit_qty=total_qty,
                    actual_pnl_pct=pnl_pct,
                    close_reason=reason,
                )
            await state.set_closed(reason)
            await state.persist(
                os.getenv("STATE_DIR", "data/state"),
                datetime.now(KST).strftime("%Y%m%d"),
            )
            log(
                "EXIT_ORDER_RECOVERY_CLOSED",
                level="CRIT",
                ticker=s.target_ticker,
                order_id=snapshot.get("order_id"),
                exit_qty=total_qty,
                exit_price=exit_price,
            )
            await notifier.send(
                "EXIT_ORDER_RESPONSE_RECOVERED",
                level="CRIT",
                message=(
                    f"재시작 매도 주문 대사 완료: {s.target_ticker} "
                    f"{total_qty}주 전량 체결 확인"
                ),
                ticker=s.target_ticker,
            )
            return True
        await state.set_exit_remaining_qty(remaining_qty)

    state.get().day_skip = True
    await state.persist(
        os.getenv("STATE_DIR", "data/state"),
        datetime.now(KST).strftime("%Y%m%d"),
    )
    log(
        "EXIT_ORDER_RECOVERY_PENDING",
        level="CRIT",
        ticker=s.target_ticker,
        outcome=outcome,
        order_id=(snapshot or {}).get("order_id"),
        fill_qty=(snapshot or {}).get("fill_qty"),
        remaining_qty=s.remaining_qty,
    )
    await notifier.send(
        "EXIT_ORDER_RECOVERY_PENDING",
        level="CRIT",
        message=(
            f"재시작 매도 주문 대사 미완료({outcome}): {s.target_ticker}. "
            "자동 재주문을 차단했습니다. 주문/잔고를 확인하세요."
        ),
        ticker=s.target_ticker,
    )
    return False


async def _execute_close_impl(price: float, reason: str) -> bool:
    """잔여 전량 시장가 매도 후 로그/알림/DB 기록."""
    s = state.get()
    qty = s.remaining_qty or 0
    entry = s.entry_price or price
    mode = os.getenv("KIS_MODE", "PAPER")

    if os.getenv("DRY_RUN", "0") == "1":
        exit_price = price
        pnl_pct = round((exit_price / entry - 1) * 100, 2) if entry else 0.0
        event_name = "TRAILING_STOP" if reason == "TRAILING" else reason
        level = "INFO" if reason == "TRAILING" else "WARN"
        log_extra: dict = {}
        if reason == "TRAILING":
            stop_price = entry * (1 + s.highest_step - STEP_TRAIL)
            log_extra = {"highest_step": s.highest_step, "stop_price": round(stop_price, 0)}
        log(event_name, level=level, ticker=s.target_ticker,
            entry_price=entry, exit_price=exit_price, exit_qty=qty,
            pnl_pct=pnl_pct, dry_run=True, fill_latency_ms=0, **log_extra)
        await state.set_closed(reason)
        await state.persist(os.getenv("STATE_DIR", "data/state"),
                            datetime.now(KST).strftime("%Y%m%d"))
        return True

    sell_id = ""
    exit_price = price
    fill_latency_ms: int | None = None
    fill: dict | None = None
    filled_qty = 0
    full_fill_confirmed = False
    order_db_id = 0
    try:
        order_started_at = time.perf_counter()
        submission = await exit_recovery.submit_sell(
            qty=qty,
            reason=reason,
            phase="SLIPPAGE_SELL" if reason == "SLIPPAGE_GUARD" else "CLOSE_SELL",
            trigger_price=price,
            mode=mode,
            send=lambda: _send_sell(s.target_ticker, qty, mode),
        )
        if not submission.acknowledged:
            sell_resp = submission.response or {}
            event = (
                "F4_SELL_SUBMISSION_UNKNOWN"
                if submission.uncertain
                else "F4_SELL_ERROR"
            )
            action = (
                "주문 응답 유실 대사 실패 — 재주문하지 말고 KIS 주문/잔고 확인 필요"
                if submission.uncertain
                else "매도 주문 거절 또는 로컬 의도 저장 실패 — 수동 청산 필요"
            )
            log(
                event,
                level="CRIT",
                ticker=s.target_ticker,
                msg_cd=sell_resp.get("msg_cd"),
                msg1=sell_resp.get("msg1"),
            )
            await notifier.send(
                event,
                level="CRIT",
                message=f"{s.target_ticker} {action}",
                ticker=s.target_ticker,
            )
            return False
        sell_id = submission.order_id
        order_db_id = submission.order_db_id
        if submission.matched_order:
            matched = submission.matched_order
            matched_qty = int(matched.get("fill_qty") or 0)
            if matched_qty > 0:
                fill = {
                    "fill_price": float(matched.get("fill_price") or 0),
                    "fill_qty": matched_qty,
                }
        if not sell_id:
            raise RuntimeError(
                "sell acknowledged without order id after reconciliation"
            )
        if fill is None or int(fill.get("fill_qty") or 0) < qty:
            fill = await _poll_fill(sell_id, timeout_sec=30, expect_qty=qty)
        if fill:
            exit_price = fill["fill_price"]
            filled_qty = max(0, int(fill.get("fill_qty") or 0))
            full_fill_confirmed = filled_qty >= qty
            fill_latency_ms = max(
                0,
                round((time.perf_counter() - order_started_at) * 1000),
            )
            if not full_fill_confirmed:
                await state.set_exit_remaining_qty(qty - filled_qty)
        else:
            log("F4_FILL_UNCONFIRMED", level="WARN", ticker=s.target_ticker,
                order_id=sell_id)
    except Exception as e:
        log("F4_SELL_ERROR", level="CRIT", ticker=s.target_ticker, error=repr(e))
        await notifier.send(
            "F4_SELL_ERROR",
            level="CRIT",
            message=f"매도 주문 오류: {s.target_ticker} {repr(e)}. 수동 청산 필요",
            ticker=s.target_ticker,
        )
        return False

    pnl_pct = round((exit_price / entry - 1) * 100, 2) if entry else 0.0

    if s.trade_id or order_db_id:
        try:
            if fill and order_db_id:
                await db.update_order_fill(
                    order_db_id,
                    exit_price,
                    filled_qty,
                    fill_latency_ms,
                    status="FILLED" if full_fill_confirmed else "PARTIAL_FILL",
                )
            if full_fill_confirmed and s.trade_id:
                await db.close_trade(
                    s.trade_id,
                    exit_price,
                    reason,
                    pnl_pct,
                    s.highest_step,
                    exit_qty=qty,
                    high_price=s.high_price,
                )
        except Exception as e:
            record_action = (
                "완전체결 확인으로 CLOSED 처리"
                if full_fill_confirmed
                else "체결 확인 대기 상태 유지"
            )
            log(
                "F4_CLOSE_RECORD_ERROR",
                level="CRIT",
                ticker=s.target_ticker,
                order_id=sell_id,
                error=repr(e),
            )
            await notifier.send(
                "F4_CLOSE_RECORD_ERROR",
                level="CRIT",
                message=(
                    f"F4 매도 후 DB 기록 실패: {s.target_ticker} "
                    f"order_id={sell_id} {repr(e)}. "
                    f"{record_action}"
                ),
                ticker=s.target_ticker,
            )
            if full_fill_confirmed:
                await state.set_closed(reason)
            await state.persist(
                os.getenv("STATE_DIR", "data/state"),
                datetime.now(KST).strftime("%Y%m%d"),
            )
            return True

    if not full_fill_confirmed:
        remaining_qty = state.get().remaining_qty or qty
        log(
            "F4_CLOSE_PENDING",
            level="CRIT",
            ticker=s.target_ticker,
            order_id=sell_id,
            requested_qty=qty,
            confirmed_fill_qty=filled_qty,
            remaining_qty=remaining_qty,
            reason=reason,
        )
        await notifier.send(
            "F4_CLOSE_PENDING",
            level="CRIT",
            message=(
                f"매도 주문 체결 확인 대기: {s.target_ticker} "
                f"주문={qty}주 확인체결={filled_qty}주 잔여={remaining_qty}주. "
                f"주문/잔고 확인 필요"
            ),
            ticker=s.target_ticker,
        )
        await state.persist(
            os.getenv("STATE_DIR", "data/state"),
            datetime.now(KST).strftime("%Y%m%d"),
        )
        return True

    await finalize_trailing_shadow(
        trigger_price=price,
        actual_exit_price=exit_price,
        exit_qty=qty,
        actual_pnl_pct=pnl_pct,
        close_reason=reason,
    )

    event_name = "TRAILING_STOP" if reason == "TRAILING" else reason
    level = "INFO" if reason == "TRAILING" else "WARN"

    log_extra: dict = {}
    if reason == "TRAILING":
        stop_price = entry * (1 + s.highest_step - STEP_TRAIL)
        log_extra = {"highest_step": s.highest_step, "stop_price": round(stop_price, 0)}

    log(event_name, level=level, ticker=s.target_ticker,
        entry_price=entry, trigger_price=price, exit_price=exit_price, exit_qty=qty,
        pnl_pct=pnl_pct, fill_latency_ms=fill_latency_ms, **log_extra)
    await notifier.send(
        event_name, level=level,
        message=f"{reason} 청산: {s.target_ticker} @ {exit_price:,}원 (P&L {pnl_pct:+.2f}%)",
        ticker=s.target_ticker,
    )
    await state.set_closed(reason)
    await state.persist(os.getenv("STATE_DIR", "data/state"),
                        datetime.now(KST).strftime("%Y%m%d"))
    return True


async def _run_dry_ticks(ticker: str, spike_filter: SpikeFilter) -> None:
    s = state.get()
    entry = float(s.entry_price or os.getenv("DRY_RUN_ENTRY_PRICE", "10300"))
    delay = float(os.getenv("DRY_RUN_STEP_DELAY", "0.2"))
    prices = [
        entry,
        round(entry * 1.026),
        round(entry * 1.032),
        # Use a value slightly below the configured first-step stop so DRY_RUN
        # deterministically closes when STEP_TRAIL changes.
        round(entry * (1 + STEP_SIZE - STEP_TRAIL)) - 1,
    ]

    live.ws_connected = True
    log("DRY_RUN_F4_START", level="WARN", ticker=ticker, entry_price=entry, prices=prices)
    for price in prices:
        if state.get().position_status != "HOLDING":
            break
        live.push_tick(price, ticker=ticker)
        log("DRY_RUN_TICK", level="INFO", ticker=ticker, price=price)
        await _process_tick(price, spike_filter)
        await asyncio.sleep(delay)
    live.ws_connected = False
    log(
        "DRY_RUN_F4_DONE",
        level="WARN",
        ticker=ticker,
        position_status=state.get().position_status,
        close_reason=state.get().close_reason,
    )


async def _fetch_current_price(
    ticker: str,
    *,
    latency_context: str | None = None,
    aggregate_latency: bool = False,
    request_priority: int = kis_rest.REQUEST_PRIORITY_PRICE,
) -> float:
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        tr_id="FHKST01010100",
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        latency_context=latency_context,
        aggregate_latency=aggregate_latency,
        request_priority=request_priority,
    )
    out = resp.get("output", {}) if isinstance(resp.get("output"), dict) else {}
    return float(out.get("stck_prpr") or out.get("antc_cnpr") or 0)


async def _send_sell(ticker: str, qty: int, mode: str) -> dict:
    return await kis_rest.post(
        "/uapi/domestic-stock/v1/trading/order-cash",
        tr_id=_SELL_TR[mode],
        body={
            "CANO": kis_rest.account_no(),
            "ACNT_PRDT_CD": kis_rest.account_cd(),
            "PDNO": ticker,
            "ORD_DVSN": "01",
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
        },
        include_response_meta=True,
    )


async def _poll_fill(
    order_id: str,
    timeout_sec: int = 30,
    expect_qty: int = 0,
) -> dict | None:
    """Poll cumulative fills until the requested quantity is confirmed.

    KIS can expose an intermediate cumulative fill before a market order is
    completely filled. Preserve the latest partial result for the timeout
    path, but do not return it early when the caller supplied expect_qty.

    rmn_qty == 0 means the order is terminal (fully filled, or cancelled /
    expired after a partial). Return immediately in that case — waiting out
    the remaining attempts would only delay the caller's partial-fill
    handling (F4_CLOSE_PENDING alert, remaining-qty bookkeeping).
    """
    mode = os.getenv("KIS_MODE", "PAPER")
    today = datetime.now(KST).strftime("%Y%m%d")
    attempts = max(1, round(timeout_sec / F4_FILL_POLL_INTERVAL_SEC))
    last_partial: dict | None = None
    for _ in range(attempts):
        try:
            resp = await kis_rest.get(
                "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                tr_id=_CCLD_TR[mode],
                params={
                    "CANO": kis_rest.account_no(),
                    "ACNT_PRDT_CD": kis_rest.account_cd(),
                    "INQR_STRT_DT": today,
                    "INQR_END_DT": today,
                    "SLL_BUY_DVSN_CD": "01",
                    "INQR_DVSN": "00",
                    "PDNO": "",
                    "CCLD_DVSN": "00",
                    "ORD_GNO_BRNO": "",
                    "ODNO": order_id,
                    "INQR_DVSN_3": "00",
                    "INQR_DVSN_1": "",
                    "EXCG_ID_DVSN_CD": "KRX",
                    "CTX_AREA_FK100": "",
                    "CTX_AREA_NK100": "",
                },
                request_priority=kis_rest.REQUEST_PRIORITY_ORDER_STATUS,
            )
            for item in resp.get("output1", []):
                if item.get("odno") == order_id:
                    tot_qty = int(item.get("tot_ccld_qty") or 0)
                    tot_amt = float(item.get("tot_ccld_amt") or 0)
                    rmn_qty = item.get("rmn_qty")
                    if tot_qty > 0:
                        last_partial = {
                            "fill_price": round(tot_amt / tot_qty),
                            "fill_qty": tot_qty,
                        }
                        if expect_qty <= 0 or tot_qty >= expect_qty:
                            return last_partial
                        # 잔량 0 = 주문 종료. 더 채워질 여지가 없으므로 즉시 반환한다.
                        if rmn_qty is not None and int(rmn_qty or 0) == 0:
                            return last_partial
        except Exception:
            pass
        await asyncio.sleep(F4_FILL_POLL_INTERVAL_SEC)
    return last_partial
