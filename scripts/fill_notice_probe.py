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
import time
from datetime import datetime
from datetime import time as dtime
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
    report: dict[str, dict] = {n: {"closed": None, "pingpong": 0, "frames": 0, "responses": []}
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
