"""C1 종가 스크리닝 — 15:32에 종가 기준 후보를 뽑아
data/overnight/candidates/<date>.jsonl 에 남긴다.

규칙·임계값·순위는 docs/superpowers/specs/2026-09-21-c1-overnight-close-hypothesis.md §2에
고정돼 있다. 이 파일은 그것을 구현할 뿐이고, 값을 바꾸려면 C2를 등록한다.

    .\\.venv\\Scripts\\python.exe scripts\\overnight_screen.py            # 기록
    .\\.venv\\Scripts\\python.exe scripts\\overnight_screen.py --dry-run  # 호출만, 기록 없음

유니버스는 KIS 거래량순위 TR(FHPST01710000) 상위 30/시장 + 클라이언트 등락률 필터
(코스피·코스닥 각 30), 필터는 랭킹 응답 + 일봉 1콜(당일 OHLC와 직전 20거래일 거래대금).
마감 후에 돌므로 A·F5와 유량이 겹치지 않는다.

같은 날 15:40 이후 재실행은 15:32 실행과 동등하지 않다 — 시간외 거래가
`acml_tr_pbmn`(거래대금)과 거래량 순위를 바꾼다. 15:32 실행이 실패했으면 15:40 전에
즉시 재실행하고, 그러지 못했으면 그날은 결측으로 둔다(재실행으로 메우지 않는다).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Awaitable, Callable
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if os.getenv("STOCK_SKIP_DOTENV", "0") != "1":
    load_dotenv(ROOT / ".env")

if TYPE_CHECKING:
    from scripts.fast_path_counterfactual import Throttle
    from src.api import kis_rest

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
                         "HANARO", "SOL ", "ACE ", "KOSEF", "TIMEFOLIO", "PLUS ",
                         "RISE ", "KIWOOM ")


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


def latest_bar_date(output2: list[dict]) -> str | None:
    """달력 종목 일봉 output2에서 가장 최근 거래일. 없으면 None(운영자 메시지용)."""
    dates = [str(r["stck_bsop_date"]) for r in output2 if r.get("stck_bsop_date")]
    return max(dates) if dates else None


def is_trading_day(output2: list[dict], date: str) -> bool:
    """달력 종목(§3.4)의 일봉에 `date` 행이 있으면 거래일. 휴장일에는 랭킹 API가 전
    거래일 값을 그대로 돌려주므로, 이 일봉 유무로만 거래일 여부를 판단한다."""
    return parse_daily(output2, date) is not None


def classify_calendar_probe(
    output2: list[dict], error: str | None, date: str
) -> str:
    """달력 종목(§3.4) 프로브 결과를 셋 중 하나로 나눈다: TRADING_DAY / HOLIDAY / FAILED.

    프로브 실패를 휴장일로 오인하면 결측이 "거래일 아님"으로 둔갑한다 — fetch_daily는
    rt_cd != 0인 모든 경우(KIS 오류·토큰 만료·네트워크 전송 실패까지 kis_rest가 rt_cd="1"
    응답으로 바꿔 돌려준다)에 `([], "KIS_ERROR:...")`를 돌려주므로, 오류가 있으면 휴장일
    판정보다 먼저 실패로 갈라낸다.
    """
    if error is not None:
        return "FAILED"
    return "TRADING_DAY" if is_trading_day(output2, date) else "HOLIDAY"


def merge_calendar(existing: list[str], fetched: set[str]) -> list[str]:
    """거래일 달력(§3.4 정정) — 기존 목록과 새로 얻은 거래일의 합집합, 정렬해서 돌려준다.

    파일 I/O 없는 순수 함수. 달력은 지울 일이 없으므로 항상 합집합이다 — 과거에 한 번
    확인한 거래일이 다음 실행에서 사라질 이유가 없다.
    """
    return sorted(set(existing) | set(fetched))


def write_calendar(path: Path, fetched: set[str]) -> None:
    """``data/overnight/calendar.json``을 원자적으로 갱신한다.

    기존 파일이 있으면 ``merge_calendar``로 합쳐 쓴다. tmp에 다 쓰고 os.replace로
    교체해, 쓰는 도중 중단돼도 손상된 달력이 남지 않는다.
    """
    existing: list[str] = []
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = [str(d) for d in loaded]
        except (OSError, json.JSONDecodeError):
            existing = []
    merged = merge_calendar(existing, fetched)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, path)


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


MARKETS = ("0001", "1001")  # 코스피, 코스닥 — paper_fast_probe와 같은 코드
CALL_BUDGET = 100  # 랭킹 2 + 거래일 확인 1 + 일봉 ≤60 + 재시도 여유(스펙 §2.4)
REQUEST_INTERVAL_SEC = 1.2

FetchRanking = Callable[[str], Awaitable[list[dict]]]
FetchDaily = Callable[[str], Awaitable[tuple[list[dict], str | None]]]


def ranking_params(ranking_input: str) -> dict:
    """paper_fast_probe의 랭킹 파라미터에 등락률 범위만 §2.2 값으로 덮는다.

    이 TR(FHPST01710000, 거래량순위)은 서버가 fid_rsfl_rate1/2를 무시한다(스펙 §2.1
    정정 — 09/21 dry-run에서 유니버스 58행 중 21행만 실제로 [3, 25) 범위 안이었다).
    그래도 계속 보내는 것 자체는 무해하고, 실제 등락률 범위는 evaluate()가
    클라이언트에서 건다.
    """
    from src.modules.paper_fast_probe import _ranking_params

    params = dict(_ranking_params(ranking_input))
    params["fid_rsfl_rate1"] = f"{CHANGE_MIN:.1f}"
    params["fid_rsfl_rate2"] = f"{CHANGE_MAX:.1f}"
    return params


async def screen(
    date: str, *, fetch_ranking: FetchRanking, fetch_daily: FetchDaily
) -> tuple[list[dict], dict]:
    """유니버스 → 일봉 조인 → 규칙 → 순위. (모든 행, 요약 행)을 돌려준다.

    거부 행도 원시 필드와 함께 남긴다(스펙 §2.3). 일봉이 하나라도 실패하면
    degraded=True — 랭크 1이 그 종목이었을 가능성을 알 수 없어서다(§3.4).

    예산이 소진(RequestBudgetExceeded)되면 그 뒤 종목은 fetch_daily를 아예 부르지
    않는다 — 어차피 실패할 호출에 1.2초씩 페이싱만 낭비하지 않기 위해서다(§2.4).
    """
    from src.api.kis_rest import RequestBudgetExceeded

    ranking_rows: dict[str, dict] = {}
    for market in MARKETS:
        for row in await fetch_ranking(market):
            ticker = str(row.get("mksc_shrn_iscd") or row.get("stck_shrn_iscd") or "")
            if len(ticker) == 6 and ticker.isdigit() and ticker not in ranking_rows:
                ranking_rows[ticker] = row

    rows: list[dict] = []
    degraded = False
    budget_exceeded = False
    for ticker, ranking_row in ranking_rows.items():
        # 랭킹만으로 떨어지는 조건은 일봉을 부르지 않는다 — 호출 예산(§2.4).
        # evaluate()를 그대로 쓴다: daily=None이면 종가 위치가 없어 이름·등락률을
        # 통과한 행은 전부 CLOSE_POSITION으로 떨어지므로, 이 두 사유만 여기서 가른다.
        pre = build_row(date, ranking_row, None, None)
        reason = evaluate(pre)
        if reason in ("NAME_EXCLUDED", "CHANGE_PCT"):
            pre["rejected_reason"] = reason
            rows.append(pre)
            continue
        output2: list[dict]
        error: str | None
        if budget_exceeded:
            output2, error = [], "EXCEPTION:RequestBudgetExceeded"
        else:
            try:
                # 예산 초과(RequestBudgetExceeded)·전송 오류도 KIS 오류 응답과 동일하게 취급한다.
                output2, error = await fetch_daily(ticker)
            except RequestBudgetExceeded:
                budget_exceeded = True
                output2, error = [], "EXCEPTION:RequestBudgetExceeded"
            except Exception as exc:
                output2, error = [], f"EXCEPTION:{type(exc).__name__}"
        daily = parse_daily(output2, date) if error is None else None
        if error is None and daily is None:
            error = "NO_TODAY_BAR"
        row = build_row(date, ranking_row, daily, error)
        if row["rejected_reason"] == "DAILY_FAILED":
            degraded = True
        else:
            row["rejected_reason"] = evaluate(row)
        rows.append(row)

    ranked = rank_candidates([r for r in rows if r["rejected_reason"] is None])
    rank_of = {r["ticker"]: r["rank"] for r in ranked}
    for row in rows:
        row["rank"] = rank_of.get(row["ticker"])
        if row["rejected_reason"] is None and row["rank"] is None:
            row["rejected_reason"] = "BELOW_TOP"   # 통과했지만 6위 이하
    rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, r["ticker"]))
    summary = {
        "summary": True, "date": date, "universe": len(ranking_rows),
        "candidates": len(ranked), "degraded": degraded,
    }
    return rows, summary


def write_candidates(path: Path, rows: list[dict], summary: dict) -> None:
    """후보 파일을 원자적으로 쓴다 — tmp에 다 쓰고 os.replace로 교체해, 쓰는 도중
    중단돼도 요약 행 없는 반쪽 파일이 남지 않는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.write(json.dumps(summary, ensure_ascii=False) + "\n")
    os.replace(tmp_path, path)


