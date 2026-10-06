# 진입 체결 실시간 통보 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 매수 주문의 체결을 KIS 실시간 체결통보로 알아내, 조회보다 먼저 확인되면 그 즉시 F4 추적을 시작하게 한다.

**Architecture:** 새 모듈 `src/api/kis_notice.py`가 체결통보를 복호화·해석해 주문별 장부에 쌓는다. 구독은 **F4 시세 세션에 얹는다(B안)** — 앱키당 WS 세션이 하나뿐이기 때문이다(Task 0 실측). `f3_entry.py`는 매수 주문을 장부에 등록하고, 조회 대기·취소 직전·체결 스냅샷에서 장부를 함께 본다. 조회 창·취소·재시도 규칙은 바꾸지 않는다.

**Tech Stack:** Python 3.12, `websockets==13.1`, `pycryptodome`(신규), pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-10-06-fill-notice-design.md`

## Global Constraints

- 이 릴리스가 바꾸는 조건은 **진입 체결을 무엇으로 알아내는가** 하나다. 2초 창(`F3_LIMIT_FILL_TIMEOUT_SEC`), 취소, 재시도, 부분체결 규칙은 바꾸지 않는다(CLAUDE.md "조건 하나씩").
- TR: 모의 `H0STCNI9`, 실전 `H0STCNI0`. `tr_key` = `KIS_HTS_ID`.
- 스위치: `F3_FILL_NOTICE_ENABLED`(기본 `"1"`). `KIS_HTS_ID`가 비어 있거나 스위치가 `"0"`이면 통보를 쓰지 않고 기존 조회만 쓴다.
- 통보가 없는 경우(미등록, 비활성)의 `_poll_fill` 동작은 **기존 코드와 한 줄도 다르지 않아야** 한다 — 기존 가짜 시계 테스트가 그대로 통과해야 한다.
- F4 시세 세션의 연결 시점·재연결 규칙·PINGPONG 처리는 바꾸지 않는다. 체결통보 구독은 같은 세션·같은 접속키(`auth.get_ws_key()`)로 시세 구독 직후에 보낸다.
- 값(앱키·HTS ID·접속키·AES key/iv)은 로그·출력에 남기지 않는다.
- 파일 쓰기는 `write_bytes`. ruff `line-length = 100`. mypy 새 오류 0.
- 테스트: `./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider <파일>` (Bash, 저장소 루트 `D:\Private\stock`).
- **장중(08:00~15:30)에는 승격하지 않는다.** Task 0은 15:35 이후에만 돌린다.

---

### Task 0: 실측 관문 — HTS ID 유효성과 동시 세션 (장 마감 후, 주문 없음) — 완료 2026-10-06

> 결과: 동시 세션 불가(`OPSP8996 ALREADY IN USE appkey`), 한 세션에서 시세+통보 구독 가능, HTS ID 구독 성공. → B안으로 재설계(스펙 10절). 아래 Step은 기록으로 남긴다.

**Files:**
- Create: `scripts/fill_notice_probe.py` (진단 전용, 매매 무관)

**Interfaces:**
- Produces: 종료 코드 0(둘 다 성공) / 1(실패) / 2(설정 없음·장중). **1이 나오면 Task 1 이후를 시작하지 않고 사용자에게 보고한다.**

- [ ] **Step 1: Write the probe**

```python
r"""체결통보 구독 실측 — 장 마감 후 개발 트리에서만. 주문을 내지 않는다.

1) H0STCNI9(모의)/H0STCNI0(실전) 구독 응답이 rt_cd=0이고 key/iv가 오는가 → HTS ID 유효
2) 시세 세션(접속키1, H0STCNT0 005930)을 연 채 체결통보 세션(접속키2)을 열어,
   정해진 시간 동안 두 세션이 함께 살아 있는가 → 앱키당 동시 세션 허용

앱키·HTS ID·접속키·AES key/iv 값은 출력하지 않는다(있다/없다만).

    .\.venv\Scripts\python.exe scripts\fill_notice_probe.py --seconds 60
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import websockets
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
KST = ZoneInfo("Asia/Seoul")
WS_URL = os.getenv("KIS_WS_URL", "ws://ops.koreainvestment.com:31000")


async def approval_key() -> str:
    url = f"{os.environ['KIS_BASE_URL']}/oauth2/Approval"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json={
            "grant_type": "client_credentials",
            "appkey": os.environ["KIS_APP_KEY"],
            "secretkey": os.environ["KIS_APP_SECRET"],
        })
    resp.raise_for_status()
    return str(resp.json()["approval_key"])


def subscribe_message(key: str, tr_id: str, tr_key: str) -> str:
    return json.dumps({
        "header": {"approval_key": key, "custtype": "P", "tr_type": "1",
                   "content-type": "utf-8"},
        "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}},
    })


async def watch(name: str, ws, until: float, report: dict) -> None:
    while (left := until - time.monotonic()) > 0:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=left)
        except asyncio.TimeoutError:
            return
        except websockets.ConnectionClosed as exc:
            report[name]["closed"] = repr(exc)
            return
        if isinstance(raw, str) and raw.startswith("{"):
            msg = json.loads(raw)
            header, body = msg.get("header") or {}, msg.get("body") or {}
            if header.get("tr_id") == "PINGPONG":
                report[name]["pingpong"] += 1
                await ws.pong(raw)
                continue
            out = body.get("output") or {}
            report[name]["responses"].append({
                "tr_id": header.get("tr_id"), "rt_cd": body.get("rt_cd"),
                "msg_cd": body.get("msg_cd"), "msg1": body.get("msg1"),
                "has_key": bool(out.get("key")), "has_iv": bool(out.get("iv")),
            })
        else:
            report[name]["frames"] += 1


async def run(seconds: int) -> int:
    now = datetime.now(KST).time()
    if dtime(8, 0) <= now < dtime(15, 35):
        print("장중에는 돌리지 않는다(운영 WS에 영향을 줄 수 있다). 15:35 이후에 다시.")
        return 2
    hts_id = os.getenv("KIS_HTS_ID", "").strip()
    if not hts_id:
        print("KIS_HTS_ID 없음")
        return 2
    tr_id = "H0STCNI0" if os.getenv("KIS_MODE", "PAPER") == "REAL" else "H0STCNI9"
    report = {n: {"closed": None, "pingpong": 0, "frames": 0, "responses": []}
              for n in ("quote", "notice")}

    key1 = await approval_key()
    async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=10) as quote:
        await quote.send(subscribe_message(key1, "H0STCNT0", "005930"))
        await asyncio.sleep(3)
        key2 = await approval_key()   # 두 번째 키 발급이 첫 세션을 끊는지도 본다
        async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=10) as notice:
            await notice.send(subscribe_message(key2, tr_id, hts_id))
            until = time.monotonic() + seconds
            await asyncio.gather(watch("quote", quote, until, report),
                                 watch("notice", notice, until, report))

    print(json.dumps(report, ensure_ascii=False, indent=2))
    subscribed = any(r["tr_id"] == tr_id and r["rt_cd"] == "0" and r["has_key"] and r["has_iv"]
                     for r in report["notice"]["responses"])
    both_alive = report["quote"]["closed"] is None and report["notice"]["closed"] is None
    print(f"체결통보 구독(HTS ID): {'성공' if subscribed else '실패'}")
    print(f"동시 세션 {seconds}초 유지: {'성공' if both_alive else '실패'}")
    return 0 if subscribed and both_alive else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="체결통보 구독 실측 (주문 없음)")
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args(argv)
    return asyncio.run(run(args.seconds))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Lint**

Run: `./.venv/Scripts/python.exe -m ruff check scripts/fill_notice_probe.py`
Expected: All checks passed!

- [ ] **Step 3: Run it after 15:35 and judge**

Run: `PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/fill_notice_probe.py --seconds 60`
Expected: 종료 코드 0, "체결통보 구독(HTS ID): 성공", "동시 세션 60초 유지: 성공".
- 구독 실패(`rt_cd != "0"` 또는 key/iv 없음): HTS ID가 틀렸다. 응답의 `msg_cd`·`msg1`을 사용자에게 보고하고 멈춘다.
- 동시 세션 실패(`quote.closed`가 채워짐): 스펙 10절대로 멈추고 공용 연결(C안) 재설계를 사용자에게 제안한다.

- [ ] **Step 4: Record the result and commit**

결과를 스펙 `docs/superpowers/specs/2026-10-06-fill-notice-design.md` 10절 끝에 날짜와 함께 한 단락으로 적는다(성공/실패, `msg1`, 핑퐁 횟수, 프레임 수 — 값은 쓰지 않는다).

```bash
git add scripts/fill_notice_probe.py docs/superpowers/specs/2026-10-06-fill-notice-design.md
git commit -m "chore(notice): after-close probe for HTS ID and concurrent WS sessions"
```

---

### Task 1: 의존성과 체결통보 핵심(복호화·해석·장부)

**Files:**
- Modify: `requirements.txt` (끝에 추가)
- Create: `src/api/kis_notice.py`
- Test: `tests/test_kis_notice.py`

**Interfaces:**
- Produces (`src/api/kis_notice.py`):
  - `NOTICE_TR: dict[str, str]` = `{"PAPER": "H0STCNI9", "REAL": "H0STCNI0"}`
  - `FIELDS: tuple[str, ...]` (26개, 스펙 3절 순서)
  - `enabled() -> bool`
  - `decrypt(key: str, iv: str, cipher_text: str) -> str`
  - `@dataclass(frozen=True) class Notice: order_id: str; ticker: str; qty: int; price: float; hms: str; side: str`
  - `parse(fields: list[str]) -> Notice | None` — 체결(`CNTG_YN=="2"`)이고 거부 아님(`RFUS_YN!="1"`)만
  - `split_records(body: str, count: int | None) -> list[list[str]]`
  - `class FillBook` + 모듈 함수 `expect(order_id: str, order_qty: int) -> asyncio.Event`, `record(notice: Notice) -> None`, `fill(order_id: str) -> dict | None`, `event_for(order_id: str) -> asyncio.Event | None`, `latency_ms(order_id: str) -> int | None`, `reset() -> None`
  - `fill()` 반환 모양은 `FillSnapshot.as_fill()`과 같다: `{"status", "order_qty", "fill_qty", "remaining_qty", "fill_price"}`

- [ ] **Step 1: Install and pin pycryptodome**

```bash
./.venv/Scripts/python.exe -m pip install pycryptodome
./.venv/Scripts/python.exe -c "import Crypto; print(Crypto.__version__)"
```
`requirements.txt` 끝에 추가(설치된 버전으로 고정):
```
# 실시간 체결통보 복호화 (AES256-CBC, KIS 공식 예제와 같은 라이브러리)
pycryptodome==<위에서 출력된 버전>
```

- [ ] **Step 1b: Default the notice off in tests**

`tests/conftest.py`: right after the existing `os.environ["STOCK_SKIP_DOTENV"] = "1"` line add

```python
# 체결통보는 기본으로 끈다. 기존 진입·F4 테스트가 주문을 장부에 등록하거나 가짜 소켓에 통보 구독을
# 보내 동작이 달라지지 않게 한다. 체결통보 테스트는 각자 monkeypatch로 켠다.
os.environ.setdefault("F3_FILL_NOTICE_ENABLED", "0")
```

- [ ] **Step 2: Write the failing test**

```python
"""KIS 실시간 체결통보 — 복호화·해석·주문별 장부.

