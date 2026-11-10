import asyncio
import os
from collections.abc import Awaitable, Callable

import httpx

from src.utils.logger import log, normalize_level

_queue: asyncio.Queue[str] = asyncio.Queue()
_SEND_INTERVAL = 1.1
_SEND_ATTEMPTS = 3
# 전송을 확인하지 못한 알림. 다음 알림이 전송에 성공할 때 끝에 한 줄로 알리고 비운다.
# 응답 타임아웃이면 메시지는 이미 도착했을 수 있다 — 2026-10-26 매수 체결 알림이 타임아웃 3회
# 뒤 NOTIFICATION_FAILED로 남았는데 사용자는 3번 받았다
# (docs/NOTIFICATION_LOSS_FOLLOWUP_20261026.md).
_unconfirmed: list[str] = []
_NOTE_MAX_ITEMS = 3
_UNCONFIRMED_KEEP = 50


_ACTIONABLE_ALERT_EVENTS = {
    "ENTRY_EXECUTED",
    "PYRAMID_EXECUTED",
    "TRAILING_STOP",
    "HARD_STOP",
    "TIMEOUT_CLOSE",
    "SLIPPAGE_GUARD",
    "ENTRY_FAIL",
    "NO_TARGET",
    "VI_FILTER_ALL_EXCLUDED",
    "GAP_CHANGED",
    "F2_RETRY_EXHAUSTED",
    "MARKET_CLOSED",
    "VI_DETECTED",
    "VI_RELEASED",
    "VI_ENTRY_BLOCKED",
}
_CRITICAL_ALERT_LEVELS = {"error"}
_LEVEL_LABELS = {
    "error": "긴급",
    "warn": "확인",
    "info": "알림",
}

_ALERT_TITLES = {
    "ENTRY_EXECUTED": "매수 체결",
    "PYRAMID_EXECUTED": "추가 매수 체결",
    "TRAILING_STOP": "매도 체결",
    "HARD_STOP": "손절 매도",
    "TIMEOUT_CLOSE": "마감 청산",
    "SLIPPAGE_GUARD": "슬리피지 초과 청산",
    "ENTRY_FAIL": "진입 실패",
    "NO_TARGET": "오늘 진입 없음",
    "VI_FILTER_ALL_EXCLUDED": "오늘 진입 없음",
    "GAP_CHANGED": "진입 전 갭 변동",
    "F2_RETRY_EXHAUSTED": "오늘 진입 없음",
    "MARKET_CLOSED": "휴장일",
    "TIMEOUT_ORDER_FAILED": "마감 청산 주문 실패",
    "F4_SELL_ERROR": "매도 주문 오류",
    "TOKEN_REFRESH_FAIL": "KIS 토큰 갱신 실패",
    "WS_KEY_REFRESH_FAIL": "실시간 접속키 갱신 실패",
    "STALE_POSITION_DETECTED": "이전 포지션 상태 확인 필요",
    "PROCESS_RESTART_DETECTED": "프로세스 재시작 감지",
    "VI_DETECTED": "변동성완화장치(VI) 발동",
    "VI_RELEASED": "변동성완화장치(VI) 해제",
    "VI_ENTRY_BLOCKED": "변동성완화장치(VI) 발동으로 진입 보류",
    "ENTRY_CANCEL_UNCONFIRMED": "진입 주문 취소 확인 실패",
    "BALANCE_QUERY_FAILED": "잔고 조회 실패로 진입 차단",
}


async def send(
    event: str,
    level: str = "info",
    message: str = "",
    ticker: str | None = None,
    name: str | None = None,
) -> None:
    """Queue a non-blocking Telegram alert when it is actionable."""
    normalized_level = normalize_level(level)
    if not _should_send_alert(event, normalized_level):
        return
    text = _format_alert_text(event, normalized_level, message, ticker=ticker, name=name)
    await _queue.put(text)


