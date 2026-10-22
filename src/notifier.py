import asyncio
import os

import httpx

from src.utils.logger import log, normalize_level

_queue: asyncio.Queue[str] = asyncio.Queue()
_SEND_INTERVAL = 1.1


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
    "VI_DETECTED": "급등락 일시정지 시작(VI)",
    "VI_RELEASED": "급등락 일시정지 끝(VI)",
    "VI_ENTRY_BLOCKED": "급등락 일시정지라 진입 보류(VI)",
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
            for attempt in range(1, 4):
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        resp = await client.post(url, json={
                            "chat_id": chat_id,
                            "text": text,
                        })
                    if resp.status_code == 429:
                        retry_after = resp.json().get("parameters", {}).get("retry_after", 1)
                        await asyncio.sleep(float(retry_after))
                        continue
                    break
                except Exception as e:
                    if attempt == 3:
                        log("NOTIFICATION_FAILED", level="WARN", error=str(e))

        _queue.task_done()
        await asyncio.sleep(_SEND_INTERVAL)