설계: docs/superpowers/specs/2026-10-06-fill-notice-design.md
"""

import asyncio
from base64 import b64encode

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from src.api import kis_notice
from src.api.kis_notice import FIELDS, Notice, decrypt, parse, split_records

KEY = "0123456789abcdef0123456789abcdef"   # 32바이트 → AES256
IV = "abcdef9876543210"


def _encrypt(text: str) -> str:
    cipher = AES.new(KEY.encode(), AES.MODE_CBC, IV.encode())
    return b64encode(cipher.encrypt(pad(text.encode("utf-8"), AES.block_size))).decode()


def _fields(order="0000001226", qty="295", price="23500", cntg="2", rfus="0", hms="090008"):
    f = [""] * len(FIELDS)
    values = {"ODER_NO": order, "CNTG_QTY": qty, "CNTG_UNPR": price, "CNTG_YN": cntg,
              "RFUS_YN": rfus, "STCK_SHRN_ISCD": "348340", "STCK_CNTG_HOUR": hms,
              "SELN_BYOV_CLS": "02"}
    for name, value in values.items():
        f[FIELDS.index(name)] = value
    return f


@pytest.fixture(autouse=True)
def _clean_book():
    kis_notice.reset()
    yield
    kis_notice.reset()


def test_fields_follow_the_official_26_columns():
    assert len(FIELDS) == 26
    assert FIELDS[2] == "ODER_NO" and FIELDS[13] == "CNTG_YN"


def test_decrypt_round_trips_an_encrypted_frame():
    text = "^".join(_fields())
    assert decrypt(KEY, IV, _encrypt(text)) == text


def test_parse_reads_a_fill_and_strips_leading_zeros():
    n = parse(_fields())
    assert n == Notice(order_id="1226", ticker="348340", qty=295, price=23500.0,
                       hms="090008", side="02")


def test_parse_ignores_acceptance_and_rejection_notices():
    assert parse(_fields(cntg="1")) is None
    assert parse(_fields(rfus="1")) is None
    assert parse(_fields(qty="0")) is None
    assert parse(["short"]) is None


def test_split_records_uses_the_header_count():
    two = _fields(qty="100") + _fields(qty="195")
    records = split_records("^".join(two), 2)
    assert [r[FIELDS.index("CNTG_QTY")] for r in records] == ["100", "195"]
    assert len(split_records("^".join(_fields()), None)) == 1


def test_book_accumulates_partial_fills_and_signals_at_full():
    event = kis_notice.expect("0000001226", 295)
    kis_notice.record(Notice("1226", "348340", 100, 23500.0, "090008", "02"))
    assert not event.is_set()
    assert kis_notice.fill("0000001226")["status"] == "PARTIAL"
    kis_notice.record(Notice("1226", "348340", 195, 23400.0, "090008", "02"))
    assert event.is_set()
    got = kis_notice.fill("1226")
    assert got["status"] == "FILLED" and got["fill_qty"] == 295 and got["remaining_qty"] == 0
    assert got["fill_price"] == pytest.approx((100 * 23500 + 195 * 23400) / 295)


def test_notice_arriving_before_registration_is_kept():
    kis_notice.record(Notice("1226", "348340", 295, 23500.0, "090008", "02"))
    event = kis_notice.expect("0000001226", 295)
    assert event.is_set() and kis_notice.fill("1226")["fill_qty"] == 295


def test_unknown_order_has_no_fill_or_event():
    assert kis_notice.fill("999") is None and kis_notice.event_for("999") is None


def test_latency_is_measured_from_registration_to_first_fill():
    kis_notice.expect("1226", 295)
    assert kis_notice.latency_ms("1226") is None
    kis_notice.record(Notice("1226", "348340", 295, 23500.0, "090008", "02"))
    assert kis_notice.latency_ms("1226") >= 0


def test_enabled_needs_hts_id_and_the_switch(monkeypatch):
    monkeypatch.setenv("KIS_HTS_ID", "someone")
    monkeypatch.setenv("F3_FILL_NOTICE_ENABLED", "1")
    assert kis_notice.enabled() is True
    monkeypatch.setenv("F3_FILL_NOTICE_ENABLED", "0")
    assert kis_notice.enabled() is False
    monkeypatch.setenv("F3_FILL_NOTICE_ENABLED", "1")
    monkeypatch.setenv("KIS_HTS_ID", " ")
    assert kis_notice.enabled() is False


@pytest.mark.asyncio
async def test_event_can_be_awaited():
    event = kis_notice.expect("1226", 10)
    asyncio.get_running_loop().call_soon(
        kis_notice.record, Notice("1226", "348340", 10, 1.0, "090008", "02"))
    await asyncio.wait_for(event.wait(), timeout=1)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_kis_notice.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.api.kis_notice'`

- [ ] **Step 4: Write minimal implementation**

```python
"""KIS 실시간 체결통보 — 진입 체결을 조회보다 먼저 안다.

