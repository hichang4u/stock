r"""C1 오버나이트 체 — 후보를 D-0 종가에 산 것으로 두고 D+1 분봉에 A의 F4 규칙을 재생한다.

판정 기준은 docs/superpowers/specs/2026-09-21-c1-overnight-close-hypothesis.md §4에 고정돼
있다. 이 스크립트는 그 수치를 계산해 보여줄 뿐 판정하지 않는다 — n≥50 전에는 어떤 값도
결론이 아니다.

    .\.venv\Scripts\python.exe scripts\overnight_sieve.py --root D:\Private\stock-prod
    .\.venv\Scripts\python.exe scripts\overnight_sieve.py --root D:\Private\stock-prod --json

분봉은 track_b_backfill이 채운 data/backtest_bars/<D+1>_<ticker>.json 이다. 봉 안 순서는
"저가 먼저"로 고정한다(트랙 B와 같다).
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.track_b_backtest import bootstrap_ci  # noqa: E402
from scripts.track_b_rules import HARD_STOP, simulate_exit  # noqa: E402
from src import warmup  # noqa: E402

# 개선 계획 §2 초기 PAPER 비용·체결 가정. 연구 상수이며 요율의 단정이 아니다.
BASE_ROUND_TRIP_COST_PCT = 0.18
HARD_STOP_SLIPPAGE_PCT = 0.30
TRAILING_SLIPPAGE_PCT = 0.15
TIMEOUT_SLIPPAGE_PCT = 0.20
ENTRY_SLIPPAGE_PCT = 0.0  # 마감 동시호가 단일가 체결 가정 (스펙 §3.2)

_SLIPPAGE_BY_REASON = {
    "GAP_HARD_STOP": HARD_STOP_SLIPPAGE_PCT,
    "HARD_STOP": HARD_STOP_SLIPPAGE_PCT,
    "TRAILING": TRAILING_SLIPPAGE_PCT,
    "TIMEOUT": TIMEOUT_SLIPPAGE_PCT,
    "DATA_END": TIMEOUT_SLIPPAGE_PCT,
}


def simulate_overnight(bars: list[dict], entry_price: float) -> dict:
    """전날 종가 진입. 첫 봉 시가가 하드스탑 아래면 시가에서 끝, 아니면 트랙 B F4 재생."""
    first = bars[0]
    open_price = float(first["open"])
    gap_pct = round((open_price / entry_price - 1) * 100, 10)
    complete = warmup.covers_session(bars)
    if open_price <= entry_price * (1 - HARD_STOP):
        return {
            "open": open_price, "gap_pct": gap_pct, "exit_reason": "GAP_HARD_STOP",
            "exit_time": first["time"], "exit_price": open_price, "gross_pct": gap_pct,
            "bars_complete": complete,
        }
    result = simulate_exit(bars, 0, entry_price, order="low_first")
    return {
        "open": open_price, "gap_pct": gap_pct, "exit_reason": result["reason"],
        "exit_time": result["exit_time"], "exit_price": result["exit_price"],
        "gross_pct": result["pct"], "bars_complete": complete,
    }


def apply_costs(gross_pct: float, exit_reason: str) -> dict:
    net_cost = gross_pct - BASE_ROUND_TRIP_COST_PCT
    slip = _SLIPPAGE_BY_REASON.get(exit_reason, TIMEOUT_SLIPPAGE_PCT) + ENTRY_SLIPPAGE_PCT
    return {"net_cost_pct": net_cost, "net_slip_pct": net_cost - slip}


# 스펙 §4.1 — 바꾸지 않는다.
MIN_N = 50
EARLY_STOP_N = 30
EARLY_STOP_MEAN = -1.0
EARLY_STOP_DRAWDOWN = -15.0
TOP_REMOVED = 2


def _max_drawdown(values: list[float]) -> float:
    peak = 0.0
    cum = 0.0
    worst = 0.0
    for v in values:
        cum += v
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return round(worst, 10)


def summarize(results: list[dict], *, missing: dict) -> dict:
    """§4.1 주 지표·판정 조건과 §4.2 부 지표. 판정은 하지 않고 조건 충족 여부만 낸다."""
    rank1 = sorted((r for r in results if r.get("rank") == 1 and r.get("bars_complete")),
                   key=lambda r: r["date"])
    values = [float(r["net_slip_pct"]) for r in rank1]
    n = len(values)
    avg = mean(values) if values else None
    lo, hi = bootstrap_ci(values) if n >= 2 else (None, None)
    trimmed = sorted(values)[:-TOP_REMOVED] if n > TOP_REMOVED else []
    trimmed_mean = mean(trimmed) if trimmed else None
    drawdown = _max_drawdown(values)

    evaluable = n >= MIN_N
    conditions = {
        "evaluable": evaluable,
        "mean_positive": bool(avg is not None and avg > 0),
        "ci_low_positive": bool(lo is not None and lo > 0),
        "robust_top2": bool(trimmed_mean is not None and trimmed_mean > 0),
    }
    conditions["all"] = evaluable and all(
        conditions[k] for k in ("mean_positive", "ci_low_positive", "robust_top2")
    )

    early: dict = {"evaluable": n >= EARLY_STOP_N, "triggered": False, "reason": None}
    if early["evaluable"] and avg is not None:
        if avg < EARLY_STOP_MEAN:
            early.update(triggered=True, reason="MEAN")
        elif drawdown < EARLY_STOP_DRAWDOWN:
            early.update(triggered=True, reason="DRAWDOWN")

    gaps = [float(r["gap_pct"]) for r in rank1]
    by_rank: dict[int, list[float]] = {}
    for r in results:
        if isinstance(r.get("rank"), int) and r.get("bars_complete"):
            by_rank.setdefault(r["rank"], []).append(float(r["net_slip_pct"]))
    return {
        "n": n,
        "mean": avg,
        "ci_low": lo,
        "ci_high": hi,
        "mean_top2_removed": trimmed_mean,
        "max_drawdown_pct": drawdown,
        "pass_conditions": conditions,
        "early_stop": early,
        "reasons": dict(Counter(r["exit_reason"] for r in rank1)),
        "gap": {
            "mean": mean(gaps) if gaps else None,
            "median": median(gaps) if gaps else None,
            "share_positive": (sum(1 for g in gaps if g > 0) / n) if n else None,
        },
        "rank_means": {k: mean(v) for k, v in sorted(by_rank.items())},
        "missing": dict(missing),
    }
