"""모듈 간 공유 라이브 상태. UI SSE 및 API에서 읽는다."""

import asyncio
from collections import deque
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from src.utils.logger import event_label, normalize_level

KST = ZoneInfo("Asia/Seoul")
_TICK_HISTORY_MAX = 5000
_MINUTE_HISTORY_MAX = 180

# 마지막 WebSocket 틱 가격. HOLDING 중에만 갱신된다.
last_tick_price: float | None = None
last_tick_ticker: str | None = None
_tick_history: deque[dict] = deque(maxlen=_TICK_HISTORY_MAX)
# 분봉은 tick 버퍼에서 파생하지 않고 별도 누적한다 — 거래가 활발해 tick 버퍼(5,000건)가
# 20분을 못 담아도 분 단위 가격흐름(최대 180분)은 유지된다.
_minute_history: deque[dict] = deque(maxlen=_MINUTE_HISTORY_MAX)

# WebSocket 연결 상태
ws_connected: bool = False

# REAL 신규 매수는 main의 준비도 100% 게이트를 통과한 프로세스에서만 허용한다.
# 청산·취소 주문은 포지션 보호를 위해 이 플래그로 막지 않는다.
real_entry_enabled: bool = False

# 당일 VI(변동성완화장치) 이벤트 — UI 차트 구간 표시·사후 분석용
vi_events: list[dict] = []

# 종목별 그날 첫 체결(거래소 시각 09:00 이후). F3는 개장 직후 주문의 체결 확인 창을 여기서부터
# 센다(docs/ENTRY_FILL_TIMEOUT_FOLLOWUP_20260903.md 8.4절 가안). F4 틱 처리가 채운다.
_MARKET_OPEN = time(9, 0)
_first_trades: dict[str, dict] = {}
_first_trade_events: dict[str, asyncio.Event] = {}


def _drop_stale_first_trade(ticker: str, day: date) -> None:
    kept = _first_trades.get(ticker)
    if kept is not None and kept["at"].date() != day:
        del _first_trades[ticker]
        event = _first_trade_events.get(ticker)
        if event is not None:
            event.clear()


def note_trade(ticker: str | None, source_ts: str | None, price: float) -> None:
    """체결 틱 하나를 받는다. 그 종목의 그날 09:00 이후 첫 체결만 남긴다.

    거래소 시각이 없거나 오프셋이 없으면 버린다 — 수신 시각으로 대신하면 장전 틱을 첫 체결로
    볼 수 있다.
    """
    if not ticker or not source_ts:
        return
    try:
        at = datetime.fromisoformat(str(source_ts))
    except ValueError:
        return
    if at.tzinfo is None or at.utcoffset() is None:
        return
    at = at.astimezone(KST)
    if at.time() < _MARKET_OPEN:
        return
    _drop_stale_first_trade(ticker, at.date())
    if ticker in _first_trades:
        return
    _first_trades[ticker] = {"at": at, "price": price}
    first_trade_event(ticker, at.date()).set()


def first_trade(ticker: str, day: date) -> dict | None:
    """그날 그 종목의 첫 체결 {"at", "price"}. 아직 없으면 None."""
    _drop_stale_first_trade(ticker, day)
    return _first_trades.get(ticker)


def first_trade_event(ticker: str, day: date) -> asyncio.Event:
    """그날 그 종목의 첫 체결이 오면 set되는 이벤트."""
    _drop_stale_first_trade(ticker, day)
    event = _first_trade_events.get(ticker)
    if event is None:
        event = _first_trade_events[ticker] = asyncio.Event()
    return event

# NTP 마지막 측정값
ntp_offset_ms: float = -1.0
ntp_level: str = "OK"  # OK | WARN | CRIT | ERROR

# SSE 구독 큐 목록
_sse_queues: list[asyncio.Queue] = []


def push_tick(price: float, ticker: str | None = None) -> None:
    """F4 on_tick에서 호출. 틱 가격 갱신과 SSE 브로드캐스트를 수행한다."""
    global last_tick_price, last_tick_ticker
    last_tick_price = price
    last_tick_ticker = ticker
    now = datetime.now(KST)
    ts = now.isoformat()
    _tick_history.append({
        "ts": ts,
        "ticker": ticker,
        "price": price,
    })
    _accumulate_minute(now, ticker, price)
    _broadcast({"type": "tick", "ticker": ticker, "price": price, "ts": ts})


