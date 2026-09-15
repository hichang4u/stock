import asyncio
import dataclasses
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src import live

KST = ZoneInfo("Asia/Seoul")


@dataclass
class State:
    trading_date: str | None = None
    target_ticker: str | None = None
    target_name: str | None = None
    # 그날 처음 잠긴 1순위. F3의 후보 교체·소진은 target_ticker를 바꾸거나
    # 지우지만 이 값은 건드리지 않아, A가 진입하지 않은 날에도 F4가 15:20까지
    # 이 종목의 가격 경로를 찍는다(2026-09-10/15 소진일 캡처 0바이트 대응).
    # 매매 판단은 이 값을 보지 않는다 — 관측 전용.
    observe_ticker: str | None = None
    target_candidates: list[dict] | None = None
    entry_price: float | None = None
    entry_at: str | None = None
    entry_qty: int | None = None
    remaining_qty: int | None = None
    high_price: float | None = None
    position_status: str = "IDLE"       # IDLE | ENTERING | HOLDING | EXITING | CLOSED
    close_reason: str | None = None     # TRAILING | HARD_STOP | TIMEOUT
                                        # ENTRY_FAIL | SLIPPAGE_GUARD | GAP_CHANGED
                                        # VI_ACTIVE
    order_id: str | None = None
    trailing_active: bool = False
    highest_step: float = 0.0           # 마지막으로 통과한 이익 스텝 (0.025 단위, 예: 0.075)
    trade_id: int = 0                   # DB trades.id (0 = 미기록)
    daily_pnl_pct: float = 0.0
    day_skip: bool = False
    pending_entry: dict | None = None   # 접수 후 체결/취소 대조 전인 매수 주문
    pending_exit: dict | None = None    # 전송 전 의도부터 체결 대조까지의 매도 주문
    post_close_tracking_stopped: bool = False  # 매도 후 가격 관측 수동 종료


_state = State()
_lock = asyncio.Lock()


def get() -> State:
    return _state


@dataclass
class TrackState:
    """트랙 A 이외 트랙의 포지션 상태.

    trading_date·target_ticker·target_candidates·day_skip은 갖지 않는다 —
    종목과 후보는 F1/F2가 정하는 트랙 공유 자산이다(§3.1).
    """
    entry_price: float | None = None
    entry_at: str | None = None
    entry_qty: int | None = None
    remaining_qty: int | None = None
    high_price: float | None = None
    position_status: str = "IDLE"
    close_reason: str | None = None
    order_id: str | None = None
    trade_id: int = 0
    pending_entry: dict | None = None
    pending_exit: dict | None = None


_tracks: dict[str, TrackState] = {}


def track(name: str) -> TrackState:
    """트랙 상태를 반환한다. 없으면 IDLE로 만든다."""
    return _tracks.setdefault(name, TrackState())


def all_tracks() -> dict[str, TrackState]:
    """감사·UI용 순회. 트랙 A는 get()이며 여기 포함되지 않는다.

    dict만 새로 만든 얕은 복사다. **값은 살아 있는 TrackState 객체**이므로
    필드를 고치면 실제 트랙 상태가 바뀐다. 읽기 전용으로만 쓸 것.
    깊은 복사를 하지 않는 것은 의도다 — 감사·UI 화면이 실제 상태와 조용히
    어긋난 스냅샷을 보여주는 쪽이 더 나쁘다.
    """
    return dict(_tracks)


def _clear_for_trading_day(date_str: str) -> None:
    live.clear_tick_history()
    _state.trading_date = date_str
    _state.target_ticker = None
    _state.target_name = None
    _state.observe_ticker = None
    _state.target_candidates = None
    _state.entry_price = None
    _state.entry_at = None
    _state.entry_qty = None
    _state.remaining_qty = None
    _state.high_price = None
    _state.position_status = "IDLE"
    _state.close_reason = None
    _state.order_id = None
    _state.trailing_active = False
    _state.highest_step = 0.0
    _state.trade_id = 0
    _state.daily_pnl_pct = 0.0
    _state.day_skip = False
    _state.pending_entry = None
    _state.pending_exit = None
    _state.post_close_tracking_stopped = False
    _tracks.clear()


