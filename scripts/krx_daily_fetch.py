"""KRX OPEN API 일별매매정보·종목기본정보 수집기 — 저장만 한다.

H5 스펙 §6.2 (docs/superpowers/specs/2026-09-30-h5-daily-breakout-hypothesis.md).
성과(신고가·수익률)는 계산하지 않는다. 호출 수와 행 수만 출력한다.
응답 원문을 날짜×시장마다 gzip JSON 으로 원자적으로 저장하고, 이미 있는 파일은
다시 부르지 않는다. 휴장일의 빈 목록도 그대로 저장해 다시 부르지 않는다.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass
class RunStats:
    calls: int = 0
    saved: int = 0
    skipped_existing: int = 0
    failed: int = 0
    rows: int = 0
    stopped: str = ""


def month_first_trading_days(cache_dir: Path, days: list[str]) -> list[str]:
    """월마다 두 시장 중 한쪽이라도 행이 있는 첫 평일.

    그 달 앞쪽에 아직 파일이 없는 날이 있으면 그 달은 보류한다 — 실패로 빠진 날을
    건너뛰어 둘째 거래일을 첫 거래일로 잘못 잡지 않기 위해서다.
    """
    by_month: dict[str, list[str]] = {}
    for bas_dd in days:
        by_month.setdefault(bas_dd[:6], []).append(bas_dd)
    firsts: list[str] = []
    for month in sorted(by_month):
        for bas_dd in by_month[month]:
            paths = [cache_path(cache_dir, kind, bas_dd) for kind in DAILY_KINDS]
            if not all(p.exists() for p in paths):
                break
            if any(read_rows(p) for p in paths):
                firsts.append(bas_dd)
                break
    return firsts


def record_failure(cache_dir: Path, kind: str, bas_dd: str, error: str) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "basDd": bas_dd,
        "error": error,
    }
    with open(cache_dir / "failures.jsonl", "a", encoding="utf-8", newline="") as fh:
        print(json.dumps(entry, ensure_ascii=False), file=fh)


def run(
    cache_dir: Path,
    key: str,
    *,
    client: httpx.Client,
    start: str = START,
    end: str = END,
    budget: int = DAILY_BUDGET,
    sleep: Callable[[float], None] = time.sleep,
) -> RunStats:
    stats = RunStats()
    days = weekdays(start, end)

    def process(kind: str, bas_dd: str) -> bool:
        """False 면 이번 실행을 멈춘다."""
        path = cache_path(cache_dir, kind, bas_dd)
        if path.exists():
            stats.skipped_existing += 1
            return True
        if stats.calls >= budget:
            stats.stopped = "BUDGET"
            return False
        stats.calls += 1
        try:
            rows = fetch_one(client, kind, bas_dd, key, sleep=sleep)
        except AuthError as exc:
            stats.stopped = f"AUTH: {exc}"
            return False
        except TransientError as exc:
            stats.failed += 1
            record_failure(cache_dir, kind, bas_dd, str(exc))
            return True
        save_atomic(path, rows)
        stats.saved += 1
        stats.rows += len(rows)
        return True

    for bas_dd in days:
        for kind in DAILY_KINDS:
            if not process(kind, bas_dd):
                return stats
    for bas_dd in month_first_trading_days(cache_dir, days):
        for kind in BASE_KINDS:
            if not process(kind, bas_dd):
                return stats
    return stats


def load_key() -> str:
    value = os.getenv("KRX_AUTH_KEY", "").strip()
    if value:
        return value
    if os.getenv("STOCK_SKIP_DOTENV", "0") == "1":
        return ""
    from dotenv import dotenv_values

    return str(dotenv_values(ROOT / ".env").get("KRX_AUTH_KEY") or "").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KRX 일별매매정보·종목기본정보 수집 (H5, 저장만)")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--budget", type=int, default=DAILY_BUDGET, help="이번 실행의 최대 호출 수")
    args = parser.parse_args(argv)

    key = load_key()
    if not key:
        print("KRX_AUTH_KEY 가 비어 있습니다 (.env).", file=sys.stderr)
        return 2
    with httpx.Client(timeout=60) as client:
        stats = run(args.cache_dir, key, client=client, budget=args.budget)
    print(
        f"호출 {stats.calls} · 저장 {stats.saved} · 기존 {stats.skipped_existing} · "
        f"실패 {stats.failed} · 행 {stats.rows}"
    )
    if stats.stopped:
        print(f"멈춤: {stats.stopped}")
    return 2 if stats.stopped.startswith("AUTH") else 0


if __name__ == "__main__":
    raise SystemExit(main())