def _should_send_alert(event: str, level: str = "info") -> bool:
    normalized_level = normalize_level(level)
    return event in _ACTIONABLE_ALERT_EVENTS or normalized_level in _CRITICAL_ALERT_LEVELS


def _format_alert_text(
    event: str,
    level: str = "info",
    message: str = "",
    ticker: str | None = None,
    name: str | None = None,
) -> str:
    normalized_level = normalize_level(level)
    severity = _LEVEL_LABELS.get(normalized_level, normalized_level)
    title = _ALERT_TITLES.get(event) or event.replace("_", " ").title()
    stock = _format_stock(ticker, name)
    content = _clean_message(message) if message else title
    return "\n".join([
        f"[{severity}] {title}",
        f"종목 : {stock}",
        f"내용 : {content}",
    ])


def _target_name_for(ticker: str | None) -> str | None:
    if not ticker:
        return None
    try:
        from src import state

        s = state.get()
        if ticker == s.target_ticker:
            return s.target_name
    except Exception:
        return None
    return None


def _format_stock(ticker: str | None = None, name: str | None = None) -> str:
    name = name or _target_name_for(ticker)
    if ticker and name:
        return f"{ticker} {name}"
    if ticker:
        return ticker
    if name:
        return name
    return "-"


def _clean_message(message: str) -> str:
    return " ".join(str(message).split())


def _alert_head(text: str) -> str:
    """알림 첫 두 줄(제목·종목)을 한 줄로. 로그와 미확인 안내에 쓴다."""
    lines = str(text).split("\n")
    head = lines[0]
    if len(lines) > 1 and lines[1].startswith("종목 : "):
        stock = lines[1][len("종목 : "):]
        if stock and stock != "-":
            head = f"{head} · {stock}"
    return head[:80]


def _with_unconfirmed_note(text: str) -> str:
    if not _unconfirmed:
        return text
    shown = _unconfirmed[:_NOTE_MAX_ITEMS]
    rest = len(_unconfirmed) - len(shown)
    note = f"※ 앞선 알림 {len(_unconfirmed)}건 전송 확인 안 됨: " + ", ".join(shown)
    if rest:
        note += f" 외 {rest}건"
    return f"{text}\n{note}"


def _scrub(value: str, token: str) -> str:
    return value.replace(token, "***") if token else value


ClientFactory = Callable[[], httpx.AsyncClient]

# 요청이 서버에 닿지 않았다고 확실한 예외. 그 밖의 예외(응답 타임아웃 등)는 메시지가
# 이미 도착했을 수 있다.
_NOT_DELIVERED_ERRORS = {"ConnectError", "ConnectTimeout"}

# 앞선 시도가 도착했을 수 있는 재전송의 본문 끝에 붙인다. 중복은 막지 않고(재시도는 그대로),
# 받는 쪽이 중복인지 알게 한다(docs/NOTIFICATION_LOSS_FOLLOWUP_20261026.md 8.5절 자, 2026-11-02).
RESEND_MARK = "(재전송 — 앞 메시지와 중복일 수 있음)"


def _log_retried(text: str, attempts: int, errors: list[str], status: int | None) -> None:
    """재시도 끝에 성공한 전송. 앞선 시도가 도착했으면 같은 알림이 중복으로 간 것이다
    (2026-10-28 마감 청산 알림 2번, docs/NOTIFICATION_LOSS_FOLLOWUP_20261026.md 8절)."""
    log(
        "NOTIFICATION_RETRIED",
        level="INFO",
        alert=_alert_head(text),
        attempts=attempts,
        errors=list(errors) or None,
        status=status,
        possible_duplicate=any(e not in _NOT_DELIVERED_ERRORS for e in errors),
    )


