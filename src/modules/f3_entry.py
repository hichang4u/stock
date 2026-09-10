"""F3. 진입 주문 모듈 (09:10 이후) — PRD §F3"""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from src import db, notifier, state
from src.api import kis_rest
from src.modules import f1_selector, paper_fast_probe, tick_capture, vi_watch
from src.utils.logger import log
from src.utils.number import to_float

KST = ZoneInfo("Asia/Seoul")


def _begin_tick_capture(ticker: str, trade_id: int) -> None:
    """진입 체결 확정 직후 가격 경로 캡처를 시작한다(논블로킹·가드).

    캡처/파일/DB 실패는 진입을 막거나 지연시키지 않는다. 실제 체결 경로에서만
    호출하며 DRY_RUN은 건너뛴다.
    """
    if os.getenv("DRY_RUN", "0") == "1":
        return
    try:
        from src.modules import baseline_experiment

        tick_capture.start(
            _today(),
            ticker,
            trade_id,
            baseline_experiment.active_experiment_id(),
            state.get().entry_at,
        )
    except Exception as exc:  # noqa: BLE001 — 캡처 시작 실패가 진입을 막지 않는다
        log("TICK_CAPTURE_START_ERROR", level="WARN", ticker=ticker, error=repr(exc))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


GAP_MIN_RECHECK = 0.020  # 재검증 하한 (F1 3%보다 낮음 — 완충). F3 로컬 완충값.
# F1 고갭 유동성 정책과 정확히 일치시키기 위해 8%/10%/50억원 임계값은 F1에서 가져온다.
# F3가 자체 상수를 복제하면 정책이 갈라져 F1 이후 우회가 생긴다.
GAP_HIGH_BAND = f1_selector.GAP_CORE_MAX  # >=8%: 고갭 유동성 요건 적용 경계
GAP_MAX_ORDER = f1_selector.GAP_HARD_MAX  # F1 고갭 후보 범위와 동일: +10% 미만
GAP_MAX_FILL = f1_selector.GAP_HARD_MAX  # +10% 이상 추격 체결은 즉시 방어 청산
HIGH_GAP_MIN_EXPECTED_AMOUNT = f1_selector.HIGH_GAP_MIN_EXPECTED_AMOUNT
ALLOC_RATIO = float(os.getenv("F3_ALLOC_RATIO", "0.95"))  # 주문가능 현금 기본 95% 기준
F3_QTY_CLAMP_WARN_PCT = max(
    0.0,
    float(os.getenv("F3_QTY_CLAMP_WARN_PCT", "20.0")),
)
FIRST_RATIO = 1.00  # 1차 100%
PYRAMID_MIN_UP = 0.005  # 피라미딩 조건 +0.5% 이상 유지
F3_ENTRY_MAX_ATTEMPTS = max(1, int(os.getenv("F3_ENTRY_MAX_ATTEMPTS", "2")))
F3_ENTRY_RETRY_DELAY_SEC = float(os.getenv("F3_ENTRY_RETRY_DELAY_SEC", "0.5"))
F3_ENTRY_CANCEL_RELEASE_WAIT_SEC = float(os.getenv("F3_ENTRY_CANCEL_RELEASE_WAIT_SEC", "1.5"))
# 취소 거부 시 기체결 여부를 재확인하는 폴링 창 — 취소 거부의 흔한 원인이 기체결이다.
# 실측 체결조회(inquire-daily-ccld) 왕복은 0.7~3.3초(20260827~20260902)다. 2초 창은
# 느린 날 한 번도 완주하지 못해 무조건 빈손으로 끝났다. 최악 실측치의 두 배를 준다.
F3_ENTRY_CANCEL_CONFIRM_FILL_SEC = float(os.getenv("F3_ENTRY_CANCEL_CONFIRM_FILL_SEC", "6.5"))
F3_ENTRY_RETRY_DEADLINE = os.getenv("F3_ENTRY_RETRY_DEADLINE", "09:11:00")
# 우선 계측(shadow)만 수행한다. 초과해도 신규 주문을 차단하지 않으며 실제
# 분포를 확인한 뒤 별도 변경으로 enforcement를 활성화한다.
F3_ENTRY_TOTAL_BUDGET_SEC = max(
    0.0,
    _env_float("F3_ENTRY_TOTAL_BUDGET_SEC", 45.0),
)
F3_PRE_ORDER_QUIET_SEC = float(os.getenv("F3_PRE_ORDER_QUIET_SEC", "1.5"))
# 갭 상한과 체결 상한이 동일하므로 매수는 지정가 전용이다.
F3_ASK_SLIPPAGE_RATIO = max(
    0.0,
    _env_float("F3_ASK_SLIPPAGE_RATIO", 0.01),
)
F3_QUOTE_MOVE_WARN_PCT = max(
    0.0,
    _env_float("F3_QUOTE_MOVE_WARN_PCT", 1.5),
)

# 제거된 설정 → 대체 설정. 남아 있는 값은 조용히 무시되므로 기동 시 1회 경고한다.
# 슬리피지 상한처럼 운영자가 "아직 걸려 있다"고 믿는 안전장치는 특히 위험하다.
_REMOVED_ENV_VARS = {
    "F3_MAX_ENTRY_SLIPPAGE_RATIO": "F3_ASK_SLIPPAGE_RATIO",
}


def _warn_removed_env_vars() -> None:
    for removed, replacement in _REMOVED_ENV_VARS.items():
        value = os.getenv(removed)
        if value is None:
            continue
        log(
            "F3_ENV_REMOVED",
            level="WARN",
            removed_env=removed,
            removed_value=value,
            replacement_env=replacement,
            replacement_value=os.getenv(replacement),
        )
    configured_limit_mode = os.getenv("F3_LIMIT_BUY_ENABLED")
    if configured_limit_mode not in (None, "1"):
        log(
            "F3_LIMIT_BUY_REQUIRED",
            level="WARN",
            configured_value=configured_limit_mode,
            effective_value="1",
            reason="MARKET_BUY_UNSAFE_WITH_ZERO_GAP_BUFFER",
        )


_warn_removed_env_vars()


def _default_final_quote_max_age_ms(mode: str) -> int:
    """PAPER 1.1초 호출 간격을 통과시키되 REAL은 더 엄격하게 유지한다."""
    return 1_500 if mode == "PAPER" else 500


def _effective_final_quote_max_age_ms(mode: str, configured_ms: int) -> int:
    """PAPER에서 양수 신선도 가드가 REST 호출 간격보다 짧아지지 않게 한다."""
    configured_ms = max(0, configured_ms)
    if mode == "PAPER" and configured_ms > 0:
        return max(configured_ms, _default_final_quote_max_age_ms(mode))
    return configured_ms


_KIS_MODE = os.getenv("KIS_MODE", "PAPER")
_F3_FINAL_QUOTE_MAX_AGE_MS_CONFIGURED = int(
    os.getenv(
        "F3_FINAL_QUOTE_MAX_AGE_MS",
        str(_default_final_quote_max_age_ms(_KIS_MODE)),
    )
)
F3_FINAL_QUOTE_MAX_AGE_MS = _effective_final_quote_max_age_ms(
    _KIS_MODE,
    _F3_FINAL_QUOTE_MAX_AGE_MS_CONFIGURED,
)
F3_LIMIT_FILL_TIMEOUT_SEC = max(
    0.1,
    float(os.getenv("F3_LIMIT_FILL_TIMEOUT_SEC", "2.0")),
)
F3_ENTRY_AUDIT_TIMEOUT_SEC = max(
    0.01,
    _env_float("F3_ENTRY_AUDIT_TIMEOUT_SEC", 0.25),
)
_entry_audit_tasks: set[asyncio.Task[None]] = set()
# 체결 폴링 간격 상한. 실제 대기는 min(interval, 남은 예산)로 적응 — 마감을
# 넘겨 새 조회를 시작하지 않으면서, 절삭/무조건 1초 대기로 폴링이 1회로 굳는
# 것을 막는다. 기본 1.0초로 기존 폴링 부하(조회 빈도)를 유지한다.
F3_FILL_POLL_INTERVAL_SEC = max(
    0.05,
    _env_float("F3_FILL_POLL_INTERVAL_SEC", 1.0),
)
F3_RECHECK_MAX_ATTEMPTS = max(1, int(os.getenv("F3_RECHECK_MAX_ATTEMPTS", "3")))
F3_RECHECK_RETRY_DELAY_SEC = max(0.0, _env_float("F3_RECHECK_RETRY_DELAY_SEC", 0.5))
# Hard wall-clock cap on the whole opening-transition recheck (all gets + sleeps).
F3_RECHECK_TOTAL_BUDGET_SEC = max(0.0, _env_float("F3_RECHECK_TOTAL_BUDGET_SEC", 5.0))
F3_RECHECK_BATCH_TIMEOUT_SEC = float(os.getenv("F3_RECHECK_BATCH_TIMEOUT_SEC", "0"))
BALANCE_QUERY_MAX_ATTEMPTS = max(1, int(os.getenv("BALANCE_QUERY_MAX_ATTEMPTS", "3")))
BALANCE_QUERY_RETRY_DELAY_SEC = float(os.getenv("BALANCE_QUERY_RETRY_DELAY_SEC", "1.0"))
BALANCE_SNAPSHOT_TTL_SEC = max(
    0.0,
    _env_float("BALANCE_SNAPSHOT_TTL_SEC", 90.0),
)
F3_FAST_RECHECK_MAX_AGE_SEC = max(
    0.0,
    _env_float("F3_FAST_RECHECK_MAX_AGE_SEC", 15.0),
)
F3_FIRST_ORDER_AT = "IMMEDIATE"
F3_PYRAMID_AT = os.getenv("F3_PYRAMID_AT", "09:10:40")
# 2026-07-20: VI 정지 중 시장가 진입 → 폴링 창(12s)이 VI(2분)보다 짧아 전량 미체결.
# 실제 발동 중에는 주문하지 않고 해제를 기다린 뒤 최종 호가·갭을 다시 검증한다.
F3_VI_CHECK_ENABLED = os.getenv("F3_VI_CHECK_ENABLED", "1") == "1"
F3_VI_RELEASE_WAIT_SEC = max(
    0.0,
    _env_float("F3_VI_RELEASE_WAIT_SEC", 130.0),
)
F3_VI_RELEASE_POLL_SEC = max(
    0.1,
    _env_float("F3_VI_RELEASE_POLL_SEC", 2.0),
)


