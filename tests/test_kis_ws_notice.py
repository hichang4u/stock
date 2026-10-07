"""F4 시세 세션에 체결통보 구독을 얹는다(B안) — 앱키당 WS 세션은 하나뿐이다.

설계: docs/superpowers/specs/2026-10-06-fill-notice-design.md §5
"""

import json
from base64 import b64encode
from unittest.mock import AsyncMock

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from src.api import kis_notice, kis_ws
from src.api.kis_notice import FIELDS

KEY = "0123456789abcdef0123456789abcdef"
IV = "abcdef9876543210"


def _encrypt(text):
    cipher = AES.new(KEY.encode(), AES.MODE_CBC, IV.encode())
    return b64encode(cipher.encrypt(pad(text.encode("utf-8"), AES.block_size))).decode()


def _notice_frame():
    f = [""] * len(FIELDS)
    for name, value in {"ODER_NO": "0000001226", "CNTG_QTY": "295", "CNTG_UNPR": "23500",
                        "CNTG_YN": "2", "RFUS_YN": "0", "STCK_SHRN_ISCD": "005930",
                        "STCK_CNTG_HOUR": "091015", "SELN_BYOV_CLS": "02"}.items():
        f[FIELDS.index(name)] = value
    return "1|H0STCNI9|001|" + _encrypt("^".join(f))


def _quote_frame(price="10300"):
    f = [""] * 47
    f[0], f[1], f[2], f[12] = "005930", "091015", price, "7"
    return "0|H0STCNT0|001|" + "^".join(f)


def _ack(tr_id, rt_cd="0"):
    out = {"key": KEY, "iv": IV} if rt_cd == "0" else {}
    return json.dumps({"header": {"tr_id": tr_id, "tr_key": "x", "encrypt": "Y"},
                       "body": {"rt_cd": rt_cd, "msg_cd": "OPSP0000", "msg1": "X",
                                "output": out}})


class FakeWS:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    async def send(self, msg):
        self.sent.append(json.loads(msg))

    async def recv(self):
        if not self.messages:
            raise RuntimeError("closed")
        return self.messages.pop(0)


class Conn:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    kis_notice.reset()
    monkeypatch.setenv("KIS_HTS_ID", "hts-user")
    monkeypatch.setenv("F3_FILL_NOTICE_ENABLED", "1")
    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setattr(kis_ws.auth, "refresh_ws_key", AsyncMock())
    monkeypatch.setattr(kis_ws.auth, "get_ws_key", lambda: "quote-key")
    monkeypatch.setattr(kis_ws, "log", lambda *a, **k: None)
    monkeypatch.setattr(kis_notice, "log", lambda *a, **k: None)
    yield
    kis_notice.reset()


async def _run(monkeypatch, sockets):
    pending = list(sockets)
    state = {"stop": False}
    ticks = []

    async def fake_sleep(_):
        if not pending:
            state["stop"] = True

    monkeypatch.setattr(kis_ws.websockets, "connect", lambda *a, **k: Conn(pending.pop(0)))
    monkeypatch.setattr(kis_ws.asyncio, "sleep", fake_sleep)

    async def on_tick(tick):
        ticks.append(tick)

    await kis_ws.subscribe("005930", on_tick, stop_if=lambda: state["stop"])
    return ticks


def _trs(ws):
    return [m["body"]["input"]["tr_id"] for m in ws.sent]


@pytest.mark.asyncio
async def test_notice_is_subscribed_on_the_same_session_with_the_same_key(monkeypatch):
    ws = FakeWS([_ack("H0STCNT0"), _ack("H0STCNI9")])
    await _run(monkeypatch, [ws])
    assert _trs(ws) == ["H0STCNT0", "H0STCNI9"]
    assert ws.sent[1]["header"]["approval_key"] == "quote-key"
    assert ws.sent[1]["body"]["input"]["tr_key"] == "hts-user"


@pytest.mark.asyncio
async def test_notice_frames_go_to_the_book_not_to_ticks(monkeypatch):
    kis_notice.expect("0000001226", 295)
    ws = FakeWS([_ack("H0STCNT0"), _ack("H0STCNI9"), _notice_frame(), _quote_frame()])
    ticks = await _run(monkeypatch, [ws])
    assert [t["price"] for t in ticks] == [10300.0]
    assert kis_notice.fill("1226")["fill_qty"] == 295


@pytest.mark.asyncio
async def test_rejected_notice_keeps_quotes_and_is_not_resubscribed_today(monkeypatch):
    first = FakeWS([_ack("H0STCNT0"), _ack("H0STCNI9", rt_cd="1"), _quote_frame("10300")])
    second = FakeWS([_quote_frame("10310")])
    ticks = await _run(monkeypatch, [first, second])
    assert [t["price"] for t in ticks] == [10300.0, 10310.0]
    assert _trs(first) == ["H0STCNT0", "H0STCNI9"]
    assert _trs(second) == ["H0STCNT0"]


@pytest.mark.asyncio
async def test_disabled_notice_sends_only_the_quote_subscription(monkeypatch):
    monkeypatch.setenv("F3_FILL_NOTICE_ENABLED", "0")
    ws = FakeWS([_ack("H0STCNT0"), _quote_frame()])
    ticks = await _run(monkeypatch, [ws])
    assert _trs(ws) == ["H0STCNT0"] and len(ticks) == 1


@pytest.mark.asyncio
async def test_reconnect_resubscribes_the_notice(monkeypatch):
    first = FakeWS([_ack("H0STCNT0"), _ack("H0STCNI9")])
    second = FakeWS([_ack("H0STCNT0"), _ack("H0STCNI9")])
    await _run(monkeypatch, [first, second])
    assert _trs(first) == ["H0STCNT0", "H0STCNI9"] and _trs(second) == ["H0STCNT0", "H0STCNI9"]


# ── 시세 구독 응답 로그 (docs/OPENING_WINDOW_FOLLOWUP_20261006.md §1.4) ──


def _rejected(tr_id, msg_cd, msg1):
    return json.dumps({"header": {"tr_id": tr_id, "tr_key": "001440", "encrypt": "N"},
                       "body": {"rt_cd": "9", "msg_cd": msg_cd, "msg1": msg1}})


@pytest.mark.asyncio
async def test_quote_subscribe_responses_are_logged_with_their_codes(monkeypatch):
    logs = []
    monkeypatch.setattr(kis_ws, "log", lambda e, **k: logs.append((e, k)))
    ws = FakeWS([_ack("H0STCNT0"), _ack("H0STCNI9"),
                 _rejected("H0STCNT0", "OPSP8996", "ALREADY IN USE appkey"),
                 json.dumps({"header": {"tr_id": "PINGPONG"}})])
    await _run(monkeypatch, [ws])
    resp = [(k["level"], k["tr_id"], k["rt_cd"], k["msg_cd"])
            for e, k in logs if e == "WS_SUBSCRIBE_RESPONSE"]
    assert resp == [("INFO", "H0STCNT0", "0", "OPSP0000"),
                    ("WARN", "H0STCNT0", "9", "OPSP8996")]
