import asyncio
import json
import os
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import websockets

from src.api import auth
from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")

_RETRY_INTERVAL_BASE = 2     # 최초 재연결 대기 (초)
_RETRY_INTERVAL_MAX  = 30    # 지수 백오프 상한 (초)
_STALE_TIMEOUT       = 30.0  # 수신 중단 감지 기준 (초)
_CRIT_THRESHOLD      = 10    # 연속 실패 N회 이후 CRIT 로그

# H0STCNT0 레코드 필드 수. KIS 공식 저장소 open-trading-api의
# examples_llm/domestic_stock/ccnl_krx/ccnl_krx.py columns 목록 기준
# (0=MKSC_SHRN_ISCD, 1=STCK_CNTG_HOUR, 2=STCK_PRPR, 12=CNTG_VOL,
#  18=CTTR 체결강도, 21=CCLD_DVSN 체결구분, 10/11=ASKP1/BIDP1, 45=VI_STND_PRC,
#  46=MARKET_CLS_CODE). 2026-09-14부터 47개이고 그 전에는 46개였다. 46만 가정한
# 파서가 다건 프레임의 첫 체결만 넘긴 사고가 있었다
# (docs/WS_47FIELD_PARSER_FOLLOWUP_20261001.md). 그래서 1순위는 프레임 헤더의
# 건수이고, 이 목록은 헤더를 못 믿을 때의 대체 경로다.
_CNT_FIELD_COUNT = 47
_KNOWN_FIELD_COUNTS = (47, 46)
_HMS = re.compile(r"\d{6}")
# 프레임 이상은 같은 원인이면 매 프레임 반복된다. 처음과 N번째마다만 남긴다.
_ANOMALY_LOG_EVERY = 1000
_frame_anomaly_counts: Counter[str] = Counter()


async def subscribe(
    ticker: str,
    on_tick: Callable[[dict], Awaitable[None]],
    *,
    stop_if: Callable[[], bool] | None = None,
    on_connection_change: Callable[[bool], None] | None = None,
) -> None:
    """
    KIS WebSocket 실시간 체결 구독 (PRD §F4, §6-3).
    지수 백오프로 무한 재연결. stop_if() == True 이면 즉시 반환.
    """
    ws_url = os.getenv("KIS_WS_URL", "ws://ops.koreainvestment.com:31000")
    consec = 0
    interval = _RETRY_INTERVAL_BASE
    ws_key_ready = False

    while True:
        if stop_if and stop_if():
            return

        connected = False
        try:
            if not ws_key_ready:
                # OAuth token과 별개인 WS 전용 접속키 1회 발급.
                # 발급 실패가 subscribe 밖으로 전파되면 F4 모니터링 전체가 죽으므로
                # 재연결 루프 안에서 백오프와 함께 재시도한다.
                await auth.refresh_ws_key()
                ws_key_ready = True
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                await _send_subscribe(ws, ticker)
                connected = True
                if on_connection_change is not None:
                    on_connection_change(True)
                log("WS_CONNECTED", level="INFO", ticker=ticker, consec_failures=consec)
                consec = 0
                interval = _RETRY_INTERVAL_BASE

                while True:
                    if stop_if and stop_if():
                        return
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=_STALE_TIMEOUT)
                    except asyncio.TimeoutError:
                        raise TimeoutError(f"데이터 수신 없음 >{_STALE_TIMEOUT}s")
                    # 한 프레임에 체결이 여러 건 올 수 있다. 전부 전달한다 —
                    # 프레임이 묶이는 건 체결 폭주 구간이고, 그 구간이 바로
                    # 트레일링·하드스탑 판정이 가장 민감한 구간이다.
                    for tick in _parse_ticks(raw):
                        await on_tick(tick)

        except Exception as e:
            if on_connection_change is not None:
                on_connection_change(False)
            connected = False
            consec += 1
            level = "CRIT" if consec >= _CRIT_THRESHOLD else "WARN"
            log("WS_DISCONNECTED", level=level,
                ticker=ticker, consec=consec, error=repr(e))
            if stop_if and stop_if():
                return
            await asyncio.sleep(interval)
            interval = min(interval * 2, _RETRY_INTERVAL_MAX)
        finally:
            if connected and on_connection_change is not None:
                on_connection_change(False)


async def _send_subscribe(ws: websockets.WebSocketClientProtocol, ticker: str) -> None:
    req = {
        "header": {
            "approval_key": auth.get_ws_key(),
            "custtype": "P",
            "tr_type": "1",
            "content-type": "utf-8",
        },
        "body": {
            "input": {
                "tr_id": "H0STCNT0",   # 주식 체결 실시간 조회
                "tr_key": ticker,
            }
        },
    }
    await ws.send(json.dumps(req, ensure_ascii=False))


def _exchange_iso(hms: str, now: datetime | None = None) -> str | None:
    """체결시간 HHMMSS를 당일 KST 오프셋 포함 ISO8601로 변환. 유효하지 않으면 None."""
    if not hms or len(hms) != 6 or not hms.isdigit():
        return None
    hh, mm, ss = int(hms[0:2]), int(hms[2:4]), int(hms[4:6])
    if hh > 23 or mm > 59 or ss > 59:
        return None
    base = now or datetime.now(KST)
    return base.replace(hour=hh, minute=mm, second=ss, microsecond=0).isoformat()