설계: docs/superpowers/specs/2026-10-06-fill-notice-design.md

체결통보(H0STCNI9 모의 / H0STCNI0 실전)는 HTS ID로 구독하고, 데이터부는 구독 응답의 key/iv로
AES256-CBC 복호화한다. 체결(CNTG_YN=2) 통보만 주문별 장부에 쌓는다. 장부는 조회 결과와 같은
모양으로 체결을 돌려주고, 전량에 도달하면 주문의 이벤트를 켠다.
"""

from __future__ import annotations

import asyncio
import os
import time
from base64 import b64decode
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

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


def reset() -> None:
    _book.reset()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_kis_notice.py`
Expected: 11 passed

- [ ] **Step 6: Lint, type-check, commit**

```bash
./.venv/Scripts/python.exe -m ruff check src/api/kis_notice.py tests/test_kis_notice.py
./.venv/Scripts/python.exe scripts/mypy_baseline.py
git add requirements.txt src/api/kis_notice.py tests/test_kis_notice.py tests/conftest.py
git commit -m "feat(notice): decrypt, parse and book KIS real-time fill notices"
```

---

### Task 2: F4 시세 세션에 체결통보 구독 얹기 (B안)

**Files:**
- Modify: `src/api/kis_notice.py` (끝에 추가, Task 1의 `reset()` 교체)
- Modify: `src/api/kis_ws.py` — import, `subscribe()` 연결 직후·수신 루프·`finally`, `_parse_ticks`
- Test: `tests/test_kis_notice.py` (끝에 추가), `tests/test_kis_ws_notice.py` (신규), 기존 `tests/test_kis_ws.py`·`tests/test_ws_frame_header.py`

**Interfaces:**
- Consumes: Task 1 전부
- Produces (`kis_notice`):
  - `tr_id() -> str` — `KIS_MODE=REAL`이면 `H0STCNI0`, 아니면 `H0STCNI9`
  - `should_subscribe() -> bool` — `enabled()`이고 오늘 구독이 거절된 적 없음
  - `subscribe_request(approval_key: str) -> str` — JSON 문자열
  - `handle_message(raw: str) -> str | None` — `"PONG"` / `"DISABLE"` / None. 거절 시 오늘 날짜를 기록
  - `connection_lost() -> None`, `healthy() -> bool`, `reset() -> None`(장부+상태)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_kis_notice.py`:

```python
import json


def _ack(tr_id="H0STCNI9", rt_cd="0"):
    out = {"key": KEY, "iv": IV} if rt_cd == "0" else {}
    return json.dumps({"header": {"tr_id": tr_id, "tr_key": "x", "encrypt": "Y"},
                       "body": {"rt_cd": rt_cd, "msg_cd": "OPSP0000",
                                "msg1": "SUBSCRIBE SUCCESS", "output": out}})


def test_handle_message_subscribes_then_books_an_encrypted_fill(monkeypatch):
    logs = []
    monkeypatch.setattr(kis_notice, "log", lambda e, **k: logs.append(e))
    assert kis_notice.handle_message(_ack()) is None
    assert kis_notice.healthy() is True
    kis_notice.expect("0000001226", 295)
    assert kis_notice.handle_message("1|H0STCNI9|001|" + _encrypt("^".join(_fields()))) is None
    assert kis_notice.fill("1226")["fill_qty"] == 295
    assert "FILL_NOTICE_SUBSCRIBED" in logs and "FILL_NOTICE_RECEIVED" in logs


def test_rejected_subscription_disables_for_the_day(monkeypatch):
    monkeypatch.setenv("KIS_HTS_ID", "hts-user")
    monkeypatch.setenv("F3_FILL_NOTICE_ENABLED", "1")
    monkeypatch.setattr(kis_notice, "log", lambda e, **k: None)
    assert kis_notice.should_subscribe() is True
    assert kis_notice.handle_message(_ack(rt_cd="1")) == "DISABLE"
    assert kis_notice.healthy() is False and kis_notice.should_subscribe() is False


def test_quote_traffic_and_pingpong_are_not_notices(monkeypatch):
    monkeypatch.setattr(kis_notice, "log", lambda e, **k: None)
    assert kis_notice.handle_message(json.dumps({"header": {"tr_id": "PINGPONG"}})) == "PONG"
    assert kis_notice.handle_message(_ack(tr_id="H0STCNT0")) is None
    assert kis_notice.healthy() is False
    assert kis_notice.handle_message("0|H0STCNT0|001|005930^091015^10300") is None


def test_encrypted_frame_without_a_key_is_not_booked(monkeypatch):
    logs = []
    monkeypatch.setattr(kis_notice, "log", lambda e, **k: logs.append(e))
    kis_notice.expect("1226", 295)
    kis_notice.handle_message("1|H0STCNI9|001|" + _encrypt("^".join(_fields())))
    assert kis_notice.fill("1226") is None and "FILL_NOTICE_UNDECRYPTABLE" in logs


def test_connection_lost_forgets_the_key(monkeypatch):
    monkeypatch.setattr(kis_notice, "log", lambda e, **k: None)
    kis_notice.handle_message(_ack())
    kis_notice.connection_lost()
    assert kis_notice.healthy() is False
    kis_notice.expect("1226", 295)
    kis_notice.handle_message("1|H0STCNI9|001|" + _encrypt("^".join(_fields())))
    assert kis_notice.fill("1226") is None


def test_subscribe_request_uses_hts_id_and_mode(monkeypatch):
    monkeypatch.setenv("KIS_HTS_ID", "hts-user")
    monkeypatch.setenv("KIS_MODE", "PAPER")
    body = json.loads(kis_notice.subscribe_request("quote-key"))
    assert body["header"]["approval_key"] == "quote-key"
    assert body["body"]["input"] == {"tr_id": "H0STCNI9", "tr_key": "hts-user"}
    monkeypatch.setenv("KIS_MODE", "REAL")
    assert kis_notice.tr_id() == "H0STCNI0"
```

Create `tests/test_kis_ws_notice.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_kis_notice.py tests/test_kis_ws_notice.py`
Expected: FAIL — `AttributeError: module 'src.api.kis_notice' has no attribute 'handle_message'`

- [ ] **Step 3: Append to `src/api/kis_notice.py`**

Add `import json` and `from src.utils.logger import log` to the import block. **Delete** the Task 1 `def reset() -> None: _book.reset()` at the end of the file (the version below replaces it; ruff F811 otherwise). Then append:

```python
_state: dict = {"healthy": False, "key": None, "iv": None, "disabled_day": None}


def reset() -> None:
    """장부와 구독 상태를 비운다(테스트·재시작용)."""
    _book.reset()
    _state.update(healthy=False, key=None, iv=None, disabled_day=None)


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
    if len(parts) < 4 or parts[1] not in NOTICE_TR.values():
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
    for fields in split_records(data, count):
        notice = parse(fields)
        if notice is None:
            continue
        record(notice)
        log("FILL_NOTICE_RECEIVED", level="INFO", ticker=notice.ticker,
            order_id=notice.order_id, qty=notice.qty, price=notice.price,
            exchange_time=notice.hms, side=notice.side)
    return None
```

- [ ] **Step 4: Wire it into `src/api/kis_ws.py`**

(a) import: `from src.api import auth` → `from src.api import auth, kis_notice`

(b) in `subscribe()`, right after `await _send_subscribe(ws, ticker)`:

```python
                # 체결통보를 같은 세션·같은 접속키로 얹는다 — 앱키당 WS 세션은 하나뿐이다
                # (OPSP8996, docs/superpowers/specs/2026-10-06-fill-notice-design.md 10절).
                if kis_notice.should_subscribe():
                    await ws.send(kis_notice.subscribe_request(auth.get_ws_key()))
```

(c) in the receive loop, right before `for tick in _parse_ticks(raw):` add `_route_notice(raw)`, and add this module-level helper right above `async def _send_subscribe(`:

```python
def _route_notice(raw: str) -> None:
    """체결통보 구독 응답·프레임을 kis_notice로 넘긴다. 실패가 시세 수신을 끊으면 안 된다."""
    try:
        kis_notice.handle_message(raw)
    except Exception as exc:  # noqa: BLE001
        log("FILL_NOTICE_ERROR", level="WARN", error=repr(exc))
```

(d) in the existing `finally:` block of the connection loop, add `kis_notice.connection_lost()` as its first line.

(e) in `_parse_ticks`, right after `if len(parts) < 4: return []`:

```python
        if parts[1] != "H0STCNT0":
            return []  # 체결통보 등 다른 TR 프레임은 시세가 아니다
```

- [ ] **Step 5: Run tests**

Run: `./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_kis_notice.py tests/test_kis_ws_notice.py tests/test_kis_ws.py tests/test_ws_frame_header.py tests/test_ws_multi_record_frame.py tests/test_ws_raw_field_preservation.py`
Expected: all passed

- [ ] **Step 6: Lint, type-check, commit**

```bash
./.venv/Scripts/python.exe -m ruff check src/api/kis_notice.py src/api/kis_ws.py tests/test_kis_notice.py tests/test_kis_ws_notice.py
./.venv/Scripts/python.exe scripts/mypy_baseline.py
git add src/api/kis_notice.py src/api/kis_ws.py tests/test_kis_notice.py tests/test_kis_ws_notice.py
git commit -m "feat(notice): subscribe fill notices on the F4 quote session"
```

---

### Task 3: f3_entry 연결 — 체결을 통보로도 안다

**Files:**
- Modify: `src/modules/f3_entry.py` — imports(17행 부근), 상수(194행 `F3_FILL_POLL_INTERVAL_SEC` 아래), `_send_buy`(4077), `_fetch_order_fill_snapshot`(4244), `_poll_fill`(4140), `_cancel_entry_order_confirmed`(2640)
- Test: `tests/test_f3_fill_notice.py`

**Interfaces:**
- Consumes: `kis_notice.expect/fill/event_for/latency_ms/reset`, `Notice`
- Produces: `f3.F3_FILL_NOTICE_ENABLED: bool`, 로그 이벤트 `ENTRY_FILL_CONFIRMED_BY_NOTICE`(stage: `"POLL_WAIT"` | `"BEFORE_CANCEL"`)

- [ ] **Step 1: Write the failing test**

```python
"""진입 체결을 실시간 통보로도 확인한다 — 조회 창·취소·재시도 규칙은 그대로.

