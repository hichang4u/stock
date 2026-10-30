"""H1 복원값 검증 관문 — 읽기 전용.

스펙 `docs/superpowers/specs/2026-09-11-h1-gap-selection-hypothesis.md` §4.1 결정(2026-10-30).
등록 표본(2026-07-01 ~ 09-10)에서 로그의 `F3_RECHECK.gap_pct`(09:01 재검증 갭)와
정의 1의 값(09:01 1분봉 시가 ÷ 전일종가 − 1)이 "7% 이상 / 미만"으로 얼마나 일치하는지 센다.
90% 이상이면 정의 1을 판정에 쓴다. 손익은 계산하지 않는다.

비교 표본은 종목별로 로그 시각이 09:01:00 ~ 09:01:59인 첫 `F3_RECHECK`다(§4.1 결정 가, 2026-10-30).
`--any-time`은 시각을 가리지 않는 첫 기록으로 비교한다(첫 판정 85.7% 재현용).

    python scripts/h1_gap_concordance.py \\
        --log-dir data/logs.migrated --log-dir D:/Private/stock-prod/data/logs
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
THRESHOLD_PCT = 7.0
GATE_PCT = 90.0
START, END = "20260701", "20260910"


def first_rechecks(
    log_dirs: list[Path], day: str, minute: str | None = "09:01"
) -> dict[str, dict]:
    """그날 종목별 첫 `F3_RECHECK`(gap_pct·prev_close가 있는 것). minute이면 그 분에 찍힌 것만."""
    found: dict[str, dict] = {}
    for d in log_dirs:
        path = d / f"{day}.jsonl"
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if "F3_RECHECK" not in line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("event") != "F3_RECHECK":
                    continue
                tk = str(ev.get("ticker") or "")
                has_gap = ev.get("gap_pct") is not None and ev.get("prev_close")
                in_minute = minute is None or str(ev.get("ts", ""))[11:16] == minute
                if tk and tk not in found and has_gap and in_minute:
                    found[tk] = ev
        if found:
            break
    return found


def bar_0901_open(bars_dir: Path, day: str, ticker: str) -> float | None:
    path = bars_dir / f"{day}_{ticker}.json"
    if not path.exists():
        return None
    for bar in json.loads(path.read_text(encoding="utf-8")):
        if str(bar.get("time")) == "090100" and bar.get("open"):
            return float(bar["open"])
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--universes", type=Path, default=ROOT / "data/replay/universes.json")
    ap.add_argument("--bars-dir", type=Path, default=ROOT / "data/backtest_bars")
    ap.add_argument("--log-dir", type=Path, action="append", required=True)
    ap.add_argument("--any-time", action="store_true", help="기록 시각을 가리지 않는다")
    args = ap.parse_args(argv)

    days = json.loads(args.universes.read_text(encoding="utf-8"))["days"]
    rows = []
    missing = {"no_recheck": 0, "no_bar": 0}
    for day in sorted(days):
        if not (START <= day <= END):
            continue
        rechecks = first_rechecks(args.log_dir, day, None if args.any_time else "09:01")
        for cand in days[day]:
            tk = str(cand["ticker"])
            ev = rechecks.get(tk)
            if ev is None:
                missing["no_recheck"] += 1
                continue
            op = bar_0901_open(args.bars_dir, day, tk)
            if op is None:
                missing["no_bar"] += 1
                continue
            logged = float(ev["gap_pct"])
            rebuilt = (op / float(ev["prev_close"]) - 1) * 100
            rows.append((day, cand["rank"], tk, ev["ts"][11:19], logged, rebuilt))

    n = len(rows)
    agree = sum(1 for r in rows if (r[4] >= THRESHOLD_PCT) == (r[5] >= THRESHOLD_PCT))
    for day, rank, tk, ts, logged, rebuilt in rows:
        same = (logged >= THRESHOLD_PCT) == (rebuilt >= THRESHOLD_PCT)
        mark = "" if same else "  <<< 분류 불일치"
        print(f"{day} r{rank} {tk} {ts}  로그 {logged:6.2f}%  복원 {rebuilt:6.2f}%{mark}")
    pct = agree / n * 100 if n else 0.0
    diffs = sorted(abs(r[5] - r[4]) for r in rows)
    median = diffs[len(diffs) // 2] if diffs else 0.0
    print(f"\n쌍 {n} · 7% 분류 일치 {agree} ({pct:.1f}%) · "
          f"절대차 중앙값 {median:.2f}%p · 제외 {missing}")
    print(f"관문({GATE_PCT:.0f}% 이상): {'통과' if pct >= GATE_PCT else '미달'}")
    return 0 if pct >= GATE_PCT else 1


if __name__ == "__main__":
    sys.exit(main())