def _accumulate_minute(ts_dt: datetime, ticker: str | None, price: float) -> None:
    """같은 분이면 마지막 가격 갱신, 새 분이면 행 추가."""
    minute_ts = ts_dt.replace(second=0, microsecond=0).isoformat()
    last = _minute_history[-1] if _minute_history else None
    if last and last.get("ts") == minute_ts and last.get("ticker") == ticker:
        last["price"] = price
        last["tick_count"] = int(last.get("tick_count", 0)) + 1
    else:
        _minute_history.append({
            "ts": minute_ts,
            "ticker": ticker,
            "price": price,
            "tick_count": 1,
        })


def _parse_since(since: datetime | str | None) -> datetime | None:
    if isinstance(since, str):
        try:
            return datetime.fromisoformat(since)
        except ValueError:
            return None
    if isinstance(since, datetime):
        return since
    return None


def tick_history(ticker: str | None = None, since: datetime | str | None = None) -> list[dict]:
    since_dt = _parse_since(since)
    rows = [row for row in _tick_history if not ticker or row.get("ticker") == ticker]
    if since_dt is None:
        return rows

    filtered = []
    for row in rows:
        try:
            row_ts = datetime.fromisoformat(str(row.get("ts")))
        except ValueError:
            continue
        if row_ts >= since_dt:
            filtered.append(row)
    return filtered


def minute_price_history(
    ticker: str | None = None, since: datetime | str | None = None,
) -> list[dict]:
    """별도 누적된 분봉 조회. since는 분 단위로 내림해 진입 분을 포함한다."""
    since_dt = _parse_since(since)
    if since_dt is not None:
        since_dt = since_dt.replace(second=0, microsecond=0)
    rows: list[dict] = []
    for row in _minute_history:
        if ticker and row.get("ticker") != ticker:
            continue
        if since_dt is not None:
            try:
                row_ts = datetime.fromisoformat(str(row.get("ts")))
            except ValueError:
                continue
            if row_ts < since_dt:
                continue
        rows.append(dict(row))
    return rows


def clear_tick_history() -> None:
    _tick_history.clear()
    _minute_history.clear()
    vi_events.clear()


def record_vi_detected(event: dict) -> None:
    """VI 발동 기록. UI가 즉시 갱신하도록 상태 신호를 보낸다.

    ts는 API 기록 기준 실제 발동 시각, detected_ts는 시스템 감지 시각.
    """
    vi_events.append({
        "ts": event.get("ts"),
        "detected_ts": event.get("detected_ts"),
        "released_ts": None,
        "vi_kind_code": event.get("vi_kind_code"),
        "cntg_vi_hour": event.get("cntg_vi_hour"),
        "vi_prc": event.get("vi_prc"),
        "vi_stnd_prc": event.get("vi_stnd_prc"),
        "vi_dprt": event.get("vi_dprt"),
        "frozen_price": event.get("frozen_price"),
    })
    push_status()


def record_vi_released(event: dict) -> None:
    """가장 최근의 미해제 VI 기록에 해제 정보를 채운다."""
    for row in reversed(vi_events):
        if row.get("released_ts") is None:
            row["released_ts"] = event.get("ts")
            row["release_price"] = event.get("release_price")
            row["duration_sec"] = event.get("duration_sec")
            break
    push_status()


def push_status() -> None:
    """상태 변경 시 호출. SSE 클라이언트에 갱신 신호를 보낸다."""
    _broadcast({"type": "status"})


def push_log(event: str, level: str, **kwargs) -> None:
    """로그 이벤트 발생 시 호출. SSE 클라이언트에 새 로그 신호를 보낸다."""
    _broadcast({
        "type": "log",
        "event": event,
        "event_label": event_label(event),
        "level": normalize_level(level),
        **kwargs,
    })


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    _sse_queues.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    try:
        _sse_queues.remove(q)
    except ValueError:
        pass


def _broadcast(data: dict) -> None:
    dead = []
    for q in _sse_queues:
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _sse_queues.remove(q)
