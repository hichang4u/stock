"""F1 스냅샷 기반 전략 정책 백테스트 — 읽기 전용.

``data/f1_snapshots`` 에 남은 그날의 후보 유니버스(약 60종목)에 임의의 선정 정책을
다시 적용해, "그 정책이었다면 무엇을 샀고 개장 30분이 어땠을지"를 낸다. 운영 코드의
파라미터를 바꾸기 전에 근거를 만드는 용도이며 주문 경로는 없다.

재현하는 것
  1. F1 점수/필터  — gap·대금·거래량급증 가중치, 과열 페널티, 고갭 대금 하한
  2. F3 재검증 게이트 — 진입 시점 실제가로 갭을 다시 재고 하한/상한/고갭대금 판정
  3. 후보 교체     — 상위 N개를 순서대로 시도하고 통과하는 첫 종목에 진입

재현하지 않는 것
  분봉은 봉 내부 경로를 모른다. 트레일링은 재현하지 않고 승인된 장벽(+2.5%/-2.0%)
  선착만 본다. 같은 봉이 양쪽에 닿으면 AMBIGUOUS로 남기고 판정에서 뺀다.

진입 지연은 봉 단위로만 구분한다. 09:00 진입은 ``0900`` 봉 시가, 레거시(약 82초
지연) 진입은 ``0901`` 봉 시가를 쓴다. 재검증 갭도 같은 가격으로 계산한다.

분봉은 ``data/backtest_bars`` 에 캐시한다. 정책을 여러 개 쓸어보는 동안 KIS를
다시 때리지 않게 하기 위함이다. 캐시가 비면 ``--with-kis`` 로 채운다
(PAPER, 09:35 이후, 분봉 GET만).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if os.getenv("STOCK_SKIP_DOTENV", "0") != "1":
    load_dotenv(ROOT / ".env")

from scripts.fast_path_counterfactual import (  # noqa: E402
    Throttle,
    entry_price_from_bars,
    first_barrier,
    load_bars,
)
from scripts.kis_minute_bar_poc import (  # noqa: E402
    DOWN_BARRIER_PCT,
    UP_BARRIER_PCT,
    PocStop,
    mfe_mae,
)
from src.modules import f1_selector  # noqa: E402

KST = ZoneInfo("Asia/Seoul")

SNAPSHOT_DIR = ROOT / "data" / "f1_snapshots"
BAR_CACHE_DIR = ROOT / "data" / "backtest_bars"

WINDOW_END = "0930"
FAST_BAR = "0900"
LEGACY_BAR = "0901"

# 유니버스가 이만큼도 안 되는 스냅샷은 그날 F1이 중단된 흔적이라 정책 비교에 못 쓴다.
MIN_UNIVERSE_ROWS = 30


# ── 정책 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Policy:
    """한 벌의 선정·게이트 파라미터. 운영 기본값은 ``BASELINE`` 참고."""

    name: str
    # F1 선정
    gap_min: float = 0.025
    gap_core_max: float = 0.080
    gap_hard_max: float = 0.100
    min_expected_amount: float = 100_000_000
    high_gap_min_amount: float = 5_000_000_000
    w_gap: float = 25.0
    w_amount: float = 25.0
    w_surge: float = 25.0
    overheat_penalty: float = 30.0
    # 제거된 VI 배점. 운영이 만점 보존용으로 다시 얹는 값이라 그대로 들고 간다.
    vi_weight_carryover: float = 10.0
    # F3 재검증 게이트
    recheck_gap_min: float = 0.020
    recheck_gap_max: float = 0.100
    recheck_high_band: float = 0.080
    recheck_high_gap_min_amount: float = 5_000_000_000
    # 후보 교체: 상위 몇 개까지 순서대로 시도하는가
    depth: int = 3
    # 진입 봉 (0900=지연 없음, 0901=레거시 약 82초 지연)
    entry_bar: str = LEGACY_BAR
    # 진입 봉의 어느 가격에 체결됐다고 볼 것인가. ``open`` 은 그 봉 시작 즉시 체결,
    # ``close`` 는 그 봉이 끝날 때까지 밀린 최악을 가정한다(낙관/비관 경계).
    entry_field: str = "open"
    # 봉 가격 위에 얹는 체결 불리분(%). 실측 근거는 docs 참고: 09:00:00.3 프로브의
    # 실제 매도호가는 09:00 봉 시가보다 평균 +0.19%(중앙값 +0.07%)였다.
    entry_slippage_pct: float = 0.0


BASELINE = Policy(name="BASELINE")


# ── 점수 (f1_selector 이식, 파라미터만 정책에서 받는다) ──────────────────


def _score_gap(gap: float, p: Policy) -> float:
    if gap < p.gap_min or gap >= p.gap_hard_max:
        return 0.0
    core_span = max(0.0001, p.gap_core_max - p.gap_min)
    if gap <= p.gap_core_max:
        return min(1.0, (gap - p.gap_min) / core_span)
    return max(
        0.45,
        1.0 - ((gap - p.gap_core_max) / max(0.0001, p.gap_hard_max - p.gap_core_max)),
    )


def _score_amount(amount: float) -> float:
    if amount <= 0:
        return 0.0
    return max(0.0, min(1.0, math.log10(max(1.0, amount) / 200_000_000) / math.log10(150)))


def _score_surge(surge: float) -> float:
    if surge <= 0:
        return 0.0
    return min(1.0, math.log2(max(1.0, surge)) / math.log2(10))


def expected_amount(row: dict) -> float:
    return float(row.get("expected_amount") or row.get("avg_amount_5d") or 0.0)


def volume_surge(row: dict) -> float:
    avg = float(row.get("avg_amount_5d") or 0.0)
    if avg <= 0:
        return 0.0
    return expected_amount(row) / avg


def score(row: dict, p: Policy) -> float:
    """운영 ``f1_selector.f1_score`` 와 같은 순서로 계산한다.

    제거된 VI 배점(10점)을 다시 얹어 만점을 보존하는 재스케일이 페널티보다 **먼저**
    적용된다. 순서를 바꾸면 과열 페널티의 상대 강도가 달라져 다른 랭킹이 나온다.
    """
    gap = float(row.get("gap_pct") or 0.0)
    raw = (
        _score_gap(gap, p) * p.w_gap
        + _score_amount(expected_amount(row)) * p.w_amount
        + _score_surge(volume_surge(row)) * p.w_surge
    )
    configured_max = sum(max(0.0, w) for w in (p.w_gap, p.w_amount, p.w_surge))
    target_max = configured_max + p.vi_weight_carryover
    scaled = raw * target_max / configured_max if configured_max else 0.0
    if p.gap_core_max <= gap < p.gap_hard_max and p.overheat_penalty:
        scaled -= p.overheat_penalty * (
            (gap - p.gap_core_max) / max(0.0001, p.gap_hard_max - p.gap_core_max)
        )
    return round(scaled, 4)


def selection_rejection(row: dict, p: Policy) -> str | None:
    """F1 선정 하한에서 걸리는 첫 사유. 통과면 None."""
    gap = float(row.get("gap_pct") or 0.0)
    if f1_selector.is_excluded_product(row.get("name") or ""):
        return "PRODUCT"
    if not math.isfinite(gap) or gap < p.gap_min or gap >= p.gap_hard_max:
        return "GAP"
    if expected_amount(row) < p.min_expected_amount:
        return "EXPECTED_AMOUNT"
    if gap >= p.gap_core_max and expected_amount(row) < p.high_gap_min_amount:
        return "HIGH_GAP"
    return None


def rank(universe: list[dict], p: Policy) -> list[dict]:
    ranked = []
    for row in universe:
        if selection_rejection(row, p) is not None:
            continue
        enriched = {**row, "volume_surge": volume_surge(row), "f1_score": score(row, p)}
        ranked.append(enriched)
    return sorted(
        ranked,
        key=lambda c: (
            c["f1_score"],
            c["volume_surge"],
            expected_amount(c),
            float(c.get("gap_pct") or 0.0),
        ),
        reverse=True,
    )


def recheck_gate(gap: float, amount: float | None, p: Policy) -> tuple[bool, str]:
    """F3 진입 직전 갭 판정. ``f3_entry._evaluate_order_gap`` 과 같은 순서."""
    if not math.isfinite(gap):
        return (False, "INVALID_GAP")
    if gap < p.recheck_gap_min:
        return (False, "BELOW_MIN")
    if gap >= p.recheck_gap_max:
        return (False, "ABOVE_MAX")
    if gap >= p.recheck_high_band and (
        amount is None or amount <= 0 or amount < p.recheck_high_gap_min_amount
    ):
        return (False, "HIGH_GAP_AMOUNT_LOW")
    return (True, "OK")


# ── 스냅샷 로딩 ─────────────────────────────────────────────────────────


def load_universes(snapshot_dir: Path = SNAPSHOT_DIR) -> dict[str, list[dict]]:
    """날짜별 후보 유니버스. 같은 날 스냅샷이 여러 개면 가장 늦은 것을 쓴다."""
    universes: dict[str, list[dict]] = {}
    for path in sorted(snapshot_dir.glob("*.jsonl")):
        date = path.name[:8]
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if len(rows) < MIN_UNIVERSE_ROWS:
            continue
        universes[date] = rows
    return universes


# ── 분봉 캐시 ───────────────────────────────────────────────────────────


def cache_path(date: str, ticker: str, cache_dir: Path = BAR_CACHE_DIR) -> Path:
    return cache_dir / f"{date}_{ticker}.json"


def read_cached_bars(date: str, ticker: str, cache_dir: Path = BAR_CACHE_DIR) -> list[dict] | None:
    path = cache_path(date, ticker, cache_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_cached_bars(
    date: str, ticker: str, bars: list[dict], cache_dir: Path = BAR_CACHE_DIR
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path(date, ticker, cache_dir).write_text(
        json.dumps(bars, ensure_ascii=False), encoding="utf-8"
    )


# ── 하루 시뮬레이션 ─────────────────────────────────────────────────────


def evaluate_entry(bars: list[dict], entry_price: float, start_bar: str = FAST_BAR) -> dict | None:
    """진입가 기준 장벽 선착과 MFE/MAE. 측정 창에 봉이 없으면 None.

    측정은 반드시 **진입 봉부터** 시작한다. 09:00부터 재면 09:01 진입 정책이 아직
    들고 있지도 않은 09:00 봉의 저가로 손절 판정을 받아, 지연이 있는 쪽이 부당하게
    불리해진다.
    """
    if not entry_price or entry_price <= 0:
        return None
    excursion = mfe_mae(bars, entry_price, start=start_bar, end=WINDOW_END)
    if not excursion or not excursion.get("bar_count"):
        return None
    barrier = first_barrier(bars, entry_price, start=start_bar, end=WINDOW_END)
    return {
        "entry_price": entry_price,
        "outcome": barrier["outcome"],
        "barrier_time": barrier["time"],
        "mfe_pct": excursion.get("mfe_pct"),
        "mae_pct": excursion.get("mae_pct"),
        "bar_count": excursion.get("bar_count", 0),
    }


def realized_pct(result: dict, bars: list[dict], start_bar: str = FAST_BAR) -> float | None:
    """장벽 규칙을 손익으로 환산한다. AMBIGUOUS는 판정 불가라 None.

    UP_FIRST/DOWN_FIRST는 승인된 장벽 그대로, NONE은 측정 창 마지막 종가로 청산한
    것으로 본다(F5 타임아웃 근사).
    """
    outcome = result.get("outcome")
    if outcome == "UP_FIRST":
        return UP_BARRIER_PCT * 100
    if outcome == "DOWN_FIRST":
        return -DOWN_BARRIER_PCT * 100
    if outcome != "NONE":
        return None
    window = [b for b in bars if start_bar <= str(b.get("time") or "")[:4] <= WINDOW_END]
    if not window:
        return None
    last = max(window, key=lambda b: str(b.get("time") or ""))
    try:
        close = float(last["close"])
    except (KeyError, TypeError, ValueError):
        return None
    entry = result["entry_price"]
    return (close / entry - 1) * 100 if entry > 0 else None


def simulate_day(
    date: str,
    universe: list[dict],
    p: Policy,
    bars_by_ticker: dict[str, list[dict]],
) -> dict:
    """정책 하나로 하루를 돌린다. 진입 못 하면 ``entered=False``."""
    ranked = rank(universe, p)
    attempts: list[dict[str, Any]] = []
    for candidate in ranked[: max(1, p.depth)]:
        ticker = str(candidate.get("ticker") or "")
        prev_close = float(candidate.get("prev_close") or 0.0)
        bars = bars_by_ticker.get(ticker)
        if not bars or prev_close <= 0:
            attempts.append({"ticker": ticker, "reason": "NO_BARS"})
            continue
        price = entry_price_from_bars(bars, p.entry_bar, field=p.entry_field)
        if not price:
            attempts.append({"ticker": ticker, "reason": "NO_ENTRY_BAR"})
            continue
        price *= 1 + p.entry_slippage_pct / 100
        gap = price / prev_close - 1
        allowed, reason = recheck_gate(gap, expected_amount(candidate), p)
        if not allowed:
            attempts.append(
                {"ticker": ticker, "reason": reason, "recheck_gap_pct": round(gap * 100, 2)}
            )
            continue
        result = evaluate_entry(bars, price, p.entry_bar)
        if result is None:
            attempts.append({"ticker": ticker, "reason": "NO_WINDOW"})
            continue
        return {
            "date": date,
            "entered": True,
            "ticker": ticker,
            "name": candidate.get("name"),
            "rank": ranked.index(candidate) + 1,
            "selection_gap_pct": round(float(candidate.get("gap_pct") or 0.0) * 100, 2),
            "recheck_gap_pct": round(gap * 100, 2),
            "expected_amount": expected_amount(candidate),
            "attempts": attempts,
            **result,
            "realized_pct": realized_pct(result, bars, p.entry_bar),
        }
    return {
        "date": date,
        "entered": False,
        "reason": "NO_CANDIDATE" if not ranked else "ALL_BLOCKED",
        "ranked_count": len(ranked),
        "attempts": attempts,
    }


def tickers_needed(universes: dict[str, list[dict]], policies: list[Policy]) -> dict[str, set[str]]:
    """정책들이 건드릴 수 있는 (날짜 → 종목) 집합. 분봉을 이만큼만 받는다."""
    needed: dict[str, set[str]] = {}
    for date, universe in universes.items():
        picks: set[str] = set()
        for p in policies:
            for candidate in rank(universe, p)[: max(1, p.depth)]:
                ticker = str(candidate.get("ticker") or "")
                if ticker:
                    picks.add(ticker)
        if picks:
            needed[date] = picks
    return needed


# ── 집계 ────────────────────────────────────────────────────────────────


def summarize(rows: list[dict]) -> dict:
    entered = [r for r in rows if r.get("entered")]
    scored = [r for r in entered if r.get("realized_pct") is not None]
    total = sum(r["realized_pct"] for r in scored)
    wins = [r for r in scored if r["realized_pct"] > 0]
    return {
        "days": len(rows),
        "entered": len(entered),
        "entry_rate_pct": round(len(entered) / len(rows) * 100, 1) if rows else 0.0,
        "scored": len(scored),
        "undecidable": len(entered) - len(scored),
        "up_first": sum(1 for r in entered if r.get("outcome") == "UP_FIRST"),
        "down_first": sum(1 for r in entered if r.get("outcome") == "DOWN_FIRST"),
        "none": sum(1 for r in entered if r.get("outcome") == "NONE"),
        "ambiguous": sum(1 for r in entered if r.get("outcome") == "AMBIGUOUS"),
        "win_rate_pct": round(len(wins) / len(scored) * 100, 1) if scored else 0.0,
        "total_pct": round(total, 2),
        "avg_pct": round(total / len(scored), 2) if scored else 0.0,
        "avg_mfe_pct": (
            round(sum(r["mfe_pct"] for r in entered if r.get("mfe_pct") is not None)
                  / max(1, sum(1 for r in entered if r.get("mfe_pct") is not None)), 2)
        ),
        "avg_mae_pct": (
            round(sum(r["mae_pct"] for r in entered if r.get("mae_pct") is not None)
                  / max(1, sum(1 for r in entered if r.get("mae_pct") is not None)), 2)
        ),
    }


def run_policy(
    universes: dict[str, list[dict]], p: Policy, bars: dict[str, dict[str, list[dict]]]
) -> dict:
    rows = [simulate_day(d, u, p, bars.get(d, {})) for d, u in sorted(universes.items())]
    return {"policy": p.name, "rows": rows, "summary": summarize(rows)}


# ── 분봉 수집 (KIS) ─────────────────────────────────────────────────────


def _assert_paper_mode() -> None:
    if os.getenv("KIS_MODE", "").upper() != "PAPER":
        raise PocStop("NOT_PAPER_MODE")


def _assert_safe_live_window(now: datetime) -> None:
    if now.hour == 9 and now.minute < 35:
        raise PocStop("UNSAFE_WINDOW")


async def fill_bar_cache(
    needed: dict[str, set[str]],
    *,
    cache_dir: Path = BAR_CACHE_DIR,
    max_calls: int = 400,
    interval_sec: float = 2.0,
    rate_limit_backoff_sec: float = 15.0,
    max_rate_limit_retries: int = 5,
) -> dict[str, int]:
    """캐시에 없는 (날짜, 종목) 분봉만 받아 채운다. 이미 받은 건 건드리지 않는다.

    PAPER는 초당 허용량이 좁아 연속 호출이 EGW00201로 끊긴다. 그때마다 표본을
    통째로 잃지 않도록 물러섰다가 같은 종목부터 다시 붙는다. 캐시가 남으므로
    재실행하면 중단 지점부터 이어진다.
    """
    from src.api import auth, kis_rest

    _assert_paper_mode()
    _assert_safe_live_window(datetime.now(KST))
    if not await auth.load_or_refresh():
        raise PocStop("TOKEN_UNAVAILABLE")
    budget = kis_rest.CallBudget(max_calls)
    throttle = Throttle(interval_sec)
    stats = {"cached": 0, "fetched": 0, "empty": 0, "failed": 0, "rate_limited": 0}
    for date in sorted(needed):
        for ticker in sorted(needed[date]):
            if read_cached_bars(date, ticker, cache_dir) is not None:
                stats["cached"] += 1
                continue
            for attempt in range(max_rate_limit_retries + 1):
                try:
                    bars = await load_bars(ticker, date, budget=budget, throttle=throttle)
                except PocStop as exc:
                    if exc.reason != "RATE_LIMIT" or attempt == max_rate_limit_retries:
                        raise
                    stats["rate_limited"] += 1
                    await asyncio.sleep(rate_limit_backoff_sec * (attempt + 1))
                    continue
                except Exception:
                    stats["failed"] += 1
                    break
                write_cached_bars(date, ticker, bars, cache_dir)
                stats["fetched" if bars else "empty"] += 1
                break
    return stats


def load_bar_cache(
    needed: dict[str, set[str]], *, cache_dir: Path = BAR_CACHE_DIR
) -> dict[str, dict[str, list[dict]]]:
    bars: dict[str, dict[str, list[dict]]] = {}
    for date, tickers in needed.items():
        day: dict[str, list[dict]] = {}
        for ticker in tickers:
            cached = read_cached_bars(date, ticker, cache_dir)
            if cached:
                day[ticker] = cached
        if day:
            bars[date] = day
    return bars


# ── 실행 ────────────────────────────────────────────────────────────────


def default_policies() -> list[Policy]:
    """운영 기본값과, 원인 1~4를 하나씩 푼 대조군."""
    fast = replace(BASELINE, name="FAST_ONLY", entry_bar=FAST_BAR)
    gap_first = replace(
        BASELINE,
        name="GAP_FIRST",
        w_gap=50.0,
        w_amount=15.0,
        w_surge=15.0,
        overheat_penalty=0.0,
    )
    deep = replace(BASELINE, name="DEPTH_8", depth=8)
    loose = replace(
        BASELINE,
        name="LOOSE_GAP",
        gap_hard_max=0.150,
        high_gap_min_amount=500_000_000,
        recheck_gap_max=0.150,
        recheck_high_gap_min_amount=500_000_000,
    )
    # 진입가 가정은 로그의 실제 매도호가에서 실측한다.
    #   legacy(09:01 진입): 0901 봉 시가 대비 평균 -0.23% (n=15)
    #   fast  (09:00 진입): 0900 봉 시가 대비 평균 +0.19% (n=17)
    # 개장 경계의 스프레드가 더 넓어 fast 가 구조적으로 0.42%p 비싸다. 한쪽에만
    # 얹으면 비교가 기운다. BASELINE(무보정)은 기준선으로 남겨 둔다.
    legacy_sym = replace(BASELINE, name="LEGACY_SYM", entry_slippage_pct=-0.23)
    fast_sym = replace(
        BASELINE, name="FAST_SYM", entry_bar=FAST_BAR, entry_slippage_pct=+0.19
    )
    # 손익분기 확인용. 실측의 두 배까지 밀려도 부호가 서는가.
    fast_worst = replace(
        BASELINE, name="FAST_SLIP40", entry_bar=FAST_BAR, entry_slippage_pct=0.40
    )
    all_worst = replace(
        BASELINE,
        name="ALLFIX_SYM",
        entry_bar=FAST_BAR,
        entry_slippage_pct=+0.19,
        w_gap=50.0,
        w_amount=15.0,
        w_surge=15.0,
        overheat_penalty=0.0,
        gap_hard_max=0.150,
        high_gap_min_amount=500_000_000,
        recheck_gap_max=0.150,
        recheck_high_gap_min_amount=500_000_000,
        depth=8,
    )
    combined = replace(
        BASELINE,
        name="ALL_FIXES",
        entry_bar=FAST_BAR,
        entry_slippage_pct=+0.19,
        w_gap=50.0,
        w_amount=15.0,
        w_surge=15.0,
        overheat_penalty=0.0,
        gap_hard_max=0.150,
        high_gap_min_amount=500_000_000,
        recheck_gap_max=0.150,
        recheck_high_gap_min_amount=500_000_000,
        depth=8,
    )
    return [
        BASELINE,
        legacy_sym,
        fast,
        fast_sym,
        fast_worst,
        gap_first,
        deep,
        loose,
        combined,
        all_worst,
    ]


def _print_report(results: list[dict]) -> None:
    header = (
        f"{'policy':<12}{'days':>5}{'entry':>6}{'entry%':>8}"
        f"{'win%':>7}{'up':>4}{'dn':>4}{'none':>6}{'total%':>9}{'avg%':>7}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        s = r["summary"]
        print(
            f"{r['policy']:<12}{s['days']:>5}{s['entered']:>6}{s['entry_rate_pct']:>8}"
            f"{s['win_rate_pct']:>7}{s['up_first']:>4}{s['down_first']:>4}"
            f"{s['none']:>6}{s['total_pct']:>9}{s['avg_pct']:>7}"
        )


# ── 실측 Fast Path (프로브 기록) ────────────────────────────────────────
#
# 위 FAST_* 정책은 F1 스냅샷의 60종목 유니버스를 09:00 봉 시가로 평가한 **상한**이다.
# 실제 hybrid는 장전에 추린 30종목만 보고, 09:00:00.3에 실제로 호가된 매도호가를
# 때린다. 아래는 그 기록(``data/paper_fast_probe``)만으로 실제 성과를 낸다.
# 추정 가격이 하나도 들어가지 않는다는 점이 위 정책들과 다르다.

PROBE_DIR = ROOT / "data" / "paper_fast_probe"


def load_probe_days(probe_dir: Path = PROBE_DIR) -> dict[str, list[dict]]:
    """날짜 → hybrid가 F2에 넘겼을 후보(순위 순). 전부 실측 기록에서 나온다.

    후보 dict 는 운영 ``paper_fast_probe._candidate_from_multi`` 로 만든다. 여기서
    직접 계산하면 ``expected_amount`` 의 의미(예상체결대금 vs 누적거래대금)가 달라져
    고갭 대금 게이트가 다른 답을 낸다.

    순서는 ``PAPER_FAST_SHADOW_COMPARE`` 의 ``fast_tickers`` 를 쓴다. 그게 없으면
    ``PAPER_FAST_PROBE_OPEN_DONE`` 의 ``shadow_tickers`` 로 떨어진다.
    """
    from src.modules import paper_fast_probe

    days: dict[str, list[dict]] = {}
    for path in sorted(probe_dir.glob("*.jsonl")):
        date = path.name[:8]
        rows_by_ticker: dict[str, dict] = {}
        order: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = record.get("event")
            if event == "PAPER_FAST_PROBE_OPEN_MULTI":
                for row in (record.get("response") or {}).get("output") or []:
                    ticker = str(row.get("inter_shrn_iscd") or "")
                    if ticker:
                        rows_by_ticker[ticker] = row
            elif event == "PAPER_FAST_PROBE_OPEN_DONE":
                order = order or [str(t) for t in (record.get("shadow_tickers") or [])]
            elif event == "PAPER_FAST_SHADOW_COMPARE":
                fast = [str(t) for t in (record.get("fast_tickers") or [])]
                if fast:
                    order = fast
        candidates = []
        for ticker in order:
            row = rows_by_ticker.get(ticker)
            if not row:
                continue
            candidate = paper_fast_probe._candidate_from_multi(row, "SHORTLIST", {})
            if not candidate:
                continue
            if candidate.get("ask_price", 0) <= 0 or candidate.get("prev_close", 0) <= 0:
                continue
            candidates.append(candidate)
        if candidates:
            days[date] = candidates
    return days


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def simulate_probe_day(date: str, candidates: list[dict], p: Policy, bars_by_ticker: dict) -> dict:
    """실측 호가로 하루를 돌린다. 진입가는 09:00:00.3의 실제 매도호가."""
    attempts = []
    for index, candidate in enumerate(candidates[: max(1, p.depth)], start=1):
        ticker = candidate["ticker"]
        bars = bars_by_ticker.get(ticker)
        if not bars:
            attempts.append({"ticker": ticker, "reason": "NO_BARS"})
            continue
        gap = float(candidate.get("gap_pct") or 0.0)
        allowed, reason = recheck_gate(gap, candidate.get("expected_amount"), p)
        if not allowed:
            attempts.append(
                {"ticker": ticker, "reason": reason, "recheck_gap_pct": round(gap * 100, 2)}
            )
            continue
        result = evaluate_entry(bars, candidate["ask_price"], FAST_BAR)
        if result is None:
            attempts.append({"ticker": ticker, "reason": "NO_WINDOW"})
            continue
        return {
            "date": date,
            "entered": True,
            "ticker": ticker,
            "name": candidate.get("name"),
            "rank": index,
            "selection_gap_pct": round(gap * 100, 2),
            "recheck_gap_pct": round(gap * 100, 2),
            "expected_amount": candidate.get("expected_amount"),
            "attempts": attempts,
            **result,
            "realized_pct": realized_pct(result, bars, FAST_BAR),
        }
    return {
        "date": date,
        "entered": False,
        "reason": "ALL_BLOCKED" if candidates else "NO_CANDIDATE",
        "ranked_count": len(candidates),
        "attempts": attempts,
    }


def probe_tickers_needed(days: dict[str, list[dict]], depth: int = 3) -> dict[str, set[str]]:
    return {
        date: {c["ticker"] for c in candidates[: max(1, depth)]}
        for date, candidates in days.items()
        if candidates
    }


def run_probe_policy(days: dict[str, list[dict]], p: Policy, bars: dict) -> dict:
    rows = [
        simulate_probe_day(date, candidates, p, bars.get(date, {}))
        for date, candidates in sorted(days.items())
    ]
    return {"policy": p.name, "rows": rows, "summary": summarize(rows)}


def _print_rows(rows: list[dict]) -> None:
    for row in rows:
        if row.get("entered"):
            pnl = row.get("realized_pct")
            shown = "AMBIG" if pnl is None else f"{pnl:+6.2f}%"
            print(
                f"  {row['date']} {row['ticker']} rank{row['rank']} "
                f"gap{row['recheck_gap_pct']:>6}% {row['outcome']:<11}{shown}"
            )
        else:
            reasons = [a.get("reason") for a in row.get("attempts", [])]
            print(f"  {row['date']} -- {row.get('reason')} {reasons}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("snapshot", "probe", "both"),
        default="both",
        help=(
            "snapshot=F1 스냅샷 60종목 유니버스에 정책을 다시 적용(가정이 섞인 탐색용). "
            "probe=Fast Path 프로브 실측 호가로 hybrid 성과만 계산(가정 없음). "
            "두 값이 갈리면 probe 쪽을 믿는다."
        ),
    )
    parser.add_argument("--snapshot-dir", default=str(SNAPSHOT_DIR))
    parser.add_argument("--probe-dir", default=str(PROBE_DIR))
    parser.add_argument("--cache-dir", default=str(BAR_CACHE_DIR))
    parser.add_argument(
        "--with-kis",
        action="store_true",
        help="캐시에 없는 분봉을 KIS에서 채운다 (PAPER, 09:35 이후, GET만).",
    )
    parser.add_argument("--max-calls", type=int, default=400)
    parser.add_argument("--interval", type=float, default=2.0, help="분봉 호출 간 최소 간격(초)")
    parser.add_argument("--depth", type=int, default=3, help="probe 모드에서 시도할 후보 수")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    cache_dir = Path(args.cache_dir)
    policies = default_policies()
    universes: dict[str, list[dict]] = {}
    probe_days: dict[str, list[dict]] = {}
    needed: dict[str, set[str]] = {}

    if args.mode in ("snapshot", "both"):
        universes = load_universes(Path(args.snapshot_dir))
        needed = tickers_needed(universes, policies)
    if args.mode in ("probe", "both"):
        probe_days = load_probe_days(Path(args.probe_dir))
        for date, tickers in probe_tickers_needed(probe_days, args.depth).items():
            needed.setdefault(date, set()).update(tickers)

    if args.with_kis:
        try:
            stats = asyncio.run(
                fill_bar_cache(
                    needed,
                    cache_dir=cache_dir,
                    max_calls=args.max_calls,
                    interval_sec=args.interval,
                )
            )
        except PocStop as exc:
            print(json.dumps({"stopped": exc.reason, "msg_cd": exc.msg_cd}, ensure_ascii=False))
            return 1
        print(json.dumps({"bar_cache": stats}, ensure_ascii=False))

    bars = load_bar_cache(needed, cache_dir=cache_dir)
    missing = sum(len(t) for t in needed.values()) - sum(len(d) for d in bars.values())
    if missing:
        print(f"분봉 미보유 {missing}건. --with-kis 로 채워야 판정이 완전해진다.")

    results: list[dict] = []
    if universes:
        print()
        print("[snapshot] F1 스냅샷 유니버스 + 봉 가격 가정. 탐색용이며 그대로 믿지 않는다.")
        snapshot_results = [run_policy(universes, p, bars) for p in policies]
        _print_report(snapshot_results)
        results.extend(snapshot_results)
    if probe_days:
        print()
        print("[probe] Fast Path 실측 호가. hybrid 를 켰다면 실제로 나왔을 성과.")
        probe_policy = replace(BASELINE, name="HYBRID_REAL", depth=args.depth)
        probe_result = run_probe_policy(probe_days, probe_policy, bars)
        _print_report([probe_result])
        _print_rows(probe_result["rows"])
        results.append(probe_result)

    if args.out:
        Path(args.out).write_text(
            json.dumps({"results": results}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
