"""KRX OPEN API 일별매매정보·종목기본정보 수집기 — 저장만 한다.

H5 스펙 §6.2 (docs/superpowers/specs/2026-09-30-h5-daily-breakout-hypothesis.md).
성과(신고가·수익률)는 계산하지 않는다. 호출 수와 행 수만 출력한다.
응답 원문을 날짜×시장마다 gzip JSON 으로 원자적으로 저장하고, 이미 있는 파일은
다시 부르지 않는다. 휴장일의 빈 목록도 그대로 저장해 다시 부르지 않는다.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE_URL = "https://data-dbg.krx.co.kr/svc/apis/sto"
ENDPOINTS = {
    "stk": "stk_bydd_trd",
    "ksq": "ksq_bydd_trd",
    "base/stk": "stk_isu_base_info",
    "base/ksq": "ksq_isu_base_info",
}
DAILY_KINDS = ("stk", "ksq")
BASE_KINDS = ("base/stk", "base/ksq")
START = "20100104"
END = "20260929"
DAILY_BUDGET = 9000
RETRIES = 2
DEFAULT_CACHE = ROOT / "data" / "krx_daily"


class AuthError(RuntimeError):
    """401. 'Unauthorized Key'(키 틀림)든 'Unauthorized API Call'(서비스 미승인)든 즉시 멈춘다."""


class TransientError(RuntimeError):
    """5xx·타임아웃·전송 오류·본문 이상. 재시도한 뒤에도 남으면 그 날짜를 건너뛴다."""


def weekdays(start: str, end: str) -> list[str]:
    day = datetime.strptime(start, "%Y%m%d").date()
    last = datetime.strptime(end, "%Y%m%d").date()
    out: list[str] = []
    while day <= last:
        if day.weekday() < 5:
            out.append(day.strftime("%Y%m%d"))
        day += timedelta(days=1)
    return out


def cache_path(cache_dir: Path, kind: str, bas_dd: str) -> Path:
    return cache_dir / kind / f"{bas_dd}.json.gz"


def save_atomic(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False)
    os.replace(tmp, path)


def read_rows(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        rows = json.load(fh)
    return rows if isinstance(rows, list) else []


def fetch_one(
    client: httpx.Client,
    kind: str,
    bas_dd: str,
    key: str,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict]:
    url = f"{BASE_URL}/{ENDPOINTS[kind]}"
    last_error = ""
    for attempt in range(RETRIES + 1):
        if attempt:
            sleep(2.0 * attempt)
        try:
            resp = client.get(url, params={"basDd": bas_dd}, headers={"AUTH_KEY": key})
        except httpx.TransportError as exc:
            last_error = f"전송 오류 {type(exc).__name__}"
            continue
        if resp.status_code == 401:
            try:
                message = str(resp.json().get("respMsg") or "")
            except ValueError:
                message = ""
            raise AuthError(message or "401")
        if resp.status_code >= 500:
            last_error = f"HTTP {resp.status_code}"
            continue
        if resp.status_code != 200:
            raise TransientError(f"HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            last_error = "JSON 아님"
            continue
        rows = body.get("OutBlock_1") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            last_error = "OutBlock_1 없음"
            continue
        return rows
    raise TransientError(last_error or "알 수 없는 실패")