def _normalize_expected_amount(value: object) -> float | None:
    """예상 체결대금을 유한한 float 또는 None으로 정규화한다.

    None/비수치/문자열/±inf/NaN/bool은 모두 무효(None)로 취급한다. 이렇게
    정규화한 값만 고갭 유동성 판정·저장·로깅에 사용해 raw Any가 새지 않게 한다.
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount):
        return None
    return amount


def _amount_qualifies_high_gap(expected_amount: float | None) -> bool:
    """[8%,10%) 고갭 진입 유동성 요건: 예상 체결대금이 F1 하한 이상인지.

    F1 f1_selector.high_gap_allowed와 동일한 판정을 F3에서 재사용한다.
    대금이 없거나 무효(None/NaN/±inf/비수치/<=0)면 fail-closed로 False.
    """
    amount = _normalize_expected_amount(expected_amount)
    if amount is None or amount <= 0:
        return False
    return amount >= HIGH_GAP_MIN_EXPECTED_AMOUNT


def _evaluate_order_gap(gap: float, expected_amount: float | None) -> tuple[bool, str]:
    """F1 고갭 유동성 정책을 반영한 주문 전 갭 판정. 모든 매수 게이트의 단일 기준.

    반환: (허용 여부, 사유). 사유는 OK/INVALID_GAP/BELOW_MIN/ABOVE_MAX/HIGH_GAP_AMOUNT_LOW.
    - 비유한(NaN/±inf) 갭: INVALID_GAP fail-closed 차단 (NaN 비교가 모두 False라
      fail-open 되는 것을 방지; 호출부는 GAP_CHANGED로 매핑해 주문하지 않는다)
    - gap < 2%: BELOW_MIN 차단
    - 2% <= gap < 8%: 허용 (대금 무관)
    - 8% <= gap < 10%: 예상 체결대금이 F1 하한 이상일 때만 허용, 아니면(대금
      부재·무효 포함) HIGH_GAP_AMOUNT_LOW fail-closed 차단
    - gap >= 10%: ABOVE_MAX 차단
    """
    if not math.isfinite(gap):
        return (False, "INVALID_GAP")
    if gap < GAP_MIN_RECHECK:
        return (False, "BELOW_MIN")
    if gap >= GAP_MAX_ORDER:
        return (False, "ABOVE_MAX")
    if gap >= GAP_HIGH_BAND and not _amount_qualifies_high_gap(expected_amount):
        return (False, "HIGH_GAP_AMOUNT_LOW")
    return (True, "OK")


def _evaluate_post_fill_guard(
    *,
    fill_price: float,
    submitted_order_price: float,
    prev_close: float,
    expected_amount: float | None,
) -> list[str]:
    """체결 후 방어 청산(SLIPPAGE_GUARD) 사유를 순서대로 반환한다. 빈 리스트=통과.

    기존 불변식(체결 갭 >=10% → FILL_GAP, 제출 지정가 초과 → LIMIT_PRICE)을
    그대로 유지하고, [8%,10%) 체결이 고갭 유동성 요건을 만족하지 못하면(대금
    부재·무효 포함) fail-safe로 HIGH_GAP_AMOUNT_LOW를 추가한다. 최초 매수·
    피라미딩·재시작 복구가 모두 이 단일 평가기를 공유한다.
    """
    # NaN/무한대는 아래 비교를 모두 False로 만들어 가드를 우회할 수 있다.
    # 지정가 0은 복구 시 "미확인" 표식으로 허용하되, 체결가·전일종가는
    # 반드시 양의 유한값이어야 한다.
    price_values_valid = (
        isinstance(fill_price, (int, float))
        and not isinstance(fill_price, bool)
        and math.isfinite(float(fill_price))
        and fill_price > 0
        and isinstance(prev_close, (int, float))
        and not isinstance(prev_close, bool)
        and math.isfinite(float(prev_close))
        and prev_close > 0
        and isinstance(submitted_order_price, (int, float))
        and not isinstance(submitted_order_price, bool)
        and math.isfinite(float(submitted_order_price))
        and submitted_order_price >= 0
    )
    if not price_values_valid:
        return ["INVALID_FILL_DATA"]

    reasons: list[str] = []
    fill_gap = (fill_price / prev_close) - 1 if prev_close > 0 else 0.0
    if prev_close > 0 and _fill_gap_reaches_max(fill_gap):
        reasons.append("FILL_GAP")
    if submitted_order_price > 0 and fill_price > submitted_order_price:
        reasons.append("LIMIT_PRICE")
    if (
        prev_close > 0
        and GAP_HIGH_BAND <= fill_gap < GAP_MAX_FILL
        and not _amount_qualifies_high_gap(expected_amount)
    ):
        reasons.append("HIGH_GAP_AMOUNT_LOW")
    return reasons


def _fill_gap_reaches_max(fill_gap: float) -> bool:
    """체결가 갭이 체결 상한 이상이면 SLIPPAGE_GUARD 청산 대상."""
    return fill_gap >= GAP_MAX_FILL


def _tick_size(price: float, api_tick_size: float = 0.0) -> int:
    """KRX 주권 호가단위. API aspr_unit이 유효하면 그 값을 우선한다."""
    if api_tick_size > 0:
        return max(1, int(api_tick_size))
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def _floor_to_tick(price: float, api_tick_size: float = 0.0) -> float:
    tick = _tick_size(price, api_tick_size)
    return float(int(price // tick) * tick)


def _strict_gap_cap(
    prev_close: float,
    api_tick_size: float = 0.0,
    *,
    expected_amount: float | None = None,
) -> float:
    """유효 갭 상한 미만인 마지막 매수가를 반환한다.

    예상 체결대금이 고갭 유동성 요건을 만족하면 상한은 10%(GAP_MAX_ORDER),
    아니면(대금 부재·무효 포함) fail-closed로 8%(GAP_HIGH_BAND)를 쓴다.
    """
    if prev_close <= 0:
        return 0.0

    ceiling = GAP_MAX_ORDER if _amount_qualifies_high_gap(expected_amount) else GAP_HIGH_BAND

    # 경계 계산과 비교를 모두 Decimal로 수행한다. float의 1.10이
    # 1.1000000000000001로 올라가면 경계 호가를 경계 미만으로 오인해 정적 VI
    # 발동가에 주문을 제출할 수 있다.
    decimal_prev_close = Decimal(str(prev_close))
    raw_cap = decimal_prev_close * (Decimal("1") + Decimal(str(ceiling)))

    # raw_cap이 호가단위 구간 경계(예: 50,000원)에 정확히 놓이면 그 가격보다
    # 아래 구간의 틱이 마지막 유효 호가를 결정한다.
    tick_probe = max(Decimal("0"), raw_cap - Decimal("0.000001"))
    tick = Decimal(_tick_size(float(tick_probe), api_tick_size))
    cap = (raw_cap / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    if cap >= raw_cap:
        cap -= tick
    return max(0.0, float(cap))


def _vi_price_reaches_gap_cap(
    vi_info: dict,
    prev_close: float,
    expected_amount: float | None = None,
) -> bool:
    """확인된 VI 발동가가 유효 갭 상한 이상인지 정확한 10진수로 판정한다.

    적격 유동성이면 상한은 10%, 아니면(대금 부재·무효 포함) 8%다.
    """
    vi_price = to_float(vi_info.get("vi_prc"))
    if vi_price <= 0 or prev_close <= 0:
        return False
    ceiling = GAP_MAX_ORDER if _amount_qualifies_high_gap(expected_amount) else GAP_HIGH_BAND
    boundary = Decimal(str(prev_close)) * (Decimal("1") + Decimal(str(ceiling)))
    return Decimal(str(vi_price)) >= boundary


def _entry_limit_price(
    ask_price: float,
    gap_cap: float,
    api_tick_size: float = 0.0,
) -> tuple[float, float]:
    """신선한 최종 매도호가 상한과 절대 갭 상한 중 더 낮은 지정가를 사용한다."""
    ask_cap = _floor_to_tick(
        ask_price * (1 + F3_ASK_SLIPPAGE_RATIO),
        api_tick_size,
    )
    return min(ask_cap, gap_cap), ask_cap


def _quote_age_ms(quote: EntryQuote) -> int:
    return max(0, round((time.monotonic() - quote.fetched_monotonic) * 1000))


def _quote_is_fresh(quote: EntryQuote) -> bool:
    return F3_FINAL_QUOTE_MAX_AGE_MS <= 0 or _quote_age_ms(quote) <= F3_FINAL_QUOTE_MAX_AGE_MS


# KIS TR ID (PAPER/REAL 분기) — 신TR 기준
_BUY_TR = {"REAL": kis_rest.REAL_CASH_BUY_TR, "PAPER": "VTTC0012U"}
_SELL_TR = {"REAL": "TTTC0011U", "PAPER": "VTTC0011U"}
_CANCEL_TR = {"REAL": "TTTC0013U", "PAPER": "VTTC0013U"}
_CCLD_TR = {"REAL": "TTTC0081R", "PAPER": "VTTC0081R"}
_BAL_TR = {"REAL": "TTTC8434R", "PAPER": "VTTC8434R"}
_BUY_PSBL_TR = {"REAL": "TTTC8908R", "PAPER": "VTTC8908R"}

_last_fill_poll_summary: dict = {}
_pending_buy_org_no: str = ""  # 매수 주문 후 저장, 취소 시 사용
_available_cash_snapshot: dict | None = None
_CANDIDATE_RETRY_REASONS = {
    "ORDER_REJECTED",
    "BUYABLE_QTY_ZERO",
    "QTY_ZERO",
    "VI_ACTIVE",
    "FINAL_QUOTE_UNAVAILABLE",
    "FINAL_QUOTE_STALE",
    "GAP_CHANGED",
    "HIGH_GAP_AMOUNT_LOW",
}
_EXPECTED_CANDIDATE_REJECTIONS = {
    "BUYABLE_QTY_ZERO",
    "QTY_ZERO",
    "VI_ACTIVE",
    "GAP_CHANGED",
    "HIGH_GAP_AMOUNT_LOW",
}
# KIS "모의투자 영업일이 아닙니다" — CTCA0903R이 모의투자 미지원이라 주문 거부가 유일한 휴장 신호
_MARKET_CLOSED_MSG_CD = "40100000"


@dataclass(frozen=True)
class EntryQuote:
    ask_price: float
    ask_qty: int
    antc_price: float
    fetched_monotonic: float
    rt_cd: str
    msg_cd: str
    msg1: str


@dataclass(frozen=True)
class FillSnapshot:
    status: Literal["UNFILLED", "PARTIAL", "FILLED"]
    order_qty: int
    fill_qty: int
    remaining_qty: int
    fill_price: float

    def as_fill(self) -> dict:
        return {
            "status": self.status,
            "order_qty": self.order_qty,
            "fill_qty": self.fill_qty,
            "remaining_qty": self.remaining_qty,
            "fill_price": self.fill_price,
        }


async def prepare_available_cash_snapshot() -> float | None:
    """Prefetch the PAPER entry budget; final buyable-quantity remains mandatory.

    This cache is intentionally independent from the experimental fast F1 path.
    It only removes the broad balance lookup from the entry-time critical path;
    the ticker-specific buyable-quantity check still runs immediately before an
    order and remains the authoritative cap.
    """
    global _available_cash_snapshot
    if (
        os.getenv("KIS_MODE", "PAPER").upper() != "PAPER"
        or os.getenv("DRY_RUN", "0") == "1"
        or os.getenv("BALANCE_SNAPSHOT_PREFETCH", "1") != "1"
    ):
        return None
    cash = await _fetch_available_cash()
    if cash is None:
        _available_cash_snapshot = None
        return None
    _available_cash_snapshot = {
        "date": _today(),
        "cash": float(cash),
        "created_monotonic": time.monotonic(),
    }
    log(
        "BALANCE_SNAPSHOT_READY",
        level="INFO",
        cash=float(cash),
        ttl_sec=BALANCE_SNAPSHOT_TTL_SEC,
    )
    return float(cash)


def _cached_available_cash() -> float | None:
    if (
        os.getenv("KIS_MODE", "PAPER").upper() != "PAPER"
        or os.getenv("DRY_RUN", "0") == "1"
        or os.getenv("BALANCE_SNAPSHOT_PREFETCH", "1") != "1"
    ):
        return None
    snapshot = _available_cash_snapshot
    if not snapshot or snapshot.get("date") != _today():
        return None
    age_sec = max(
        0.0,
        time.monotonic() - float(snapshot.get("created_monotonic") or 0.0),
    )
    if BALANCE_SNAPSHOT_TTL_SEC <= 0 or age_sec > BALANCE_SNAPSHOT_TTL_SEC:
        return None
    cash = float(snapshot.get("cash") or 0.0)
    log(
        "BALANCE_SNAPSHOT_HIT",
        level="INFO",
        cash=cash,
        age_ms=round(age_sec * 1000),
        ttl_sec=BALANCE_SNAPSHOT_TTL_SEC,
    )
    return cash


async def _available_cash_for_entry() -> float | None:
    cached = _cached_available_cash()
    if cached is not None:
        return cached
    log("BALANCE_SNAPSHOT_MISS", level="INFO", ttl_sec=BALANCE_SNAPSHOT_TTL_SEC)
    return await _fetch_available_cash()


def _more_complete_fill(first: dict | None, second: dict | None) -> dict | None:
    """취소 경쟁 중 누적체결이 증가할 수 있으므로 더 큰 누적값을 선택한다."""
    first_qty = int((first or {}).get("fill_qty") or 0)
    second_qty = int((second or {}).get("fill_qty") or 0)
    return second if second_qty >= first_qty else first


def _is_market_closed_rejection(resp: dict) -> bool:
    """휴장일 주문 거부 여부 — 다른 후보로 재시도해도 동일하므로 당일 전체 스킵 신호."""
    if str(resp.get("msg_cd") or "") == _MARKET_CLOSED_MSG_CD:
        return True
    return "영업일이 아닙" in str(resp.get("msg1") or "")


def _candidate_retry_log_level(reason: str) -> str:
    """Expected candidate protection is informational while fallback remains."""
    return "INFO" if reason in _EXPECTED_CANDIDATE_REJECTIONS else "WARN"


def _qty_clamp_reduction_pct(planned_qty: int, order_qty: int) -> float:
    if planned_qty <= 0:
        return 0.0
    return round(max(0.0, (planned_qty - order_qty) / planned_qty * 100), 2)


def _qty_clamp_log_level(planned_qty: int, order_qty: int) -> str:
    reduction_pct = _qty_clamp_reduction_pct(planned_qty, order_qty)
    return "WARN" if reduction_pct >= F3_QTY_CLAMP_WARN_PCT else "INFO"


async def _fetch_vi_active(ticker: str) -> dict | None:
    """현재 VI 발동 중이면 발동 정보, 아니면 None. 조회 실패는 예외로 전파."""
    resp = await vi_watch.fetch_vi_status(ticker)
    return vi_watch.parse_vi_payload(resp, ticker)


async def _vi_active_or_none(ticker: str, entry_attempt: int) -> dict | None:
    """VI 발동 정보 조회. 관측 실패로 기회를 버리지 않는다 (fail-open)."""
    if not F3_VI_CHECK_ENABLED:
        return None
    try:
        return await _fetch_vi_active(ticker)
    except Exception as e:
        log(
            "F3_VI_CHECK_ERROR",
            level="WARN",
            ticker=ticker,
            error=repr(e),
            entry_attempt=entry_attempt,
        )
        return None


async def _wait_for_vi_release(
    ticker: str,
    vi_info: dict,
    *,
    entry_attempt: int,
) -> bool:
    """이미 확인된 VI가 해제될 때까지 제한적으로 대기한다.

    발동을 한 번 확인한 뒤의 조회 오류는 해제로 오인하지 않는다. 대기 한도나
    진입 마감에 도달하면 False를 반환해 기존 후보 차단 경로로 수렴시킨다.
    """
    if F3_VI_RELEASE_WAIT_SEC <= 0:
        return False

    started = time.monotonic()
    checks = 0
    log(
        "VI_ENTRY_WAIT_STARTED",
        level="INFO",
        ticker=ticker,
        entry_attempt=entry_attempt,
        max_wait_sec=F3_VI_RELEASE_WAIT_SEC,
        poll_sec=F3_VI_RELEASE_POLL_SEC,
        vi_kind_code=vi_info.get("vi_kind_code"),
        cntg_vi_hour=vi_info.get("cntg_vi_hour"),
        vi_prc=vi_info.get("vi_prc"),
    )

    timeout_reason = "WAIT_LIMIT"
    while True:
        elapsed = time.monotonic() - started
        remaining = F3_VI_RELEASE_WAIT_SEC - elapsed
        if remaining <= 0:
            break
        if not _before_deadline(_entry_retry_deadline()):
            timeout_reason = "ENTRY_DEADLINE"
            break

        await asyncio.sleep(min(F3_VI_RELEASE_POLL_SEC, remaining))
        try:
            current = await _fetch_vi_active(ticker)
        except Exception as exc:
            log(
                "F3_VI_CHECK_ERROR",
                level="WARN",
                ticker=ticker,
                error=repr(exc),
                entry_attempt=entry_attempt,
                waiting_for_release=True,
            )
            continue

        checks += 1
        if current is None:
            log(
                "VI_ENTRY_RELEASED",
                level="INFO",
                ticker=ticker,
                entry_attempt=entry_attempt,
                wait_ms=round((time.monotonic() - started) * 1000),
                checks=checks,
            )
            return True

    log(
        "VI_ENTRY_WAIT_TIMEOUT",
        level="WARN",
        ticker=ticker,
        entry_attempt=entry_attempt,
        wait_ms=round((time.monotonic() - started) * 1000),
        checks=checks,
        reason=timeout_reason,
    )
    return False


def _picked_is_funded(picked: dict | None, ticker: str) -> bool:
    """A picked object carries pre-fetched funding only when every funding key is
    present. The multi-candidate ranking path supplies them; the single
    fresh-FAST branch supplies only quote identity/data, so it must fall through
    to the legacy deadline-before-balance funding path inside _run_single."""
    return bool(
        picked
        and picked.get("ticker") == ticker
        and picked.get("cash") is not None
        and picked.get("total_amount") is not None
        and picked.get("total_qty") is not None
    )


async def run() -> None:
    """진입 파이프라인 실행과 총예산 shadow 계측을 감싼다."""
    started_at = time.perf_counter()
    try:
        await _run_pipeline()
    finally:
        elapsed_sec = max(0.0, time.perf_counter() - started_at)
        if F3_ENTRY_TOTAL_BUDGET_SEC > 0 and elapsed_sec > F3_ENTRY_TOTAL_BUDGET_SEC:
            s = state.get()
            log(
                "ENTRY_BUDGET_EXCEEDED_SHADOW",
                level="INFO",
                ticker=s.target_ticker,
                elapsed_ms=round(elapsed_sec * 1000),
                budget_ms=round(F3_ENTRY_TOTAL_BUDGET_SEC * 1000),
                position_status=s.position_status,
                close_reason=s.close_reason,
                enforcement=False,
            )


async def _run_pipeline() -> None:
    s = state.get()
    candidates = _entry_candidate_tickers(s)
    if s.day_skip or not candidates or os.getenv("DRY_RUN", "0") == "1":
        await _run_single()
        return

    if len(candidates) == 1:
        # Reuse only a fresh PAPER FAST_MULTI snapshot for the single candidate so
        # a locked ticker is not re-blocked by a stale opening-transition single
        # quote. Do NOT route through _rank_final_entry_candidates: its eager
        # parallel balance query changes legacy failure precedence. When no fresh
        # valid FAST row exists, fall back to the exact legacy _run_single path,
        # preserving balance / insufficient-balance / close_reason / notifier /
        # record_skip semantics untouched.
        ticker = candidates[0]
        candidate_by_ticker = {
            c.get("ticker"): c
            for c in (s.target_candidates or [])
            if isinstance(c, dict) and c.get("ticker")
        }
        fast_rows = _fast_recheck_rows(candidates, candidate_by_ticker)
        picked = None
        if fast_rows:
            row = fast_rows[0]
            candidate = (
                row["candidate"] if isinstance(row["candidate"], dict) else {"ticker": ticker}
            )
            # Quote identity/data only — no cash query or qty computation here.
            # _run_single reuses this to skip _fetch_expected_price, then runs the
            # exact legacy existing-trade → gap → VI → deadline → balance path.
            picked = {
                "ticker": ticker,
                "candidate": candidate,
                "candidate_rank": row["rank"],
                "expected_price": float(row["expected_price"]),
                "prev_close": float(row["prev_close"]),
            }
        if picked is not None:
            s = state.get()
            s.target_ticker = picked["ticker"]
            s.target_name = picked["candidate"].get("name")
            s.target_candidates = [picked["candidate"]]
            await _run_single(picked=picked, allow_candidate_retry=False)
        else:
            await _run_single()
        return

    original_ticker = s.target_ticker
    original_name = s.target_name
    original_candidates = list(s.target_candidates or [])
    rejected_tickers: set[str] = set()
    rejection_reasons: set[str] = set()

    s = state.get()
    s.target_ticker = original_ticker
    s.target_name = original_name
    s.target_candidates = original_candidates
    ranked_candidates = await _rank_final_entry_candidates(s)
    if not ranked_candidates:
        return

    for candidate_index, picked in enumerate(ranked_candidates):
        if picked["ticker"] in rejected_tickers:
            continue
        if candidate_index > 0:
            refreshed = await _refresh_entry_candidate(picked)
            if refreshed is None:
                rejected_tickers.add(picked["ticker"])
                rejection_reasons.add("RECHECK_FAILED")
                continue
            picked = refreshed

        s = state.get()
        s.target_ticker = picked["ticker"]
        s.target_name = picked["candidate"].get("name")
        s.target_candidates = [picked["candidate"]]
        result = await _run_single(picked=picked, allow_candidate_retry=True)
        if result not in _CANDIDATE_RETRY_REASONS:
            return
        rejected_tickers.add(picked["ticker"])
        rejection_reasons.add(result)
        log(
            "ENTRY_CANDIDATE_RETRY",
            level=_candidate_retry_log_level(result),
            ticker=picked["ticker"],
            rejected_count=len(rejected_tickers),
            remaining_candidates=[t for t in candidates if t not in rejected_tickers],
            reason=result,
        )
        if not _before_deadline(_entry_retry_deadline()):
            s = state.get()
            s.day_skip = True
            s.close_reason = "ENTRY_FAIL"
            await _persist_terminal_or_log("ENTRY_FAIL")
            log(
                "ENTRY_CANDIDATE_RETRY_SKIPPED",
                level="WARN",
                ticker=picked["ticker"],
                rejected_count=len(rejected_tickers),
                reason="DEADLINE_REACHED",
            )
            await notifier.send(
                "ENTRY_FAIL",
                level="WARN",
                message=(
                    f"진입 재시도 마감시각 초과로 거래를 중단합니다. "
                    f"마지막 거절={picked['ticker']}"
                ),
                ticker=picked["ticker"],
            )
            await db.record_skip(
                _today(),
                "ENTRY_FAIL",
                f"reason=CANDIDATE_RETRY_DEADLINE,rejected={','.join(sorted(rejected_tickers))}",
            )
            return

    s = state.get()
    # 소진 사유가 전부 VI면 최종 사유도 VI_ACTIVE — 시작 복원(main)은
    # VI_ACTIVE만 복원하므로 ENTRY_FAIL로 남기면 재시작 catch-up이
    # VI 해제가에 추격 진입할 수 있다.
    all_vi = bool(rejection_reasons) and rejection_reasons == {"VI_ACTIVE"}
    final_reason = "VI_ACTIVE" if all_vi else "ENTRY_FAIL"
    s.day_skip = True
    s.close_reason = final_reason
    s.target_ticker = None
    s.target_name = None
    await _persist_terminal_or_log(final_reason)
    log(
        "ENTRY_CANDIDATE_EXHAUSTED",
        level="WARN",
        rejected_count=len(rejected_tickers),
        reason="NO_REMAINING_CANDIDATE",
        final_reason=final_reason,
    )
    await notifier.send(
        "ENTRY_FAIL",
        level="WARN",
        message=(
            "후보 전원이 VI 발동으로 차단되어 거래를 중단합니다."
            if all_vi
            else "진입 가능한 후보를 모두 소진해 거래를 중단합니다."
        ),
        ticker=None,
    )
    await db.record_skip(
        _today(),
        final_reason,
        f"reason=NO_REMAINING_CANDIDATE,rejected={','.join(sorted(rejected_tickers))}",
    )


async def _fail_pending_entry_recovery(
    pending: dict,
    *,
    ticker: str | None,
    order_id: str | None,
    reason: str,
    error: Exception | None = None,
) -> bool:
    """복구 실패 상태를 보존하고 신규 진입을 fail-closed로 차단한다."""
    state.get().day_skip = True
    log(
        "PENDING_ENTRY_RECOVERY_FAILED",
        level="CRIT",
        ticker=ticker,
        order_id=order_id,
        reason=reason,
        error=repr(error) if error is not None else None,
        pending_entry=pending,
    )
    await notifier.send(
        "ENTRY_CANCEL_UNCONFIRMED",
        level="CRIT",
        message=(
            f"재시작 매수 주문 복구 실패({reason}): "
            f"{ticker or '-'} 주문 {order_id or '-'}. 신규 진입을 차단했습니다."
        ),
        ticker=ticker,
    )
    try:
        await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
    except Exception as persist_error:
        log(
            "ENTRY_PENDING_PERSIST_ERROR",
            level="CRIT",
            ticker=ticker,
            order_id=order_id,
            error=repr(persist_error),
        )
    audit = _entry_audit_from_pending(pending, ticker=ticker, order_id=order_id)
    if audit is not None:
        await _upsert_entry_attempt_safe(audit, "UNCERTAIN")
    try:
        await db.record_skip(
            _today(),
            "ENTRY_FAIL",
            f"reason=PENDING_ENTRY_{reason},order_id={order_id or ''}",
        )
    except Exception as db_error:
        log(
            "PENDING_ENTRY_RECOVERY_FAILED",
            level="CRIT",
            ticker=ticker,
            order_id=order_id,
            reason="SKIP_RECORD_FAILED",
            error=repr(db_error),
        )
    return False


def _entry_audit_from_pending(
    pending: dict,
    *,
    ticker: str | None = None,
    order_id: str | None = None,
) -> dict | None:
    """Build a complete natural-key audit payload from durable pending state."""
    resolved_order_id = str(order_id or pending.get("order_id") or "")
    resolved_ticker = str(ticker or pending.get("ticker") or "")
    requested_qty = int(pending.get("requested_qty") or 0)
    if not resolved_order_id or not resolved_ticker or requested_qty <= 0:
        return None
    attempt = max(1, int(pending.get("attempt") or 1))
    order_phase = (
        "PYRAMID_BUY"
        if str(pending.get("phase") or "ENTRY") == "PYRAMID"
        else "FIRST_BUY"
    )
    return {
        "date": str(pending.get("date") or _today()),
        "kis_order_id": resolved_order_id,
        "ticker": resolved_ticker,
        "qty": requested_qty,
        "price": float(pending.get("limit_price") or 0),
        "trigger_price": float(pending.get("anchor_price") or 0),
        "attempt": attempt,
        "max_attempts": max(attempt, int(pending.get("max_attempts") or attempt)),
        "mode": str(pending.get("mode") or os.getenv("KIS_MODE", "PAPER")),
        "org_no": str(pending.get("org_no") or "") or None,
        "name": pending.get("name") or state.get().target_name,
        "order_phase": order_phase,
    }


async def _upsert_entry_attempt_safe(
    audit: dict | None,
    status: str,
    *,
    fill: dict | None = None,
    fill_latency_ms: int | None = None,
) -> None:
    """Bound audit I/O so it never stalls live-order reconciliation."""
    if audit is None:
        return
    try:
        await asyncio.wait_for(
            db.record_entry_order_attempt(
                **audit,
                status=status,
                fill_price=(float(fill.get("fill_price") or 0) if fill else None),
                fill_qty=(int(fill.get("fill_qty") or 0) if fill else None),
                fill_latency_ms=fill_latency_ms,
            ),
            timeout=F3_ENTRY_AUDIT_TIMEOUT_SEC,
        )
    except Exception as exc:
        log(
            "ENTRY_DB_DEGRADED",
            level="CRIT",
            ticker=audit.get("ticker"),
            order_id=audit.get("kis_order_id"),
            phase="ENTRY_ATTEMPT_AUDIT_UPSERT",
            attempt_status=status,
            error=repr(exc),
        )


def _start_entry_attempt_audit(audit: dict) -> None:
    """Schedule the initial PENDING audit without delaying fill polling."""
    stale_tasks = [task for task in tuple(_entry_audit_tasks) if task.done()]
    _entry_audit_tasks.difference_update(stale_tasks)
    task = asyncio.create_task(_upsert_entry_attempt_safe(audit, "PENDING"))
    _entry_audit_tasks.add(task)
    task.add_done_callback(_entry_audit_tasks.discard)


async def drain_entry_audit_tasks() -> None:
    """Give in-flight audit writes one bounded chance to finish before DB close."""
    loop = asyncio.get_running_loop()
    tasks = tuple(
        task
        for task in _entry_audit_tasks
        if task.get_loop() is loop and not task.done()
    )
    if not tasks:
        return
    _done, pending = await asyncio.wait(
        tasks,
        timeout=F3_ENTRY_AUDIT_TIMEOUT_SEC,
    )
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        log(
            "ENTRY_DB_DEGRADED",
            level="WARN",
            phase="ENTRY_ATTEMPT_AUDIT_SHUTDOWN",
            pending_count=len(pending),
        )
    _entry_audit_tasks.difference_update(tasks)


async def recover_pending_entry() -> bool:
    """재시작 시 살아 있을 수 있는 매수 주문을 취소·최종 대조한다."""
    s = state.get()
    pending = s.pending_entry or {}
    order_id = str(pending.get("order_id") or "")
    org_no = str(pending.get("org_no") or "")
    ticker = str(pending.get("ticker") or s.target_ticker or "")
    requested_qty = int(pending.get("requested_qty") or 0)
    phase = str(pending.get("phase") or "ENTRY")
    mode = os.getenv("KIS_MODE", "PAPER")
    if not order_id or not ticker or requested_qty <= 0:
        return await _fail_pending_entry_recovery(
            pending,
            ticker=ticker or None,
            order_id=order_id or None,
            reason="INVALID_PENDING_STATE",
        )
    audit = _entry_audit_from_pending(pending, ticker=ticker, order_id=order_id)
    if audit is None:
        log(
            "ENTRY_DB_DEGRADED",
            level="CRIT",
            ticker=ticker,
            order_id=order_id,
            phase="ENTRY_ATTEMPT_AUDIT_PAYLOAD",
            reason="INVALID_PENDING_AUDIT_FIELDS",
        )
    else:
        _start_entry_attempt_audit(audit)

    try:
        fill = await _fetch_order_fill_snapshot(
            order_id,
            ticker=ticker,
            expected_qty=requested_qty,
        )
        cancel_outcome = "FILLED"
        if not fill or int(fill.get("fill_qty") or 0) < requested_qty:
            cancel_outcome, fill = await _cancel_entry_order_confirmed(
                order_id,
                org_no,
                mode,
                ticker,
                int(pending.get("attempt") or 1),
                int(pending.get("attempt") or 1),
                expected_qty=requested_qty,
                known_fill=fill,
            )
    except Exception as exc:
        return await _fail_pending_entry_recovery(
            pending,
            ticker=ticker,
            order_id=order_id,
            reason="RECONCILIATION_ERROR",
            error=exc,
        )

    if not fill or int(fill.get("fill_qty") or 0) < requested_qty:
        if cancel_outcome != "CANCELLED" and not (
            fill and int(fill.get("fill_qty") or 0) >= requested_qty
        ):
            return await _fail_pending_entry_recovery(
                pending,
                ticker=ticker,
                order_id=order_id,
                reason="CANCEL_UNCONFIRMED",
            )

    fill_qty = int((fill or {}).get("fill_qty") or 0)
    if fill_qty <= 0:
        await state.clear_pending_entry()
        if phase == "PYRAMID" and s.position_status == "HOLDING":
            await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
            await _upsert_entry_attempt_safe(audit, "CANCELLED")
            log(
                "PENDING_ENTRY_RECOVERED",
                level="WARN",
                ticker=ticker,
                order_id=order_id,
                recovered_status="PYRAMID_CANCELLED_NO_FILL",
            )
            return True
        await state.reset_to_idle("ENTRY_FAIL")
        state.get().day_skip = True
        await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
        await _upsert_entry_attempt_safe(audit, "CANCELLED")
        await db.record_skip(
            _today(),
            "ENTRY_FAIL",
            f"reason=PENDING_ENTRY_RECOVERED_NO_FILL,order_id={order_id}",
        )
        log(
            "PENDING_ENTRY_RECOVERED",
            level="WARN",
            ticker=ticker,
            order_id=order_id,
            recovered_status="CANCELLED_NO_FILL",
        )
        return True

    fill_price = float(fill.get("fill_price") or 0)
    if fill_price <= 0:
        return await _fail_pending_entry_recovery(
            pending,
            ticker=ticker,
            order_id=order_id,
            reason="INVALID_FILL_PRICE",
        )
    limit_price = float(pending.get("limit_price") or 0)
    anchor_price = float(pending.get("anchor_price") or 0)
    prev_close = float(pending.get("prev_close") or 0)
    partial = fill_qty < requested_qty

    existing_order = await db.get_order_by_kis_id(
        order_id,
        date=_today(),
        ticker=ticker,
    )
    if phase == "PYRAMID" and s.position_status == "HOLDING" and s.trade_id:
        s.entry_qty = int(s.entry_qty or 0) + fill_qty
        s.remaining_qty = int(s.remaining_qty or 0) + fill_qty
        order_db_id = (
            int(existing_order["id"])
            if existing_order
            else (
                await db.record_order(
                    s.trade_id,
                    order_id,
                    "BUY",
                    requested_qty,
                    limit_price,
                    "PYRAMID_BUY",
                    ticker,
                    s.target_name,
                    trigger_price=anchor_price,
                )
            )
        )
        await db.update_order_fill(
            order_db_id,
            fill_price,
            fill_qty,
            None,
            status="PARTIAL_FILL" if partial else "FILLED",
        )
        await db.mark_pyramided(s.trade_id)
    else:
        existing_trade = await db.get_trade_by_date(_today())
        if existing_trade and existing_trade.get("ticker") != ticker:
            return await _fail_pending_entry_recovery(
                pending,
                ticker=ticker,
                order_id=order_id,
                reason="TRADE_TICKER_CONFLICT",
            )
        await state.set_holding(fill_price, fill_qty, order_id)
        trade_id = await db.open_trade(
            _today(),
            ticker,
            fill_price,
            fill_qty,
            name=state.get().target_name,
        )
        state.get().trade_id = trade_id
        _begin_tick_capture(ticker, trade_id)
        order_db_id = (
            int(existing_order["id"])
            if existing_order
            else (
                await db.record_order(
                    trade_id,
                    order_id,
                    "BUY",
                    requested_qty,
                    limit_price,
                    "FIRST_BUY",
                    ticker,
                    state.get().target_name,
                    trigger_price=anchor_price,
                )
            )
        )
        await db.update_order_fill(
            order_db_id,
            fill_price,
            fill_qty,
            None,
            status="PARTIAL_FILL" if partial else "FILLED",
        )

    await state.clear_pending_entry()
    await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
    await _upsert_entry_attempt_safe(
        audit,
        "PARTIAL_FILL" if partial else "FILLED",
        fill=fill,
    )
    log(
        "PENDING_ENTRY_RECOVERED",
        level="CRIT",
        ticker=ticker,
        order_id=order_id,
        recovered_status="PARTIAL_HOLDING" if partial else "HOLDING",
        requested_qty=requested_qty,
        fill_qty=fill_qty,
        fill_price=fill_price,
        cancel_outcome=cancel_outcome,
    )
    await notifier.send(
        "PROCESS_RESTART_DETECTED",
        level="CRIT",
        message=f"재시작 매수 주문 대조 완료: {ticker} {fill_qty}주 @ {fill_price:g}",
        ticker=ticker,
    )

    # 정상 지정가 체결에서는 FILL_GAP/LIMIT_PRICE가 불변식상 발생하지 않는다.
    # 과거 주문을 복구했거나 거래소/API 대조 이상일 때 마지막 방어선으로 쓴다.
    # 고갭(>=8%) 유동성 검증은 저장된 대금이 있으면 그대로, 없으면
    # state.target_candidates에서 해소한다. 현재 스키마의 누락·무효는
    # fail-safe로 방어 청산하되, expected_amount 키 자체가 없던 구버전
    # pending은 당시 합법 체결과 구분할 수 없으므로 별도 경고 후 보유한다.
    recovery_amount, amount_source = _resolve_recovery_expected_amount(pending, s, ticker)
    guard_reasons = _evaluate_post_fill_guard(
        fill_price=fill_price,
        submitted_order_price=limit_price,
        prev_close=prev_close,
        expected_amount=recovery_amount,
    )
    legacy_amount_unverified = (
        amount_source == "legacy_unavailable"
        and "HIGH_GAP_AMOUNT_LOW" in guard_reasons
    )
    if legacy_amount_unverified:
        guard_reasons = [reason for reason in guard_reasons if reason != "HIGH_GAP_AMOUNT_LOW"]
        log(
            "PENDING_ENTRY_LEGACY_AMOUNT_UNVERIFIED",
            level="WARN",
            ticker=ticker,
            order_id=order_id,
            fill_price=fill_price,
            prev_close=prev_close,
            amount_source=amount_source,
            recovery=True,
        )
    if guard_reasons:
        state.get().day_skip = True
        log(
            "SLIPPAGE_GUARD",
            level="WARN",
            ticker=ticker,
            order_id=order_id,
            fill_price=fill_price,
            prev_close=prev_close,
            submitted_limit_price=limit_price,
            fill_gap_pct=round((fill_price / prev_close - 1) * 100, 3) if prev_close > 0 else None,
            guard_reasons=guard_reasons,
            expected_amount=recovery_amount,
            amount_source=amount_source,
            threshold=HIGH_GAP_MIN_EXPECTED_AMOUNT,
            recovery=True,
        )
        from src.modules import f4_tracking

        await f4_tracking.close_now(fill_price, "SLIPPAGE_GUARD")
    return True


async def _run_single(
    picked: dict | None = None, allow_candidate_retry: bool = False
) -> str | None:
    """
    갭 재검증 후 설정된 시각에 배정 수량을 가격 상한 지정가로 매수하고,
    체결 확인 / 잔량 취소 / 슬리피지 가드 / 선택적 피라미딩을 수행한다.
    운영 경로는 지정가 매수만 허용하며 신규 주문의 진입 마감을 우회하지 않는다.
    """
    s = state.get()
    if s.day_skip or not s.target_ticker:
        reason = "DAY_SKIP" if s.day_skip else "NO_TARGET"
        log("F3_SKIPPED", level="WARN", reason=reason)
        _log_entry_blocked(s.target_ticker, reason)
        return
    candidate_tickers = _entry_candidate_tickers(s)
    ticker = candidate_tickers[0]
    mode = os.getenv("KIS_MODE", "PAPER")

    if os.getenv("DRY_RUN", "0") == "1":
        await _run_dry_entry(ticker)
        return

    # FORCE_CATCHUP이나 즉시 체이닝 여부와 무관하게 실주문 진입 마감은 절대적이다.
    if not _before_deadline(_entry_retry_deadline()):
        await _block_entry_deadline_passed(ticker, "BEFORE_RECHECK")
        return

    existing_trade = await _existing_trade_for_today()
    if existing_trade:
        await _block_existing_trade(ticker, existing_trade)
        return

    # ── 진입 직전 갭 재검증 ─────────────────────────────────────────
    if picked and picked.get("ticker") == ticker:
        expected_price = float(picked["expected_price"])
        prev_close = float(picked.get("prev_close") or 0)
        entry_candidate = picked.get("candidate")
    else:
        entry_candidate = _candidate_for_ticker(s, ticker)
        fallback_prev_close = _candidate_prev_close(entry_candidate)
        expected_price, prev_close = await _fetch_expected_price(
            ticker,
            fallback_prev_close=fallback_prev_close,
        )
        if prev_close <= 0:
            prev_close = fallback_prev_close
    # 고갭(>=8%) 유동성 판정에 쓰는 예상 체결대금. 부재 시 None → fail-closed.
    entry_expected_amount = _candidate_expected_amount(
        entry_candidate if isinstance(entry_candidate, dict) else None
    )
    if not expected_price or prev_close <= 0:
        s.day_skip = True
        s.close_reason = "GAP_RECHECK_UNAVAILABLE"
        _log_entry_blocked(
            ticker,
            "GAP_RECHECK_UNAVAILABLE",
            expected_price=expected_price,
            prev_close=prev_close,
        )
        log(
            "GAP_RECHECK_UNAVAILABLE",
            level="WARN",
            ticker=ticker,
            expected_price=expected_price,
            prev_close=prev_close,
            reason="MISSING_PREV_CLOSE" if prev_close <= 0 else "MISSING_EXPECTED_PRICE",
        )
        await notifier.send(
            "ENTRY_FAIL",
            level="WARN",
            message="진입 직전 갭 재검증 데이터가 없어 거래를 스킵합니다.",
            ticker=ticker,
        )
        await db.record_skip(
            _today(),
            "ENTRY_FAIL",
            f"reason=GAP_RECHECK_UNAVAILABLE,expected_price={expected_price},prev_close={prev_close}",
        )
        return

    gap = (expected_price / prev_close) - 1
    log(
        "F3_RECHECK",
        level="INFO",
        ticker=ticker,
        expected_price=expected_price,
        prev_close=prev_close,
        gap_pct=round(gap * 100, 2),
        gap_min_pct=round(GAP_MIN_RECHECK * 100, 2),
        gap_max_pct=round(GAP_MAX_ORDER * 100, 2),
    )
    gap_allowed, gap_reason = _evaluate_order_gap(gap, entry_expected_amount)
    if not gap_allowed:
        is_high_gap_low = gap_reason == "HIGH_GAP_AMOUNT_LOW"
        block_reason = "HIGH_GAP_AMOUNT_LOW" if is_high_gap_low else "GAP_CHANGED"
        # 대체 후보가 있으면 day_skip 없이 후보 사유를 반환해 다음 후보로 넘어간다.
        if allow_candidate_retry:
            _log_entry_blocked(
                ticker,
                block_reason,
                level="INFO",
                gap_at_entry=round(gap * 100, 2),
                gap_min_pct=round(GAP_MIN_RECHECK * 100, 2),
                gap_max_pct=round(GAP_MAX_ORDER * 100, 2),
                high_gap_band_pct=round(GAP_HIGH_BAND * 100, 2),
                expected_amount=entry_expected_amount,
                threshold=HIGH_GAP_MIN_EXPECTED_AMOUNT,
                gap_reason=gap_reason,
                candidate_retry=True,
            )
            return block_reason
        s.day_skip = True
        s.close_reason = "GAP_CHANGED"
        log(
            "GAP_CHANGED" if not is_high_gap_low else "HIGH_GAP_AMOUNT_LOW",
            level="WARN",
            ticker=ticker,
            gap_at_lockup=None,
            gap_at_entry=round(gap * 100, 2),
            reason=gap_reason,
            expected_amount=entry_expected_amount,
            threshold=HIGH_GAP_MIN_EXPECTED_AMOUNT,
        )
        _log_entry_blocked(
            ticker,
            block_reason,
            gap_at_entry=round(gap * 100, 2),
            gap_min_pct=round(GAP_MIN_RECHECK * 100, 2),
            gap_max_pct=round(GAP_MAX_ORDER * 100, 2),
            high_gap_band_pct=round(GAP_HIGH_BAND * 100, 2),
            expected_amount=entry_expected_amount,
            threshold=HIGH_GAP_MIN_EXPECTED_AMOUNT,
            gap_reason=gap_reason,
        )
        await notifier.send(
            "GAP_CHANGED",
            level="WARN",
            message=(
                f"진입 직전 갭 변동({gap*100:.1f}%, 사유={gap_reason}). 거래 스킵."
            ),
            ticker=ticker,
        )
        await db.record_skip(
            _today(),
            "GAP_CHANGED",
            f"gap={gap*100:.2f}%,reason={gap_reason},expected_amount={entry_expected_amount}",
        )
        return

    # ── 잔고 조회 및 수량 산정 ────────────────────────────────────────
    prefetched_buyable: dict | None = None
    if _picked_is_funded(picked, ticker):
        cash = float(picked["cash"])
        total_amount = int(picked["total_amount"])
        total_qty = int(picked["total_qty"])
    else:
        # 잔고 재시도(최대 수 초)로 마감을 넘길 수 있으므로 조회 전에 먼저 확인한다
        if not _before_deadline(_entry_retry_deadline()):
            await _block_entry_deadline_passed(ticker, "BEFORE_BALANCE_QUERY")
            return
        # 종목별 주문가능 조회를 산정 앞으로 당긴다. 주문 루프가 1차 시도에서
        # 이 결과를 재사용하므로 09:01 창에서 API 왕복은 늘지 않는다.
        prefetched_buyable = await _fetch_buyable_qty(ticker, mode)
        cash, cash_basis = _entry_cash_basis(prefetched_buyable)
        if cash is None:
            cash = await _available_cash_for_entry()
            if cash is None:
                s.day_skip = True
                s.close_reason = "BALANCE_QUERY_FAILED"
                await _alert_balance_query_failed(ticker, candidate_tickers)
                return
        log(
            "ENTRY_CASH_BASIS",
            level="INFO",
            ticker=ticker,
            cash=cash,
            cash_basis=cash_basis,
            alloc_ratio=ALLOC_RATIO,
            ord_psbl_cash=float((prefetched_buyable or {}).get("ord_psbl_cash") or 0.0),
        )
        total_amount = int(cash * ALLOC_RATIO)
        total_qty = int(total_amount / expected_price) if expected_price else 0
    if total_qty == 0:
        candidate_block_level = "INFO" if allow_candidate_retry else "WARN"
        _log_entry_blocked(
            ticker,
            "QTY_ZERO",
            level=candidate_block_level,
            cash=cash,
            alloc_ratio=ALLOC_RATIO,
            order_price=expected_price,
            total_amount=total_amount,
            candidate_retry=allow_candidate_retry,
        )
        log(
            "INSUFFICIENT_BALANCE",
            level=candidate_block_level,
            ticker=ticker,
            cash=cash,
            alloc_ratio=ALLOC_RATIO,
            order_price=expected_price,
            total_amount=total_amount,
            filter_count=0,
            reason="QTY_ZERO",
            candidate_retry=allow_candidate_retry,
        )
        if allow_candidate_retry:
            return "QTY_ZERO"
        s.day_skip = True
        s.close_reason = "INSUFFICIENT_BALANCE"
        # 다른 두 INSUFFICIENT_BALANCE 종료와 같이 디스크를 최종 값으로 맞춘다.
        # 여기만 빠져 있으면 재시작 복구가 메모리와 다른 사유를 읽는다.
        await _persist_terminal_or_log("INSUFFICIENT_BALANCE")
        await db.record_skip(
            _today(),
            "ENTRY_FAIL",
            (
                "reason=QTY_ZERO,"
                f"cash={cash},alloc_ratio={ALLOC_RATIO},order_price={expected_price}"
            ),
        )
        return

    first_qty = max(1, int(total_qty * FIRST_RATIO))
    second_qty = total_qty - first_qty

    # ── 진입 마감 재확인 — 잔고 재시도·느린 조회로 지연됐으면 최초 주문도 내지 않는다.
    # 주문 루프의 마감 검사는 attempt>1에만 적용되므로 여기서 1차 주문을 막는다.
    if not _before_deadline(_entry_retry_deadline()):
        await _block_entry_deadline_passed(ticker, "BEFORE_FIRST_ORDER")
        return

    # ── 1차 배정 수량 100% 상한 지정가 매수 ──────────────────────────
    if not await state.set_entering():
        _log_entry_blocked(
            ticker,
            "STATE_NOT_IDLE",
            position_status=state.get().position_status,
        )
        return

    global _pending_buy_org_no
    fill = None
    fill_latency_ms: int | None = None
    order_started_at: float | None = None
    order_id = "UNKNOWN"
    max_attempts = F3_ENTRY_MAX_ATTEMPTS
    last_run_attempt = 0
    last_entry_fail_reason = "UNFILLED"
    submitted_order_price = 0.0
    gap_cap = _strict_gap_cap(prev_close, expected_amount=entry_expected_amount)
    ask_cap = 0.0
    fill_was_partial = False
    for attempt in range(1, max_attempts + 1):
        entry_audit: dict | None = None
        if attempt > 1:
            if not _before_deadline(_entry_retry_deadline()):
                log(
                    "ENTRY_RETRY_SKIPPED",
                    level="WARN",
                    ticker=ticker,
                    order_price=expected_price,
                    order_qty=first_qty,
                    entry_attempt=attempt,
                    max_attempts=max_attempts,
                    reason="DEADLINE_REACHED",
                )
                break
            await asyncio.sleep(F3_ENTRY_RETRY_DELAY_SEC)
            log(
                "ENTRY_RETRY_START",
                level="WARN",
                ticker=ticker,
                order_price=expected_price,
                order_qty=first_qty,
                entry_attempt=attempt,
                max_attempts=max_attempts,
            )

        await _pre_order_quiet_wait(ticker, attempt, max_attempts, expected_price, first_qty)
        last_run_attempt = attempt
        if attempt > 1:
            early_reject_reason = await _early_retry_gap_guard(
                ticker,
                expected_price=expected_price,
                prev_close=prev_close,
                allow_candidate_retry=allow_candidate_retry,
                entry_attempt=attempt,
                expected_amount=entry_expected_amount,
            )
            if early_reject_reason is not None:
                return early_reject_reason
        if attempt == 1 and prefetched_buyable is not None:
            buyable = prefetched_buyable
        else:
            buyable = await _fetch_buyable_qty(ticker, mode)
        if buyable.get("query_failed"):
            _log_entry_blocked(
                ticker,
                "BUYABLE_QTY_QUERY_FAILED",
                order_price=expected_price,
                planned_qty=first_qty,
                entry_attempt=attempt,
                max_attempts=max_attempts,
                rt_cd=buyable.get("rt_cd"),
                msg_cd=buyable.get("msg_cd"),
                msg1=buyable.get("msg1"),
            )
            log(
                "BUYABLE_QTY_QUERY_FAILED",
                level="WARN",
                ticker=ticker,
                order_price=expected_price,
                planned_qty=first_qty,
                entry_attempt=attempt,
                max_attempts=max_attempts,
                rt_cd=buyable.get("rt_cd"),
                msg_cd=buyable.get("msg_cd"),
                msg1=buyable.get("msg1"),
            )
            last_entry_fail_reason = "BUYABLE_QTY_QUERY_FAILED"
            if attempt < max_attempts:
                continue
            break
        last_entry_fail_reason = "UNFILLED"

        buyable_qty = int(buyable.get("nrcvb_buy_qty", 0))
        order_qty = min(first_qty, buyable_qty)
        if 0 < order_qty < first_qty:
            reduction_pct = _qty_clamp_reduction_pct(first_qty, order_qty)
            log(
                "ENTRY_QTY_CLAMPED",
                level=_qty_clamp_log_level(first_qty, order_qty),
                ticker=ticker,
                planned_qty=first_qty,
                buyable_qty=buyable_qty,
                order_qty=order_qty,
                order_price=expected_price,
                entry_attempt=attempt,
                max_attempts=max_attempts,
                nrcvb_buy_amt=buyable.get("nrcvb_buy_amt"),
                max_buy_qty=buyable.get("max_buy_qty"),
                ord_psbl_cash=buyable.get("ord_psbl_cash"),
                reduction_pct=reduction_pct,
                warn_threshold_pct=F3_QTY_CLAMP_WARN_PCT,
            )
        if order_qty <= 0:
            await _reset_to_idle_persisted("ENTRY_FAIL")
            _log_entry_blocked(
                ticker,
                "BUYABLE_QTY_ZERO",
                level=("INFO" if allow_candidate_retry else "WARN"),
                order_price=expected_price,
                planned_qty=first_qty,
                buyable_qty=buyable_qty,
                entry_attempt=attempt,
                max_attempts=max_attempts,
                nrcvb_buy_amt=buyable.get("nrcvb_buy_amt"),
                ord_psbl_cash=buyable.get("ord_psbl_cash"),
                candidate_retry=allow_candidate_retry,
            )
            log(
                "INSUFFICIENT_BALANCE",
                level=("INFO" if allow_candidate_retry else "WARN"),
                ticker=ticker,
                cash=cash,
                alloc_ratio=ALLOC_RATIO,
                order_price=expected_price,
                planned_qty=first_qty,
                buyable_qty=buyable_qty,
                nrcvb_buy_amt=buyable.get("nrcvb_buy_amt"),
                ord_psbl_cash=buyable.get("ord_psbl_cash"),
                reason="BUYABLE_QTY_ZERO",
                candidate_retry=allow_candidate_retry,
            )
            if allow_candidate_retry:
                return "BUYABLE_QTY_ZERO"
            state.get().day_skip = True
            state.get().close_reason = "INSUFFICIENT_BALANCE"
            # 종료 close_reason을 확정한 뒤 디스크를 최종 값으로 재동기화한다.
            await _persist_terminal_or_log("INSUFFICIENT_BALANCE")
            await notifier.send(
                "ENTRY_FAIL",
                level="WARN",
                message=f"종목별 매수가능수량이 0입니다. {ticker}",
                ticker=ticker,
            )
            await db.record_skip(
                _today(),
                "ENTRY_FAIL",
                f"reason=BUYABLE_QTY_ZERO,planned_qty={first_qty},buyable_qty={buyable_qty}",
            )
            return
        if order_qty < first_qty:
            first_qty = order_qty
            second_qty = 0

        # ── 주문 직전 VI 확인 ──────────────────────────────────────────
        # quiet wait와 수량 조회가 끝난 뒤 모든 시도에서 확인한다. 대체 후보가
        # 있거나 발동가가 갭 상한 이상이면 기다리지 않고 즉시 후보를 교체한다.
        vi_info = await _vi_active_or_none(ticker, entry_attempt=attempt)
        vi_wait_skipped_reason = None
        if vi_info:
            if allow_candidate_retry:
                vi_wait_skipped_reason = "CANDIDATE_AVAILABLE"
            elif _vi_price_reaches_gap_cap(vi_info, prev_close, entry_expected_amount):
                vi_wait_skipped_reason = "VI_PRICE_AT_OR_ABOVE_GAP_CAP"
            elif await _wait_for_vi_release(
                ticker,
                vi_info,
                entry_attempt=attempt,
            ):
                vi_info = None
        if vi_info:
            # 아직 살아 있는 주문이 없는 1차 시도이거나, 직전 주문의 취소가
            # 확인된 재시도이므로 IDLE 전환·후보 이동이 안전하다.
            await _reset_to_idle_persisted("VI_ACTIVE")
            candidate_block_level = "INFO" if allow_candidate_retry else "WARN"
            log(
                "VI_ENTRY_BLOCKED",
                level=candidate_block_level,
                ticker=ticker,
                entry_attempt=attempt,
                candidate_retry=allow_candidate_retry,
                wait_skipped_reason=vi_wait_skipped_reason,
                **vi_info,
            )
            _log_entry_blocked(
                ticker,
                "VI_ACTIVE",
                level=candidate_block_level,
                vi_kind_code=vi_info.get("vi_kind_code"),
                cntg_vi_hour=vi_info.get("cntg_vi_hour"),
                vi_prc=vi_info.get("vi_prc"),
                entry_attempt=attempt,
                candidate_retry=allow_candidate_retry,
                wait_skipped_reason=vi_wait_skipped_reason,
            )
            if allow_candidate_retry:
                return "VI_ACTIVE"
            state.get().day_skip = True
            await notifier.send(
                "VI_ENTRY_BLOCKED",
                level="WARN",
                message=(
                    f"주문 직전 VI 발동 중(발동시각 {vi_info.get('cntg_vi_hour')}). " "거래 스킵."
                ),
                ticker=ticker,
            )
            await db.record_skip(
                _today(),
                "VI_ACTIVE",
                (
                    f"cntg_vi_hour={vi_info.get('cntg_vi_hour')},"
                    f"vi_kind={vi_info.get('vi_kind_code')},entry_attempt={attempt}"
                ),
            )
            return

        # ── 마감 최종 확인 — quiet wait·수량 조회(재시도면 sleep·취소 대기까지)로
        # 앞선 검사 이후에도 시간이 흘렀다. 아직 살아 있는 주문이 없으므로
        # ENTERING을 IDLE로 되돌리고 차단하는 것이 안전하다.
        if not _before_deadline(_entry_retry_deadline()):
            await _reset_to_idle_persisted("ENTRY_FAIL")
            await _block_entry_deadline_passed(ticker, "AT_ORDER")
            return

        entry_quote = await _fetch_final_entry_quote(ticker)
        if entry_quote is None:
            return await _reject_final_entry_price(
                ticker,
                "FINAL_QUOTE_UNAVAILABLE",
                allow_candidate_retry=allow_candidate_retry,
                anchor_price=expected_price,
                prev_close=prev_close,
            )

        quote_age_ms = _quote_age_ms(entry_quote)
        if not _quote_is_fresh(entry_quote):
            return await _reject_final_entry_price(
                ticker,
                "FINAL_QUOTE_STALE",
                allow_candidate_retry=allow_candidate_retry,
                anchor_price=expected_price,
                prev_close=prev_close,
                ask_price=entry_quote.ask_price,
                quote_age_ms=quote_age_ms,
            )

        fresh_gap = (entry_quote.ask_price / prev_close) - 1
        fresh_allowed, fresh_reason = _evaluate_order_gap(fresh_gap, entry_expected_amount)
        if not fresh_allowed:
            reject_reason = (
                "HIGH_GAP_AMOUNT_LOW" if fresh_reason == "HIGH_GAP_AMOUNT_LOW" else "GAP_CHANGED"
            )
            return await _reject_final_entry_price(
                ticker,
                reject_reason,
                allow_candidate_retry=allow_candidate_retry,
                anchor_price=expected_price,
                prev_close=prev_close,
                ask_price=entry_quote.ask_price,
                limit_price=gap_cap,
                quote_age_ms=quote_age_ms,
                fresh_gap=fresh_gap,
                expected_amount=entry_expected_amount,
            )
        submitted_order_price, ask_cap = _entry_limit_price(
            entry_quote.ask_price,
            gap_cap,
        )
        quote_move_pct = round(
            (entry_quote.ask_price / expected_price - 1) * 100,
            3,
        )
        if quote_move_pct > F3_QUOTE_MOVE_WARN_PCT:
            log(
                "ENTRY_QUOTE_MOVE_HIGH",
                level="WARN",
                ticker=ticker,
                anchor_price=expected_price,
                ask_price=entry_quote.ask_price,
                quote_move_pct=quote_move_pct,
                warn_threshold_pct=F3_QUOTE_MOVE_WARN_PCT,
                limit_price=submitted_order_price,
                ask_cap_price=ask_cap,
                gap_cap_price=gap_cap,
                entry_attempt=attempt,
            )

        # 시장가 기준 매수가능수량 조회와 별개로, 실제 제출 지정가 기준으로
        # 배정금액 및 주문가능현금을 넘지 않도록 마지막 수량을 제한한다.
        limit_budget = float(total_amount)
        ord_psbl_cash = float(buyable.get("ord_psbl_cash") or 0)
        if ord_psbl_cash > 0:
            limit_budget = min(limit_budget, ord_psbl_cash)
        limit_buyable_qty = int(limit_budget / submitted_order_price)
        if limit_buyable_qty < first_qty:
            planned_qty = first_qty
            order_qty = max(0, limit_buyable_qty)
            log(
                "ENTRY_QTY_SIZED_AT_LIMIT",
                level=_qty_clamp_log_level(planned_qty, order_qty),
                ticker=ticker,
                planned_qty=planned_qty,
                buyable_qty=buyable_qty,
                limit_buyable_qty=limit_buyable_qty,
                order_qty=order_qty,
                order_price=submitted_order_price,
                entry_attempt=attempt,
                max_attempts=max_attempts,
                ord_psbl_cash=ord_psbl_cash,
                allocated_amount=total_amount,
                reduction_pct=_qty_clamp_reduction_pct(planned_qty, order_qty),
                warn_threshold_pct=F3_QTY_CLAMP_WARN_PCT,
                reason="LIMIT_PRICE_BUDGET",
            )
            if order_qty <= 0:
                await _reset_to_idle_persisted("ENTRY_FAIL")
                _log_entry_blocked(
                    ticker,
                    "QTY_ZERO",
                    level=("INFO" if allow_candidate_retry else "WARN"),
                    order_price=submitted_order_price,
                    allocated_amount=total_amount,
                    ord_psbl_cash=ord_psbl_cash,
                    candidate_retry=allow_candidate_retry,
                )
                log(
                    "INSUFFICIENT_BALANCE",
                    level=("INFO" if allow_candidate_retry else "WARN"),
                    ticker=ticker,
                    cash=cash,
                    alloc_ratio=ALLOC_RATIO,
                    order_price=submitted_order_price,
                    planned_qty=planned_qty,
                    buyable_qty=buyable_qty,
                    limit_buyable_qty=limit_buyable_qty,
                    allocated_amount=total_amount,
                    ord_psbl_cash=ord_psbl_cash,
                    reason="QTY_ZERO_AT_LIMIT",
                    candidate_retry=allow_candidate_retry,
                )
                if allow_candidate_retry:
                    return "QTY_ZERO"
                state.get().day_skip = True
                state.get().close_reason = "INSUFFICIENT_BALANCE"
                # 종료 close_reason 확정 후 디스크를 최종 값으로 재동기화한다.
                await _persist_terminal_or_log("INSUFFICIENT_BALANCE")
                await notifier.send(
                    "ENTRY_FAIL",
                    level="WARN",
                    message=(
                        f"지정가 {submitted_order_price:,.0f}원 기준 주문가능수량이 "
                        f"0입니다. {ticker}"
                    ),
                    ticker=ticker,
                )
                await db.record_skip(
                    _today(),
                    "ENTRY_FAIL",
                    (
                        "reason=QTY_ZERO_AT_LIMIT,"
                        f"limit={submitted_order_price},allocated={total_amount},"
                        f"ord_psbl_cash={ord_psbl_cash}"
                    ),
                )
                return
            first_qty = order_qty
            second_qty = 0
        log(
            "ENTRY_PRICE_APPROVED",
            level="INFO",
            ticker=ticker,
            anchor_price=expected_price,
            ask_price=entry_quote.ask_price,
            ask_qty=entry_quote.ask_qty,
            limit_price=submitted_order_price,
            ask_cap_price=ask_cap,
            gap_cap_price=gap_cap,
            quote_move_pct=quote_move_pct,
            quote_move_warn_pct=F3_QUOTE_MOVE_WARN_PCT,
            quote_age_ms=quote_age_ms,
            entry_attempt=attempt,
        )

        def _send_allowed() -> bool:
            deadline_ok = _before_deadline(_entry_retry_deadline())
            return deadline_ok and _quote_is_fresh(entry_quote)

        order_started_at = time.perf_counter()
        order_resp = await _send_buy(
            ticker,
            first_qty,
            mode,
            limit_price=submitted_order_price,
            send_guard=_send_allowed,
        )
        if order_resp.get("msg_cd") == kis_rest.SEND_GUARD_BLOCKED_MSG_CD:
            if entry_quote is not None and not _quote_is_fresh(entry_quote):
                return await _reject_final_entry_price(
                    ticker,
                    "FINAL_QUOTE_STALE",
                    allow_candidate_retry=allow_candidate_retry,
                    anchor_price=expected_price,
                    prev_close=prev_close,
                    ask_price=entry_quote.ask_price,
                    limit_price=submitted_order_price,
                    quote_age_ms=_quote_age_ms(entry_quote),
                )
            await _reset_to_idle_persisted("ENTRY_FAIL")
            await _block_entry_deadline_passed(ticker, "AT_HTTP_SEND")
            return
        order_id = str(order_resp.get("output", {}).get("ODNO") or "")
        _pending_buy_org_no = order_resp.get("output", {}).get("KRX_FWDG_ORD_ORGNO", "")
        log(
            "ENTRY_ORDER_SENT",
            level="INFO",
            ticker=ticker,
            order_id=order_id,
            org_no=_pending_buy_org_no,
            order_price=submitted_order_price,
            trigger_price=expected_price,
            order_qty=first_qty,
            order_type="LIMIT",
            ask_cap_price=ask_cap or None,
            gap_cap_price=gap_cap or None,
            mode=mode,
            entry_attempt=attempt,
            max_attempts=max_attempts,
            rt_cd=order_resp.get("rt_cd"),
            msg_cd=order_resp.get("msg_cd"),
            msg1=order_resp.get("msg1"),
        )
        if not order_id or str(order_resp.get("rt_cd", "0")) != "0":
            if _is_market_closed_rejection(order_resp):
                await _reset_to_idle_persisted("MARKET_CLOSED")
                state.get().day_skip = True
                log(
                    "MARKET_CLOSED",
                    level="INFO",
                    ticker=ticker,
                    msg_cd=order_resp.get("msg_cd"),
                    msg1=order_resp.get("msg1"),
                )
                await notifier.send(
                    "MARKET_CLOSED",
                    level="INFO",
                    message=f"휴장일 감지(주문 거부 {order_resp.get('msg_cd')}). 당일 거래 없음.",
                    ticker=None,
                )
                await db.record_skip(
                    _today(),
                    "MARKET_CLOSED",
                    f"msg_cd={order_resp.get('msg_cd')},source=ORDER_REJECTION",
                )
                return "MARKET_CLOSED"
            await _reset_to_idle_persisted("ENTRY_FAIL")
            if not allow_candidate_retry:
                state.get().day_skip = True
            log(
                "ENTRY_FAIL",
                level="WARN",
                ticker=ticker,
                order_id=order_id,
                order_price=expected_price,
                order_qty=first_qty,
                entry_attempt=attempt,
                max_attempts=max_attempts,
                reason="ORDER_REJECTED",
                rt_cd=order_resp.get("rt_cd"),
                msg_cd=order_resp.get("msg_cd"),
                msg1=order_resp.get("msg1"),
                candidate_retry=allow_candidate_retry,
            )
            if allow_candidate_retry:
                return "ORDER_REJECTED"
            await notifier.send(
                "ENTRY_FAIL",
                level="WARN",
                message=(
                    f"진입 주문 거절. {ticker} "
                    f"{order_resp.get('msg_cd') or ''} {order_resp.get('msg1') or ''}"
                ),
                ticker=ticker,
            )
            await db.record_skip(
                _today(),
                "ENTRY_FAIL",
                f"order_id={order_id},reason=ORDER_REJECTED",
            )
            return "ORDER_REJECTED"

        await state.set_pending_entry(
            {
                "order_id": order_id,
                "org_no": _pending_buy_org_no,
                "ticker": ticker,
                "requested_qty": first_qty,
                "limit_price": submitted_order_price,
                "anchor_price": expected_price,
                "prev_close": prev_close,
                "expected_amount": entry_expected_amount,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "mode": mode,
                "name": state.get().target_name,
                "date": _today(),
                "phase": "ENTRY",
                "sent_at": datetime.now(KST).isoformat(),
            }
        )
        try:
            await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
        except Exception as exc:
            log(
                "ENTRY_PENDING_PERSIST_ERROR",
                level="CRIT",
                ticker=ticker,
                order_id=order_id,
                error=repr(exc),
            )
            await notifier.send(
                "ENTRY_CANCEL_UNCONFIRMED",
                level="CRIT",
                message=(f"진입 주문 복구정보 저장 실패. 주문 {order_id}를 취소·확인합니다."),
                ticker=ticker,
            )

        entry_audit = _entry_audit_from_pending(
            state.get().pending_entry or {},
            ticker=ticker,
            order_id=order_id,
        )
        # 상태 파일 저장 이후에만 감사 기록을 시작한다. DB가 잠겨 있어도
        # 살아 있는 주문의 체결 폴링은 즉시 시작되어야 하므로 기다리지 않는다.
        if entry_audit is None:
            log(
                "ENTRY_DB_DEGRADED",
                level="CRIT",
                ticker=ticker,
                order_id=order_id,
                phase="ENTRY_ATTEMPT_AUDIT_PAYLOAD",
                reason="INVALID_PENDING_AUDIT_FIELDS",
            )
        else:
            _start_entry_attempt_audit(entry_audit)

        fill_deadline = _deadline_dt_after_seconds(F3_LIMIT_FILL_TIMEOUT_SEC)
        fill = await _poll_fill(
            order_id,
            deadline=fill_deadline,
            ticker=ticker,
            expected_qty=first_qty,
        )
        if fill and int(fill.get("fill_qty") or 0) >= first_qty:
            fill_latency_ms = max(
                0,
                round((time.perf_counter() - order_started_at) * 1000),
            )
            await _upsert_entry_attempt_safe(
                entry_audit,
                "FILLED",
                fill=fill,
                fill_latency_ms=fill_latency_ms,
            )
            await state.clear_pending_entry()
            break

        cancel_outcome, late_fill = await _cancel_entry_order_confirmed(
            order_id,
            _pending_buy_org_no,
            mode,
            ticker,
            attempt,
            max_attempts,
            expected_qty=first_qty,
            known_fill=fill,
        )
        if late_fill:
            # 부분체결 잔량 취소 또는 취소 경쟁 중 체결. 재주문하지 않는다.
            fill = late_fill
            fill_latency_ms = max(
                0,
                round((time.perf_counter() - order_started_at) * 1000),
            )
            fill_was_partial = int(fill.get("fill_qty") or 0) < first_qty
            await _upsert_entry_attempt_safe(
                entry_audit,
                "PARTIAL_FILL" if fill_was_partial else "FILLED",
                fill=fill,
                fill_latency_ms=fill_latency_ms,
            )
            await state.clear_pending_entry()
            log(
                "ENTRY_FILL_RECONCILED",
                level="WARN" if fill_was_partial else "INFO",
                ticker=ticker,
                order_id=order_id,
                order_qty=first_qty,
                fill_qty=fill.get("fill_qty"),
                remaining_qty=max(0, first_qty - int(fill.get("fill_qty") or 0)),
                cancel_outcome=cancel_outcome,
            )
            break
        if cancel_outcome != "CANCELLED":
            # 취소 확인 실패 — 미체결 주문이 살아 있을 수 있다. 재주문·후보
            # 전환·IDLE 전환 모두 금지하고(중복 포지션 위험) ENTERING을 유지한
            # 채 pending 복구 경로로 즉시 한 번 더 대조한다.
            state.get().day_skip = True
            await _upsert_entry_attempt_safe(entry_audit, "UNCERTAIN")
            _log_entry_blocked(
                ticker,
                "CANCEL_UNCONFIRMED",
                order_id=order_id,
                entry_attempt=attempt,
                max_attempts=max_attempts,
            )
            if state.get().pending_entry:
                await recover_pending_entry()
                return
            await notifier.send(
                "ENTRY_CANCEL_UNCONFIRMED",
                level="ERROR",
                message=(
                    f"진입 주문 취소 확인 실패 — 미체결 주문이 살아 있을 수 "
                    f"있습니다. 수동 확인 필요: {ticker} 주문 {order_id}"
                ),
                ticker=ticker,
            )
            await db.record_skip(
                _today(),
                "ENTRY_FAIL",
                f"reason=CANCEL_UNCONFIRMED,order_id={order_id},entry_attempt={attempt}",
            )
            return
        await _upsert_entry_attempt_safe(entry_audit, "CANCELLED")
        await state.clear_pending_entry()
        await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
        if attempt < max_attempts and F3_ENTRY_CANCEL_RELEASE_WAIT_SEC > 0:
            log(
                "ENTRY_CANCEL_RELEASE_WAIT",
                level="INFO",
                ticker=ticker,
                order_id=order_id,
                sleep_sec=F3_ENTRY_CANCEL_RELEASE_WAIT_SEC,
                entry_attempt=attempt,
                max_attempts=max_attempts,
            )
            await asyncio.sleep(F3_ENTRY_CANCEL_RELEASE_WAIT_SEC)
    if not fill:
        await _reset_to_idle_persisted("ENTRY_FAIL")
        log(
            "ENTRY_FAIL",
            level="WARN",
            ticker=ticker,
            order_id=order_id,
            order_price=expected_price,
            order_qty=first_qty,
            entry_attempt=last_run_attempt,
            max_attempts=max_attempts,
            reason=last_entry_fail_reason,
            **_last_fill_poll_summary,
        )
        await notifier.send(
            "ENTRY_FAIL",
            level="WARN",
            message=f"진입 실패({last_entry_fail_reason}). {ticker}",
            ticker=ticker,
        )
        await db.record_skip(
            _today(),
            "ENTRY_FAIL",
            (
                f"order_id={order_id},reason={last_entry_fail_reason},attempts={last_run_attempt},"
                f"poll_attempts={_last_fill_poll_summary.get('poll_attempts', 0)}"
            ),
        )
        return

    fill_price: float = fill["fill_price"]
    fill_qty: int = fill["fill_qty"]

    # ── HOLDING 전환 + DB 기록 + 영속화 ──────────────────────────────
    await state.set_holding(fill_price, fill_qty, order_id)
    # 실체결 포지션은 SQLite 감사 기록보다 먼저 상태 파일에 보존한다. DB 장애가
    # 나더라도 trade_id=0인 HOLDING을 F4/F5가 계속 청산할 수 있어야 한다.
    await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
    try:
        trade_id = await db.open_trade(
            _today(),
            ticker,
            fill_price,
            fill_qty,
            name=state.get().target_name,
        )
        state.get().trade_id = trade_id
        _begin_tick_capture(ticker, trade_id)
        order_db_id = await db.record_order(
            trade_id,
            order_id,
            "BUY",
            first_qty,
            submitted_order_price,
            "FIRST_BUY",
            ticker,
            state.get().target_name,
            trigger_price=expected_price,
        )
        await db.update_order_fill(
            order_db_id,
            fill_price,
            fill_qty,
            fill_latency_ms,
            status=("PARTIAL_FILL" if fill_was_partial or fill_qty < first_qty else "FILLED"),
        )
        await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
    except Exception as exc:
        try:
            await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
        except Exception as persist_exc:
            log(
                "ENTRY_PENDING_PERSIST_ERROR",
                level="CRIT",
                ticker=ticker,
                phase="HOLDING_DB_DEGRADED",
                error=repr(persist_exc),
            )
        log(
            "ENTRY_DB_DEGRADED",
            level="CRIT",
            ticker=ticker,
            order_id=order_id,
            error=repr(exc),
        )
        await notifier.send(
            "ENTRY_DB_DEGRADED",
            level="CRIT",
            message=(
                f"진입 체결 후 DB 기록 실패: {ticker} {fill_qty}주. "
                "손절·마감 청산은 계속되지만 거래 이력을 수동 복구하세요."
            ),
            ticker=ticker,
        )
    log(
        "ENTRY_EXECUTED",
        level="INFO",
        ticker=ticker,
        order_id=order_id,
        order_price=submitted_order_price,
        trigger_price=expected_price,
        order_qty=first_qty,
        fill_price=fill_price,
        fill_qty=fill_qty,
        remaining_order_qty=max(0, first_qty - fill_qty),
        partial_fill=fill_was_partial or fill_qty < first_qty,
        fill_latency_ms=fill_latency_ms,
    )
    await notifier.send(
        "ENTRY_EXECUTED",
        level="INFO",
        message=f"진입: {ticker} {fill_qty}주 @ {fill_price:,}원",
        ticker=ticker,
    )

    # ── 거래소/API 불변식 위반 방어. 정상 지정가는 제출가 이하 체결이므로
    # 갭 10%와 지정가 초과 조건에 도달하지 않는다.
    fill_gap = (fill_price / prev_close) - 1
    direct_slippage = (fill_price / expected_price) - 1
    reasons = _evaluate_post_fill_guard(
        fill_price=fill_price,
        submitted_order_price=submitted_order_price,
        prev_close=prev_close,
        expected_amount=entry_expected_amount,
    )
    if reasons:
        log(
            "SLIPPAGE_GUARD",
            level="WARN",
            ticker=ticker,
            expected_price=expected_price,
            submitted_limit_price=submitted_order_price,
            fill_price=fill_price,
            prev_close=prev_close,
            fill_gap_pct=round(fill_gap * 100, 3),
            gap_max_pct=round(GAP_MAX_FILL * 100, 2),
            slippage_pct=round(direct_slippage * 100, 3),
            guard_reasons=reasons,
        )
        state.get().day_skip = True
        await notifier.send(
            "SLIPPAGE_GUARD",
            level="WARN",
            message=(
                f"체결 안전상한 위반({','.join(reasons)}). "
                f"{ticker} {fill_qty}주 확인 청산을 시작합니다."
            ),
            ticker=ticker,
        )
        await db.record_skip(
            _today(),
            "SLIPPAGE_GUARD",
            (
                f"expected={expected_price},limit={submitted_order_price},"
                f"fill={fill_price},prev_close={prev_close},"
                f"fill_gap_pct={round(fill_gap * 100, 3)},"
                f"slippage_pct={round(direct_slippage * 100, 3)},"
                f"reasons={'+'.join(reasons)}"
            ),
        )
        from src.modules import f4_tracking

        await f4_tracking.close_now(fill_price, "SLIPPAGE_GUARD")
        return

    # 2nd buy is inactive while FIRST_RATIO is 1.00.
    if second_qty <= 0:
        log(
            "PYRAMID_SKIPPED",
            level="INFO",
            ticker=ticker,
            reason="NO_SECOND_QTY",
            first_ratio=FIRST_RATIO,
        )
        return

    # ── 과거 비율 호환용 2차 피라미딩 (현재 FIRST_RATIO=1.00이라 비활성) ──
    await _sleep_until(*_pyramid_at())
    if state.get().position_status != "HOLDING":
        return

    current_price = await _fetch_current_price(ticker)
    if second_qty > 0 and current_price and current_price >= fill_price * (1 + PYRAMID_MIN_UP):
        await _pre_order_quiet_wait(ticker, 1, 1, current_price, second_qty, phase="PYRAMID")
        py_quote = await _fetch_final_entry_quote(ticker)
        if py_quote is None:
            log(
                "PYRAMID_SKIPPED",
                level="WARN",
                ticker=ticker,
                reason="FINAL_QUOTE_UNAVAILABLE",
            )
            return
        py_gap_cap = gap_cap
        py_limit_price, py_ask_cap = _entry_limit_price(
            py_quote.ask_price,
            py_gap_cap,
        )
        py_gap = (py_quote.ask_price / prev_close) - 1
        if not _quote_is_fresh(py_quote):
            log(
                "PYRAMID_SKIPPED",
                level="INFO",
                ticker=ticker,
                reason="FINAL_QUOTE_STALE",
                anchor_price=current_price,
                ask_price=py_quote.ask_price,
                limit_price=py_limit_price,
                ask_cap_price=py_ask_cap,
                gap_cap_price=py_gap_cap,
                quote_age_ms=_quote_age_ms(py_quote),
            )
            return
        py_allowed, py_gap_reason = _evaluate_order_gap(py_gap, entry_expected_amount)
        if not py_allowed:
            log(
                "PYRAMID_SKIPPED",
                level="INFO",
                ticker=ticker,
                reason=(
                    "HIGH_GAP_AMOUNT_LOW"
                    if py_gap_reason == "HIGH_GAP_AMOUNT_LOW"
                    else "GAP_CHANGED"
                ),
                anchor_price=current_price,
                ask_price=py_quote.ask_price,
                limit_price=py_limit_price,
                ask_cap_price=py_ask_cap,
                gap_cap_price=py_gap_cap,
                gap_pct=round(py_gap * 100, 3),
                gap_reason=py_gap_reason,
                expected_amount=entry_expected_amount,
                threshold=HIGH_GAP_MIN_EXPECTED_AMOUNT,
                quote_age_ms=_quote_age_ms(py_quote),
            )
            return
        py_order_started_at = time.perf_counter()
        py_resp = await _send_buy(
            ticker,
            second_qty,
            mode,
            limit_price=py_limit_price,
            send_guard=lambda: _quote_is_fresh(py_quote),
        )
        py_id = py_resp.get("output", {}).get("ODNO", "")
        py_org_no = py_resp.get("output", {}).get("KRX_FWDG_ORD_ORGNO", "")
        if not py_id or str(py_resp.get("rt_cd", "0")) != "0":
            log(
                "PYRAMID_SKIPPED",
                level="WARN",
                ticker=ticker,
                reason="ORDER_REJECTED",
                rt_cd=py_resp.get("rt_cd"),
                msg_cd=py_resp.get("msg_cd"),
                msg1=py_resp.get("msg1"),
            )
            return
        await state.set_pending_entry(
            {
                "order_id": py_id,
                "org_no": py_org_no,
                "ticker": ticker,
                "requested_qty": second_qty,
                "limit_price": py_limit_price,
                "anchor_price": current_price,
                "prev_close": prev_close,
                "expected_amount": entry_expected_amount,
                "attempt": 1,
                "max_attempts": 1,
                "mode": mode,
                "name": state.get().target_name,
                "date": _today(),
                "phase": "PYRAMID",
                "sent_at": datetime.now(KST).isoformat(),
            }
        )
        await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
        py_fill = await _poll_fill(
            py_id,
            deadline=_deadline_dt_after_seconds(F3_LIMIT_FILL_TIMEOUT_SEC),
            ticker=ticker,
            expected_qty=second_qty,
        )
        py_full = bool(py_fill and int(py_fill.get("fill_qty") or 0) >= second_qty)
        if not py_full:
            cancel_outcome, reconciled = await _cancel_entry_order_confirmed(
                py_id,
                py_org_no,
                mode,
                ticker,
                1,
                1,
                expected_qty=second_qty,
                known_fill=py_fill,
            )
            py_fill = reconciled
            if cancel_outcome != "CANCELLED" and not (
                py_fill and int(py_fill.get("fill_qty") or 0) >= second_qty
            ):
                state.get().day_skip = True
                if state.get().pending_entry:
                    await recover_pending_entry()
                    return
                await notifier.send(
                    "ENTRY_CANCEL_UNCONFIRMED",
                    level="ERROR",
                    message=f"피라미딩 주문 취소 확인 실패: {ticker} {py_id}",
                    ticker=ticker,
                )
                return
        await state.clear_pending_entry()
        await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
        if not py_fill:
            log("PYRAMID_TIMEOUT", level="WARN", ticker=ticker, py_id=py_id)
        if py_fill:
            py_fill_latency_ms = max(
                0,
                round((time.perf_counter() - py_order_started_at) * 1000),
            )
            s = state.get()
            s.entry_qty = (s.entry_qty or 0) + py_fill["fill_qty"]
            s.remaining_qty = (s.remaining_qty or 0) + py_fill["fill_qty"]
            py_order_db_id = await db.record_order(
                trade_id,
                py_id,
                "BUY",
                second_qty,
                py_limit_price,
                "PYRAMID_BUY",
                ticker,
                s.target_name,
                trigger_price=current_price,
            )
            await db.update_order_fill(
                py_order_db_id,
                py_fill["fill_price"],
                py_fill["fill_qty"],
                py_fill_latency_ms,
                status=(
                    "FILLED" if int(py_fill.get("fill_qty") or 0) >= second_qty else "PARTIAL_FILL"
                ),
            )
            await db.mark_pyramided(trade_id)
            await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
            log(
                "PYRAMID_EXECUTED",
                level="INFO",
                ticker=ticker,
                order_price=py_limit_price,
                trigger_price=current_price,
                fill_price=py_fill["fill_price"],
                fill_qty=py_fill["fill_qty"],
                fill_latency_ms=py_fill_latency_ms,
            )
            await notifier.send(
                "PYRAMID_EXECUTED",
                level="INFO",
                message=(
                    f"추가 매수: {ticker} {py_fill['fill_qty']}주 " f"@ {py_fill['fill_price']:,}원"
                ),
                ticker=ticker,
            )
            # 최초 매수와 동일한 단일 평가기로 피라미딩 체결도 방어한다.
            py_guard_reasons = _evaluate_post_fill_guard(
                fill_price=float(py_fill["fill_price"]),
                submitted_order_price=py_limit_price,
                prev_close=prev_close,
                expected_amount=entry_expected_amount,
            )
            if py_guard_reasons:
                state.get().day_skip = True
                log(
                    "SLIPPAGE_GUARD",
                    level="WARN",
                    ticker=ticker,
                    phase="PYRAMID",
                    fill_price=float(py_fill["fill_price"]),
                    prev_close=prev_close,
                    submitted_limit_price=py_limit_price,
                    guard_reasons=py_guard_reasons,
                    expected_amount=entry_expected_amount,
                    threshold=HIGH_GAP_MIN_EXPECTED_AMOUNT,
                )
                from src.modules import f4_tracking

                await f4_tracking.close_now(float(py_fill["fill_price"]), "SLIPPAGE_GUARD")
    elif second_qty > 0:
        diff_pct = ((current_price or 0.0) / fill_price - 1) * 100
        log(
            "PYRAMID_SKIPPED",
            level="INFO",
            ticker=ticker,
            entry_price=fill_price,
            current_price=current_price,
            diff_pct=round(diff_pct, 2),
        )
        await notifier.send("PYRAMID_SKIPPED", level="INFO", message=f"2차 피라미딩 생략. {ticker}")


# ── 헬퍼 ─────────────────────────────────────────────────────────────


def _entry_cash_basis(buyable: dict | None) -> tuple[float | None, str]:
    """수량 산정에 쓸 현금과 그 출처.

    ord_psbl_cash는 브로커가 "지금 실제로 얼마 쓸 수 있는가"에 직접 답한 값이다.
    잔고 요약의 dnca_tot_amt는 미결제 매수대금을 반영하지 않아 장부에만 남은
    금액을 그대로 부른다 — 20260709에 장부는 10,064,148인데 실제 주문가능액은
    119,940이었고, 장부값으로 잡은 수량은 주문가능금액 부족으로 거부됐다.

    값이 없거나 0이면 None을 돌려 기존 잔고 요약 경로로 물러난다. 이 필드를
    주지 않는 계좌·모드에서 수량 0으로 막히면 그것이 회귀다.
    """
    if not buyable or buyable.get("query_failed"):
        return None, "BALANCE_SUMMARY"
    cash = float(buyable.get("ord_psbl_cash") or 0.0)
    if cash <= 0:
        return None, "BALANCE_SUMMARY"
    return cash, "ORD_PSBL_CASH"


def _is_confirmed_full_fill(fill: dict | None, expected_qty: int | None) -> bool:
    """전량 체결로 확정할 수 있는 스냅샷인지 판정한다.

    수량이 맞아도 체결가가 0이면 확정하지 않는다. 브로커가 누적수량만 주고
    금액·평균가를 아직 못 채운 행이 있는데, 그대로 set_holding(0.0)이 되면
    F4의 스탑·트레일링 계산이 통째로 무너진다. 가격을 모르면 UNCERTAIN으로
    넘겨 recover_pending_entry의 INVALID_FILL_PRICE 검사에 맡기는 편이 낫다.
    """
    if not fill:
        return False
    if expected_qty is not None and int(fill.get("fill_qty") or 0) < expected_qty:
        return False
    # expected_qty=None은 현재 호출부에서 도달하지 않는다(모두 int를 넘긴다).
    # 방어적으로만 남겨 두며, 이 경우에도 가격 검사는 동일하게 적용한다.
    return float(fill.get("fill_price") or 0.0) > 0.0


async def _cancel_entry_order_confirmed(
    order_id: str,
    org_no: str,
    mode: str,
    ticker: str,
    attempt: int,
    max_attempts: int,
    *,
    expected_qty: int | None = None,
    known_fill: dict | None = None,
) -> tuple[str, dict | None]:
    """진입 주문 취소를 '확인'까지 수행한다.

    반환: ("CANCELLED", None) | ("FILLED", fill) | ("UNCERTAIN", None).
    취소 거부의 흔한 원인은 기체결이므로 거부 시 체결부터 재확인하고,
    아니면 취소를 1회 재시도한다. 끝까지 확인되지 않으면 UNCERTAIN —
    호출부는 재주문·후보 전환·IDLE 전환을 해서는 안 된다.
    """
    cancel_resp = await _cancel_order(order_id, org_no, mode)
    log(
        "ENTRY_CANCEL_SENT",
        level="WARN",
        ticker=ticker,
        order_id=order_id,
        org_no=org_no,
        entry_attempt=attempt,
        max_attempts=max_attempts,
        rt_cd=cancel_resp.get("rt_cd"),
        msg_cd=cancel_resp.get("msg_cd"),
        msg1=cancel_resp.get("msg1"),
    )
    if str(cancel_resp.get("rt_cd", "0")) == "0":
        reconciled = _more_complete_fill(
            known_fill,
            await _fetch_order_fill_snapshot(
                order_id,
                ticker=ticker,
                expected_qty=expected_qty,
            ),
        )
        return "CANCELLED", reconciled

    fill = await _poll_fill(
        order_id,
        deadline=_deadline_dt_after_seconds(F3_ENTRY_CANCEL_CONFIRM_FILL_SEC),
        ticker=ticker,
        expected_qty=expected_qty,
    )
    fill = _more_complete_fill(known_fill, fill)
    if _is_confirmed_full_fill(fill, expected_qty):
        log(
            "ENTRY_CANCEL_REJECTED_FILLED",
            level="WARN",
            ticker=ticker,
            order_id=order_id,
            entry_attempt=attempt,
            fill_price=fill["fill_price"],
            fill_qty=fill["fill_qty"],
            confirmed_after="CANCEL_POLL",
        )
        return "FILLED", fill

    retry_resp = await _cancel_order(order_id, org_no, mode)
    log(
        "ENTRY_CANCEL_RETRY",
        level="WARN",
        ticker=ticker,
        order_id=order_id,
        org_no=org_no,
        entry_attempt=attempt,
        rt_cd=retry_resp.get("rt_cd"),
        msg_cd=retry_resp.get("msg_cd"),
        msg1=retry_resp.get("msg1"),
    )
    if str(retry_resp.get("rt_cd", "0")) == "0":
        reconciled = _more_complete_fill(
            fill,
            await _fetch_order_fill_snapshot(
                order_id,
                ticker=ticker,
                expected_qty=expected_qty,
            ),
        )
        return "CANCELLED", reconciled

    # 재시도 취소까지 거부됐다. 거부의 가장 흔한 원인은 기체결인데, 체결조회는
    # 주문 직후 수 초간 빈 응답을 준다. 앞선 폴링 창이 그 지연보다 짧으면 체결을
    # 못 보고 UNCERTAIN이 된다. 하루를 접기 전에 마지막으로 한 번 더 대조한다 —
    # 두 번째 취소 거부까지 왕복한 만큼 시간이 더 흘렀으므로 이제는 잡힐 수 있다.
    # 조회가 실패해도 여기서 예외를 올리면 안 된다. 호출부의 day_skip·CRIT
    # 알림·pending 복구가 통째로 건너뛰어지고 체결된 포지션이 ENTERING인 채
    # F4 추적 밖에 남는다. 확인에 실패하면 원래대로 UNCERTAIN으로 떨어진다.
    try:
        late_snapshot = await _fetch_order_fill_snapshot(
            order_id,
            ticker=ticker,
            expected_qty=expected_qty,
        )
    except Exception as exc:  # noqa: BLE001 — 확인 실패가 안전장치를 막으면 안 된다
        log(
            "ENTRY_CANCEL_CONFIRM_ERROR",
            level="WARN",
            ticker=ticker,
            order_id=order_id,
            entry_attempt=attempt,
            error=repr(exc),
        )
        late_snapshot = None
    final_fill = _more_complete_fill(fill, late_snapshot)
    if _is_confirmed_full_fill(final_fill, expected_qty):
        log(
            "ENTRY_CANCEL_REJECTED_FILLED",
            level="WARN",
            ticker=ticker,
            order_id=order_id,
            entry_attempt=attempt,
            fill_price=final_fill["fill_price"],
            fill_qty=final_fill["fill_qty"],
            confirmed_after="CANCEL_RETRY",
        )
        return "FILLED", final_fill

    log(
        "ENTRY_CANCEL_UNCONFIRMED",
        level="ERROR",
        ticker=ticker,
        order_id=order_id,
        entry_attempt=attempt,
        rt_cd=retry_resp.get("rt_cd"),
        msg_cd=retry_resp.get("msg_cd"),
        msg1=retry_resp.get("msg1"),
    )
    return "UNCERTAIN", None


def _candidate_for_ticker(s: state.State, ticker: str) -> dict | None:
    for candidate in s.target_candidates or []:
        if isinstance(candidate, dict) and candidate.get("ticker") == ticker:
            return candidate
    return None


def _candidate_expected_amount(candidate: dict | None) -> float | None:
    """후보의 예상 체결대금(고갭 유동성 판정용). 유한 float 또는 None으로 정규화.

    부재·무효(None/NaN/±inf/비수치)는 모두 None (fail-closed)으로 반환한다.
    """
    if not isinstance(candidate, dict):
        return None
    # F1 selector와 동일하게 raw 예상 체결대금이 0/None이면 5일 평균
    # 거래대금으로 폴백한다. 두 단계가 서로 다른 유동성 지표를 쓰지 않는다.
    value = candidate.get("expected_amount") or candidate.get("avg_amount_5d")
    return _normalize_expected_amount(value)


def _resolve_recovery_expected_amount(
    pending: dict,
    s: state.State,
    ticker: str,
) -> tuple[float | None, str]:
    """재시작 복구에서 고갭 판정용 예상 체결대금을 해소한다.

    우선순위: 유효(유한)로 정규화되는 pending 키 → state.target_candidates의
    동일 종목 → 미확인. pending 대금이 있으나 무효(NaN/±inf/비수치)면 pending을
    쓰지 않고 후보로 폴백한다. 유효한 저대금 pending은 더 큰 후보 대금으로
    대체되지 않고 그대로 우선한다. 현재 버전 pending에 키가 있으나 끝까지
    확인 불가면 (None, "unavailable")로 fail-safe 처리한다. expected_amount
    키 자체가 없는 구버전 pending만 (None, "legacy_unavailable")로 구분한다.
    """
    pending_amount = _normalize_expected_amount(pending.get("expected_amount"))
    if pending_amount is not None:
        return (pending_amount, "pending")
    candidate = _candidate_for_ticker(s, ticker)
    amount = _candidate_expected_amount(candidate)
    if amount is not None:
        return (amount, "candidates")
    if "expected_amount" not in pending:
        return (None, "legacy_unavailable")
    return (None, "unavailable")


def _candidate_prev_close(candidate: dict | None) -> float:
    if not isinstance(candidate, dict):
        return 0.0
    prev_close = float(candidate.get("prev_close") or 0)
    if prev_close > 0:
        return prev_close
    # 갭으로 유도할 때는 반드시 스냅샷 가격을 사용한다.
    # 라이브 가격으로 유도하면 재계산 갭 ≡ 스냅샷 갭이 되어 GAP_CHANGED 가드가 무력화됨.
    snapshot_price = float(candidate.get("expected_price") or 0)
    gap_pct = candidate.get("gap_pct")
    if snapshot_price > 0 and gap_pct is not None:
        gap = float(gap_pct)
        if gap > -0.99:
            return snapshot_price / (1 + gap)
    return 0.0


async def _existing_trade_for_today() -> dict | None:
    try:
        return await db.get_trade_by_date(_today())
    except RuntimeError:
        return None


async def _block_existing_trade(ticker: str, existing_trade: dict) -> None:
    s = state.get()
    status = existing_trade.get("status")
    trade_id = int(existing_trade.get("id") or 0)
    existing_ticker = existing_trade.get("ticker")
    _log_entry_blocked(
        ticker,
        "TRADE_ALREADY_EXISTS",
        trade_id=trade_id,
        existing_ticker=existing_ticker,
        existing_status=status,
    )
    log(
        "TRADE_ALREADY_EXISTS",
        level="WARN",
        ticker=ticker,
        trade_id=trade_id,
        existing_ticker=existing_ticker,
        existing_status=status,
    )
    if status == "OPEN":
        entry_price = float(existing_trade.get("entry_price") or 0)
        entry_qty = int(existing_trade.get("entry_qty") or 0)
        s.target_ticker = existing_ticker or s.target_ticker
        if existing_ticker and existing_ticker != ticker:
            # 다른 종목의 기존 거래를 복구하면 이전 후보의 종목명이 남지 않게 한다.
            s.target_name = existing_trade.get("name")
        else:
            s.target_name = existing_trade.get("name") or s.target_name
        s.trade_id = trade_id
        if entry_price > 0 and entry_qty > 0 and s.position_status != "HOLDING":
            await state.set_holding(entry_price, entry_qty, s.order_id or "")
            state.get().trade_id = trade_id
        return
    s.day_skip = True
    s.close_reason = "TRADE_ALREADY_EXISTS"


def _entry_candidate_tickers(s: state.State) -> list[str]:
    tickers: list[str] = []
    for candidate in s.target_candidates or []:
        ticker = candidate.get("ticker") if isinstance(candidate, dict) else str(candidate)
        if ticker and ticker not in tickers:
            tickers.append(ticker)
    if s.target_ticker and s.target_ticker not in tickers:
        tickers.insert(0, s.target_ticker)
    return tickers


async def _block_entry_deadline_passed(ticker: str | None, stage: str) -> None:
    """진입 마감(F3_ENTRY_RETRY_DEADLINE) 초과 — 잔고 재시도 등으로 지연된 최초 주문 차단."""
    s = state.get()
    s.day_skip = True
    s.close_reason = "ENTRY_FAIL"
    log(
        "ENTRY_DEADLINE_PASSED",
        level="WARN",
        ticker=ticker,
        stage=stage,
        deadline=F3_ENTRY_RETRY_DEADLINE,
    )
    _log_entry_blocked(ticker, "ENTRY_DEADLINE_PASSED", stage=stage)
    await notifier.send(
        "ENTRY_FAIL",
        level="WARN",
        message=(
            f"진입 마감시각({F3_ENTRY_RETRY_DEADLINE}) 초과로 " "최초 주문을 내지 않고 중단합니다."
        ),
        ticker=ticker,
    )
    await db.record_skip(
        _today(),
        "ENTRY_FAIL",
        f"reason=ENTRY_DEADLINE_PASSED,stage={stage},ticker={ticker}",
    )


async def _persist_terminal_or_log(reason: str) -> bool:
    """이미 안전한 종료 상태로 정리된 인메모리 상태를 디스크에 durable하게 반영.

    예외를 전파하지 않고 실패 시 CRIT로만 남긴다 — 재진입 차단은 인메모리
    day_skip과 DB daily_skips 복원이, 디스크에 남은 상태는 재시작 fail-closed가
    담당한다. 성공을 가장하지 않도록 성공 여부를 반환한다.
    """
    try:
        await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
        return True
    except Exception as exc:
        log("ENTRY_TERMINAL_PERSIST_ERROR", level="CRIT", reason=reason, error=repr(exc))
        try:
            await notifier.send(
                "ENTRY_TERMINAL_PERSIST_ERROR",
                level="CRIT",
                message=(f"종료 상태({reason}) 저장 실패. 재시작 시 디스크 상태를 확인하세요."),
                ticker=state.get().target_ticker,
            )
        except Exception:
            pass
        return False


async def _reset_to_idle_persisted(reason: str) -> bool:
    """안전한 종료(ENTERING→IDLE)를 디스크까지 durable하게 반영한다.

    살아 있는 주문이 없다고 확인된 종료 경로에서만 호출한다 — 취소/포지션 대조가
    불확실하면 ENTERING을 유지해야 한다. 예외를 전파하지 않는다(F3/F4 async
    오케스트레이션을 중단시키지 않기 위해). 영속화 실패 시 성공을 가장하지 않고
    fail-closed 처리한다: 인메모리는 IDLE로 두되 당일 신규 진입을 차단하고 CRIT로
    기록·통지한다. 디스크에 남은 ENTERING은 재시작 시 fail-closed로 걸린다.
    """
    ticker = state.get().target_ticker
    await state.reset_to_idle(reason)
    try:
        await state.persist(os.getenv("STATE_DIR", "data/state"), _today())
        return True
    except Exception as exc:
        state.get().day_skip = True
        log(
            "ENTRY_TERMINAL_PERSIST_ERROR",
            level="CRIT",
            ticker=ticker,
            reason=reason,
            error=repr(exc),
        )
        try:
            await notifier.send(
                "ENTRY_TERMINAL_PERSIST_ERROR",
                level="CRIT",
                message=(
                    f"종료 상태({reason}) 저장 실패로 신규 진입을 차단했습니다. "
                    "재시작 시 디스크 상태를 확인하세요."
                ),
                ticker=ticker,
            )
        except Exception:
            pass
        return False


async def _reject_final_entry_price(
    ticker: str,
    reason: str,
    *,
    allow_candidate_retry: bool,
    anchor_price: float,
    prev_close: float,
    ask_price: float = 0.0,
    limit_price: float = 0.0,
    quote_age_ms: int | None = None,
    fresh_gap: float | None = None,
    expected_amount: float | None = None,
) -> str:
    """살아 있는 주문이 없을 때 최종 가격검사를 fail-closed로 종료한다."""
    await _reset_to_idle_persisted(reason)
    level = "INFO" if allow_candidate_retry else "WARN"
    # HIGH_GAP_AMOUNT_LOW도 갭 계열 종료로 취급한다 (후보 전환/종료 사유·이벤트 매핑).
    is_gap_reason = reason in ("GAP_CHANGED", "HIGH_GAP_AMOUNT_LOW")
    log(
        "ENTRY_PRICE_BLOCKED",
        level=level,
        ticker=ticker,
        reason=reason,
        anchor_price=anchor_price,
        prev_close=prev_close,
        ask_price=ask_price,
        limit_price=limit_price,
        quote_age_ms=quote_age_ms,
        fresh_gap_pct=(round(fresh_gap * 100, 3) if fresh_gap is not None else None),
        expected_amount=expected_amount,
        threshold=HIGH_GAP_MIN_EXPECTED_AMOUNT,
        candidate_retry=allow_candidate_retry,
    )
    _log_entry_blocked(
        ticker,
        reason,
        level=level,
        anchor_price=anchor_price,
        ask_price=ask_price,
        limit_price=limit_price,
        expected_amount=expected_amount,
        threshold=HIGH_GAP_MIN_EXPECTED_AMOUNT,
        candidate_retry=allow_candidate_retry,
    )
    if allow_candidate_retry:
        return reason

    state.get().day_skip = True
    state.get().close_reason = "GAP_CHANGED" if is_gap_reason else "ENTRY_FAIL"
    # reset 시 원시 거절 사유로 persist된 disk를 최종 close_reason으로 재동기화한다.
    await _persist_terminal_or_log(state.get().close_reason)
    await notifier.send(
        "ENTRY_FAIL",
        level="WARN",
        message=(
            f"진입 직전 가격 안전검사 차단: {ticker} "
            f"reason={reason} ask={ask_price:g} cap={limit_price:g}"
        ),
        ticker=ticker,
    )
    await db.record_skip(
        _today(),
        "GAP_CHANGED" if is_gap_reason else "ENTRY_FAIL",
        (
            f"reason={reason},anchor={anchor_price},ask={ask_price},"
            f"limit={limit_price},quote_age_ms={quote_age_ms}"
        ),
    )
    return reason


async def _early_retry_gap_guard(
    ticker: str,
    *,
    expected_price: float,
    prev_close: float,
    allow_candidate_retry: bool,
    entry_attempt: int,
    expected_amount: float | None = None,
) -> str | None:
    """느린 수량·VI 조회 전에 재시도 후보의 명백한 갭 이탈만 조기 차단한다.

    조기 호가가 없거나 이미 오래됐으면 기존 주문 직전 검사를 그대로 수행한다.
    유효한 경우에도 실제 주문 직전 호가 검사는 생략하지 않는다.
    """
    entry_quote = await _fetch_final_entry_quote(ticker)
    if entry_quote is None:
        return None
    quote_age_ms = _quote_age_ms(entry_quote)
    if F3_FINAL_QUOTE_MAX_AGE_MS > 0 and quote_age_ms > F3_FINAL_QUOTE_MAX_AGE_MS:
        return None

    fresh_gap = (entry_quote.ask_price / prev_close) - 1 if prev_close > 0 else -1.0
    fresh_allowed, fresh_reason = _evaluate_order_gap(fresh_gap, expected_amount)
    log(
        "ENTRY_RETRY_EARLY_GAP_CHECK",
        level="INFO",
        ticker=ticker,
        entry_attempt=entry_attempt,
        ask_price=entry_quote.ask_price,
        prev_close=prev_close,
        fresh_gap_pct=round(fresh_gap * 100, 3),
        gap_allowed=fresh_allowed,
        gap_reason=fresh_reason,
        expected_amount=expected_amount,
        quote_age_ms=quote_age_ms,
    )
    if fresh_allowed:
        return None
    reject_reason = (
        "HIGH_GAP_AMOUNT_LOW" if fresh_reason == "HIGH_GAP_AMOUNT_LOW" else "GAP_CHANGED"
    )
    return await _reject_final_entry_price(
        ticker,
        reject_reason,
        allow_candidate_retry=allow_candidate_retry,
        anchor_price=expected_price,
        prev_close=prev_close,
        ask_price=entry_quote.ask_price,
        limit_price=_strict_gap_cap(prev_close, expected_amount=expected_amount),
        quote_age_ms=quote_age_ms,
        fresh_gap=fresh_gap,
        expected_amount=expected_amount,
    )


async def _alert_balance_query_failed(ticker: str | None, candidates: list[str]) -> None:
    """잔고 조회 실패 확정 — 실제 잔고 부족(INSUFFICIENT_BALANCE)과 구분해 기록·통지한다."""
    log(
        "BALANCE_QUERY_FAILED",
        level="CRIT",
        ticker=ticker,
        candidates=candidates,
        max_attempts=BALANCE_QUERY_MAX_ATTEMPTS,
    )
    _log_entry_blocked(ticker, "BALANCE_QUERY_FAILED", candidates=candidates)
    await notifier.send(
        "BALANCE_QUERY_FAILED",
        level="CRIT",
        message=(
            f"잔고 조회가 {BALANCE_QUERY_MAX_ATTEMPTS}회 재시도 끝에 실패해 "
            "진입을 차단했습니다. 잔고 부족이 아니라 API 오류입니다. "
            "KIS 상태와 계좌를 확인하세요."
        ),
        ticker=ticker,
    )
    await db.record_skip(
        _today(),
        "ENTRY_FAIL",
        f"reason=BALANCE_QUERY_FAILED,candidates={','.join(candidates)}",
    )


def _fast_recheck_rows(
    tickers: list[str],
    candidate_by_ticker: dict[str, dict],
) -> list[dict] | None:
    if not paper_fast_probe.hybrid_enabled():
        return None
    fast_by_ticker = {
        str(candidate.get("ticker") or ""): candidate
        for candidate in paper_fast_probe.get_open_candidates()
    }
    now_mono = time.monotonic()
    rows: list[dict] = []
    max_age_ms = 0
    for rank, ticker in enumerate(tickers, start=1):
        fast = fast_by_ticker.get(ticker)
        candidate = candidate_by_ticker.get(ticker) or fast
        if fast is None or candidate is None:
            return None
        observed = float(fast.get("fast_observed_monotonic") or 0.0)
        age_sec = max(0.0, now_mono - observed) if observed > 0 else float("inf")
        if F3_FAST_RECHECK_MAX_AGE_SEC <= 0 or age_sec > F3_FAST_RECHECK_MAX_AGE_SEC:
            return None
        expected_price = float(fast.get("expected_price") or 0.0)
        prev_close = float(fast.get("prev_close") or 0.0)
        if expected_price <= 0 or prev_close <= 0:
            return None
        max_age_ms = max(max_age_ms, round(age_sec * 1000))
        rows.append(
            {
                "rank": rank,
                "ticker": ticker,
                "candidate": candidate,
                "expected_price": expected_price,
                "prev_close": prev_close,
            }
        )
    log(
        "F3_FAST_RECHECK_USED",
        level="INFO",
        requested_count=len(tickers),
        completed_count=len(rows),
        max_age_ms=max_age_ms,
        max_age_sec=F3_FAST_RECHECK_MAX_AGE_SEC,
    )
    return rows


async def _held_tickers_to_exclude() -> set[str]:
    """계좌에 이미 보유 중인 종목. 개발 트리에서만 채워진다.

    운영과 개발이 모의계좌를 공유할 때(설계 6절), 같은 종목을 들면 먼저
    청산하는 쪽이 상대 물량까지 판다 — F5가 계좌 전체 hldg_qty로 수량을
    정하기 때문이다. 개발이 다음 순위로 내려가 충돌 자체를 없앤다.

    조회가 실패하면 빈 집합을 낸다. 여기서 fail-closed 하면 개발 트리가
    아무것도 테스트하지 못하는데, 실패의 대가는 종목 중복뿐이고 그 손해는
    개발 몫(계좌의 30%)에 한정된다.
    """
    if os.getenv("DEV_EXCLUDE_HELD_TICKERS", "0") != "1":
        return set()
    try:
        resp = await kis_rest.get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id=_BAL_TR[os.getenv("KIS_MODE", "PAPER")],
            params=kis_rest.balance_inquiry_params(),
        )
    except Exception as exc:  # noqa: BLE001 — 조회 실패가 진입을 막지 않는다
        log("DEV_HELD_QUERY_FAILED", level="WARN", error=repr(exc))
        return set()
    held = {
        str(row.get("pdno") or "")
        for row in (resp.get("output1") or [])
        if str(row.get("pdno") or "") and int(float(row.get("hldg_qty") or 0)) > 0
    }
    if held:
        log("DEV_HELD_TICKERS_EXCLUDED", level="INFO", tickers=sorted(held))
    return held


async def _rank_final_entry_candidates(
    s: state.State,
    exclude_tickers: set[str] | None = None,
) -> list[dict] | None:
    candidates = s.target_candidates or []
    candidate_by_ticker = {
        c.get("ticker"): c for c in candidates if isinstance(c, dict) and c.get("ticker")
    }
    exclude_tickers = set(exclude_tickers or set()) | await _held_tickers_to_exclude()
    tickers = [ticker for ticker in _entry_candidate_tickers(s) if ticker not in exclude_tickers]
    valid: list[dict] = []
    blocked_reasons: list[str] = []

    async def recheck_one(rank: int, ticker: str) -> dict:
        candidate = candidate_by_ticker.get(ticker)
        fallback_prev_close = _candidate_prev_close(candidate)
        try:
            expected_price, prev_close = await _fetch_expected_price(
                ticker,
                fallback_prev_close=fallback_prev_close,
            )
        except Exception as exc:
            log(
                "F3_RECHECK_QUOTE_ERROR",
                level="WARN",
                ticker=ticker,
                candidate_rank=rank,
                error=repr(exc),
            )
            expected_price, prev_close = 0.0, fallback_prev_close
        if prev_close <= 0:
            prev_close = fallback_prev_close
        if candidate is None:
            log(
                "F3_CANDIDATE_SNAPSHOT_MISSING",
                level="WARN",
                ticker=ticker,
                candidate_rank=rank,
                candidates=tickers,
            )
            candidate = {"ticker": ticker}
        return {
            "rank": rank,
            "ticker": ticker,
            "candidate": candidate,
            "expected_price": expected_price,
            "prev_close": prev_close,
        }

    async def fetch_available_cash_safe() -> float | None:
        try:
            return await _available_cash_for_entry()
        except Exception as exc:
            log(
                "BALANCE_QUERY_ERROR",
                level="WARN",
                reason="EXCEPTION",
                error=repr(exc),
            )
            return None

    batch_started = time.perf_counter()
    cash_task = asyncio.create_task(fetch_available_cash_safe())
    recheck_source = "FAST_MULTI"
    recheck_rows = _fast_recheck_rows(tickers, candidate_by_ticker)
    if recheck_rows is None:
        recheck_source = "SINGLE_QUOTE"
        recheck_tasks = [
            asyncio.create_task(recheck_one(rank, ticker))
            for rank, ticker in enumerate(tickers, start=1)
        ]
        task_tickers = dict(zip(recheck_tasks, tickers, strict=False))
        if F3_RECHECK_BATCH_TIMEOUT_SEC > 0:
            done, pending = await asyncio.wait(
                recheck_tasks,
                timeout=F3_RECHECK_BATCH_TIMEOUT_SEC,
            )
            if pending:
                log(
                    "F3_RECHECK_BATCH_TIMEOUT",
                    level="WARN",
                    requested_count=len(recheck_tasks),
                    completed_count=len(done),
                    pending_count=len(pending),
                    pending_tickers=[task_tickers[task] for task in pending],
                    timeout_sec=F3_RECHECK_BATCH_TIMEOUT_SEC,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            recheck_rows = [task.result() for task in recheck_tasks if task in done]
        else:
            recheck_rows = await asyncio.gather(*recheck_tasks)
    cash = await cash_task
    batch_elapsed_ms = round((time.perf_counter() - batch_started) * 1000, 1)
    log(
        "F3_RECHECK_BATCH_TIMING",
        level="DEBUG",
        requested_count=len(tickers),
        completed_count=len(recheck_rows),
        elapsed_ms=batch_elapsed_ms,
        timeout_sec=F3_RECHECK_BATCH_TIMEOUT_SEC,
        source=recheck_source,
    )
    if cash is None:
        s.day_skip = True
        s.close_reason = "BALANCE_QUERY_FAILED"
        s.target_ticker = None
        s.target_name = None
        await _alert_balance_query_failed(tickers[0] if tickers else None, tickers)
        return None
    total_amount = int(cash * ALLOC_RATIO)

    for row in recheck_rows:
        rank = int(row["rank"])
        ticker = str(row["ticker"])
        candidate = row["candidate"]
        expected_price = float(row["expected_price"] or 0)
        prev_close = float(row["prev_close"] or 0)
        if not expected_price or prev_close <= 0:
            blocked_reasons.append("GAP_RECHECK_UNAVAILABLE")
            _log_entry_blocked(
                ticker,
                "GAP_RECHECK_UNAVAILABLE",
                candidate_rank=rank,
                expected_price=expected_price,
                prev_close=prev_close,
                cash=cash,
            )
            log(
                "GAP_RECHECK_UNAVAILABLE",
                level="WARN",
                ticker=ticker,
                candidate_rank=rank,
                expected_price=expected_price,
                prev_close=prev_close,
                reason="MISSING_PREV_CLOSE" if prev_close <= 0 else "MISSING_EXPECTED_PRICE",
            )
            continue

        gap = (expected_price / prev_close) - 1
        log(
            "F3_RECHECK",
            level="INFO",
            ticker=ticker,
            candidate_rank=rank,
            expected_price=expected_price,
            prev_close=prev_close,
            gap_pct=round(gap * 100, 2),
            gap_min_pct=round(GAP_MIN_RECHECK * 100, 2),
            gap_max_pct=round(GAP_MAX_ORDER * 100, 2),
        )
        cand_amount = _candidate_expected_amount(candidate)
        gap_allowed, gap_reason = _evaluate_order_gap(gap, cand_amount)
        if not gap_allowed:
            block_reason = (
                "HIGH_GAP_AMOUNT_LOW" if gap_reason == "HIGH_GAP_AMOUNT_LOW" else "GAP_CHANGED"
            )
            blocked_reasons.append(block_reason)
            log(
                block_reason,
                level="INFO",
                ticker=ticker,
                candidate_rank=rank,
                gap_at_lockup=None,
                gap_at_entry=round(gap * 100, 2),
                reason=gap_reason,
                expected_amount=cand_amount,
                threshold=HIGH_GAP_MIN_EXPECTED_AMOUNT,
            )
            _log_entry_blocked(
                ticker,
                block_reason,
                level="INFO",
                candidate_rank=rank,
                gap_at_entry=round(gap * 100, 2),
                gap_min_pct=round(GAP_MIN_RECHECK * 100, 2),
                gap_max_pct=round(GAP_MAX_ORDER * 100, 2),
                high_gap_band_pct=round(GAP_HIGH_BAND * 100, 2),
                expected_amount=cand_amount,
                threshold=HIGH_GAP_MIN_EXPECTED_AMOUNT,
                gap_reason=gap_reason,
            )
            continue
        total_qty = int(total_amount / expected_price)
        if total_qty == 0:
            blocked_reasons.append("INSUFFICIENT_BALANCE")
            _log_entry_blocked(
                ticker,
                "QTY_ZERO",
                level="INFO",
                candidate_rank=rank,
                cash=cash,
                alloc_ratio=ALLOC_RATIO,
                order_price=expected_price,
                total_amount=total_amount,
            )
            continue

        valid.append(
            {
                "ticker": ticker,
                "candidate": candidate,
                "candidate_rank": rank,
                "expected_price": expected_price,
                "prev_close": prev_close,
                "cash": cash,
                "total_amount": total_amount,
                "total_qty": total_qty,
            }
        )

    if not valid:
        reason = blocked_reasons[-1] if blocked_reasons else "NO_ENTRY_CANDIDATE"
        s.day_skip = True
        s.close_reason = reason
        s.target_ticker = None
        s.target_name = None
        alert_event = "GAP_CHANGED" if reason == "GAP_CHANGED" else "ENTRY_FAIL"
        alert_ticker = tickers[0] if tickers else None
        _log_entry_blocked(
            alert_ticker,
            reason,
            candidates=tickers,
            checked_count=len(recheck_rows),
            blocked_count=len(blocked_reasons),
            terminal=True,
        )
        await notifier.send(
            alert_event,
            level="WARN",
            message=f"F3 후보 전체가 주문 전 재검증에서 제외되었습니다. 사유={reason}",
            ticker=alert_ticker,
        )
        await db.record_skip(
            _today(),
            "ENTRY_FAIL" if reason != "GAP_CHANGED" else "GAP_CHANGED",
            f"reason={reason},candidates={','.join(tickers)}",
        )
        return None

    ranked = sorted(
        valid,
        key=lambda item: (
            item["candidate"].get("expected_amount", 0.0),
            item["candidate"].get("buy_sell_ratio", 0.0),
            -item["candidate_rank"],
        ),
        reverse=True,
    )
    picked = ranked[0]
    log(
        "F3_FINAL_PICK",
        level="INFO",
        ticker=picked["ticker"],
        name=picked["candidate"].get("name"),
        candidate_rank=picked["candidate_rank"],
        checked_count=len(tickers),
        valid_count=len(valid),
        candidates=tickers,
        expected_price=picked["expected_price"],
        total_qty=picked["total_qty"],
    )
    return ranked


async def _pick_final_entry_candidate(
    s: state.State,
    exclude_tickers: set[str] | None = None,
) -> dict | None:
    ranked = await _rank_final_entry_candidates(s, exclude_tickers=exclude_tickers)
    return ranked[0] if ranked else None


async def _refresh_entry_candidate(picked: dict) -> dict | None:
    ticker = str(picked["ticker"])
    candidate = picked.get("candidate")
    if not isinstance(candidate, dict):
        candidate = None
    fallback_prev_close = _candidate_prev_close(candidate)
    expected_price, prev_close = await _fetch_expected_price(
        ticker,
        fallback_prev_close=fallback_prev_close,
    )
    if prev_close <= 0:
        prev_close = fallback_prev_close
    if not expected_price or prev_close <= 0:
        _log_entry_blocked(
            ticker,
            "GAP_RECHECK_UNAVAILABLE",
            candidate_rank=picked.get("candidate_rank"),
            expected_price=expected_price,
            prev_close=prev_close,
            freshness_check=True,
        )
        log(
            "GAP_RECHECK_UNAVAILABLE",
            level="WARN",
            ticker=ticker,
            candidate_rank=picked.get("candidate_rank"),
            expected_price=expected_price,
            prev_close=prev_close,
            freshness_check=True,
            reason="MISSING_PREV_CLOSE" if prev_close <= 0 else "MISSING_EXPECTED_PRICE",
        )
        return None

    gap = (expected_price / prev_close) - 1
    log(
        "F3_RECHECK",
        level="INFO",
        ticker=ticker,
        candidate_rank=picked.get("candidate_rank"),
        expected_price=expected_price,
        prev_close=prev_close,
        gap_pct=round(gap * 100, 2),
        gap_min_pct=round(GAP_MIN_RECHECK * 100, 2),
        gap_max_pct=round(GAP_MAX_ORDER * 100, 2),
        freshness_check=True,
    )
    cand_amount = _candidate_expected_amount(candidate)
    gap_allowed, gap_reason = _evaluate_order_gap(gap, cand_amount)
    if not gap_allowed:
        block_reason = (
            "HIGH_GAP_AMOUNT_LOW" if gap_reason == "HIGH_GAP_AMOUNT_LOW" else "GAP_CHANGED"
        )
        log(
            block_reason,
            level="INFO",
            ticker=ticker,
            candidate_rank=picked.get("candidate_rank"),
            gap_at_lockup=None,
            gap_at_entry=round(gap * 100, 2),
            reason=gap_reason,
            expected_amount=cand_amount,
            threshold=HIGH_GAP_MIN_EXPECTED_AMOUNT,
            freshness_check=True,
        )
        _log_entry_blocked(
            ticker,
            block_reason,
            level="INFO",
            candidate_rank=picked.get("candidate_rank"),
            gap_at_entry=round(gap * 100, 2),
            gap_min_pct=round(GAP_MIN_RECHECK * 100, 2),
            gap_max_pct=round(GAP_MAX_ORDER * 100, 2),
            high_gap_band_pct=round(GAP_HIGH_BAND * 100, 2),
            expected_amount=cand_amount,
            threshold=HIGH_GAP_MIN_EXPECTED_AMOUNT,
            gap_reason=gap_reason,
            freshness_check=True,
        )
        return None

    total_amount = int(picked["total_amount"])
    total_qty = int(total_amount / expected_price)
    if total_qty == 0:
        _log_entry_blocked(
            ticker,
            "QTY_ZERO",
            level="INFO",
            candidate_rank=picked.get("candidate_rank"),
            cash=picked.get("cash"),
            alloc_ratio=ALLOC_RATIO,
            order_price=expected_price,
            total_amount=total_amount,
            freshness_check=True,
        )
        return None

    refreshed = dict(picked)
    refreshed.update(
        {
            "expected_price": expected_price,
            "prev_close": prev_close,
            "total_qty": total_qty,
        }
    )
    return refreshed


def _today() -> str:
    return datetime.now(KST).strftime("%Y%m%d")


def _log_entry_blocked(
    ticker: str | None,
    reason: str,
    *,
    level: str = "WARN",
    **extra: object,
) -> None:
    log(
        "F3_ENTRY_BLOCKED",
        level=level,
        ticker=ticker,
        reason=reason,
        **extra,
    )


async def _sleep_until(h: int, m: int, s: int) -> None:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return
    now = datetime.now(KST)
    target = now.replace(hour=h, minute=m, second=s, microsecond=0)
    delta = (target - now).total_seconds()
    if delta > 0:
        await asyncio.sleep(delta)


async def _pre_order_quiet_wait(
    ticker: str,
    attempt: int,
    max_attempts: int,
    order_price: float | None,
    order_qty: int,
    *,
    phase: str = "ENTRY",
) -> None:
    if F3_PRE_ORDER_QUIET_SEC <= 0:
        return
    log(
        "ENTRY_PRE_ORDER_WAIT",
        level="INFO",
        ticker=ticker,
        phase=phase,
        sleep_sec=F3_PRE_ORDER_QUIET_SEC,
        order_price=order_price,
        order_qty=order_qty,
        entry_attempt=attempt,
        max_attempts=max_attempts,
    )
    await asyncio.sleep(F3_PRE_ORDER_QUIET_SEC)


def _parse_deadline(value: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    try:
        h, m, s = [int(part) for part in value.split(":")]
        return h, m, s
    except (ValueError, AttributeError) as exc:
        log(
            "F3_DEADLINE_PARSE_ERROR",
            level="WARN",
            value=str(value),
            default=f"{default[0]:02d}:{default[1]:02d}:{default[2]:02d}",
            error=repr(exc),
        )
        return default


def _deadline_datetime(deadline: tuple[int, int, int]) -> datetime:
    h, m, s = deadline
    return datetime.now(KST).replace(hour=h, minute=m, second=s, microsecond=0)


def _entry_retry_deadline() -> tuple[int, int, int]:
    return _parse_deadline(F3_ENTRY_RETRY_DEADLINE, (9, 11, 0))


def _pyramid_at() -> tuple[int, int, int]:
    return _parse_deadline(F3_PYRAMID_AT, (9, 10, 40))


def _before_deadline(deadline: tuple[int, int, int]) -> bool:
    return datetime.now(KST) < _deadline_datetime(deadline)


def _deadline_dt_after_seconds(seconds: float) -> datetime:
    """정밀 절대 마감(datetime). 초 단위 절삭 없이 체결 폴링 예산을 보존한다."""
    return datetime.now(KST) + timedelta(seconds=seconds)


async def _run_dry_entry(ticker: str) -> None:
    expected_price = float(os.getenv("DRY_RUN_EXPECTED_PRICE", "10300"))
    fill_price = float(os.getenv("DRY_RUN_ENTRY_PRICE", str(expected_price)))
    fill_qty = int(os.getenv("DRY_RUN_ENTRY_QTY", "10"))
    order_id = f"DRY-{datetime.now(KST).strftime('%H%M%S')}"

    if not await state.set_entering():
        log("DRY_RUN_F3_SKIPPED", level="WARN", ticker=ticker, reason="STATE_NOT_IDLE")
        await db.record_skip(_today(), "DRY_RUN_F3_SKIPPED", "reason=STATE_NOT_IDLE")
        return

    await asyncio.sleep(float(os.getenv("DRY_RUN_STEP_DELAY", "0.2")))
    await state.set_holding(fill_price, fill_qty, order_id)
    await state.persist(os.getenv("STATE_DIR", "data/state"), _today())

    log(
        "DRY_RUN_ENTRY_EXECUTED",
        level="WARN",
        ticker=ticker,
        order_id=order_id,
        order_price=expected_price,
        order_qty=fill_qty,
        fill_price=fill_price,
        fill_qty=fill_qty,
        fill_latency_ms=0,
    )


async def _fetch_expected_price(
    ticker: str,
    fallback_prev_close: float = 0.0,
) -> tuple[float, float]:
    """Return expected price and previous close. Before open, prefer antc_cnpr.

    An opening-transition stale quote (antc<=0, stck_oprc<=0, current==prev close)
    is retried within a hard wall-clock budget that bounds every ``kis_rest.get``
    call and sleep. On exhaustion returns ``(0.0, prev_close)`` so callers emit
    GAP_RECHECK_UNAVAILABLE rather than a false 0% GAP_CHANGED. External
    ``CancelledError`` is never swallowed.
    """
    last_prev_close = fallback_prev_close
    budget_deadline = (
        time.monotonic() + F3_RECHECK_TOTAL_BUDGET_SEC if F3_RECHECK_TOTAL_BUDGET_SEC > 0 else None
    )
    for attempt in range(1, F3_RECHECK_MAX_ATTEMPTS + 1):
        remaining = None
        if budget_deadline is not None:
            remaining = budget_deadline - time.monotonic()
            if remaining <= 0:
                log(
                    "F3_RECHECK_QUOTE_BUDGET_EXHAUSTED",
                    level="WARN",
                    ticker=ticker,
                    attempt=attempt,
                    max_attempts=F3_RECHECK_MAX_ATTEMPTS,
                    budget_sec=F3_RECHECK_TOTAL_BUDGET_SEC,
                )
                break
        get_coro = kis_rest.get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id="FHKST01010100",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
            request_priority=kis_rest.REQUEST_PRIORITY_CRITICAL,
        )
        try:
            if remaining is not None:
                resp = await asyncio.wait_for(get_coro, timeout=remaining)
            else:
                resp = await get_coro
        except asyncio.TimeoutError:
            # Budget elapsed mid-call — treat as unavailable, do not retry.
            log(
                "F3_RECHECK_QUOTE_TIMEOUT",
                level="WARN",
                ticker=ticker,
                attempt=attempt,
                max_attempts=F3_RECHECK_MAX_ATTEMPTS,
                budget_sec=F3_RECHECK_TOTAL_BUDGET_SEC,
            )
            break
        out = resp.get("output", {}) if isinstance(resp.get("output"), dict) else {}
        antc_price = float(out.get("antc_cnpr") or 0)
        current_price = float(out.get("stck_prpr") or 0)
        open_price = float(out.get("stck_oprc") or 0)
        prev_close = float(out.get("stck_prdy_clpr") or 0)
        effective_prev_close = prev_close if prev_close > 0 else fallback_prev_close
        last_prev_close = effective_prev_close

        if antc_price > 0:
            expected, source, is_stale = antc_price, "antc_cnpr", False
        elif current_price <= 0 or effective_prev_close <= 0:
            expected, source, is_stale = current_price, "stck_prpr", True
        elif open_price > 0 or current_price != effective_prev_close:
            # Market-open evidence, or price already moved off prev close → valid.
            expected, source, is_stale = current_price, "stck_prpr", False
        else:
            # antc<=0 and open<=0 and current==prev: opening-transition stale.
            expected, source, is_stale = current_price, "stck_prpr", True

        log(
            "F3_RECHECK_QUOTE_FIELDS",
            level="DEBUG",
            ticker=ticker,
            attempt=attempt,
            antc_cnpr=antc_price,
            stck_prpr=current_price,
            stck_oprc=open_price,
            selected_price=expected,
            selected_source=source,
            prev_close=effective_prev_close,
            is_stale=is_stale,
            rt_cd=resp.get("rt_cd"),
            msg_cd=resp.get("msg_cd"),
        )
        if not is_stale and expected and effective_prev_close > 0:
            if attempt > 1:
                log(
                    "F3_RECHECK_QUOTE_RECOVERED",
                    level="INFO",
                    ticker=ticker,
                    attempt=attempt,
                    max_attempts=F3_RECHECK_MAX_ATTEMPTS,
                    expected_price=expected,
                    prev_close=effective_prev_close,
                )
            return expected, effective_prev_close
        if attempt >= F3_RECHECK_MAX_ATTEMPTS:
            break
        sleep_for = F3_RECHECK_RETRY_DELAY_SEC
        if budget_deadline is not None:
            remaining = budget_deadline - time.monotonic()
            if remaining <= 0:
                break
            sleep_for = min(F3_RECHECK_RETRY_DELAY_SEC, remaining)
        log(
            "F3_RECHECK_QUOTE_RETRY",
            level="WARN",
            ticker=ticker,
            attempt=attempt,
            max_attempts=F3_RECHECK_MAX_ATTEMPTS,
            retry_after_sec=sleep_for,
            reason="OPENING_TRANSITION_STALE",
            expected_price=expected,
            prev_close=effective_prev_close,
            rt_cd=resp.get("rt_cd"),
            msg_cd=resp.get("msg_cd"),
            msg1=resp.get("msg1"),
        )
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
    # Stale / missing / timed-out after exhaustion → unavailable (not a real gap).
    return 0.0, last_prev_close


async def _fetch_current_price(ticker: str) -> float:
    """현재 체결가 반환."""
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        tr_id="FHKST01010100",
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        request_priority=kis_rest.REQUEST_PRIORITY_CRITICAL,
    )
    return float(resp.get("output", {}).get("stck_prpr") or 0)


async def _fetch_final_entry_quote(ticker: str) -> EntryQuote | None:
    """주문 직전 최우선 매도호가를 조회한다. 누락 시 fail-closed."""
    started = time.monotonic()
    try:
        resp = await kis_rest.get(
            "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            tr_id="FHKST01010200",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
            request_priority=kis_rest.REQUEST_PRIORITY_CRITICAL,
        )
    except Exception as exc:
        log(
            "F3_FINAL_QUOTE_ERROR",
            level="WARN",
            ticker=ticker,
            error=repr(exc),
        )
        return None

    out1 = resp.get("output1", {})
    if isinstance(out1, list):
        out1 = out1[0] if out1 else {}
    if not isinstance(out1, dict):
        out1 = {}
    out2 = resp.get("output2", {})
    if isinstance(out2, list):
        out2 = out2[0] if out2 else {}
    if not isinstance(out2, dict):
        out2 = {}

    ask_price = float(out1.get("askp1") or 0)
    ask_qty = int(float(out1.get("askp_rsqn1") or 0))
    antc_price = float(out2.get("antc_cnpr") or 0)
    elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
    log(
        "F3_FINAL_QUOTE",
        level="INFO" if ask_price > 0 else "WARN",
        ticker=ticker,
        ask_price=ask_price,
        ask_qty=ask_qty,
        antc_price=antc_price,
        elapsed_ms=elapsed_ms,
        quote_max_age_ms=F3_FINAL_QUOTE_MAX_AGE_MS,
        quote_max_age_configured_ms=_F3_FINAL_QUOTE_MAX_AGE_MS_CONFIGURED,
        rt_cd=resp.get("rt_cd"),
        msg_cd=resp.get("msg_cd"),
        msg1=resp.get("msg1"),
    )
    if str(resp.get("rt_cd", "0")) != "0" or ask_price <= 0:
        return None
    return EntryQuote(
        ask_price=ask_price,
        ask_qty=ask_qty,
        antc_price=antc_price,
        fetched_monotonic=time.monotonic(),
        rt_cd=str(resp.get("rt_cd") or ""),
        msg_cd=str(resp.get("msg_cd") or ""),
        msg1=str(resp.get("msg1") or ""),
    )


async def _fetch_available_cash() -> float | None:
    """잔고 요약 기반 1차 매수 예산 반환. 조회 실패 시 None.

    비문서 확장 필드 ord_psbl_cash 우선, 부재 시 dnca_tot_amt와
    prvs_rcdl_excc_amt(가수도정산금액) 중 큰 값을 1차 예산으로 사용한다.
    오류 응답(호출 제한 포함)은 백오프 후 재시도하고, 끝내 실패하면
    실제 잔고 부족(0원)과 구분되도록 None을 반환한다.
    """
    mode = os.getenv("KIS_MODE", "PAPER")
    output2 = None
    for attempt in range(1, BALANCE_QUERY_MAX_ATTEMPTS + 1):
        resp = None
        error = None
        try:
            resp = await kis_rest.get(
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                tr_id=_BAL_TR[mode],
                params=kis_rest.balance_inquiry_params(),
                request_priority=kis_rest.REQUEST_PRIORITY_CRITICAL,
            )
        except Exception as exc:
            error = exc
        if error is not None:
            error_reason = "EXCEPTION"
        elif str(resp.get("rt_cd", "0")) == "0":
            output2 = resp.get("output2")
            if isinstance(output2, list) and output2 and isinstance(output2[0], dict):
                break
            error_reason = "MISSING_OUTPUT2"
        else:
            error_reason = "ERROR_RESPONSE"
        log(
            "BALANCE_QUERY_ERROR",
            level="WARN",
            reason=error_reason,
            attempt=attempt,
            max_attempts=BALANCE_QUERY_MAX_ATTEMPTS,
            error=repr(error) if error is not None else None,
            rt_cd=resp.get("rt_cd") if resp else None,
            msg_cd=resp.get("msg_cd") if resp else None,
            msg1=resp.get("msg1") if resp else None,
        )
        if attempt >= BALANCE_QUERY_MAX_ATTEMPTS:
            return None
        await asyncio.sleep(BALANCE_QUERY_RETRY_DELAY_SEC)

    summary = output2[0]
    ord_psbl_present = (
        "ord_psbl_cash" in summary and str(summary.get("ord_psbl_cash", "")).strip() != ""
    )
    ord_psbl_cash = to_float(summary.get("ord_psbl_cash"))
    dnca_tot_amt = to_float(summary.get("dnca_tot_amt"))
    prvs_rcdl_excc_amt = to_float(summary.get("prvs_rcdl_excc_amt"))
    cash_source = "ord_psbl_cash"
    cash = ord_psbl_cash
    if not ord_psbl_present:
        # 예수금보다 가수도정산금액(prvs_rcdl_excc_amt)이 크면 1차 예산으로 사용한다.
        # 과대평가되더라도 주문 직전 종목별 매수가능조회(nrcvb_buy_qty)가 상한을 재적용한다.
        if prvs_rcdl_excc_amt > dnca_tot_amt:
            cash = prvs_rcdl_excc_amt
            cash_source = "prvs_rcdl_excc_amt"
        else:
            cash = dnca_tot_amt
            cash_source = "dnca_tot_amt"

    log(
        "BALANCE_CASH_CHECK",
        level="DEBUG",
        cash=cash,
        cash_source=cash_source,
        ord_psbl_cash=ord_psbl_cash,
        ord_psbl_present=ord_psbl_present,
        dnca_tot_amt=dnca_tot_amt,
        prvs_rcdl_excc_amt=prvs_rcdl_excc_amt,
    )
    return cash


async def _fetch_buyable_qty(ticker: str, mode: str) -> dict:
    """종목별 시장가 매수가능수량 조회 [v1_국내주식-007]."""
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
        tr_id=_BUY_PSBL_TR[mode],
        params={
            "CANO": kis_rest.account_no(),
            "ACNT_PRDT_CD": kis_rest.account_cd(),
            "PDNO": ticker,
            "ORD_UNPR": "",
            "ORD_DVSN": "01",
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN": "N",
        },
        request_priority=kis_rest.REQUEST_PRIORITY_CRITICAL,
    )
    if str(resp.get("rt_cd", "0")) != "0":
        log(
            "BUYABLE_QTY_ERROR",
            level="WARN",
            ticker=ticker,
            rt_cd=resp.get("rt_cd"),
            msg_cd=resp.get("msg_cd"),
            msg1=resp.get("msg1"),
        )
        return {
            "query_failed": True,
            "nrcvb_buy_qty": 0,
            "nrcvb_buy_amt": 0.0,
            "max_buy_qty": 0,
            "max_buy_amt": 0.0,
            "ord_psbl_cash": 0.0,
            "rt_cd": resp.get("rt_cd"),
            "msg_cd": resp.get("msg_cd"),
            "msg1": resp.get("msg1"),
        }

    out = resp.get("output", {}) if isinstance(resp.get("output"), dict) else {}
    result = {
        "query_failed": False,
        "nrcvb_buy_qty": int(to_float(out.get("nrcvb_buy_qty"))),
        "nrcvb_buy_amt": to_float(out.get("nrcvb_buy_amt")),
        "max_buy_qty": int(to_float(out.get("max_buy_qty"))),
        "max_buy_amt": to_float(out.get("max_buy_amt")),
        "ord_psbl_cash": to_float(out.get("ord_psbl_cash")),
    }
    log("BUYABLE_QTY_CHECK", level="DEBUG", ticker=ticker, **result)
    return result


async def _send_buy(
    ticker: str,
    qty: int,
    mode: str,
    *,
    limit_price: float,
    send_guard: Callable[[], bool] | None = None,
) -> dict:
    """양수 제출가가 필수인 지정가 매수."""
    if limit_price <= 0:
        raise ValueError("limit_price must be positive for buy orders")
    return await kis_rest.post(
        "/uapi/domestic-stock/v1/trading/order-cash",
        tr_id=_BUY_TR[mode],
        send_guard=send_guard,
        body={
            "CANO": kis_rest.account_no(),
            "ACNT_PRDT_CD": kis_rest.account_cd(),
            "PDNO": ticker,
            "ORD_DVSN": "00",
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(int(limit_price)),
        },
    )


async def _send_sell(ticker: str, qty: int, mode: str) -> dict:
    """시장가 매도 주문 (ORD_DVSN=01)."""
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
    )


async def _cancel_order(order_id: str, org_no: str, mode: str) -> dict:
    """주문 전량 취소 (RVSE_CNCL_DVSN_CD=02)."""
    return await kis_rest.post(
        "/uapi/domestic-stock/v1/trading/order-rvsecncl",
        tr_id=_CANCEL_TR[mode],
        body={
            "CANO": kis_rest.account_no(),
            "ACNT_PRDT_CD": kis_rest.account_cd(),
            "KRX_FWDG_ORD_ORGNO": org_no,
            "ORGN_ODNO": order_id,
            # KIS 정정취소 공식 예제의 주문구분(00)과 필수 거래소 코드를 사용한다.
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": "0",
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
            "EXCG_ID_DVSN_CD": "KRX",
        },
    )


async def _poll_fill(
    order_id: str,
    deadline: datetime,
    ticker: str | None = None,
    expected_qty: int | None = None,
) -> dict | None:
    """전량체결까지 폴링하고, 마감 시 확인된 부분체결을 반환한다.

    정밀 절대 마감(datetime)을 쓰고, 대기는 min(간격, 남은 예산)로 적응한다.
    마감에 도달/초과한 뒤에는 새 체결조회를 시작하지 않는다(주문 노출창 미연장).
    """
    global _last_fill_poll_summary
    attempts = 0
    latest_fill: dict | None = None
    _last_fill_poll_summary = {
        "poll_attempts": 0,
        "poll_deadline": deadline.strftime("%H:%M:%S"),
        "poll_last_rt_cd": None,
        "poll_last_msg_cd": None,
        "poll_last_msg1": None,
        "poll_last_output_count": 0,
        "poll_last_matched": False,
        "poll_last_ccld_qty": 0,
        "poll_last_ccld_amt": 0.0,
        "poll_last_error": None,
    }
    while True:
        if datetime.now(KST) >= deadline:
            log(
                "ENTRY_FILL_POLL_TIMEOUT",
                level="WARN",
                ticker=ticker,
                order_id=order_id,
                **_last_fill_poll_summary,
            )
            if latest_fill and expected_qty is None:
                return {
                    "fill_price": latest_fill["fill_price"],
                    "fill_qty": latest_fill["fill_qty"],
                }
            return latest_fill
        try:
            attempts += 1
            remaining = (deadline - datetime.now(KST)).total_seconds()
            latest = await asyncio.wait_for(
                _fetch_order_fill_snapshot(
                    order_id,
                    ticker=ticker,
                    expected_qty=expected_qty,
                    update_poll_summary=True,
                ),
                timeout=max(0.001, remaining),
            )
            _last_fill_poll_summary["poll_attempts"] = attempts
            latest_fill = _more_complete_fill(latest_fill, latest)
            if latest_fill:
                fill_qty = int(latest_fill.get("fill_qty") or 0)
                if expected_qty is None or fill_qty >= expected_qty:
                    if expected_qty is None:
                        return {
                            "fill_price": latest_fill["fill_price"],
                            "fill_qty": fill_qty,
                        }
                    return latest_fill
                log(
                    "ENTRY_PARTIAL_FILL",
                    level="INFO",
                    ticker=ticker,
                    order_id=order_id,
                    order_qty=expected_qty,
                    fill_qty=fill_qty,
                    remaining_qty=max(0, expected_qty - fill_qty),
                    fill_price=latest_fill.get("fill_price"),
                )
        except asyncio.TimeoutError:
            _last_fill_poll_summary.update(
                {
                    "poll_attempts": attempts,
                    "poll_last_error": "DEADLINE_TIMEOUT",
                }
            )
            log(
                "ENTRY_FILL_POLL_TIMEOUT",
                level="WARN",
                ticker=ticker,
                order_id=order_id,
                **_last_fill_poll_summary,
            )
            return latest_fill
        except Exception as exc:
            _last_fill_poll_summary.update(
                {
                    "poll_attempts": attempts,
                    "poll_last_error": str(exc)[:160],
                }
            )
        # 남은 예산만큼만 적응형으로 대기한다. 예산이 없으면 즉시 루프 상단으로
        # 돌아가 마감 처리 — 마감 이후 새 조회를 시작하지 않는다.
        remaining = (deadline - datetime.now(KST)).total_seconds()
        if remaining <= 0:
            continue
        await asyncio.sleep(min(F3_FILL_POLL_INTERVAL_SEC, remaining))


async def _fetch_order_fill_snapshot(
    order_id: str,
    *,
    ticker: str | None = None,
    expected_qty: int | None = None,
    update_poll_summary: bool = False,
) -> dict | None:
    """KIS 주문행 한 번 조회. 누적체결과 잔량을 일관된 형태로 반환한다."""
    mode = os.getenv("KIS_MODE", "PAPER")
    today = datetime.now(KST).strftime("%Y%m%d")
    resp = await kis_rest.get(
        "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
        tr_id=_CCLD_TR[mode],
        params={
            "CANO": kis_rest.account_no(),
            "ACNT_PRDT_CD": kis_rest.account_cd(),
            "INQR_STRT_DT": today,
            "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00",
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
    rows = resp.get("output1", []) or []
    if isinstance(rows, dict):
        rows = [rows]
    if update_poll_summary:
        _last_fill_poll_summary.update(
            {
                "poll_last_rt_cd": resp.get("rt_cd"),
                "poll_last_msg_cd": resp.get("msg_cd"),
                "poll_last_msg1": resp.get("msg1"),
                "poll_last_output_count": len(rows),
                "poll_last_matched": False,
                "poll_last_error": None,
            }
        )
    for item in rows:
        if str(item.get("odno") or "") != str(order_id):
            continue
        order_qty = int(float(item.get("ord_qty") or item.get("tot_ord_qty") or expected_qty or 0))
        fill_qty = int(float(item.get("tot_ccld_qty") or 0))
        remaining_qty = int(float(item.get("rmn_qty") or max(0, order_qty - fill_qty)))
        total_amount = float(item.get("tot_ccld_amt") or 0)
        avg_price = float(item.get("avg_prvs") or 0)
        if fill_qty > 0 and total_amount > 0:
            avg_price = round(total_amount / fill_qty)
        status: Literal["UNFILLED", "PARTIAL", "FILLED"]
        if fill_qty <= 0:
            status = "UNFILLED"
        elif remaining_qty <= 0 or (order_qty > 0 and fill_qty >= order_qty):
            status = "FILLED"
        else:
            status = "PARTIAL"
        snapshot = FillSnapshot(
            status=status,
            order_qty=order_qty,
            fill_qty=fill_qty,
            remaining_qty=max(0, remaining_qty),
            fill_price=avg_price,
        )
        if update_poll_summary:
            _last_fill_poll_summary.update(
                {
                    "poll_last_matched": True,
                    "poll_last_ccld_qty": fill_qty,
                    "poll_last_ccld_amt": total_amount,
                }
            )
        return snapshot.as_fill() if fill_qty > 0 else None
    return None