def _aligned(records: list[list[str]]) -> bool:
    """모든 레코드가 같은 종목코드로 시작하고 1번이 6자리 체결시각인가."""
    first = records[0][0]
    return all(len(r) > 1 and r[0] == first and _HMS.fullmatch(r[1]) for r in records)


def _chunk(values: list[str], length: int) -> list[list[str]]:
    return [values[i:i + length] for i in range(0, len(values), length)]


def _header_count(text: str) -> int | None:
    """프레임 헤더의 건수(``0|H0STCNT0|004|...``의 ``004``). 읽을 수 없으면 None."""
    try:
        count = int(text)
    except (TypeError, ValueError):
        return None
    return count if count > 0 else None


def _split_frame(body: str, count: int | None) -> tuple[list[list[str]], str]:
    """프레임 데이터부를 체결 레코드 목록으로 나누고, 어떻게 나눴는지 함께 돌려준다.

    구분자 형태(줄바꿈 또는 ``^``)에 의존하지 않도록 둘 다 값 구분자로 본다.

    1. ``HEADER`` — 값 개수를 헤더 건수로 나눠 정렬되면 그 길이를 쓴다. 필드가 또
       늘어도 버틴다.
    2. ``FIELD_COUNT`` — 헤더가 없거나 맞지 않으면 알려진 필드 수(47, 46)로 쪼갠다.
    3. ``UNSPLIT`` — 그래도 정렬되지 않으면 쪼개지 않는다. 오정렬된 레코드로 잘못된
       가격을 내보내느니 첫 레코드만 쓰는 편이 안전하다. 호출부가 경보를 남긴다.
    """
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not body:
        return [], "EMPTY"
    values = re.split(r"[\^\n]", body)
    if count and len(values) % count == 0:
        records = _chunk(values, len(values) // count)
        if _aligned(records):
            return records, "HEADER"
    for length in _KNOWN_FIELD_COUNTS:
        if len(values) >= length and len(values) % length == 0:
            records = _chunk(values, length)
            if _aligned(records):
                return records, "FIELD_COUNT"
    return [values], "UNSPLIT"


def _split_records(body: str, count: int | None = None) -> list[list[str]]:
    """프레임 데이터부 → 체결 레코드(필드 배열) 목록. ``_split_frame`` 참고."""
    return _split_frame(body, count)[0]


def _note_frame_anomaly(event: str, **fields) -> None:
    _frame_anomaly_counts[event] += 1
    occurrences = _frame_anomaly_counts[event]
    if occurrences == 1 or occurrences % _ANOMALY_LOG_EVERY == 0:
        log(event, level="WARN", occurrences=occurrences, **fields)


def _tick_from_fields(fields: list[str]) -> dict | None:
    """단일 체결 레코드 → tick dict. 파싱 불가면 None."""
    try:
        exchange_time = fields[1] if len(fields) > 1 else ""
        qty = int(fields[12])
        return {
            "ticker": fields[0],
            "price": float(fields[2]),
            "volume": qty,
            "qty": qty,
            "exchange_time": exchange_time,
            "source_ts": _exchange_iso(exchange_time),
            # 해석하지 않은 필드까지 순서 그대로 넘긴다. 인덱스별 의미는 확인
            # 됐지만(체결강도 18·체결구분 21·최우선호가 10/11 등) 해석은 트랙
            # 작업에서 하고, 여기서는 원본을 그대로 캡처에 남긴다.
            # 매매 판단 경로는 이 값을 읽지 않는다.
            "raw": fields,
        }
    except Exception:
        return None


def _parse_ticks(raw: str) -> list[dict]:
    """KIS 체결 프레임 파싱 → tick 리스트. 한 프레임에 여러 건이 올 수 있다.

    체결시간(거래소 시각)을 오프셋 포함 ISO(``source_ts``)로 함께 제공한다.
    시각이 없거나 유효하지 않으면 ``source_ts=None``으로 표시해 하위에서 naive
    시각을 소리 없이 받아들이지 않게 한다.
    """
    try:
        if raw.startswith("{"):
            return []  # 시스템/PINGPONG 메시지
        parts = raw.split("|")
        if len(parts) < 4:
            return []
        count = _header_count(parts[2])
        records, how = _split_frame(parts[3], count)
        if how == "UNSPLIT":
            _note_frame_anomaly(
                "WS_FRAME_UNSPLIT", tr_id=parts[1], header_count=count,
                values=len(records[0]),
            )
        elif count is not None and count != len(records):
            _note_frame_anomaly(
                "WS_FRAME_HEADER_MISMATCH", tr_id=parts[1], header_count=count,
                records=len(records),
            )
        ticks = []
        for index, fields in enumerate(records):
            tick = _tick_from_fields(fields)
            if tick is None:
                continue
            # 프레임 안 위치. 캡처가 헤더 건수와 실제 레코드 수를 매일 대조하게 한다.
            tick["frame_count"] = count
            tick["frame_size"] = len(records)
            tick["frame_index"] = index
            ticks.append(tick)
        return ticks
    except Exception:
        return []


def _parse_tick(raw: str) -> dict | None:
    """프레임의 **첫** 체결만 반환하는 편의 래퍼.

    구독 루프는 ``_parse_ticks``를 쓴다 — 이 함수만 쓰면 다건 프레임에서
    나머지 체결을 잃는다.
    """
    ticks = _parse_ticks(raw)
    return ticks[0] if ticks else None
