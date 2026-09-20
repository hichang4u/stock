"""VWAP 청산 체(sieve) — 실제 A 거래를 틱으로 재생해 VWAP 청산을 트레일과 비교한다.

트랙 A의 실제 진입가·진입 시각을 그대로 두고 청산 규칙만 바꿔 본다. 모든 변형에
하드스탑 -2.0%는 그대로 있고, VWAP은 당일 첫 WS 틱부터 누적한 값(시가 동시호가
거래량 포함)이다. "재현" 변형은 f4와 같은 하드스탑 + 스텝 트레일만 돌려 시뮬레이터가
실제 손익을 만들어 내는지 먼저 확인한다 — 이 열이 실제와 어긋나면 나머지 열도 믿지
않는다.

결과는 체다. 2026-09-20 첫 실행(17거래)의 판정은 `docs/VWAP_EXIT_SIEVE_20260920.md`에
있고, H1 문서 §6의 닫힌 축에 올라 있다. 새 표본 없이 다시 열지 않는다.

    .\\.venv\\Scripts\\python.exe scripts\\vwap_exit_sieve.py --root D:\\Private\\stock-prod

틱은 `data/strategy_ticks/<date>/<ticker>.<HH>.jsonl.gz`의 WS 행만 쓴다. 잘린 청크는
읽힌 데까지만 쓴다. 캡처가 없는 거래는 표에서 뺀다.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.trail_near_miss import load_ws_ticks  # noqa: E402

# f4_tracking.py의 상수와 같은 값. 그쪽은 지문 대상이라 import 하지 않는다.
HARD_STOP_RATIO = 0.020
STEP_SIZE = 0.025
STEP_TRAIL = 0.020
TIMEOUT_HHMM = "15:15"

VARIANTS: dict[str, dict] = {
    "재현(트레일만)": dict(grace_min=0, use_trail=True, use_vwap=False),
    "V1 VWAP 5분": dict(grace_min=5, use_trail=False, use_vwap=True),
    "V3 VWAP 10분": dict(grace_min=10, use_trail=False, use_vwap=True),
    "V5 VWAP 15분": dict(grace_min=15, use_trail=False, use_vwap=True),
    "V4 트레일+VWAP 5분": dict(grace_min=5, use_trail=True, use_vwap=True),
}


def simulate_exit(
    ticks: list[dict],
    *,
    entry_price: float,
    entry_at: datetime,
    grace_min: int,
    use_trail: bool,
    use_vwap: bool,
    vwap_buffer: float = 0.0,
    timeout_hhmm: str = TIMEOUT_HHMM,
) -> tuple[float, str, str]:
    """틱 단위 청산 시뮬. (손익%, 사유, HH:MM)를 돌려준다.

    - 하드스탑: 트레일 미활성 구간에서 price <= entry*(1-2%)  (f4와 동일)
    - 스텝 트레일(use_trail): 2.5% 스텝, 스텝 도달 후 entry*(1+step-2%) 이탈  (f4와 동일)
    - VWAP(use_vwap): 분이 바뀌는 순간 직전 분 마지막 체결가가 그 시점 VWAP*(1-buffer)
      아래면 청산. 진입 후 grace_min 분이 지나야 본다.
    - 15:15 이후 첫 틱에서 타임아웃.
    ticks는 source_ts 오름차순이며 진입 전 틱도 포함한다(VWAP 누적에 쓴다).
    """
    cum_qty = 0.0
    cum_pv = 0.0
    highest_step = 0.0
    trailing = False
    cur_min: str | None = None
    last_px: float | None = None
    px = entry_price
    m = entry_at.strftime("%H:%M")
    for tick in ticks:
        qty = float(tick["qty"])
        px = float(tick["price"])
        t = datetime.fromisoformat(tick["source_ts"])
        m = t.strftime("%H:%M")
        # 직전 분 마감가는 그 분이 끝난 시점의 VWAP(이 틱을 더하기 전)과 비교한다.
        vwap_prev = cum_pv / cum_qty if cum_qty > 0 else None
        cum_qty += qty
        cum_pv += px * qty
        if t < entry_at:
            cur_min, last_px = m, px
            continue
        if (
            use_vwap
            and cur_min is not None
            and m != cur_min
            and last_px is not None
            and vwap_prev is not None
            and (t - entry_at) >= timedelta(minutes=grace_min)
            and last_px < vwap_prev * (1 - vwap_buffer)
        ):
            return px / entry_price * 100 - 100, "VWAP", m
        cur_min, last_px = m, px
        if m >= timeout_hhmm:
            return px / entry_price * 100 - 100, "TIMEOUT", m
        if use_trail:
            pnl = px / entry_price - 1
            current_step = max(math.floor(pnl / STEP_SIZE) * STEP_SIZE, 0.0)
            if current_step > highest_step:
                highest_step = current_step
            if highest_step >= STEP_SIZE:
                trailing = True
            if trailing and px <= entry_price * (1 + highest_step - STEP_TRAIL):
                return px / entry_price * 100 - 100, "TRAIL", m
        if not trailing and px <= entry_price * (1 - HARD_STOP_RATIO):
            return px / entry_price * 100 - 100, "HARD", m
    return px / entry_price * 100 - 100, "EOD", m


def closed_trades(db_path: Path, since: str) -> list[dict]:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """
        select id, date, ticker, name, entry_price, entry_at, pnl_pct, close_reason
          from trades
         where status = 'CLOSED' and track = 'A' and date >= ?
         order by date
        """,
        (since,),
    ).fetchall()
    return [dict(r) for r in rows]


def run(root: Path, since: str) -> list[dict]:
    results: list[dict] = []
    for trade in closed_trades(root / "data" / "db" / "trading.db", since):
        ticks = load_ws_ticks(root, trade["date"], trade["ticker"])
        if len(ticks) < 50:
            continue
        row = {k: trade[k] for k in ("date", "ticker", "name", "pnl_pct", "close_reason")}
        row["variants"] = {}
        for name, params in VARIANTS.items():
            pnl, reason, hhmm = simulate_exit(
                ticks,
                entry_price=trade["entry_price"],
                entry_at=datetime.fromisoformat(trade["entry_at"]),
                **params,
            )
            row["variants"][name] = {"pnl_pct": pnl, "reason": reason, "at": hhmm}
        results.append(row)
    return results


def print_table(results: list[dict]) -> None:
    names = list(VARIANTS)
    print(f"{'거래일':8} {'이름':6} {'실제':>7} | " + " | ".join(f"{n:>19}" for n in names))
    total: dict[str, float] = defaultdict(float)
    wins: dict[str, int] = defaultdict(int)
    for r in results:
        total["실제"] += r["pnl_pct"]
        wins["실제"] += r["pnl_pct"] > 0
        cells = []
        for n in names:
            v = r["variants"][n]
            total[n] += v["pnl_pct"]
            wins[n] += v["pnl_pct"] > 0
            cells.append(f"{v['pnl_pct']:+6.2f}% {v['reason'][:5]:5} {v['at']}")
        print(
            f"{r['date']:8} {r['name'][:3]:6} {r['pnl_pct']:+6.2f}% | "
            + " | ".join(f"{c:>19}" for c in cells)
        )
    print(f"\nn={len(results)}")
    print(f"  {'실제':18} {total['실제']:+7.2f}% (승 {wins['실제']})")
    for n in names:
        print(f"  {n:18} {total[n]:+7.2f}% (승 {wins[n]})")
    if results:
        # 열마다 자기 최대 수익 거래를 뺀다 — 한 거래가 합계를 떠받치는지 본다.
        print()
        print("각 열의 최대 수익 거래 1건 제외 시:")
        for n in ["실제", *names]:
            def pick(r: dict, n: str = n) -> float:
                return r["pnl_pct"] if n == "실제" else r["variants"][n]["pnl_pct"]
            best = max(results, key=pick)
            print(f"  {n:18} {total[n] - pick(best):+7.2f}%  (제외: {best['date']} {best['name']})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VWAP 청산 체 — 실제 A 거래 틱 재생")
    parser.add_argument("--root", type=Path, default=ROOT, help="data/ 가 있는 트리")
    parser.add_argument("--since", default="20260813", help="이 거래일부터 (틱 캡처 시작일)")
    args = parser.parse_args(argv)
    print_table(run(args.root, args.since))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
