"""H5 판정 층 — 블록 부트스트랩, G1~G5, 기록 지표, 20칸 자금 곡선.

스펙 §4.2(통과 조건), §4.3(부트스트랩), §4.4(기록만 하는 지표). 순수 파이썬 —
프로젝트 venv 에 numpy 가 없다.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from scripts.breakout_rules import COST, Trade

BLOCK = 20
REPS = 10_000
SEED = 20260930
TRIM_FRAC = 0.01
PERIODS = (("2011", "2015"), ("2016", "2020"), ("2021", "2026"))
MIN_N = 50
SLOTS = 20


@dataclass(frozen=True)
class Sample:
    ticker: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    net: float  # A = 총수익 − 왕복 비용
    excess: float  # X = 총수익 − 주 대조군(거래대금 맞춤) 평균
    excess_eq: float | None  # X_eq = 총수익 − 기록용 동일가중 대조군 평균


def samples_from_trades(
    trades: Iterable[Trade],
) -> tuple[list[Sample], Counter[str]]:
    """완료되고 주 대조군이 있는 거래만 판정 표본이 된다 (스펙 §2.6, §3.3)."""
    samples: list[Sample] = []
    dropped: Counter[str] = Counter()
    for t in trades:
        o = t.outcome
        if not o.complete:
            dropped["incomplete"] += 1
            continue
        if t.matched_mean is None:
            dropped["no_matched_control"] += 1
            continue
        gross = o.gross
        excess_eq = (
            None
            if t.equal_mean is None
            else gross - t.equal_mean
        )
        samples.append(
            Sample(
                t.ticker,
                o.entry_date,
                o.exit_date,
                o.entry_price,
                o.exit_price,
                gross - COST,
                gross - t.matched_mean,
                excess_eq,
            )
        )
    samples.sort(key=lambda s: s.entry_date)
    return samples, dropped


def block_bootstrap_ci(
    values: list[float],
    *,
    block: int = BLOCK,
    reps: int = REPS,
    seed: int = SEED,
) -> tuple[float, float]:
    """이동 블록 부트스트랩 평균의 95% CI (스펙 §4.3).

    Returns (lower, upper) at indices 250 and 9749 for 10k reps.
    Raises ValueError if any value is not finite.
    Returns (-inf, inf) if fewer than one block.
    """
    if not all(math.isfinite(v) for v in values):
        raise ValueError("non-finite value in bootstrap input")
    n = len(values)
    k = n // block
    if k < 1:
        return (-math.inf, math.inf)
    prefix = [0.0]
    for v in values:
        prefix.append(prefix[-1] + v)
    sums = [prefix[i + block] - prefix[i] for i in range(n - block + 1)]
    rng = random.Random(seed)
    m = len(sums)
    means = sorted(
        sum(sums[rng.randrange(m)] for _ in range(k)) / (k * block)
        for _ in range(reps)
    )
    idx_lower = int(reps * 0.025)
    idx_upper = reps - 1 - idx_lower
    return (means[idx_lower], means[idx_upper])


def block_bootstrap_lower(
    values: list[float],
    *,
    block: int = BLOCK,
    reps: int = REPS,
    seed: int = SEED,
) -> float:
    """이동 블록 부트스트랩 평균의 95% CI 하한 (스펙 §4.3). 한 블록도 안 되면 -inf."""
    return block_bootstrap_ci(values, block=block, reps=reps, seed=seed)[0]


def trimmed_mean(
    values: list[float], frac: float = TRIM_FRAC
) -> float:
    """큰 쪽 상위 ⌈frac⌉ 를 뺀 평균 (스펙 §4.2 G3)."""
    if not values:
        return math.nan
    drop = math.ceil(len(values) * frac)
    kept = sorted(values)[: len(values) - drop]
    return statistics.fmean(kept) if kept else math.nan


def period_means(samples: list[Sample]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for lo, hi in PERIODS:
        xs = [s.excess for s in samples if lo <= s.entry_date[:4] <= hi]
        out[f"{lo}-{hi}"] = statistics.fmean(xs) if xs else None
    return out


def evaluate(samples: list[Sample]) -> dict:
    """G1~G5 (스펙 §4.2). 다섯 개 모두여야 통과, 부분 통과 없음.

    Raises ValueError if entry_date values are not non-decreasing
    (G2's block structure depends on entry-date order).
    """
    # Check entry_date ordering for G2
    for i in range(1, len(samples)):
        if samples[i].entry_date < samples[i - 1].entry_date:
            raise ValueError("entry_date values not non-decreasing")

    net = [s.net for s in samples]
    excess = [s.excess for s in samples]
    n = len(samples)
    mean_net = statistics.fmean(net) if net else math.nan
    mean_excess = statistics.fmean(excess) if excess else math.nan
    lower_excess, upper_excess = block_bootstrap_ci(excess)
    trimmed_net, trimmed_excess = (
        trimmed_mean(net),
        trimmed_mean(excess),
    )
    periods = period_means(samples)
    gates = {
        "G1": mean_net > 0,
        "G2": lower_excess > 0,
        "G3": trimmed_net > 0 and trimmed_excess > 0,
        "G4": all(
            m is not None and m > 0 for m in periods.values()
        ),
        "G5": n >= MIN_N,
    }
    return {
        "n": n,
        "mean_net": mean_net,
        "mean_excess": mean_excess,
        "lower_excess": lower_excess,
        "upper_excess": upper_excess,
        "trimmed_net": trimmed_net,
        "trimmed_excess": trimmed_excess,
        "period_excess": periods,
        "gates": gates,
        "passed": all(gates.values()),
    }


def recorded_metrics(samples: list[Sample]) -> dict:
    """판정에 쓰지 않는 기록 지표 (스펙 §4.4)."""
    net = [s.net for s in samples]
    with_eq = [s for s in samples if s.excess_eq is not None]
    eq = [s.excess_eq for s in with_eq if s.excess_eq is not None]
    by_year: dict[str, list[Sample]] = {}
    for s in samples:
        by_year.setdefault(s.entry_date[:4], []).append(s)
    lower_net, upper_net = block_bootstrap_ci(net)
    lower_eq, upper_eq = block_bootstrap_ci(eq)
    return {
        "win_rate": (
            sum(v > 0 for v in net) / len(net) if net else math.nan
        ),
        "median_net": (
            statistics.median(net) if net else math.nan
        ),
        "lower_net": lower_net,
        "upper_net": upper_net,
        "mean_excess_eq": (
            statistics.fmean(eq) if eq else math.nan
        ),
        "lower_excess_eq": lower_eq,
        "upper_excess_eq": upper_eq,
        # 주 대조군 평균 − 동일가중 평균 = X_eq − X. 거래대금이 몰린 종목
        # 집단의 효과 추정치.
        "control_gap": (
            statistics.fmean(
                [
                    s.excess_eq - s.excess
                    for s in with_eq
                    if s.excess_eq is not None
                ]
            )
            if with_eq
            else math.nan
        ),
        "yearly": {
            year: {
                "n": len(group),
                "mean_net": statistics.fmean(s.net for s in group),
                "mean_excess": statistics.fmean(
                    s.excess for s in group
                ),
            }
            for year, group in sorted(by_year.items())
        },
    }


def equity_curve_mdd(
    samples: list[Sample],
    close_on: Callable[[str, str], float | None],
    calendar: list[str],
    slots: int = SLOTS,
) -> dict:
    """Marks-to-market equity curve with slots.

    Each slot is a fixed 1/slots of starting capital (does not compound).
    Each admitted trade occupies one slot. On entry, a position is held at
    entry_price; on exit, it realizes net return including COST. Open
    positions are marked to market daily from close_on callback.

    Returns mdd (max drawdown), excluded_trades, and over_capacity_days
    (the number of distinct entry days on which a trade was refused a slot).
    """
    admitted: list[Sample] = []
    excluded = 0
    over_days: set[str] = set()
    for s in sorted(samples, key=lambda s: (s.entry_date, s.ticker)):
        live = sum(
            1
            for q in admitted
            if q.entry_date <= s.entry_date < q.exit_date
        )
        if live >= slots:
            excluded += 1
            over_days.add(s.entry_date)
            continue
        admitted.append(s)
    result = {
        "mdd": 0.0,
        "excluded_trades": excluded,
        "over_capacity_days": len(over_days),
    }
    if not admitted:
        return result

    weight = 1.0 / slots
    start = admitted[0].entry_date
    end = max(s.exit_date for s in admitted)
    realized = 0.0
    open_idx: list[int] = []
    last_px: dict[int, float] = {}
    nxt = 0
    peak = 1.0
    mdd = 0.0
    for day in calendar:
        if day < start:
            continue
        if day > end:
            break
        while nxt < len(admitted) and admitted[nxt].entry_date <= day:
            open_idx.append(nxt)
            nxt += 1
        still: list[int] = []
        unrealized = 0.0
        for idx in open_idx:
            s = admitted[idx]
            if s.exit_date <= day:
                realized += weight * (
                    s.exit_price / s.entry_price - 1.0 - COST
                )
                continue
            px = close_on(s.ticker, day)
            if px is not None:
                last_px[idx] = px
            unrealized += weight * (
                last_px.get(idx, s.entry_price)
                / s.entry_price
                - 1.0
            )
            still.append(idx)
        open_idx = still
        equity = 1.0 + realized + unrealized
        peak = max(peak, equity)
        mdd = max(mdd, 1.0 - equity / peak)
    result["mdd"] = mdd
    return result
