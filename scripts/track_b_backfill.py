"""전 세션 분봉 백필 — 읽기 전용.

``data/backtest_bars`` 의 09:00~09:30 31봉을 09:00~15:30 전 세션으로 늘린다.
트랙 B는 09:35 이후에 판정하므로 기존 캐시로는 규칙을 고를 수 없다.

일별분봉 TR(``FHKST03010230``)은 ``FID_INPUT_HOUR_1`` 기준 이전 30봉을 준다.
커서를 가장 이른 봉으로 밀어 09:00까지 역방향으로 채운다.

KIS를 1,300번 때리므로 가드가 전부다. PAPER 고정, 15:40 이후, GET만, 예산
객체로 상한. 캐시가 남으므로 중단해도 재실행하면 이어진다.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, time
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if os.getenv("STOCK_SKIP_DOTENV", "0") != "1":
    load_dotenv(ROOT / ".env")

from scripts.fast_path_counterfactual import (  # noqa: E402
    PocStop,
    Throttle,
    _assert_success,
    fetch_daily_minute_bars,
)
from scripts.strategy_backtest import (  # noqa: E402
    BAR_CACHE_DIR,
    load_universes,
    read_cached_bars,
    write_cached_bars,
)
from scripts.track_b_backtest import previous_trading_date  # noqa: E402
from src import warmup  # noqa: E402
from src.api import kis_rest  # noqa: E402
from src.api.kis_minute_bars import parse_minute_bars  # noqa: E402
from src.api.kis_rest import RequestBudgetExceeded  # noqa: E402
from src.modules import f1_selector  # noqa: E402

KST = ZoneInfo("Asia/Seoul")

# 과거 데이터 조회는 15:40 이후. DEV_ENV.md 규약이며 각 스크립트가 직접 건다 —
# strategy_backtest 의 안전창은 09:00~09:35만 막는다.
EARLIEST_BACKFILL = time(15, 40)

SESSION_START = "090000"
# 한 세션은 연속매매 380봉 + 종가 1봉 = 381봉이다(15:20~15:30은 단일가라 봉이 없다).
# 페이지당 30봉이라 13페이지면 닿는다. 두 장 여유.
MAX_PAGES = 15


def assert_backfill_window(now: datetime) -> None:
    if now.timetz().replace(tzinfo=None) < EARLIEST_BACKFILL:
        raise PocStop("AFTER_1540_ONLY")


def assert_paper_mode() -> None:
    if os.getenv("KIS_MODE", "").upper() != "PAPER":
        raise PocStop("NOT_PAPER_MODE")


def merge_bars(existing: list[dict], fetched: list[dict]) -> list[dict]:
    """시각 기준으로 합치고 정렬한다. 기존 봉을 새 봉으로 덮지 않는다.

    같은 봉을 다시 받는 것은 정상(커서가 겹친다)이고, 그때 값이 흔들리면
    캐시가 재현 불가능해진다.
    """
    merged: dict[tuple[str, str], dict] = {}
    for bar in fetched:
        merged[(bar["date"], bar["time"])] = bar
    for bar in existing:
        merged[(bar["date"], bar["time"])] = bar
    return [merged[k] for k in sorted(merged)]


def next_cursor(bars: list[dict]) -> str | None:
    """다음 페이지 커서 — 이번 페이지에서 가장 이른 봉."""
    if not bars:
        return None
    return min(b["time"] for b in bars)


async def fetch_session_bars(
    ticker: str,
    date: str,
    *,
    budget: kis_rest.CallBudget,
    throttle: Throttle,
    max_pages: int = MAX_PAGES,
) -> list[dict]:
    """전 세션 분봉. 장 마감 커서에서 09:00까지 역방향으로 페이지를 민다.

    요청 날짜와 다른 봉은 버린다 — KIS가 휴장일을 가장 가까운 거래일로 조용히
    대체하고, 커서가 09:00을 넘어가면 전일 봉이 섞인다.
    """
    bars: list[dict] = []
    seen: set[str] = set()
    cursor = "153000"

    for _ in range(max_pages):
        await asyncio.sleep(throttle.wait_seconds(monotonic()))
        throttle.mark(monotonic())
        response = await fetch_daily_minute_bars(
            ticker, date, budget=budget, hour_cursor=cursor
        )
        _assert_success(response)
        page, _issues = parse_minute_bars(response)
        page = [b for b in page if b["date"] == date]
        fresh = [b for b in page if b["time"] not in seen]
        if not fresh:
            break
        for bar in fresh:
            seen.add(bar["time"])
            bars.append(bar)
        earliest = next_cursor(fresh)
        # fresh는 위 break가 보장하므로 None이 될 수 없지만, next_cursor의
        # 시그니처가 Optional이라 여기서 좁혀 둔다.
        if earliest is None or earliest <= SESSION_START:
            break
        cursor = earliest

    bars.sort(key=lambda b: b["time"])
    return bars


def needed_pairs(
    depth: int = 5, snapshot_dir: Path | None = None, warmup_days: int = 0
) -> dict[str, set[str]]:
    """날짜별 F1 랭크 1~depth 종목. 운영 랭킹 함수를 그대로 쓴다.

    ``warmup_days``가 0보다 크면 각 종목의 전 거래일 쌍을 함께 대상에 넣는다.
    지표 워밍업이 그 봉을 필요로 하는데, 그 종목이 그날 F1 상위에 없었으면
    캐시에 없기 때문이다(스펙 §5.1).
    """
    universes = (
        load_universes(snapshot_dir) if snapshot_dir is not None else load_universes()
    )
    needed: dict[str, set[str]] = {}
    for date, rows in universes.items():
        ranked = f1_selector.rank_candidates(rows)[:depth]
        tickers = {str(r["ticker"]) for r in ranked if r.get("ticker")}
        if tickers:
            needed[date] = tickers

    if warmup_days > 0:
        dates = sorted(universes)
        for date in list(needed):
            cursor = date
            for _ in range(warmup_days):
                previous = previous_trading_date(dates, cursor)
                if previous is None:
                    break
                cursor = previous
                needed.setdefault(cursor, set()).update(needed[date])
    return needed


def is_session_complete(bars: list[dict] | None) -> bool:
    """이미 전 세션이 채워진 쌍은 건너뛴다. 31봉짜리는 채운다.

    봉 수가 아니라 개장~마감을 덮었는지로 본다 — 거래가 뜸한 종목은 완전한
    하루도 265봉이라, 개수로 자르면 그런 쌍을 매번 다시 받는다.
    """
    if not bars:
        return False
    return warmup.covers_session(bars)


async def backfill(
    needed: dict[str, set[str]],
    *,
    cache_dir: Path = BAR_CACHE_DIR,
    budget: kis_rest.CallBudget,
    throttle: Throttle,
) -> dict[str, int]:
    stats = {
        "skipped": 0, "filled": 0, "empty": 0, "failed": 0,
        "budget_exhausted": False,
    }
    for date in sorted(needed):
        if stats["budget_exhausted"]:
            break
        for ticker in sorted(needed[date]):
            existing = read_cached_bars(date, ticker, cache_dir) or []
            if is_session_complete(existing):
                stats["skipped"] += 1
                continue
            try:
                fetched = await fetch_session_bars(
                    ticker, date, budget=budget, throttle=throttle
                )
            except PocStop:
                raise
            except RequestBudgetExceeded:
                # 예산 컷오프는 실패가 아니다 — 추가 호출을 안 낸 것뿐이고 캐시는
                # 남아 재실행하면 이어진다. failed에 섞이면 운영자가 "진짜 실패
                # 40건"과 "예산이 모자랐다"를 구분 못 한다.
                stats["budget_exhausted"] = True
                print(
                    f"  예산 소진: {date} {ticker} 이후 중단 "
                    f"(used={budget.used}/{budget.max_calls})",
                    flush=True,
                )
                break
            except Exception as exc:
                stats["failed"] += 1
                print(f"  {date} {ticker} {type(exc).__name__}: {exc}", flush=True)
                continue
            if not fetched:
                stats["empty"] += 1
                continue
            merged = merge_bars(existing, fetched)
            write_cached_bars(date, ticker, merged, cache_dir)
            stats["filled"] += 1
            print(f"  {date} {ticker}: {len(existing)} -> {len(merged)}봉", flush=True)
    return stats


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="트랙 B 전 세션 분봉 백필")
    parser.add_argument("--depth", type=int, default=5, help="F1 랭크 상위 N종목")
    parser.add_argument("--max-calls", type=int, default=1600)
    parser.add_argument("--interval", type=float, default=1.2)
    parser.add_argument("--dry-run", action="store_true", help="호출 없이 계획만 출력")
    parser.add_argument(
        "--warmup-days", type=int, default=1,
        help="지표 워밍업에 필요한 전 거래일도 함께 채운다. 0이면 채우지 않는다",
    )
    args = parser.parse_args(argv)

    needed = needed_pairs(args.depth, warmup_days=args.warmup_days)
    pairs = sum(len(v) for v in needed.values())
    print(f"대상 {len(needed)}거래일 / {pairs}쌍 (랭크 1~{args.depth})")
    if args.dry_run:
        return 0

    assert_paper_mode()
    assert_backfill_window(datetime.now(KST))
    from src.api import auth

    if not await auth.load_or_refresh():
        raise PocStop("TOKEN_UNAVAILABLE")

    stats = await backfill(
        needed,
        budget=kis_rest.CallBudget(args.max_calls),
        throttle=Throttle(args.interval),
    )
    print(stats)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(main_async(argv))
    except PocStop as exc:
        print(f"중단: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