async def ensure_trading_day(date_str: str) -> bool:
    """Reset in-memory daily state when a new trading date starts."""
    async with _lock:
        if _state.trading_date == date_str:
            return False
        if _state.position_status in {"ENTERING", "HOLDING", "EXITING"}:
            return False
        _clear_for_trading_day(date_str)
        return True


async def reset_stale_entering_for_trading_day(date_str: str) -> bool:
    """Reset a prior-day ENTERING state after the caller verified no holding.

    The normal daily reset intentionally preserves active states.  This
    narrower escape hatch only accepts an ENTERING state from another date
    when there is no persisted pending-order identity.  The caller must first
    reconcile the broker balance; HOLDING/EXITING and identified pending
    orders remain protected.
    """
    async with _lock:
        if _state.trading_date == date_str:
            return False
        if _state.position_status != "ENTERING" or _state.pending_entry is not None:
            return False
        _clear_for_trading_day(date_str)
        return True


async def reset_stale_active_for_trading_day(date_str: str) -> bool:
    """Reset a prior-day active state after broker-confirmed zero holdings.

    Unlike ``ensure_trading_day``, this accepts ENTERING/HOLDING/EXITING.
    It must only be called after the complete paginated balance response has
    confirmed that the state's ticker is not held.
    """
    async with _lock:
        if _state.trading_date == date_str:
            return False
        if _state.position_status not in {"ENTERING", "HOLDING", "EXITING"}:
            return False
        _clear_for_trading_day(date_str)
        return True


# ── 상태 전이 (atomic) ────────────────────────────────────────────────

async def set_entering() -> bool:
    """IDLE → ENTERING. 성공 시 True, 이미 전이 불가 상태면 False."""
    async with _lock:
        if _state.position_status != "IDLE":
            return False
        _state.position_status = "ENTERING"
        return True


async def set_holding(entry_price: float, entry_qty: int, order_id: str) -> None:
    """ENTERING → HOLDING. F3 1차 체결 확인 후 호출."""
    async with _lock:
        _state.entry_price = entry_price
        _state.entry_at = datetime.now(KST).isoformat()
        _state.entry_qty = entry_qty
        _state.remaining_qty = entry_qty
        _state.high_price = entry_price
        _state.position_status = "HOLDING"
        _state.order_id = order_id
        _state.trailing_active = False
        _state.highest_step = 0.0
        _state.trade_id = 0
        _state.pending_entry = None
        _state.pending_exit = None
        _state.post_close_tracking_stopped = False


async def set_pending_entry(pending: dict) -> None:
    """ENTERING 주문 식별자를 영속 복구용으로 보관한다."""
    async with _lock:
        if _state.position_status not in {"ENTERING", "HOLDING"}:
            raise RuntimeError(
                f"pending entry requires ENTERING/HOLDING, got {_state.position_status}"
            )
        _state.pending_entry = dict(pending)


async def clear_pending_entry() -> None:
    """체결 완료 또는 취소 확정 후 pending 주문 정보를 제거한다."""
    async with _lock:
        _state.pending_entry = None


async def set_exiting(reason: str) -> bool:
    """HOLDING → EXITING (atomic). 매도 접수 후 중복 청산을 막는다."""
    async with _lock:
        if _state.position_status != "HOLDING":
            return False
        _state.position_status = "EXITING"
        _state.close_reason = reason
        return True


