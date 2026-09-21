"""C1 종가 스크리닝 — 15:32에 종가 기준 후보를 뽑아
data/overnight/candidates/<date>.jsonl 에 남긴다.

규칙·임계값·순위는 docs/superpowers/specs/2026-09-21-c1-overnight-close-hypothesis.md §2에
고정돼 있다. 이 파일은 그것을 구현할 뿐이고, 값을 바꾸려면 C2를 등록한다.

    .\\.venv\\Scripts\\python.exe scripts\\overnight_screen.py            # 기록
    .\\.venv\\Scripts\\python.exe scripts\\overnight_screen.py --dry-run  # 호출만, 기록 없음

유니버스는 KIS 등락률 랭킹(코스피·코스닥 각 30), 필터는 랭킹 응답 + 일봉 1콜(당일 OHLC와
직전 20거래일 거래대금). 마감 후에 돌므로 A·F5와 유량이 겹치지 않는다.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if os.getenv("STOCK_SKIP_DOTENV", "0") != "1":
    load_dotenv(ROOT / ".env")

KST = ZoneInfo("Asia/Seoul")

# 스펙 §2.2 — 바꾸지 않는다.
CHANGE_MIN = 3.0
CHANGE_MAX = 25.0
CLOSE_POSITION_MIN = 0.8
AMOUNT_MULTIPLE_MIN = 2.0
AMOUNT_FLOOR = 1_000_000_000.0
PRICE_FLOOR = 1_000.0
RECORD_TOP = 5
HISTORY_DAYS = 20

# ETF/ETN/스팩은 이름으로 거른다. 랭킹 응답에 상품 구분 플래그가 없다.
_EXCLUDED_NAME_TOKENS = ("ETF", "ETN", "스팩", "KODEX", "TIGER", "KBSTAR", "ARIRANG",
                         "HANARO", "SOL ", "ACE ", "KOSEF", "TIMEFOLIO", "PLUS ")


def close_position(high: float, low: float, close: float) -> float | None:
    """종가가 당일 범위의 어디에 있는가. 0=저가, 1=고가. 고가=저가면 None."""
    if high <= low:
        return None
    return (close - low) / (high - low)


def is_excluded_name(name: str) -> bool:
    upper = (name or "").upper()
    return any(token in upper for token in _EXCLUDED_NAME_TOKENS)


def evaluate(row: dict) -> str | None:
    """스펙 §2.2 필터. 거부 사유를 돌려주고 통과면 None. 검사 순서도 고정이다."""
    if is_excluded_name(str(row.get("name") or "")):
        return "NAME_EXCLUDED"
    change = row.get("change_pct")
    if change is None or not (CHANGE_MIN <= float(change) < CHANGE_MAX):
        return "CHANGE_PCT"
    position = row.get("close_position")
    if position is None or float(position) < CLOSE_POSITION_MIN:
        return "CLOSE_POSITION"
    if float(row.get("close") or 0.0) < PRICE_FLOOR:
        return "PRICE_FLOOR"
    if float(row.get("amount") or 0.0) < AMOUNT_FLOOR:
        return "AMOUNT_FLOOR"
    if row.get("avg_amount_20d") is None or row.get("amount_multiple") is None:
        return "NO_HISTORY"
    if float(row["amount_multiple"]) < AMOUNT_MULTIPLE_MIN:
        return "AMOUNT_MULTIPLE"
    return None


def rank_candidates(rows: list[dict]) -> list[dict]:
    """통과 행을 거래대금 배수 내림차순(동률은 등락률)으로 세워 상위 RECORD_TOP개에
    rank를 붙인다."""
    passed = [r for r in rows if evaluate(r) is None]
    passed.sort(
        key=lambda r: (
            float(r["amount_multiple"]), float(r["change_pct"])
        ),
        reverse=True
    )
    ranked = []
    for rank, row in enumerate(passed[:RECORD_TOP], start=1):
        ranked.append({**row, "rank": rank, "rejected_reason": None})
    return ranked


def _f(value: object) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_daily(output2: list[dict], date: str) -> dict | None:
    """일봉 output2에서 `date` 행과 그 앞 HISTORY_DAYS일 평균 거래대금.

    응답은 보통 최신순이지만 순서에 기대지 않고 날짜로 정렬한다.
    """
    rows = [r for r in output2 if r.get("stck_bsop_date")]
    rows.sort(key=lambda r: str(r["stck_bsop_date"]), reverse=True)
    today = next((r for r in rows if str(r["stck_bsop_date"]) == date), None)
    if today is None:
        return None
    prior = [r for r in rows if str(r["stck_bsop_date"]) < date][:HISTORY_DAYS]
    amounts = [a for a in (_f(r.get("acml_tr_pbmn")) for r in prior) if a is not None]
    avg = sum(amounts) / len(amounts) if len(amounts) >= HISTORY_DAYS else None
    return {
        "open": _f(today.get("stck_oprc")),
        "high": _f(today.get("stck_hgpr")),
        "low": _f(today.get("stck_lwpr")),
        "close": _f(today.get("stck_clpr")),
        "amount": _f(today.get("acml_tr_pbmn")),
        "avg_amount_20d": avg,
        "history_days": len(amounts),
    }


def build_row(
    date: str, ranking_row: dict, daily: dict | None, daily_error: str | None
) -> dict:
    """후보 파일 한 줄. 순위·거부 사유는 rank_candidates/evaluate가 뒤에 붙인다."""
    ticker = str(ranking_row.get("mksc_shrn_iscd") or ranking_row.get("stck_shrn_iscd") or "")
    close = (daily or {}).get("close") if daily else None
    if close is None:
        close = _f(ranking_row.get("stck_prpr"))
    high = (daily or {}).get("high")
    low = (daily or {}).get("low")
    amount = (daily or {}).get("amount")
    if amount is None:
        amount = _f(ranking_row.get("acml_tr_pbmn"))
    avg = (daily or {}).get("avg_amount_20d")
    multiple = (amount / avg) if (amount is not None and avg) else None
    position = (
        close_position(high, low, close)
        if high is not None and low is not None and close is not None
        else None
    )
    row = {
        "date": date,
        "ticker": ticker,
        "name": str(ranking_row.get("hts_kor_isnm") or ""),
        "rank": None,
        "close": close,
        "open": (daily or {}).get("open"),
        "high": high,
        "low": low,
        "change_pct": _f(ranking_row.get("prdy_ctrt")),
        "close_position": position,
        "amount": amount,
        "avg_amount_20d": avg,
        "amount_multiple": multiple,
        "rejected_reason": None if daily is not None else "DAILY_FAILED",
        "raw": {"ranking": ranking_row, "daily": daily, "daily_error": daily_error},
    }
    return row
