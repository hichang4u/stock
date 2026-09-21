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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