async def begin_exit_intent(pending: dict) -> bool:
    """매도 전송 전에 HOLDING → EXITING과 주문 의도를 원자적으로 설정한다.

    F5 재시도는 직전 주문의 체결/취소 대사가 끝난 뒤 EXITING 상태에서 새
    의도를 시작할 수 있다. 살아 있는 pending_exit이 있으면 중복 주문을
    막기 위해 실패한다.
    """
    async with _lock:
        if _state.position_status == "HOLDING":
            _state.position_status = "EXITING"
        elif _state.position_status != "EXITING":
            return False
        if _state.pending_exit is not None:
            return False
        _state.close_reason = str(pending.get("reason") or _state.close_reason or "TIMEOUT")
        _state.pending_exit = dict(pending)
        return True


async def update_pending_exit(**changes) -> bool:
    """현재 매도 의도의 주문번호·전송 상태를 갱신한다."""
    async with _lock:
        if _state.position_status != "EXITING" or _state.pending_exit is None:
            return False
        _state.pending_exit.update(changes)
        return True


async def clear_pending_exit() -> None:
    """현재 매도 주문 대사가 끝난 뒤 다음 재시도 또는 종료를 허용한다."""
    async with _lock:
        _state.pending_exit = None


async def reject_exit_intent() -> bool:
    """브로커가 주문을 명시적으로 거절한 경우에만 EXITING → HOLDING 복귀."""
    async with _lock:
        if _state.position_status != "EXITING" or _state.pending_exit is None:
            return False
        _state.pending_exit = None
        _state.position_status = "HOLDING"
        _state.close_reason = None
        return True


async def set_exit_remaining_qty(remaining_qty: int) -> bool:
    """EXITING 상태의 확인된 미체결/잔여수량을 갱신한다."""
    async with _lock:
        if _state.position_status != "EXITING":
            return False
        _state.remaining_qty = max(0, int(remaining_qty))
        return True


async def set_closed(reason: str) -> bool:
    """HOLDING/EXITING → CLOSED (atomic). 완전 청산 시 잔여수량을 0으로 만든다.

    tick 이력은 지우지 않는다 — 청산 후에도 UI 가격흐름 차트를 유지하고,
    다음 거래일 시작 시 _clear_for_trading_day가 정리한다.
    """
    async with _lock:
        if _state.position_status not in {"HOLDING", "EXITING"}:
            return False
        _state.position_status = "CLOSED"
        _state.close_reason = reason
        _state.remaining_qty = 0
        _state.pending_exit = None
        return True


async def stop_post_close_tracking() -> bool:
    """CLOSED 상태의 사후 가격 관측을 멈춘다. 이미 멈춘 경우도 성공이다."""
    async with _lock:
        if _state.position_status != "CLOSED":
            return False
        _state.post_close_tracking_stopped = True
        return True


async def reset_to_idle(reason: str) -> None:
    """ENTERING → IDLE. F3 미체결 확정 시 호출."""
    async with _lock:
        _state.position_status = "IDLE"
        _state.close_reason = reason
        _state.target_ticker = None
        _state.target_name = None
        _state.target_candidates = None
        _state.entry_at = None
        _state.order_id = None
        _state.pending_entry = None
        _state.pending_exit = None
        _state.pending_entry = None
        _state.post_close_tracking_stopped = False
        live.clear_tick_history()


def update_high_price(price: float) -> None:
    if _state.high_price is None or price > _state.high_price:
        _state.high_price = price


# ── 영속화 ───────────────────────────────────────────────────────────

async def persist(state_dir: str, date_str: str) -> None:
    """today_state.json 원자적 쓰기 (tmp → rename). PRD §6-7."""
    path = Path(state_dir)
    tmp = path / "today_state.tmp"
    dst = path / "today_state.json"
    data = {
        "date": date_str,
        "ticker": _state.target_ticker,
        "name": _state.target_name,
        "observe_ticker": _state.observe_ticker,
        "target_candidates": _state.target_candidates or [],
        "entry_price": _state.entry_price,
        "entry_at": _state.entry_at,
        "entry_qty": _state.entry_qty,
        "remaining_qty": _state.remaining_qty,
        "high_price": _state.high_price,
        "trailing_active": _state.trailing_active,
        "highest_step": _state.highest_step,
        "trade_id": _state.trade_id,
        "position_status": _state.position_status,
        "close_reason": _state.close_reason,
        "pending_entry": _state.pending_entry,
        "pending_exit": _state.pending_exit,
        "post_close_tracking_stopped": _state.post_close_tracking_stopped,
        "tracks": {
            name: asdict(track_state) for name, track_state in _tracks.items()
        },
    }
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(dst)


