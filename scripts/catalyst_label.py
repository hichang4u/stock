"""H2 재료 공시 라벨 — 후보 유니버스 쌍마다 전일 공시·수급 라벨을 붙인다. 읽기 전용.

라벨 정의는 docs/superpowers/specs/2026-09-17-h2-catalyst-disclosure-hypothesis.md §2에
고정돼 있다. 여기의 화이트리스트·창 계산은 그 문서를 옮겨 적은 것이고, 판정 전에는
바꾸지 않는다.

"직전 거래일"은 유니버스가 아니라 거래일 캘린더로 센다 — 유니버스는 봇이 후보를 잠근
날만 담아서(8/25·9/7 같은 거래일이 빠진다) 그것으로 창을 그으면 창이 넓어진다.
캘린더는 KIS 일봉(005930)에서 받아 `data/catalyst/trading_days.json`에 누적 캐시한다.

운영 코드는 이 스크립트를 부르지 않고, 이 스크립트는 운영 상태를 건드리지 않는다.
DART는 `DART_API_KEY`(개발 트리 .env)로, KIS(캘린더·수급)는 백필과 같은 조건(PAPER·
15:40 이후·읽기 전용 토큰)으로 부른다.

    python scripts/catalyst_label.py --universes data/replay/universes.json \
        --out data/catalyst/labels.jsonl [--skip-flow]

`--skip-flow`는 KIS를 전혀 부르지 않는다. 그때 캘린더는 캐시에서 읽고, 캐시가 유니버스
기간을 덮지 못하면 유니버스 날짜로 대신하되 각 행에 `window_source="UNIVERSE_DAYS"`로
남긴다 — 조용히 넓은 창을 쓰지 않는다.
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

from scripts.replay_universe import load_universe_pairs  # noqa: E402

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
CATALYST_DIR = ROOT / "data" / "catalyst"
CORP_CODE_CACHE = CATALYST_DIR / "corp_codes.json"
TRADING_DAYS_CACHE = CATALYST_DIR / "trading_days.json"

INVESTOR_PATH = "/uapi/domestic-stock/v1/quotations/inquire-investor"
INVESTOR_TR = "FHKST01010900"
DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
DAILY_TR = "FHKST03010100"
CALENDAR_TICKER = "005930"  # 거래일마다 반드시 봉이 있는 종목
CALENDAR_LOOKBACK_DAYS = 14  # 창 계산에 유니버스 첫날 앞 거래일이 필요하다


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


def corp_code_for(mapping: dict[str, str], ticker: str) -> tuple[str | None, bool]:
    """종목코드 → (corp_code, 보통주 폴백 여부). DART는 회사 단위라 우선주 코드가 목록에
    없다 — 끝자리 ≠ 0이면 끝자리를 0으로 바꿔 다시 찾고, 그랬다는 사실을 돌려준다."""
    found = mapping.get(ticker)
    if found is not None:
        return found, False
    if len(ticker) == 6 and ticker[-1] != "0":
        fallback = mapping.get(ticker[:5] + "0")
        if fallback is not None:
            return fallback, True
    return None, False


def label_window(date: str, trading_dates: list[str]) -> tuple[str, str] | None:
    """직전 거래일 ~ D의 전날(달력일). 직전 거래일이 없으면 None."""
    earlier = [d for d in trading_dates if d < date]
    if not earlier:
        return None
    start = max(earlier)
    end = (datetime.strptime(date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    return start, end


def trading_days_from_daily_chart(rows: list[dict]) -> set[str]:
    """KIS 일봉 output2 → 거래일 집합. 봉이 있는 날이 거래일이다."""
    return {str(r.get("stck_bsop_date")) for r in rows if r.get("stck_bsop_date")}


def investor_rows_or_error(resp: dict) -> tuple[list[dict], str | None]:
    """kis_rest.get은 실패해도 예외 대신 rt_cd≠0을 돌려준다. 삼키지 않고 사유를 돌려준다."""
    if str(resp.get("rt_cd") or "") != "0":
        return [], f"KIS_ERROR:{resp.get('msg_cd') or 'UNKNOWN'}"
    return list(resp.get("output") or []), None


def flow_from_investor_rows(rows: list[dict], date: str) -> tuple[bool | None, str]:
    """해당일 외국인+기관 순매수 부호. 행이 없으면 OUT_OF_RANGE, 수량 필드가 없으면 MISSING_FIELD."""
    for row in rows:
        if str(row.get("stck_bsop_date")) != date:
            continue
        frgn, orgn = row.get("frgn_ntby_qty"), row.get("orgn_ntby_qty")
        if frgn is None or orgn is None:
            return None, "MISSING_FIELD"
        return int(float(frgn)) + int(float(orgn)) > 0, "KIS"
    return None, "OUT_OF_RANGE"


def calendar_covers(calendar: set[str], first_date: str, last_date: str) -> bool:
    """캐시가 유니버스 기간을 덮는가 — 첫날 앞에 거래일이 하나는 있고, 마지막 날까지 있어야 한다."""
    return any(d < first_date for d in calendar) and any(d >= last_date for d in calendar)


# ── 캐시 ──────────────────────────────────────────────────────────────────


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n")
    tmp.replace(path)


def load_trading_days(cache: Path = TRADING_DAYS_CACHE) -> set[str]:
    if not cache.exists():
        return set()
    return set(json.loads(cache.read_text(encoding="utf-8")))


def save_trading_days(days: set[str], cache: Path = TRADING_DAYS_CACHE) -> None:
    _write_json(cache, sorted(days))


# ── DART ──────────────────────────────────────────────────────────────────


def _dart_key() -> str:
    key = os.environ.get("DART_API_KEY", "").strip()
    if not key:
        raise SystemExit("DART_API_KEY 가 .env 에 없다 (개발 트리 전용).")
    return key


def load_corp_codes(client: httpx.Client, key: str, cache: Path = CORP_CODE_CACHE) -> dict[str, str]:
    """종목코드 → corp_code. 한 번 받아 캐시한다 (zip 안의 CORPCODE.xml).

    캐시는 영구다 — 신규 상장 종목이 NO_CORP_CODE로 나오면 캐시 파일을 지우고 다시 받는다.
    """
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
    _write_json(cache, mapping)
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


def _empty_label(**fields) -> dict:
    row = {"label": "NONE", "matched_report_nm": [], "report_nm": [], "disclosure_count": 0,
           "window_bgn": None, "window_end": None}
    row.update(fields)
    return row


def label_disclosures(
    trading_dates: list[str], pairs: list[dict], window_source: str
) -> list[dict]:
    key = _dart_key()
    rows: list[dict] = []
    with httpx.Client() as client:
        corp_codes = load_corp_codes(client, key)
        for pair in pairs:
            row = dict(pair, window_source=window_source)
            window = label_window(pair["date"], trading_dates)
            if window is None:
                row.update(_empty_label(label_source="NO_PREV_TRADING_DAY"))
                rows.append(row)
                continue
            corp, via_common = corp_code_for(corp_codes, pair["ticker"])
            if corp is None:
                row.update(_empty_label(window_bgn=window[0], window_end=window[1],
                                        label_source="NO_CORP_CODE"))
                rows.append(row)
                continue
            time.sleep(DART_INTERVAL_SEC)
            names = fetch_report_names(client, key, corp, window[0], window[1])
            row.update(label_pair(names))
            row.update(window_bgn=window[0], window_end=window[1],
                       label_source="DART_VIA_COMMON" if via_common else "DART")
            rows.append(row)
            print(f"{pair['date']} {pair['ticker']} r{pair['rank']} {row['label']:8} "
                  f"{row['disclosure_count']:2}건 {row['matched_report_nm']}")
    return rows


# ── KIS: 거래일 캘린더 + 수급(보고용) ─────────────────────────────────────


async def _kis_session():
    """백필과 같은 가드. 호출부가 finally에서 close_client()를 부른다."""
    from scripts.track_b_backfill import assert_backfill_window, assert_paper_mode
    from src.api import auth

    assert_paper_mode()
    assert_backfill_window(datetime.now(KST))
    if not await auth.load_or_refresh():
        raise SystemExit("TOKEN_UNAVAILABLE")


async def fetch_trading_days(first_date: str, last_date: str, interval: float) -> set[str]:
    """[first_date − 2주, last_date]의 거래일. 일봉 API는 호출당 최대 100봉이라 뒤에서 앞으로 민다."""
    from scripts.fast_path_counterfactual import _assert_success
    from src.api import kis_rest

    start = (datetime.strptime(first_date, "%Y%m%d") - timedelta(days=CALENDAR_LOOKBACK_DAYS)).strftime("%Y%m%d")
    days: set[str] = set()
    cursor = last_date
    while cursor >= start:
        await asyncio.sleep(interval)
        resp = await kis_rest.get(
            DAILY_PATH, tr_id=DAILY_TR,
            params={
                "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": CALENDAR_TICKER,
                "FID_INPUT_DATE_1": start, "FID_INPUT_DATE_2": cursor,
                "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
            },
            stop_on_rate_limit=True,
            request_priority=kis_rest.REQUEST_PRIORITY_BACKGROUND,
        )
        _assert_success(resp)
        page = trading_days_from_daily_chart(resp.get("output2") or [])
        if not page:
            break
        days |= page
        earliest = min(page)
        if earliest <= start:
            break
        cursor = (datetime.strptime(earliest, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    return days


async def label_flows(trading_dates: list[str], rows: list[dict], interval: float) -> None:
    """종목당 1회 조회해 직전 거래일의 외국인+기관 순매수 부호를 붙인다."""
    from scripts.fast_path_counterfactual import PocStop, _assert_success
    from src.api import kis_rest

    cache: dict[str, tuple[list[dict], str | None]] = {}
    for row in rows:
        ticker = row["ticker"]
        if ticker not in cache:
            await asyncio.sleep(interval)
            resp = await kis_rest.get(
                INVESTOR_PATH, tr_id=INVESTOR_TR,
                params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
                request_priority=kis_rest.REQUEST_PRIORITY_BACKGROUND,
            )
            try:
                _assert_success(resp)  # 레이트리밋이면 중단 — 백필과 같은 취급
            except PocStop as stop:
                if stop.args and stop.args[0] == "RATE_LIMIT":
                    raise
            cache[ticker] = investor_rows_or_error(resp)
        investor_rows, error = cache[ticker]
        if error:
            row.update(flow_pos=None, flow_source=error)
            continue
        prev = label_window(row["date"], trading_dates)
        if prev is None:
            row.update(flow_pos=None, flow_source="NO_PREV_TRADING_DAY")
            continue
        flow, source = flow_from_investor_rows(investor_rows, prev[0])
        row.update(flow_pos=flow, flow_source=source)


# ── 진입점 ────────────────────────────────────────────────────────────────


def write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


async def main_async(args: argparse.Namespace) -> int:
    universe_dates, pairs = load_universe_pairs(args.universes)
    print(f"유니버스 {len(universe_dates)}거래일 / {len(pairs)}쌍")
    first, last = universe_dates[0], universe_dates[-1]

    calendar = load_trading_days()
    if not args.skip_flow:
        from src.api import kis_rest

        await _kis_session()
        try:
            calendar |= await fetch_trading_days(first, last, args.interval)
            save_trading_days(calendar)
            print(f"거래일 캘린더 {len(calendar)}일 (KIS 일봉 {CALENDAR_TICKER}) → {TRADING_DAYS_CACHE}")
            trading_dates = sorted(calendar)
            rows = label_disclosures(trading_dates, pairs, window_source="CALENDAR")
            await label_flows(trading_dates, rows, args.interval)
        finally:
            await kis_rest.close_client()
    elif calendar_covers(calendar, first, last):
        rows = label_disclosures(sorted(calendar), pairs, window_source="CALENDAR")
    else:
        print("경고: 거래일 캐시가 유니버스 기간을 덮지 못한다 — 유니버스 날짜로 창을 계산한다 "
              "(window_source=UNIVERSE_DAYS). --skip-flow 없이 한 번 돌리면 캘린더가 채워진다.")
        rows = label_disclosures(universe_dates, pairs, window_source="UNIVERSE_DAYS")
    if args.skip_flow:
        for row in rows:
            row.update(flow_pos=None, flow_source="SKIPPED")

    write_rows(args.out, rows)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["label"]] = counts.get(row["label"], 0) + 1
    print(f"라벨 분포 {counts} → {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="H2 재료 공시·수급 라벨")
    parser.add_argument("--universes", type=Path, default=ROOT / "data/replay/universes.json")
    parser.add_argument("--out", type=Path, default=CATALYST_DIR / "labels.jsonl")
    parser.add_argument("--skip-flow", action="store_true", help="KIS(캘린더·수급) 호출을 생략한다")
    parser.add_argument("--interval", type=float, default=1.2, help="KIS 호출 간격(초)")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