설계: docs/superpowers/specs/2026-10-06-fill-notice-design.md §7
"""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from src.api import kis_notice
from src.api.kis_notice import Notice
from src.modules import f3_entry as f3


@pytest.fixture(autouse=True)
def _book(monkeypatch):
    kis_notice.reset()
    monkeypatch.setattr(f3, "F3_FILL_NOTICE_ENABLED", True)
    yield
    kis_notice.reset()


def _notice(qty=295, price=23500.0, order="1226"):
    return Notice(order, "348340", qty, price, "090008", "02")


@pytest.mark.asyncio
async def test_send_buy_registers_the_order_with_the_notice_book(monkeypatch):
    monkeypatch.setattr(f3.kis_rest, "post",
                        AsyncMock(return_value={"rt_cd": "0", "output": {"ODNO": "0000001226"}}))
    await f3._send_buy("348340", 295, "PAPER", limit_price=23700)
    assert kis_notice.event_for("0000001226") is not None


@pytest.mark.asyncio
async def test_send_buy_does_not_register_when_disabled(monkeypatch):
    monkeypatch.setattr(f3, "F3_FILL_NOTICE_ENABLED", False)
    monkeypatch.setattr(f3.kis_rest, "post",
                        AsyncMock(return_value={"rt_cd": "0", "output": {"ODNO": "0000001226"}}))
    await f3._send_buy("348340", 295, "PAPER", limit_price=23700)
    assert kis_notice.event_for("0000001226") is None


@pytest.mark.asyncio
async def test_snapshot_takes_the_notice_when_the_query_lags(monkeypatch):
    kis_notice.expect("0000001226", 295)
    kis_notice.record(_notice())
    monkeypatch.setattr(f3, "_query_order_fill_snapshot", AsyncMock(return_value=None))
    got = await f3._fetch_order_fill_snapshot("0000001226", expected_qty=295)
    assert got["fill_qty"] == 295 and got["status"] == "FILLED"


@pytest.mark.asyncio
async def test_snapshot_keeps_the_query_when_there_is_no_notice(monkeypatch):
    queried = {"status": "PARTIAL", "order_qty": 295, "fill_qty": 10,
               "remaining_qty": 285, "fill_price": 23500}
    monkeypatch.setattr(f3, "_query_order_fill_snapshot", AsyncMock(return_value=queried))
    assert await f3._fetch_order_fill_snapshot("0000001226", expected_qty=295) == queried


@pytest.mark.asyncio
async def test_poll_fill_returns_as_soon_as_the_notice_fills(monkeypatch):
    events = []
    monkeypatch.setattr(f3, "log", lambda e, **k: events.append((e, k)))
    kis_notice.expect("0000001226", 295)
    never = asyncio.Event()

    async def slow_query(order_id, **kwargs):
        await never.wait()

    monkeypatch.setattr(f3, "_query_order_fill_snapshot", slow_query)
    asyncio.get_running_loop().call_later(0.05, kis_notice.record, _notice())
    deadline = f3.datetime.now(f3.KST) + timedelta(seconds=5)

    got = await asyncio.wait_for(
        f3._poll_fill("0000001226", deadline=deadline, ticker="348340", expected_qty=295),
        timeout=2)

    assert got["fill_qty"] == 295
    stages = [k["stage"] for e, k in events if e == "ENTRY_FILL_CONFIRMED_BY_NOTICE"]
    assert stages == ["POLL_WAIT"]


@pytest.mark.asyncio
async def test_cancel_is_not_sent_when_the_notice_already_shows_a_full_fill(monkeypatch):
    events = []
    monkeypatch.setattr(f3, "log", lambda e, **k: events.append((e, k)))
    kis_notice.expect("0000001226", 295)
    kis_notice.record(_notice())
    cancel = AsyncMock(return_value={"rt_cd": "0"})
    monkeypatch.setattr(f3, "_cancel_order", cancel)

    outcome, fill = await f3._cancel_entry_order_confirmed(
        "0000001226", "00950", "PAPER", "348340", 1, 2, expected_qty=295)

    assert outcome == "FILLED" and fill["fill_qty"] == 295
    cancel.assert_not_awaited()
    assert [k["stage"] for e, k in events if e == "ENTRY_FILL_CONFIRMED_BY_NOTICE"] == [
        "BEFORE_CANCEL"]


@pytest.mark.asyncio
async def test_cancel_goes_ahead_when_disabled_even_with_a_full_notice(monkeypatch):
    monkeypatch.setattr(f3, "F3_FILL_NOTICE_ENABLED", False)
    monkeypatch.setattr(f3, "log", lambda e, **k: None)
    kis_notice.expect("0000001226", 295)
    kis_notice.record(_notice())
    cancel = AsyncMock(return_value={"rt_cd": "0"})
    monkeypatch.setattr(f3, "_cancel_order", cancel)
    monkeypatch.setattr(f3, "_query_order_fill_snapshot", AsyncMock(return_value=None))

    await f3._cancel_entry_order_confirmed(
        "0000001226", "00950", "PAPER", "348340", 1, 2, expected_qty=295)

    cancel.assert_awaited_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_f3_fill_notice.py`
Expected: FAIL — `AttributeError: ... has no attribute 'F3_FILL_NOTICE_ENABLED'`

- [ ] **Step 3: Implement in `src/modules/f3_entry.py`**

(a) imports: `from src.api import kis_rest` → `from src.api import kis_notice, kis_rest`; add `cast` to `from typing import Literal` → `from typing import Literal, cast`.

(b) constants, right after the `F3_FILL_POLL_INTERVAL_SEC = max(...)` block:

```python
# 진입 체결을 실시간 체결통보로도 확인한다(docs/superpowers/specs/2026-10-06-fill-notice-design.md).
# 끄면 기존 조회만 쓴다. F3_ 접두어라 지문에 잡힌다.
F3_FILL_NOTICE_ENABLED = os.getenv("F3_FILL_NOTICE_ENABLED", "1") == "1"
_NOTICE_WON = object()


def _notice_fill(order_id: str) -> dict | None:
    return kis_notice.fill(order_id) if F3_FILL_NOTICE_ENABLED else None


def _log_notice_confirmed(order_id: str, ticker: str | None, stage: str, fill: dict) -> None:
    log("ENTRY_FILL_CONFIRMED_BY_NOTICE", level="INFO", ticker=ticker, order_id=order_id,
        stage=stage, fill_qty=fill.get("fill_qty"), fill_price=fill.get("fill_price"),
        notice_latency_ms=kis_notice.latency_ms(order_id))
```

(c) `_send_buy`: replace `return await kis_rest.post(` … `)` with

```python
    resp = await kis_rest.post(
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
    # 주문번호를 체결통보 장부에 등록한다. 통보가 주문 응답보다 먼저 와도 장부가 합친다.
    odno = str(((resp or {}).get("output") or {}).get("ODNO") or "") if isinstance(resp, dict) else ""
    if F3_FILL_NOTICE_ENABLED and odno:
        kis_notice.expect(odno, qty)
    return resp
```

(d) `_fetch_order_fill_snapshot`: rename the existing function to `_query_order_fill_snapshot` (same signature and body), then add right after it:

```python
async def _fetch_order_fill_snapshot(
    order_id: str,
    *,
    ticker: str | None = None,
    expected_qty: int | None = None,
    update_poll_summary: bool = False,
) -> dict | None:
    """조회 한 번 + 체결통보 장부. 더 많이 체결된 쪽을 돌려준다."""
    queried = await _query_order_fill_snapshot(
        order_id, ticker=ticker, expected_qty=expected_qty,
        update_poll_summary=update_poll_summary,
    )
    notice = _notice_fill(order_id)
    return queried if notice is None else _more_complete_fill(queried, notice)
```

In `tests/test_f3_entry.py` line 14, `_REAL_FETCH_ORDER_FILL_SNAPSHOT = f3._fetch_order_fill_snapshot` stays as is (it now points to the wrapper, which calls the real query).

(e) `_poll_fill`: add these two helpers right above `async def _poll_fill(`:

```python
async def _fetch_or_notice(
    order_id: str, event: asyncio.Event, timeout: float, *,
    ticker: str | None, expected_qty: int | None,
) -> object:
    """조회 한 번과 체결통보 신호를 경합시킨다. 신호가 먼저면 _NOTICE_WON."""
    fetch = asyncio.ensure_future(_fetch_order_fill_snapshot(
        order_id, ticker=ticker, expected_qty=expected_qty, update_poll_summary=True))
    waiter = asyncio.ensure_future(event.wait())
    done, _ = await asyncio.wait({fetch, waiter}, timeout=max(0.001, timeout),
                                 return_when=asyncio.FIRST_COMPLETED)
    if waiter in done and fetch not in done:
        fetch.cancel()
        return _NOTICE_WON
    waiter.cancel()
    if fetch not in done:
        fetch.cancel()
        raise asyncio.TimeoutError
    return fetch.result()


async def _wait_or_notice(event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
```

Inside `_poll_fill`, make exactly these changes:
- after the `_last_fill_poll_summary = {...}` block, before `while True:`, add
  `notice_event = kis_notice.event_for(order_id) if F3_FILL_NOTICE_ENABLED else None`
- at the very top of the `while True:` body, before `if datetime.now(KST) >= deadline:`, add:

```python
        if notice_event is not None and notice_event.is_set():
            notice = _notice_fill(order_id)
            if notice is not None:
                _log_notice_confirmed(order_id, ticker, "POLL_WAIT", notice)
                if expected_qty is None:
                    return {"fill_price": notice["fill_price"], "fill_qty": notice["fill_qty"]}
                return notice
```

- replace

```python
            latest = await asyncio.wait_for(
                _fetch_order_fill_snapshot(
                    order_id,
                    ticker=ticker,
                    expected_qty=expected_qty,
                    update_poll_summary=True,
                ),
                timeout=max(0.001, remaining),
            )
```
with

```python
            if notice_event is None:
                latest = await asyncio.wait_for(
                    _fetch_order_fill_snapshot(
                        order_id,
                        ticker=ticker,
                        expected_qty=expected_qty,
                        update_poll_summary=True,
                    ),
                    timeout=max(0.001, remaining),
                )
            else:
                result = await _fetch_or_notice(
                    order_id, notice_event, remaining, ticker=ticker, expected_qty=expected_qty)
                if result is _NOTICE_WON:
                    continue
                latest = cast("dict | None", result)
```

- replace the last line of the loop `await asyncio.sleep(min(F3_FILL_POLL_INTERVAL_SEC, remaining))` with

```python
        if notice_event is None:
            await asyncio.sleep(min(F3_FILL_POLL_INTERVAL_SEC, remaining))
        else:
            await _wait_or_notice(notice_event, min(F3_FILL_POLL_INTERVAL_SEC, remaining))
```

(f) `_cancel_entry_order_confirmed`: at the top of the body, right after the docstring and before `cancel_resp = await _cancel_order(order_id, org_no, mode)`:

```python
    notice = _notice_fill(order_id)
    if notice is not None and _is_confirmed_full_fill(notice, expected_qty):
        _log_notice_confirmed(order_id, ticker, "BEFORE_CANCEL", notice)
        return "FILLED", _more_complete_fill(known_fill, notice)
```

- [ ] **Step 4: Run new and existing f3 tests**

Run: `./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_f3_fill_notice.py tests/test_f3_entry.py`
Expected: all passed (기존 `_poll_fill` 가짜 시계 테스트 포함 — 통보 미등록 경로가 기존과 같다)

- [ ] **Step 5: Lint, type-check, commit**

```bash
./.venv/Scripts/python.exe -m ruff check src/modules/f3_entry.py tests/test_f3_fill_notice.py
./.venv/Scripts/python.exe scripts/mypy_baseline.py
git add src/modules/f3_entry.py tests/test_f3_fill_notice.py
git commit -m "feat(f3): learn the entry fill from the fill notice or the poll, whichever is first"
```

---

### Task 4: 기동·지문·로그 라벨·스킬

**Files:**
- Modify: `src/release.py:29` (`_STRATEGY_FILES`)
- Modify: `src/utils/logger.py` (`EVENT_LABELS`)
- Modify: `CLAUDE.md` ("19개 파일" → "20개 파일")
- Modify: `.claude/skills/daily-log/SKILL.md` (4.2 추가)
- Modify: `docs/superpowers/specs/2026-10-06-fill-notice-design.md` (상태 줄)
- Test: 전체

**Interfaces:**
- Consumes: Task 1–3 결과

- [ ] **Step 1: main.py — 변경 없음**

B안에서는 별도 태스크를 띄우지 않는다. 체결통보 구독은 F4가 여는 시세 세션에 얹힌다(Task 2).

- [ ] **Step 2: release.py, logger labels, CLAUDE.md**

`src/release.py` `_STRATEGY_FILES`의 `"src/api/kis_ws.py",` 다음 줄에 `"src/api/kis_notice.py",`.

`src/utils/logger.py` `EVENT_LABELS`에 추가:
```python
    "FILL_NOTICE_SUBSCRIBED": "체결통보 구독 성공(Fill Notice Subscribed)",
    "FILL_NOTICE_DISABLED": "체결통보 비활성화(Fill Notice Disabled)",
    "FILL_NOTICE_ERROR": "체결통보 처리 오류(Fill Notice Error)",
    "FILL_NOTICE_UNDECRYPTABLE": "체결통보 복호화 불가(Fill Notice Undecryptable)",
    "FILL_NOTICE_RECEIVED": "체결통보 수신(Fill Notice Received)",
    "ENTRY_FILL_CONFIRMED_BY_NOTICE": "진입 체결 통보로 확인(Entry Fill Confirmed By Notice)",
```

`CLAUDE.md`의 "`src/release.py`의 `_STRATEGY_FILES`(19개 파일)" → "(20개 파일)".

- [ ] **Step 3: daily-log skill 4.2**

`.claude/skills/daily-log/SKILL.md`의 `## 5. 청산이 있었으면` 바로 위에 추가:

~~~markdown
### 4.2 체결통보 (2026-10 이후)

```bash
cd /d/Private/stock-prod && PYTHONIOENCODING=utf-8 python -c "
import json
for l in open('data/logs/<YYYYMMDD>.jsonl',encoding='utf-8'):
    d=json.loads(l); e=d.get('event','')
    if e.startswith('FILL_NOTICE') or e in ('ENTRY_ORDER_SENT','ENTRY_FILL_CONFIRMED_BY_NOTICE','ENTRY_EXECUTED'):
        x={k:v for k,v in d.items() if k in ('order_id','stage','notice_latency_ms','qty','price','exchange_time','reason','msg1','error')}
        print(d['ts'][11:23], e, x)
"
```

- `FILL_NOTICE_SUBSCRIBED`가 F4 시세 세션이 열릴 때마다(재연결 포함) 있어야 한다. `FILL_NOTICE_DISABLED`면 사유를 보고 맨 앞에 쓴다.
- 진입마다 `ENTRY_ORDER_SENT` → `FILL_NOTICE_RECEIVED` → (`ENTRY_FILL_CONFIRMED_BY_NOTICE` 또는 조회 확인) 순서를 본다. 통보 없이 조회로만 확인된 진입은 "통보 누락"으로 센다.
- `notice_latency_ms`(주문 등록 → 첫 체결 통보)와 조회 확인 시각을 비교한다.
~~~

- [ ] **Step 4: Full verification**

```bash
./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
./.venv/Scripts/python.exe -m ruff check src scripts tests main.py
./.venv/Scripts/python.exe scripts/mypy_baseline.py
```
Expected: all passed / 0 / 새 오류 없음. mypy가 행 번호만 밀린 기존 항목을 보고하면 `scripts/mypy_baseline.py --write`로 다시 동결한다.

Then PowerShell: `.\scripts\preflight.ps1` → "사전 검사 통과", 지문 변경 출력 확인.

- [ ] **Step 5: Spec status and commit**

스펙의 `상태:` 줄 → `상태: 구현 완료, 승격 대기 (2026-10-06)`.

```bash
git add src/release.py src/utils/logger.py CLAUDE.md .claude/skills/daily-log/SKILL.md docs/superpowers/specs/2026-10-06-fill-notice-design.md mypy-baseline.txt
git commit -m "feat(notice): start the fill-notice session at boot and fingerprint it"
```

---

### Task 5: 배포 (장 마감 후, 사용자 확인 후)

**Files:** 없음(운영 절차)

- [ ] **Step 1: 운영 가상환경에 의존성 설치** (15:35 이후)

```powershell
D:\Private\stock-prod\.venv\Scripts\python.exe -m pip install pycryptodome==<requirements.txt의 버전>
D:\Private\stock-prod\.venv\Scripts\python.exe -c "import Crypto; print(Crypto.__version__)"
```

- [ ] **Step 2: 푸시와 태그**

```bash
git push origin main
git tag release/<YYYYMMDD>-<n>
git push origin release/<YYYYMMDD>-<n>
```

- [ ] **Step 3: 승격 (메모리 promote-from-agent-session 절차)**

Bash에서 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./scripts/promote.ps1 -Tag <태그>`(운영 트리에서) → 1패스가 알려준 지문으로 `-AcknowledgeFingerprint <값>` 2패스. 2패스가 끝나지 않으면 `main.py` 부모 체인과 생존을 확인한 뒤 백그라운드 작업을 멈춘다.

- [ ] **Step 4: 승격 직후 확인**

운영 트리 `git describe --tags`, 로그의 `STRATEGY_FINGERPRINT_LOCKED`, `restart_guard.py --root .` 안전. `FILL_NOTICE_*`는 다음 거래일 F4가 시세 세션을 열 때 처음 나온다.

- [ ] **Step 5: 다음 거래일 daily-log 4.2로 통보 수신·확정 출처 확인 후 스펙에 결과 기록**

---

## Self-Review

- **Spec coverage:** §2 결정 → Task 1–3. §3 KIS 사양 → Task 1(FIELDS, decrypt, parse), Task 2(구독 메시지, key/iv). §4 구성 → Task 1–4 파일 전부. §5 구독 수명(B안) → Task 2(같은 세션·같은 키, 재연결 시 재구독, 거절 시 당일 중단, PINGPONG 불변, 시세 해석은 H0STCNT0만, 끊기면 key 폐기, 미설정 시 미구독). §6 장부 → Task 1(누적, 앞자리 0, 등록 전 통보, 신호, 날짜 초기화). §7 f3 → Task 3(a–f). §8 설정 → Task 1 `enabled()`, Task 3 상수. §9 측정 → Task 2 `FILL_NOTICE_RECEIVED`, Task 3 `notice_latency_ms`, Task 4 스킬. §10 실측 → Task 0(관문). §11 테스트 → Task 1–3. §12 배포 → Task 5. §13 범위 밖 → 다루지 않음.
- **Placeholder scan:** `pycryptodome==<위에서 출력된 버전>`과 `release/<YYYYMMDD>-<n>`은 실행 시점에만 정해지는 값이라 방법을 함께 적었다. 그 밖에 없음.
- **Type consistency:** `kis_notice.fill()` 반환 dict 키가 `FillSnapshot.as_fill()`과 같다(`status, order_qty, fill_qty, remaining_qty, fill_price`). `_NOTICE_WON` 비교 후 `cast("dict | None", result)`. B안이라 `auth`는 바꾸지 않는다. `kis_notice.should_subscribe/subscribe_request/handle_message/connection_lost`를 Task 2 테스트·구현·kis_ws가 같은 이름으로 쓴다. `F3_FILL_NOTICE_ENABLED`는 f3 모듈 상수(bool)이고 `kis_notice.enabled()`는 연결 측 환경 확인이다.