async def _deliver(
    client_factory: ClientFactory,
    url: str,
    chat_id: str,
    text: str,
    *,
    token: str = "",
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> bool:
    """알림 하나를 최대 3회 보낸다. 성공하면 True.

    - 429는 retry_after만큼 쉬고 다시 보낸다. 그 밖의 오류 응답은 재시도하지 않는다(기존 동작).
    - 예외는 다시 보낸다. 응답 타임아웃이면 메시지가 이미 도착했을 수 있어 "확인 못 함"으로 적는다.
      연결 실패뿐이었으면 "전송 안 됨"으로 적는다.
    - 실패하면 어떤 알림인지·시도별 예외·상태 코드를 남기고, 다음 성공 알림 끝에 덧붙인다.
    - 재시도 끝에 성공하면 `NOTIFICATION_RETRIED`를 남긴다(중복 가능 여부 포함).
    - 앞선 시도가 도착했을 수 있으면(연결 실패가 아닌 예외) 다시 보내는 본문 끝에
      `RESEND_MARK`를 붙인다.
    """
    reported = len(_unconfirmed)
    outgoing = _with_unconfirmed_note(text)
    errors: list[str] = []
    last_error = ""
    status: int | None = None
    body = ""
    retried_status: int | None = None
    attempts = 0
    for attempt in range(1, _SEND_ATTEMPTS + 1):
        attempts = attempt
        maybe_delivered = any(e not in _NOT_DELIVERED_ERRORS for e in errors)
        payload = f"{outgoing}\n{RESEND_MARK}" if maybe_delivered else outgoing
        try:
            async with client_factory() as client:
                resp = await client.post(url, json={"chat_id": chat_id, "text": payload})
        except Exception as e:  # noqa: BLE001 — 알림 실패가 호출부를 흔들면 안 된다
            errors.append(type(e).__name__)
            last_error = _scrub(repr(e), token)
            status = None
            continue
        status = resp.status_code
        if 200 <= status < 300:
            if attempt > 1:
                _log_retried(text, attempt, errors, retried_status)
            if reported:
                log("NOTIFICATION_UNCONFIRMED_REPORTED", level="INFO", count=reported)
                del _unconfirmed[:reported]
            return True
        body = _scrub(resp.text[:200], token)
        retried_status = status
        if status == 429:
            try:
                retry_after = float(resp.json().get("parameters", {}).get("retry_after", 1))
            except Exception:  # noqa: BLE001
                retry_after = 1.0
            await sleep(retry_after)
            continue
        break

    # UNCONFIRMED: 응답 타임아웃 등 도착했을 수 있는 시도가 하나라도 있었다. 뒤 시도가 거부됐어도
    #   앞 시도가 도착했을 수 있으니 먼저 본다.
    # REJECTED: 그런 시도 없이 마지막 시도가 오류 응답이었다.
    # NOT_SENT: 예외가 모두 연결 실패라 어느 시도도 서버에 닿지 않았다(앞선 429는 서버가 거부한 것).
    if any(e not in _NOT_DELIVERED_ERRORS for e in errors):
        delivery, reason = "UNCONFIRMED", "확인 못 함 — 도착·중복 가능"
    elif status is not None:
        delivery, reason = "REJECTED", "거부됨"
    else:
        delivery, reason = "NOT_SENT", "전송 안 됨"
    log(
        "NOTIFICATION_FAILED",
        level="WARN",
        alert=_alert_head(text),
        attempts=attempts,
        errors=errors or None,
        error=last_error or None,
        status=status,
        body=body or None,
        delivery=delivery,
    )
    _unconfirmed.append(f"{_alert_head(text)} ({reason})")
    del _unconfirmed[:-_UNCONFIRMED_KEEP]
    return False


async def worker() -> None:
    """
    Background sender for Telegram API.
    main.py starts this with asyncio.create_task(notifier.worker()).
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    while True:
        text = await _queue.get()

        if token and chat_id:
            try:
                await _deliver(
                    lambda: httpx.AsyncClient(timeout=5), url, chat_id, text, token=token
                )
            except Exception:  # noqa: BLE001 — 워커는 죽지 않는다
                pass

        _queue.task_done()
        await asyncio.sleep(_SEND_INTERVAL)