def _kis_fetchers(
    budget: "kis_rest.CallBudget", throttle: "Throttle", date: str
) -> tuple[FetchRanking, FetchDaily]:
    from scripts.catalyst_label import DAILY_PATH, DAILY_TR
    from scripts.fast_path_counterfactual import PocStop
    from src.api import kis_rest
    from src.modules.paper_fast_probe import RANKING_PATH, RANKING_TR_ID

    async def _pace() -> None:
        # Throttle은 순수 페이서다: wait_seconds(now)로 남은 시간을 받아 자고 mark(now)한다.
        await asyncio.sleep(throttle.wait_seconds(monotonic()))
        throttle.mark(monotonic())

    async def fetch_ranking(market: str) -> list[dict]:
        await _pace()
        resp = await kis_rest.get(
            RANKING_PATH, params=ranking_params(market), tr_id=RANKING_TR_ID,
            # 랭킹도 일봉처럼 레이트리밋에서 바로 포기하지 않는다 — 마감 후 랭킹은
            # (A의 장중 F1과 달리) 유량이 이미 한가하므로 kis_rest의 백오프 재시도
            # (최대 3회)에 맡기고, 최종 응답의 rt_cd/msg_cd로만 PocStop을 판단한다.
            stop_on_rate_limit=False, request_priority=kis_rest.REQUEST_PRIORITY_BACKGROUND,
            budget=budget,
        )
        # _assert_success는 분봉(MINUTE_PRICE_FAILED) 문구라 여기서 쓰면 사유가 틀린다(M6).
        msg_cd = str(resp.get("msg_cd") or "")
        if msg_cd in kis_rest.RATE_LIMIT_CODES:
            raise PocStop("RATE_LIMIT", msg_cd)
        if str(resp.get("rt_cd") or "") != "0":
            raise PocStop("RANKING_FAILED", msg_cd or None)
        return list(resp.get("output") or [])[:30]

    async def fetch_daily(ticker: str) -> tuple[list[dict], str | None]:
        await _pace()
        resp = await kis_rest.get(
            DAILY_PATH, tr_id=DAILY_TR,
            params={
                "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker,
                "FID_INPUT_DATE_1": "20250101", "FID_INPUT_DATE_2": date,
                "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
            },
            # 일봉도 레이트리밋에서 바로 포기하지 않는다 — kis_rest가 같은 예산
            # 안에서 백오프 재시도(최대 3회)하게 두어, 리밋 한 번에 종목 하나가 DAILY_FAILED로
            # 빠져 degraded=True가 되는 것을 막는다.
            stop_on_rate_limit=False, request_priority=kis_rest.REQUEST_PRIORITY_BACKGROUND,
            budget=budget,
        )
        if str(resp.get("rt_cd") or "") != "0":
            return [], f"KIS_ERROR:{resp.get('msg_cd') or 'UNKNOWN'}"
        return list(resp.get("output2") or []), None

    return fetch_ranking, fetch_daily


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C1 종가 스크리닝 (15:32)")
    parser.add_argument("--dry-run", action="store_true", help="호출은 하되 기록하지 않는다")
    parser.add_argument("--root", type=Path, default=ROOT, help="data/ 가 있는 트리")
    parser.add_argument("--date", default=None, help="기록 파일 이름(기본: 오늘 KST). 테스트용")
    args = parser.parse_args(argv)

    from scripts.catalyst_label import CALENDAR_TICKER, trading_days_from_daily_chart
    from scripts.fast_path_counterfactual import PocStop, Throttle
    from scripts.track_b_backfill import assert_paper_mode
    from src.api import auth, kis_rest

    assert_paper_mode()
    if not await auth.load_or_refresh():
        raise PocStop("TOKEN_UNAVAILABLE")
    date = args.date or datetime.now(KST).strftime("%Y%m%d")
    budget = kis_rest.CallBudget(CALL_BUDGET)
    fetch_ranking, fetch_daily = _kis_fetchers(budget, Throttle(REQUEST_INTERVAL_SEC), date)

    # 휴장일 가드(§3.4): 랭킹은 휴장일에도 전 거래일 값을 그대로 돌려주므로, 루프 전에
    # 달력 종목(005930)의 일봉으로 오늘 봉이 있는지 1콜로 먼저 확인한다.
    # 프로브 실패를 휴장일로 오인하면 결측이 "거래일 아님"으로 둔갑한다 — 오류부터 가른다.
    calendar_output2, calendar_error = await fetch_daily(CALENDAR_TICKER)
    calendar_status = classify_calendar_probe(calendar_output2, calendar_error, date)
    if calendar_status == "FAILED":
        raise PocStop("CALENDAR_PROBE_FAILED", calendar_error)
    # 프로브가 통과했으면(TRADING_DAY·HOLIDAY 둘 다) 005930 일봉에서 나온 거래일로
    # 거래일 달력을 갱신한다 — D+1 추정을 파일 존재가 아니라 이 달력에 기대게 한다
    # (스펙 §3.4 정정, 2026-09-21).
    write_calendar(
        args.root / "data" / "overnight" / "calendar.json",
        trading_days_from_daily_chart(calendar_output2),
    )
    if calendar_status == "HOLIDAY":
        latest = latest_bar_date(calendar_output2)
        print(f"휴장일 또는 장 마감 전: {date} 봉 없음 (최신 봉 {latest}) — 기록하지 않음")
        return 0

    rows, summary = await screen(date, fetch_ranking=fetch_ranking, fetch_daily=fetch_daily)
    if summary["universe"] == 0:
        raise PocStop("EMPTY_UNIVERSE")
    print(f"유니버스 {summary['universe']} / 후보 {summary['candidates']} / "
          f"호출 {budget.used} / degraded={summary['degraded']}")
    for row in rows:
        if row["rank"]:
            print(f"  {row['rank']} {row['ticker']} {row['name']} 종가 {row['close']:.0f} "
                  f"등락 {row['change_pct']:+.2f}% 배수 {row['amount_multiple']:.2f}")
    if args.dry_run:
        print("(dry-run: 기록하지 않음)")
        return 0
    path = args.root / "data" / "overnight" / "candidates" / f"{date}.jsonl"
    write_candidates(path, rows, summary)
    print(f"기록: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    from scripts.fast_path_counterfactual import PocStop

    try:
        return asyncio.run(main_async(argv))
    except PocStop as exc:
        print(f"중단: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
