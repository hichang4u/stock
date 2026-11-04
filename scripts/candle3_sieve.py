"""3양봉 진입·3음봉 청산 체 — 읽기 전용. KIS 호출 없음(분봉 캐시만 읽는다).

닫힌 축(H1 스펙 §6 확정 봉 진입 신호, 스텝/트레일 변형)에 닿는 아이디어라
**등록 표본(07-01 ~ 09-10)에서만** 돌린다. 결과는 체다. 좋게 나와도 채택이 아니라 새 가설 등록의
근거이고, 판정은 등록 뒤 쌓이는 데이터로 한다.

돌리기 전에 고정한 규칙 (2026-11-04):
- 1분봉, 일별 랭크 1, 하루 1거래. 양봉 = 종가 > 시가, 음봉 = 종가 < 시가.
  보합은 연속을 끊는다.
- 진입 신호: 그날 09:00봉부터 처음 나온 3연속 양봉. 다음 봉 시가에 진입한다(15:15 전까지).
- 청산 신호: 진입 봉부터 센 3연속 음봉. 다음 봉 시가에 청산한다.
  하드스탑 −2%는 유지(봉 안은 저가 먼저), 15:15 타임아웃도 유지. 트레일링은 없다.
- 네 열을 따로 잰다. 기준(09:01 시가 + A 청산), 진입만, 청산만, 둘 다.
- 비용: `src/perf_costs.py`. 3음봉 청산은 타임아웃과 같은 슬리피지(0.20%p)로 둔다(가정).
  진입 슬리피지는 0이다.

    python scripts/candle3_sieve.py --out data/replay/candle3_sieve_20261104.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.h2_sieve import ENTRY_BAR  # noqa: E402
from scripts.strategy_backtest import read_cached_bars  # noqa: E402
from scripts.track_b_backtest import bootstrap_ci  # noqa: E402
from scripts.track_b_rules import HARD_STOP, TIMEOUT_TIME, simulate_exit  # noqa: E402
from src import perf_costs  # noqa: E402

START, END = "20260701", "20260910"
STREAK = 3
VARIANTS = ("base", "entry", "exit", "both")


def assert_registration_sample(start: str, end: str) -> None:
    if start < START or end > END:
        raise SystemExit(f"등록 표본({START} ~ {END}) 밖은 돌리지 않는다: {start} ~ {end}")


def three_bull_entry(bars: list[dict]) -> int | None:
    """처음 나온 3연속 양봉의 다음 봉 인덱스. 다음 봉이 없거나 15:15 이후면 None."""
    streak = 0
    for idx, bar in enumerate(bars):
        streak = streak + 1 if bar["close"] > bar["open"] else 0
        if streak == STREAK:
            following = idx + 1
            if following < len(bars) and bars[following]["time"] < TIMEOUT_TIME:
                return following
            return None
    return None


def candle_exit(bars: list[dict], entry_idx: int, entry_price: float) -> dict:
    """하드스탑(저가 먼저) → 3연속 음봉이면 다음 봉 시가 → 15:15."""
    stop = entry_price * (1 - HARD_STOP)
    streak = 0
    for idx in range(entry_idx, len(bars)):
        bar = bars[idx]
        if bar["time"] >= TIMEOUT_TIME:
            return _result(bar, bar["close"], "TIMEOUT", entry_price)
        if streak >= STREAK:
            return _result(bar, bar["open"], "BEARISH3", entry_price)
        if bar["low"] <= stop:
            return {"exit_time": bar["time"], "reason": "HARD_STOP", "pct": -HARD_STOP * 100}
        streak = streak + 1 if bar["close"] < bar["open"] else 0
    return _result(bars[-1], bars[-1]["close"], "DATA_END", entry_price)


def _result(bar: dict, price: float, reason: str, entry_price: float) -> dict:
    return {"exit_time": bar["time"], "reason": reason,
            "pct": (float(price) / entry_price - 1) * 100}


def _index_of(bars: list[dict], time: str) -> int | None:
    return next((i for i, b in enumerate(bars) if b["time"] == time), None)


def trades_for_day(bars: list[dict]) -> dict[str, dict | None]:
    """하루치 네 열. 그 열의 진입이 없으면 None."""
    base_idx = _index_of(bars, ENTRY_BAR)
    signal_idx = three_bull_entry(bars)
    out: dict[str, dict | None] = dict.fromkeys(VARIANTS)
    for name, idx, exit_rule in (
        ("base", base_idx, "a"), ("entry", signal_idx, "a"),
        ("exit", base_idx, "candle"), ("both", signal_idx, "candle"),
    ):
        if idx is None or float(bars[idx]["open"]) <= 0:
            continue
        price = float(bars[idx]["open"])
        if exit_rule == "a":
            result = simulate_exit(bars, idx, price, order="low_first")
        else:
            result = candle_exit(bars, idx, price)
        out[name] = {"entry_time": bars[idx]["time"], "entry_price": price,
                     "exit_time": result["exit_time"], "reason": result["reason"],
                     "pct": result["pct"],
                     "net_pct": perf_costs.apply_costs(result["pct"], result["reason"])[
                         "net_conservative_pct"]}
    return out


def summarize(rows: list[dict], key: str = "net_pct") -> dict:
    if not rows:
        return {"n": 0}
    values = [float(r[key]) for r in rows]
    lo, hi = bootstrap_ci(values)
    ordered = sorted(values)
    reasons: dict[str, int] = {}
    for r in rows:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    return {
        "n": len(values), "mean": mean(values), "median": median(values),
        "win_rate": sum(v > 0 for v in values) / len(values),
        "ci_low": lo, "ci_high": hi,
        "mean_without_top1": mean(ordered[:-1]) if len(values) > 1 else None,
        "total": sum(values), "reasons": reasons,
    }


def paired_diff(days: list[dict], name: str) -> dict:
    """같은 날 그 열 − 기준(비용 차감). 양쪽 다 거래한 날만."""
    diffs = [d[name]["net_pct"] - d["base"]["net_pct"] for d in days if d[name] and d["base"]]
    if not diffs:
        return {"n": 0}
    lo, hi = bootstrap_ci(diffs)
    return {"n": len(diffs), "mean": mean(diffs), "ci_low": lo, "ci_high": hi}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--universes", type=Path, default=ROOT / "data/replay/universes.json")
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default=END)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    assert_registration_sample(args.start, args.end)

    universes = json.loads(args.universes.read_text(encoding="utf-8"))["days"]
    days: list[dict] = []
    missing = 0
    for day in sorted(universes):
        if not (args.start <= day <= args.end):
            continue
        rank1 = next((c for c in universes[day] if int(c["rank"]) == 1), None)
        ticker = str(rank1["ticker"]) if rank1 else ""
        bars = read_cached_bars(day, ticker) if ticker else None
        if not bars:
            missing += 1
            continue
        bars = sorted(bars, key=lambda b: b["time"])
        days.append({"date": day, "ticker": ticker, **trades_for_day(bars)})

    report: dict = {"start": args.start, "end": args.end, "days": len(days), "no_bars": missing}
    print(f"표본 {args.start} ~ {args.end}, 분봉 있는 날 {len(days)}일, 없는 날 {missing}일\n")
    print(f"{'열':<6}{'n':>4}{'총 평균':>9}{'비용후':>9}{'중앙값':>8}{'승률':>6}"
          f"{'95% CI':>18}{'1위 제외':>9}  청산 사유")
    for name in VARIANTS:
        rows = [d[name] for d in days if d[name]]
        gross, net = summarize(rows, "pct"), summarize(rows, "net_pct")
        report[name] = {"gross": gross, "net": net, "paired_vs_base": paired_diff(days, name)}
        if not rows:
            print(f"{name:<6}   0")
            continue
        ci = f"[{net['ci_low']:+.2f}, {net['ci_high']:+.2f}]"
        print(f"{name:<6}{net['n']:>4}{gross['mean']:>+9.2f}{net['mean']:>+9.2f}"
              f"{net['median']:>+8.2f}{net['win_rate']:>6.0%}{ci:>18}"
              f"{net['mean_without_top1']:>+9.2f}  {net['reasons']}")
    print("\n같은 날 기준 대비(비용 후)")
    for name in VARIANTS[1:]:
        p = report[name]["paired_vs_base"]
        if p["n"]:
            print(f"  {name:<6} n={p['n']} 평균 {p['mean']:+.2f}%p "
                  f"CI [{p['ci_low']:+.2f}, {p['ci_high']:+.2f}]")
    times = sorted(d["entry"]["entry_time"] for d in days if d["entry"])
    if times:
        early = sum(t < "091000" for t in times)
        print(f"\n3양봉 진입 시각: 중앙값 {times[len(times) // 2]}, 09:10 전 {early}/{len(times)}, "
              f"신호 없는 날 {sum(1 for d in days if not d['entry'])}")
    if args.out:
        report["rows"] = days
        args.out.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        args.out.write_bytes(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
