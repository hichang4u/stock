"""H2 재료 공시 라벨 — 후보 유니버스 쌍마다 전일 공시·수급 라벨을 붙인다. 읽기 전용.

라벨 정의는 docs/superpowers/specs/2026-09-17-h2-catalyst-disclosure-hypothesis.md §2에
고정돼 있다. 여기의 화이트리스트·창 계산은 그 문서를 옮겨 적은 것이고, 판정 전에는
바꾸지 않는다.

운영 코드는 이 스크립트를 부르지 않고, 이 스크립트는 운영 상태를 건드리지 않는다.
DART는 `DART_API_KEY`(개발 트리 .env)로, KIS 수급은 백필과 같은 조건(PAPER·15:40 이후·
읽기 전용 토큰)으로 부른다.

    python scripts/catalyst_label.py --universes data/replay/universes.json \
        --out data/catalyst/labels.jsonl [--skip-flow]
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

KST = ZoneInfo("Asia/Seoul")

# ── §2.1 화이트리스트 — 판정까지 고정 ─────────────────────────────────────
MATERIAL_KEYWORDS = (
    "단일판매", "공급계약", "영업(잠정)실적", "영업실적", "손익구조",
    "합병", "분할", "주식교환", "주식이전",
    "유상증자", "무상증자",
    "대량보유",
    "특허", "임상", "품목허가",
    "자기주식취득", "자기주식 취득",
)
CORRECTION_PREFIXES = ("[기재정정]", "[첨부정정]", "[첨부추가]")

DART_BASE = "https://opendart.fss.or.kr/api"
DART_INTERVAL_SEC = 0.2
CORP_CODE_CACHE = ROOT / "data" / "catalyst" / "corp_codes.json"

INVESTOR_PATH = "/uapi/domestic-stock/v1/quotations/inquire-investor"
INVESTOR_TR = "FHKST01010900"


# ── 순수 함수 ─────────────────────────────────────────────────────────────


def classify_report(report_nm: str) -> str:
    """보고서명 하나 → MATERIAL / OTHER. 정정 접두어는 떼고 원본 유형으로 본다."""
    name = report_nm.strip()
    changed = True
    while changed:
        changed = False
        for prefix in CORRECTION_PREFIXES:
            if name.startswith(prefix):
                name = name[len(prefix):].strip()
                changed = True
    return "MATERIAL" if any(k in name for k in MATERIAL_KEYWORDS) else "OTHER"


def label_pair(report_names: list[str]) -> dict:
    """창 안의 보고서명 목록 → 쌍 라벨. 하나라도 MATERIAL이면 MATERIAL."""
    matched = [n for n in report_names if classify_report(n) == "MATERIAL"]
    if not report_names:
        label = "NONE"
    elif matched:
        label = "MATERIAL"
    else:
        label = "OTHER"
    return {
        "label": label,
        "matched_report_nm": matched,
        "report_nm": [n.strip() for n in report_names],
        "disclosure_count": len(report_names),
    }


def corp_code_for(mapping: dict[str, str], ticker: str) -> str | None:
    """종목코드 → corp_code. 우선주(끝자리 ≠ 0)는 보통주 코드로 폴백한다 — DART는 회사
    단위라 우선주 종목코드가 목록에 없다."""
    found = mapping.get(ticker)
    if found is None and len(ticker) == 6 and ticker[-1] != "0":
        found = mapping.get(ticker[:5] + "0")
    return found


def label_window(date: str, trading_dates: list[str]) -> tuple[str, str] | None:
    """직전 거래일 ~ D의 전날(달력일). 직전 거래일이 없으면 None."""
    earlier = [d for d in trading_dates if d < date]
    if not earlier:
        return None
    start = max(earlier)
    end = (datetime.strptime(date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    return start, end


def flow_from_investor_rows(rows: list[dict], date: str) -> tuple[bool | None, str]:
    """KIS 투자자 일별 행에서 해당일 외국인+기관 순매수 부호. 없으면 (None, OUT_OF_RANGE)."""
    for row in rows:
        if str(row.get("stck_bsop_date")) != date:
            continue
        total = int(float(row.get("frgn_ntby_qty") or 0)) + int(float(row.get("orgn_ntby_qty") or 0))
        return total > 0, "KIS"
    return None, "OUT_OF_RANGE"


def load_universe_pairs(path: Path) -> tuple[list[str], list[dict]]:
    """replay_universe.py 산출물 → (거래일 목록, 쌍 목록)."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    days = doc["days"]
    dates = sorted(days)
    pairs = [
        {"date": date, "ticker": str(row["ticker"]), "rank": int(row["rank"])}
        for date in dates
        for row in days[date]
    ]
    return dates, pairs


# ── DART ──────────────────────────────────────────────────────────────────


def _dart_key() -> str:
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key:
        raise SystemExit("DART_API_KEY 가 .env 에 없다 (개발 트리 전용).")
    return key


