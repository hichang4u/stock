r"""C1 오버나이트 체 — 후보를 D-0 종가에 산 것으로 두고 D+1 분봉에 A의 F4 규칙을 재생한다.

판정 기준은 docs/superpowers/specs/2026-09-21-c1-overnight-close-hypothesis.md §4에 고정돼
있다. 이 스크립트는 그 수치를 계산해 보여줄 뿐 판정하지 않는다 — n≥50 전에는 어떤 값도
결론이 아니다.

    .\.venv\Scripts\python.exe scripts\overnight_sieve.py --root D:\Private\stock-prod
    .\.venv\Scripts\python.exe scripts\overnight_sieve.py \
        --root D:\Private\stock-prod --json out.json

분봉은 track_b_backfill이 채운 data/backtest_bars/<D+1>_<ticker>.json 이다. 봉 안 순서는
"저가 먼저"로 고정한다(트랙 B와 같다).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.strategy_backtest import read_cached_bars  # noqa: E402
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

    # 스펙 §5는 "n=30 도달 시 한 번만 본다". 여기서는 n≥30이면 계산만 하고, 한 번만 판정하는
    # 것은 운영자의 몫이다(재실행마다 새 판정이 아니다).
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


def load_candidates(candidates_dir: Path) -> tuple[dict[str, list[dict]], dict]:
    """{D-0: [rank가 정수인 행]}. 요약 행이 없는 파일은 중간에 죽은 것 — 표본에서 빼고 센다."""
    loaded: dict[str, list[dict]] = {}
    missing = {"screen_died": 0, "degraded": 0, "no_candidate": 0}
    if not candidates_dir.exists():
        return loaded, missing
    for path in sorted(candidates_dir.glob("*.jsonl")):
        rows: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            # 잘린 줄 하나가 그날 보고 전체를 멈추면 안 된다 — overnight_pairs와 같은 규칙.
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            rows.append(row)
        if not rows or not rows[-1].get("summary"):
            missing["screen_died"] += 1
            continue
        summary = rows[-1]
        if summary.get("degraded"):
            missing["degraded"] += 1
        ranked = [r for r in rows[:-1] if _is_valid_candidate_row(r)]
        if not ranked:
            missing["no_candidate"] += 1
        loaded[path.stem] = ranked
    return loaded, missing


def _is_valid_candidate_row(row: dict) -> bool:
    if not isinstance(row.get("rank"), int):
        return False
    ticker = str(row.get("ticker") or "")
    if len(ticker) != 6 or not ticker.isdigit():
        return False
    try:
        return float(row.get("close")) > 0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _next_dates(candidate_dates: list[str], bars_dir: Path) -> dict[str, str | None]:
    bar_dates = {p.name[:8] for p in bars_dir.glob("*_*.json")} if bars_dir.exists() else set()
    calendar = sorted(set(candidate_dates) | bar_dates)
    out: dict[str, str | None] = {}
    for date in candidate_dates:
        later = [d for d in calendar if d > date]
        out[date] = later[0] if later else None
    return out


def run(root: Path) -> tuple[list[dict], dict]:
    candidates_dir = root / "data" / "overnight" / "candidates"
    results_dir = root / "data" / "overnight" / "results"
    bars_dir = root / "data" / "backtest_bars"
    loaded, missing = load_candidates(candidates_dir)
    missing.update({"bars_missing": 0, "bars_incomplete": 0, "awaiting_next_day": 0})
    next_of = _next_dates(sorted(loaded), bars_dir)
    results: list[dict] = []
    for date, rows in sorted(loaded.items()):
        next_date = next_of[date]
        if next_date is None:
            missing["awaiting_next_day"] += 1
            continue
        day_results: list[dict] = []
        for row in rows:
            # load_candidates가 이미 ticker/rank/close를 보장한다 — 여기서 다시 지키지 않는다.
            bars = read_cached_bars(next_date, str(row["ticker"]), cache_dir=bars_dir)
            if not bars:
                missing["bars_missing"] += 1
                continue
            sim = simulate_overnight(bars, float(row["close"]))
            if not sim["bars_complete"]:
                missing["bars_incomplete"] += 1
            costs = apply_costs(sim["gross_pct"], sim["exit_reason"])
            day_results.append({
                "date": date, "next_date": next_date, "ticker": str(row["ticker"]),
                "rank": int(row["rank"]), "entry_price": float(row["close"]),
                **sim, **costs, "ambiguous": False,
            })
        if day_results:
            results_dir.mkdir(parents=True, exist_ok=True)
            (results_dir / f"{date}.json").write_text(
                json.dumps(day_results, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        results.extend(day_results)
    return results, missing


def print_report(results: list[dict], summary: dict) -> None:
    print(f"{'D-0':8} {'D+1':8} {'종목':6} {'순위':>2} {'진입':>8} {'갭%':>6} {'사유':13} "
          f"{'시각':6} {'총%':>7} {'비용후%':>7}")
    for r in results:
        print(f"{r['date']:8} {r['next_date']:8} {r['ticker']:6} {r['rank']:>2} "
              f"{r['entry_price']:8.0f} {r['gap_pct']:+6.2f} {r['exit_reason']:13} "
              f"{r['exit_time']:6} {r['gross_pct']:+7.2f} {r['net_slip_pct']:+7.2f}")
    s = summary
    print(f"\n랭크1 n={s['n']}  평균 {s['mean']}  CI [{s['ci_low']}, {s['ci_high']}]  "
          f"상위2제외 {s['mean_top2_removed']}  최대낙폭 {s['max_drawdown_pct']:+.2f}%p")
    print(f"판정 가능 n≥{MIN_N}: {s['pass_conditions']['evaluable']}  조건: {s['pass_conditions']}")
    print(f"조기 중단(n={EARLY_STOP_N}): {s['early_stop']}")
    print(f"청산 사유: {s['reasons']}   갭: {s['gap']}   랭크별 평균: {s['rank_means']}")
    print(f"결측: {s['missing']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C1 오버나이트 체 — 후보의 D+1 F4 재생")
    parser.add_argument("--root", type=Path, default=ROOT, help="data/ 가 있는 트리")
    parser.add_argument("--json", type=Path, default=None, help="집계를 JSON으로도 저장")
    args = parser.parse_args(argv)
    results, missing = run(args.root)
    summary = summarize(results, missing=missing)
    print_report(results, summary)
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "results": results},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n저장: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
