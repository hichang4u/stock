"""H1 재평가 — 읽기 전용. KIS 호출 없음(분봉은 `track_b_backfill.py`가 미리 받아 둔다).

스펙 `docs/superpowers/specs/2026-09-11-h1-gap-selection-hypothesis.md` §3·§4.1 결정(2026-10-30).

- 09:01 갭(정의 1) = 그 종목 09:01 1분봉 시가 ÷ 전일종가 − 1. 전일종가는 그날 로그에서 그 종목의 첫
  `F3_RECHECK.prev_close`다(관문 스크립트 `h1_gap_concordance.py`와 같은 분모).
- 유니버스(정의 2) = `data/replay/universes.json`
  (`replay_universe.py` 산출물, `TARGET_LOCKED` 최대 3종목).
- 규칙(§1) = 갭 7% 이상인 후보 중 랭크 최상위. 없으면 그날 진입 없음.
- 대조군(정의 3) = 같은 날 랭크 1.
- 진입·청산(§3) = 09:01봉 시가 진입, A 청산(하드스탑 −2.0% / 스텝 2.5% 트레일 2.0% / 15:15),
  봉 안 순서 "저가 먼저".
- 09:01봉이 없는 후보(정의 4)는 그 후보만 빼고 센다. 랭크 1이 빠지면 그날 대조군은 없다.
- 보조 보고(정의 5) = 빠른 경로 날(`F3_FAST_RECHECK_USED`가 있는 날) / 레거시 날.
  판정에는 쓰지 않는다.

    python scripts/h1_reevaluation.py --end 20261030 \\
        --log-dir data/logs.migrated --log-dir D:/Private/stock-prod/data/logs

`--start 20260701 --end 20260910`은 등록 표본에 같은 진입·청산을 돌린다(이식 점검용, 판정과 무관).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.h2_sieve import ENTRY_BAR, simulate_open_entry  # noqa: E402
from scripts.strategy_backtest import read_cached_bars  # noqa: E402
from scripts.track_b_backtest import bootstrap_ci  # noqa: E402

THRESHOLD_PCT = 7.0
MIN_N = 50
REGISTRATION_END = "20260911"   # §3: 2026-09-12 이후 거래일만
SEED = 20260828                 # track_b_backtest.bootstrap_ci 기본값


def gap_0901_pct(bars: list[dict] | None, prev_close: float | None) -> float | None:
    if not bars or not prev_close:
        return None
    for bar in bars:
        if str(bar.get("time")) == ENTRY_BAR and bar.get("open"):
            return (float(bar["open"]) / float(prev_close) - 1) * 100
    return None


def _trade(candidate: dict, gap: float, bars: list[dict]) -> dict | None:
    result = simulate_open_entry(bars)
    if result is None:
        return None
    return {"rank": int(candidate["rank"]), "ticker": str(candidate["ticker"]),
            "gap_pct": gap, **result}


def evaluate_day(
    candidates: list[dict],
    bars_by_ticker: dict[str, list[dict] | None],
    prev_close: dict[str, float],
) -> dict:
    """하루치 규칙 진입·대조군 진입·제외 건수.

    대조군(랭크 1)은 갭을 보지 않으므로 전일종가가 없어도 09:01봉만 있으면 진입한다.
    """
    excluded = {"no_bar": 0, "no_prev_close": 0}
    priced: list[tuple[dict, float, list[dict]]] = []
    control = None
    for cand in sorted(candidates, key=lambda c: int(c["rank"])):
        tk = str(cand["ticker"])
        bars = bars_by_ticker.get(tk)
        has_bar = gap_0901_pct(bars, 1.0) is not None
        if not has_bar or bars is None:
            excluded["no_bar"] += 1
            continue
        if int(cand["rank"]) == 1:
            control = _trade(cand, gap_0901_pct(bars, prev_close.get(tk)) or 0.0, bars)
        if tk not in prev_close:
            excluded["no_prev_close"] += 1
            continue
        gap = gap_0901_pct(bars, prev_close[tk])
        if gap is None:
            continue
        priced.append((cand, gap, bars))

    rule = None
    for cand, gap, bars in priced:
        if gap >= THRESHOLD_PCT:
            rule = _trade(cand, gap, bars)
            break
    return {"rule": rule, "control": control, "excluded": excluded,
            "gaps": {str(c["ticker"]): round(g, 2) for c, g, _ in priced}}


def _group(pcts: list[float], seed: int) -> dict:
    if not pcts:
        return {"n": 0, "mean_pct": None, "win_rate": None, "ci_low": None, "ci_high": None}
    lo, hi = bootstrap_ci(pcts, seed=seed)
    return {"n": len(pcts), "mean_pct": mean(pcts),
            "win_rate": sum(p > 0 for p in pcts) / len(pcts), "ci_low": lo, "ci_high": hi}


def judge(rule_pcts: list[float], control_pcts: list[float], *, seed: int = SEED) -> dict:
    """§3 판정. 규칙 n < 50이면 판정하지 않는다(INTERIM, 중간 기록)."""
    rule = _group(rule_pcts, seed)
    control = _group(control_pcts, seed)
    diff = (
        rule["mean_pct"] - control["mean_pct"]
        if rule["n"] and control["n"] else None
    )
    if rule["n"] < MIN_N:
        verdict = "INTERIM"
    elif rule["ci_low"] is not None and rule["ci_low"] > 0 and diff is not None and diff > 0:
        verdict = "ADOPT"
    else:
        verdict = "REJECT"
    return {"rule": rule, "control": control, "diff_pct": diff, "verdict": verdict}


def day_records(log_dirs: list[Path], day: str) -> tuple[dict[str, float], bool]:
    """그날 종목별 첫 `F3_RECHECK.prev_close`, 빠른 경로 날 여부."""
    prev: dict[str, float] = {}
    fast = False
    for d in log_dirs:
        path = d / f"{day}.jsonl"
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if "F3_" not in line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                name = ev.get("event")
                if name == "F3_FAST_RECHECK_USED":
                    fast = True
                elif name == "F3_RECHECK" and ev.get("prev_close"):
                    prev.setdefault(str(ev.get("ticker") or ""), float(ev["prev_close"]))
        if prev:
            break
    return prev, fast


def _fmt(group: dict) -> str:
    if not group["n"]:
        return "n=0"
    return (f"n={group['n']} 평균 {group['mean_pct']:+.2f}% 승률 {group['win_rate']:.0%} "
            f"CI [{group['ci_low']:+.2f}, {group['ci_high']:+.2f}]")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--universes", type=Path, default=ROOT / "data/replay/universes.json")
    ap.add_argument("--log-dir", type=Path, action="append", required=True)
    ap.add_argument("--start", default="20260912")
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    days = json.loads(args.universes.read_text(encoding="utf-8"))["days"]
    rows = []
    excluded = {"no_bar": 0, "no_prev_close": 0}
    for day in sorted(days):
        if not (args.start <= day <= args.end):
            continue
        prev, fast = day_records(args.log_dir, day)
        bars = {str(c["ticker"]): read_cached_bars(day, str(c["ticker"])) for c in days[day]}
        result = evaluate_day(days[day], bars, prev)
        for key, value in result["excluded"].items():
            excluded[key] += value
        rows.append({"date": day, "path": "FAST" if fast else "LEGACY", **result})

    def pcts(side: str, path: str | None = None) -> list[float]:
        return [r[side]["pct"] for r in rows
                if r[side] is not None and (path is None or r["path"] == path)]

    report = {
        "start": args.start, "end": args.end, "days": len(rows), "excluded": excluded,
        "judgement": judge(pcts("rule"), pcts("control")),
        "by_path": {p: judge(pcts("rule", p), pcts("control", p)) for p in ("FAST", "LEGACY")},
        "rows": rows,
    }
    if args.start <= REGISTRATION_END:
        report["note"] = "등록 표본이 섞였다 — 판정에 쓰지 않는다(§3)."

    for r in rows:
        rule = r["rule"]
        ctl = r["control"]
        rule_s = (f"규칙 r{rule['rank']} {rule['ticker']} 갭 {rule['gap_pct']:5.2f}% "
                  f"{rule['reason']:<9} {rule['pct']:+6.2f}%") if rule else "규칙 —"
        ctl_s = f"대조 {ctl['ticker']} {ctl['reason']:<9} {ctl['pct']:+6.2f}%" if ctl else "대조 —"
        print(f"{r['date']} {r['path']:<6} {rule_s:<48} | {ctl_s}")
    j = report["judgement"]
    print(f"\n표본 {args.start} ~ {args.end}, {len(rows)}일 · 제외 {excluded}")
    print(f"규칙   {_fmt(j['rule'])}")
    print(f"대조군 {_fmt(j['control'])}")
    diff = j["diff_pct"]
    print(f"규칙 − 대조군 {diff:+.2f}%p" if diff is not None else "규칙 − 대조군 —")
    interim = f" (n < {MIN_N}, 중간 기록)" if j["verdict"] == "INTERIM" else ""
    print(f"판정: {j['verdict']}{interim}")
    for path, sub in report["by_path"].items():
        print(f"  보조 {path:<6} 규칙 {_fmt(sub['rule'])} | 대조 {_fmt(sub['control'])}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(
            (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