def load_corp_codes(client: httpx.Client, key: str, cache: Path = CORP_CODE_CACHE) -> dict[str, str]:
    """종목코드 → corp_code. 한 번 받아 캐시한다 (zip 안의 CORPCODE.xml)."""
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    resp = client.get(f"{DART_BASE}/corpCode.xml", params={"crtfc_key": key}, timeout=60)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml = zf.read(zf.namelist()[0])
    mapping: dict[str, str] = {}
    for item in ElementTree.fromstring(xml).iter("list"):
        stock = (item.findtext("stock_code") or "").strip()
        corp = (item.findtext("corp_code") or "").strip()
        if stock and corp:
            mapping[stock] = corp
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return mapping


def fetch_report_names(
    client: httpx.Client, key: str, corp_code: str, bgn: str, end: str
) -> list[str]:
    """창 안의 보고서명 전부. status 013(조회 결과 없음)은 빈 목록이다."""
    resp = client.get(
        f"{DART_BASE}/list.json",
        params={
            "crtfc_key": key, "corp_code": corp_code,
            "bgn_de": bgn, "end_de": end, "page_count": 100,
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    status = body.get("status")
    if status == "013":
        return []
    if status != "000":
        raise RuntimeError(f"DART {status}: {body.get('message')}")
    return [str(item.get("report_nm", "")) for item in body.get("list", [])]


def label_disclosures(dates: list[str], pairs: list[dict]) -> list[dict]:
    key = _dart_key()
    rows: list[dict] = []
    with httpx.Client() as client:
        corp_codes = load_corp_codes(client, key)
        for pair in pairs:
            window = label_window(pair["date"], dates)
            row = dict(pair)
            if window is None:
                row.update(label="NONE", matched_report_nm=[], report_nm=[], disclosure_count=0,
                           window_bgn=None, window_end=None, label_source="NO_PREV_TRADING_DAY")
                rows.append(row)
                continue
            corp = corp_code_for(corp_codes, pair["ticker"])
            if corp is None:
                row.update(label="NONE", matched_report_nm=[], report_nm=[], disclosure_count=0,
                           window_bgn=window[0], window_end=window[1], label_source="NO_CORP_CODE")
                rows.append(row)
                continue
            time.sleep(DART_INTERVAL_SEC)
            names = fetch_report_names(client, key, corp, window[0], window[1])
            row.update(label_pair(names))
            row.update(window_bgn=window[0], window_end=window[1], label_source="DART")
            rows.append(row)
            print(f"{pair['date']} {pair['ticker']} r{pair['rank']} {row['label']:8} "
                  f"{row['disclosure_count']:2}건 {row['matched_report_nm']}")
    return rows


# ── KIS 수급 (보고용) ──────────────────────────────────────────────────────


async def label_flows(dates: list[str], rows: list[dict], interval: float) -> None:
    """종목당 1회 조회해 직전 거래일의 외국인+기관 순매수 부호를 붙인다."""
    from scripts.track_b_backfill import assert_backfill_window, assert_paper_mode
    from src.api import auth, kis_rest

    assert_paper_mode()
    assert_backfill_window(datetime.now(KST))
    if not await auth.load_or_refresh():
        raise SystemExit("TOKEN_UNAVAILABLE")

    cache: dict[str, list[dict]] = {}
    try:
        for row in rows:
            ticker = row["ticker"]
            if ticker not in cache:
                await asyncio.sleep(interval)
                resp = await kis_rest.get(
                    INVESTOR_PATH, tr_id=INVESTOR_TR,
                    params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
                    request_priority=kis_rest.REQUEST_PRIORITY_BACKGROUND,
                )
                cache[ticker] = resp.get("output") or []
            prev = label_window(row["date"], dates)
            if prev is None:
                row.update(flow_pos=None, flow_source="NO_PREV_TRADING_DAY")
                continue
            flow, source = flow_from_investor_rows(cache[ticker], prev[0])
            row.update(flow_pos=flow, flow_source=source)
    finally:
        await kis_rest.close_client()


# ── 진입점 ────────────────────────────────────────────────────────────────


def write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="H2 재료 공시·수급 라벨")
    parser.add_argument("--universes", type=Path, default=ROOT / "data/replay/universes.json")
    parser.add_argument("--out", type=Path, default=ROOT / "data/catalyst/labels.jsonl")
    parser.add_argument("--skip-flow", action="store_true", help="KIS 수급 조회를 생략한다")
    parser.add_argument("--interval", type=float, default=1.2, help="KIS 호출 간격(초)")
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env")
    dates, pairs = load_universe_pairs(args.universes)
    print(f"유니버스 {len(dates)}거래일 / {len(pairs)}쌍")

    rows = label_disclosures(dates, pairs)
    if args.skip_flow:
        for row in rows:
            row.update(flow_pos=None, flow_source="SKIPPED")
    else:
        asyncio.run(label_flows(dates, rows, args.interval))

    write_rows(args.out, rows)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["label"]] = counts.get(row["label"], 0) + 1
    print(f"라벨 분포 {counts} → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