def discard(state_dir: str) -> None:
    """지난 거래일의 today_state.json 폐기. 파일이 없으면 무시."""
    (Path(state_dir) / "today_state.json").unlink(missing_ok=True)


def backup_stale(state_dir: str, stale_date: str) -> bool:
    """지난 거래일 상태 파일을 증거 사본으로 격리 (today_state.stale_<날짜>.json).

    이후 당일 DB OPEN 거래 복구의 persist가 원본을 덮어써도 증거가 남는다.
    같은 stale 날짜면 같은 이름에 덮어쓰므로 재시작이 반복돼도 사본이 쌓이지 않는다.
    날짜는 YYYYMMDD만 신뢰한다 — 손상된 값(경로 문자 포함 가능)은 해시로 치환해
    파일명 오류를 막는다. 사본 확보 여부를 반환하며 예외는 전파하지 않는다
    (증거 백업 실패가 포지션 복구를 중단시키면 안 된다).
    """
    if not re.fullmatch(r"\d{8}", stale_date or ""):
        digest = hashlib.md5((stale_date or "").encode("utf-8")).hexdigest()[:8]
        stale_date = f"unknown_{digest}"
    try:
        src = Path(state_dir) / "today_state.json"
        if not src.exists():
            return True
        dst = Path(state_dir) / f"today_state.stale_{stale_date}.json"
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        return True
    except Exception:
        return False


def load(state_dir: str) -> dict | None:
    """today_state.json 읽기. 없거나 손상 시 None 반환."""
    dst = Path(state_dir) / "today_state.json"
    if not dst.exists():
        return None
    try:
        return json.loads(dst.read_text(encoding="utf-8"))
    except Exception:
        return None


def restore_from(data: dict) -> None:
    """재시작 복구: today_state.json → 인메모리 State 복원. PRD §6-7."""
    _state.trading_date = data.get("date")
    _state.target_ticker = data.get("ticker")
    _state.target_name = data.get("name")
    _state.observe_ticker = data.get("observe_ticker")
    _state.target_candidates = data.get("target_candidates") or None
    _state.entry_price = data.get("entry_price")
    _state.entry_at = data.get("entry_at")
    _state.entry_qty = data.get("entry_qty")
    _state.remaining_qty = data.get("remaining_qty")
    _state.high_price = data.get("high_price")
    _state.trailing_active = data.get("trailing_active", False)
    _state.highest_step = data.get("highest_step", 0.0)
    _state.trade_id = data.get("trade_id", 0)
    _state.position_status = data.get("position_status", "IDLE")
    _state.close_reason = data.get("close_reason")
    pending = data.get("pending_entry")
    _state.pending_entry = dict(pending) if isinstance(pending, dict) else None
    pending_exit = data.get("pending_exit")
    _state.pending_exit = dict(pending_exit) if isinstance(pending_exit, dict) else None
    _state.post_close_tracking_stopped = bool(
        data.get("post_close_tracking_stopped", False)
    )

    _tracks.clear()
    tracks = data.get("tracks")
    if isinstance(tracks, dict):
        allowed = {f.name for f in dataclasses.fields(TrackState)}
        for name, payload in tracks.items():
            if not isinstance(payload, dict):
                continue
            _tracks[name] = TrackState(
                **{k: v for k, v in payload.items() if k in allowed}
            )
