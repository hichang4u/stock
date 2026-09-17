"""H2 소급 탐색 — 라벨과 분봉을 조인해 §3 예측(P1·P2)을 등록 전 표본에서 미리 재본다.

결과는 체(sieve)다. 문서 §5에 기록만 하고 규칙·라벨·판정 기준을 바꾸지 않는다.

진입·청산은 H1과 같은 자다: 09:01봉 시가 진입, A 청산(하드스탑 -2.0% / 스텝 2.5% 트레일
2.0% / 15:15), 봉 내 순서는 "저가 먼저" 한 가지로 고정.

    python scripts/h2_sieve.py --labels data/catalyst/labels.jsonl \
        --out data/replay/h2_sieve_20260917.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.strategy_backtest import read_cached_bars  # noqa: E402
from scripts.track_b_backtest import bootstrap_ci  # noqa: E402
from scripts.track_b_rules import simulate_exit  # noqa: E402

ENTRY_BAR = "090100"
# H2 기본값. H3는 --treatment BID_DOMINANT --control ASK_DOMINANT 로 같은 집계를 쓴다.
DEFAULT_TREATMENT = "MATERIAL"
DEFAULT_CONTROL = "NONE"


def simulate_open_entry(bars: list[dict]) -> dict | None:
    """09:01봉 시가에 진입해 A 청산(저가 먼저)까지. 09:01봉이 없으면 None."""
    for idx, bar in enumerate(bars):
        if bar["time"] == ENTRY_BAR:
            entry_price = float(bar["open"])
            if entry_price <= 0:
                return None
            exit_ = simulate_exit(bars, idx, entry_price, order="low_first")
            return {
                "entry_price": entry_price,
                "reason": exit_["reason"],
                "exit_time": exit_["exit_time"],
                "pct": exit_["pct"],
            }
    return None


def _group(rows: list[dict], seed: int) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0, "hard_stop_rate": None, "mean_pct": None, "ci_low": None, "ci_high": None}
    pcts = [float(r["pct"]) for r in rows]
    lo, hi = bootstrap_ci(pcts, seed=seed)
    return {
        "n": n,
        "hard_stop_rate": sum(r["reason"] == "HARD_STOP" for r in rows) / n,
        "mean_pct": mean(pcts),
        "ci_low": lo,
        "ci_high": hi,
    }


def summarize(
    rows: list[dict],
    *,
    treatment: str = DEFAULT_TREATMENT,
    control: str = DEFAULT_CONTROL,
    seed: int = 20260917,
) -> dict:
    """라벨별 요약과 §3 예측. 한쪽이 비면 예측은 None(판정 불가).

    P1: treatment 손절률 < control 손절률. P2: treatment 손절률 < 50%.
    """
    labels = [treatment, control] + sorted({r["label"] for r in rows} - {treatment, control})
    by_label = {label: _group([r for r in rows if r["label"] == label], seed) for label in labels}
    treated = by_label[treatment]
    controlled = by_label[control]
    p1 = p2 = None
    if treated["n"] and controlled["n"]:
        p1 = treated["hard_stop_rate"] < controlled["hard_stop_rate"]
    if treated["n"]:
        p2 = treated["hard_stop_rate"] < 0.5

    by_flow: dict[str, dict] = {}
    for label in labels:
        for flow in (True, False):
            subset = [r for r in rows if r["label"] == label and r.get("flow_pos") is flow]
            by_flow[f"{label}/{'FLOW_POS' if flow else 'FLOW_NEG'}"] = _group(subset, seed)

    return {
        "treatment": treatment,
        "control": control,
        "by_label": by_label,
        "p1_treatment_below_control": p1,
        "p2_treatment_below_half": p2,
        # H2 문서·테스트가 쓰는 이름. 같은 값이다.
        "p1_material_below_none": p1,
        "p2_material_below_half": p2,
        "by_label_and_flow": by_flow,
    }


def join_labels_with_bars(labels: list[dict]) -> tuple[list[dict], dict]:
    """쌍마다 봉을 찾아 진입을 시뮬레이션한다. 빠진 쌍은 사유별로 센다."""
    rows: list[dict] = []
    missing = {"NO_BARS": 0, "NO_0901_BAR": 0}
    for label in labels:
        bars = read_cached_bars(label["date"], label["ticker"])
        if not bars:
            missing["NO_BARS"] += 1
            continue
        result = simulate_open_entry(bars)
        if result is None:
            missing["NO_0901_BAR"] += 1
            continue
        rows.append({**label, **result})
    return rows, missing


def _fmt(group: dict) -> str:
    if not group["n"]:
        return "n=0"
    return (f"n={group['n']:3}  hard_stop={group['hard_stop_rate']*100:5.1f}%  "
            f"mean={group['mean_pct']:+.2f}%  CI=[{group['ci_low']:+.2f}, {group['ci_high']:+.2f}]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="H2 소급 탐색 (체)")
    parser.add_argument("--labels", type=Path, default=ROOT / "data/catalyst/labels.jsonl")
    parser.add_argument(
        "--out", type=Path,
        default=ROOT / f"data/replay/h2_sieve_{datetime.now().strftime('%Y%m%d')}.json",
    )
    parser.add_argument("--treatment", default=DEFAULT_TREATMENT, help="규칙이 통과시키는 라벨")
    parser.add_argument("--control", default=DEFAULT_CONTROL, help="P1의 비교 대상 라벨")
    args = parser.parse_args(argv)

    labels = [json.loads(line) for line in args.labels.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows, missing = join_labels_with_bars(labels)
    report = summarize(rows, treatment=args.treatment, control=args.control)

    print(f"라벨 {len(labels)}쌍 → 진입 시뮬레이션 {len(rows)}쌍, 빠짐 {missing}")
    for label, group in report["by_label"].items():
        print(f"  {label:13} {_fmt(group)}")
    print(f"P1 {args.treatment} 손절률 < {args.control} 손절률: {report['p1_treatment_below_control']}")
    print(f"P2 {args.treatment} 손절률 < 50%: {report['p2_treatment_below_half']}")
    print("참고 — 수급으로 한 번 더 가름 (규칙과 무관):")
    for key, group in report["by_label_and_flow"].items():
        if group["n"]:
            print(f"  {key:18} {_fmt(group)}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"labels_source": str(args.labels), "missing": missing,
                    "rows": rows, "report": report}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
