"""KIS 실시간 체결통보 — 진입 체결을 조회보다 먼저 안다.

설계: docs/superpowers/specs/2026-10-06-fill-notice-design.md

체결통보(H0STCNI9 모의 / H0STCNI0 실전)는 HTS ID로 구독하고, 데이터부는 구독 응답의 key/iv로
AES256-CBC 복호화한다. 체결(CNTG_YN=2) 통보만 주문별 장부에 쌓는다. 장부는 조회 결과와 같은
모양으로 체결을 돌려주고, 전량에 도달하면 주문의 이벤트를 켠다.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from base64 import b64decode
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")
NOTICE_TR = {"PAPER": "H0STCNI9", "REAL": "H0STCNI0"}

# KIS 공식 저장소 examples_llm/domestic_stock/ccnl_notice/ccnl_notice.py columns
FIELDS = (
    "CUST_ID", "ACNT_NO", "ODER_NO", "OODER_NO", "SELN_BYOV_CLS", "RCTF_CLS",
    "ODER_KIND", "ODER_COND", "STCK_SHRN_ISCD", "CNTG_QTY", "CNTG_UNPR",
    "STCK_CNTG_HOUR", "RFUS_YN", "CNTG_YN", "ACPT_YN", "BRNC_NO", "ODER_QTY",
    "ACNT_NAME", "ORD_COND_PRC", "ORD_EXG_GB", "POPUP_YN", "FILLER", "CRDT_CLS",
    "CRDT_LOAN_DATE", "CNTG_ISNM40", "ODER_PRC",
)
_IDX = {name: i for i, name in enumerate(FIELDS)}


def enabled() -> bool:
    return (os.getenv("F3_FILL_NOTICE_ENABLED", "1") == "1"
            and bool(os.getenv("KIS_HTS_ID", "").strip()))


def decrypt(key: str, iv: str, cipher_text: str) -> str:
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, iv.encode("utf-8"))
    return unpad(cipher.decrypt(b64decode(cipher_text)), AES.block_size).decode("utf-8")


def _norm(order_id: object) -> str:
    return str(order_id or "").strip().lstrip("0")


@dataclass(frozen=True)
class Notice:
    order_id: str
    ticker: str
    qty: int
    price: float
    hms: str
    side: str


def parse(fields: list[str]) -> Notice | None:
    """체결 통보 한 건. 접수·거부 통보와 깨진 레코드는 None."""
    if len(fields) < len(FIELDS):
        return None
    if fields[_IDX["CNTG_YN"]] != "2" or fields[_IDX["RFUS_YN"]] == "1":
        return None
    try:
        qty = int(fields[_IDX["CNTG_QTY"]])
        price = float(fields[_IDX["CNTG_UNPR"]])
    except ValueError:
        return None
    if qty <= 0 or price <= 0:
        return None
    return Notice(order_id=_norm(fields[_IDX["ODER_NO"]]),
                  ticker=fields[_IDX["STCK_SHRN_ISCD"]], qty=qty, price=price,
                  hms=fields[_IDX["STCK_CNTG_HOUR"]], side=fields[_IDX["SELN_BYOV_CLS"]])


def split_records(body: str, count: int | None) -> list[list[str]]:
    """헤더 건수로 나눈다. 나누어떨어지지 않으면 한 레코드로 둔다."""
    values = body.split("^")
    if count and count > 1 and len(values) % count == 0:
        size = len(values) // count
        return [values[i:i + size] for i in range(0, len(values), size)]
    return [values]


class FillBook:
    """주문별 체결 누적. 날짜가 바뀌면 비운다."""

    def __init__(self) -> None:
        self._day: date | None = None
        self._orders: dict[str, dict] = {}
        self._orphans: dict[str, list[Notice]] = {}

    def _roll(self) -> None:
        today = datetime.now(KST).date()
        if self._day != today:
            self._day = today
            self._orders.clear()
            self._orphans.clear()

    def reset(self) -> None:
        self._day = None
        self._orders.clear()
        self._orphans.clear()

    def expect(self, order_id: str, order_qty: int) -> asyncio.Event:
        self._roll()
        key = _norm(order_id)
        entry = self._orders.get(key)
        if entry is None:
            entry = {"order_qty": order_qty, "qty": 0, "amount": 0.0,
                     "event": asyncio.Event(), "registered": time.monotonic(),
                     "first_fill": None}
            self._orders[key] = entry
        entry["order_qty"] = order_qty
        for notice in self._orphans.pop(key, []):
            self._apply(entry, notice)
        return entry["event"]

    def record(self, notice: Notice) -> None:
        self._roll()
        entry = self._orders.get(notice.order_id)
        if entry is None:
            self._orphans.setdefault(notice.order_id, []).append(notice)
            return
        self._apply(entry, notice)

    @staticmethod
    def _apply(entry: dict, notice: Notice) -> None:
        entry["qty"] += notice.qty
        entry["amount"] += notice.qty * notice.price
        if entry["first_fill"] is None:
            entry["first_fill"] = time.monotonic()
        if entry["order_qty"] and entry["qty"] >= entry["order_qty"]:
            entry["event"].set()

    def fill(self, order_id: str) -> dict | None:
        entry = self._orders.get(_norm(order_id))
        if entry is None or entry["qty"] <= 0:
            return None
        qty, order_qty = int(entry["qty"]), int(entry["order_qty"])
        return {"status": "FILLED" if qty >= order_qty else "PARTIAL",
                "order_qty": order_qty, "fill_qty": qty,
                "remaining_qty": max(0, order_qty - qty),
                "fill_price": entry["amount"] / qty}

    def event_for(self, order_id: str) -> asyncio.Event | None:
        entry = self._orders.get(_norm(order_id))
        return None if entry is None else entry["event"]

    def latency_ms(self, order_id: str) -> int | None:
        entry = self._orders.get(_norm(order_id))
        if entry is None or entry["first_fill"] is None:
            return None
        return max(0, round((entry["first_fill"] - entry["registered"]) * 1000))


_book = FillBook()


def expect(order_id: str, order_qty: int) -> asyncio.Event:
    return _book.expect(order_id, order_qty)


def record(notice: Notice) -> None:
    _book.record(notice)


def fill(order_id: str) -> dict | None:
    return _book.fill(order_id)


def event_for(order_id: str) -> asyncio.Event | None:
    return _book.event_for(order_id)


def latency_ms(order_id: str) -> int | None:
    return _book.latency_ms(order_id)


_state: dict = {"healthy": False, "key": None, "iv": None, "disabled_day": None}
# 시세도 체결통보도 아닌 TR 프레임 — 처음과 1,000번째마다만 남긴다
# (docs/FILL_NOTICE_FOLLOWUP_20261007.md 6.2절: K0STCNI9로 올 가능성).
_QUOTE_TR = "H0STCNT0"
_UNKNOWN_LOG_EVERY = 1000
_unknown_tr_counts: dict[str, int] = {}


def reset() -> None:
    """장부와 구독 상태를 비운다(테스트·재시작용)."""
    _book.reset()
    _state.update(healthy=False, key=None, iv=None, disabled_day=None)
    _unknown_tr_counts.clear()


def healthy() -> bool:
    return bool(_state["healthy"])


def tr_id() -> str:
    return NOTICE_TR["REAL"] if os.getenv("KIS_MODE", "PAPER") == "REAL" else NOTICE_TR["PAPER"]


def should_subscribe() -> bool:
    """설정돼 있고 오늘 구독이 거절된 적이 없으면 True."""
    return enabled() and _state["disabled_day"] != datetime.now(KST).date()


def subscribe_request(approval_key: str) -> str:
    return json.dumps({
        "header": {"approval_key": approval_key, "custtype": "P", "tr_type": "1",
                   "content-type": "utf-8"},
        "body": {"input": {"tr_id": tr_id(), "tr_key": os.getenv("KIS_HTS_ID", "").strip()}},
    }, ensure_ascii=False)


def connection_lost() -> None:
    """세션이 끊기면 key/iv를 버린다. 다음 구독 응답이 새로 준다."""
    _state.update(healthy=False, key=None, iv=None)


def handle_message(raw: str) -> str | None:
    """세션 수신 메시지 하나. 반환: "PONG"(PINGPONG) / "DISABLE"(구독 거절) / None.

    체결통보 구독 응답에서 key/iv를 저장하고, 체결통보 프레임은 복호화해 장부에 쌓는다.
    시세(H0STCNT0)와 그 밖의 메시지는 건드리지 않는다.
    """
    if raw.startswith("{"):
        msg = json.loads(raw)
        header, body = msg.get("header") or {}, msg.get("body") or {}
        tr = header.get("tr_id")
        if tr == "PINGPONG":
            return "PONG"
        if tr not in NOTICE_TR.values() or not body:
            return None
        out = body.get("output") or {}
        if str(body.get("rt_cd")) == "0":
            if out.get("key") and out.get("iv"):
                _state["key"], _state["iv"] = out["key"], out["iv"]
            _state["healthy"] = True
            log("FILL_NOTICE_SUBSCRIBED", level="INFO", tr_id=tr, msg1=body.get("msg1"),
                has_key=bool(out.get("key")))
            return None
        _state["healthy"] = False
        _state["disabled_day"] = datetime.now(KST).date()
        log("FILL_NOTICE_DISABLED", level="WARN", reason="SUBSCRIBE_REJECTED",
            msg_cd=body.get("msg_cd"), msg1=body.get("msg1"))
        return "DISABLE"

    parts = raw.split("|")
    if len(parts) < 4:
        return None
    if parts[1] not in NOTICE_TR.values():
        if parts[1] != _QUOTE_TR:
            _log_unknown_tr(parts)
        return None
    data = parts[3]
    if parts[0] == "1":
        if not (_state["key"] and _state["iv"]):
            log("FILL_NOTICE_UNDECRYPTABLE", level="WARN", reason="NO_KEY")
            return None
        try:
            data = decrypt(str(_state["key"]), str(_state["iv"]), data)
        except Exception as exc:  # noqa: BLE001 — 깨진 프레임 하나가 세션을 끊으면 안 된다
            log("FILL_NOTICE_UNDECRYPTABLE", level="WARN", reason="DECRYPT", error=repr(exc))
            return None
    try:
        count: int | None = int(parts[2])
    except ValueError:
        count = None
    records = split_records(data, count)
    for fields in records:
        notice = parse(fields)
        _log_frame(parts, count, len(records), fields, notice)
        if notice is None:
            continue
        record(notice)
        log("FILL_NOTICE_RECEIVED", level="INFO", ticker=notice.ticker,
            order_id=notice.order_id, qty=notice.qty, price=notice.price,
            exchange_time=notice.hms, side=notice.side)
    return None


def _field(fields: list[str], name: str) -> str | None:
    i = _IDX[name]
    return fields[i] if i < len(fields) else None


def _skip_reason(fields: list[str]) -> str:
    if len(fields) < len(FIELDS):
        return "SHORT"
    if fields[_IDX["CNTG_YN"]] != "2":
        return "NOT_FILL"
    if fields[_IDX["RFUS_YN"]] == "1":
        return "REJECTED"
    return "BAD_VALUE"


def _log_frame(parts: list[str], count: int | None, records: int, fields: list[str],
               notice: Notice | None) -> None:
    """체결통보 레코드 하나의 모양. 고객 ID·계좌번호·계좌명은 남기지 않는다."""
    log("FILL_NOTICE_FRAME", level="INFO", tr_id=parts[1], encrypted=parts[0] == "1",
        header_count=count, records=records, field_count=len(fields),
        cntg_yn=_field(fields, "CNTG_YN"), rfus_yn=_field(fields, "RFUS_YN"),
        acpt_yn=_field(fields, "ACPT_YN"), order_id=_norm(_field(fields, "ODER_NO") or ""),
        booked=notice is not None, reason="FILL" if notice is not None else _skip_reason(fields))


def _log_unknown_tr(parts: list[str]) -> None:
    tr = parts[1][:16]
    n = _unknown_tr_counts.get(tr, 0) + 1
    _unknown_tr_counts[tr] = n
    if n == 1 or n % _UNKNOWN_LOG_EVERY == 0:
        log("WS_FRAME_UNKNOWN_TR", level="WARN", tr_id=tr, encrypted=parts[0] == "1",
            header_count=parts[2][:8], body_len=len(parts[3]), occurrences=n)
