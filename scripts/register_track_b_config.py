"""확정된 트랙 B 규칙을 strategy_configs 에 EXPLORATORY 로 등록한다.

값이 하나라도 다르면 새 해시 → 새 config_id 다. 기존 결과를 덮지 않으므로
규칙을 바꿔도 과거 그림자 기록의 해석이 살아 있다.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.track_b_rules import (  # noqa: E402
    DEFAULT_PARAMS,
    HARD_STOP,
    RULES,
    STEP_SIZE,
    STEP_TRAIL,
)


def build_config(rule_key: str, params: dict) -> dict:
    if rule_key not in RULES:
        raise ValueError(f"unknown rule: {rule_key}")
    merged = {**DEFAULT_PARAMS, **params}
    return {
        "b_rule": rule_key,
        "b_signal_start": "09:35",
        "b_entry_deadline": "14:00",
        "b_sma_period": merged["sma_period"],
        "b_macd_fast": merged["macd_fast"],
        "b_macd_slow": merged["macd_slow"],
        "b_macd_signal": merged["macd_signal"],
        "b_vol_window": merged["vol_window"],
        "b_hist_maturity_bars": merged["hist_maturity_bars"],
        "b_min_bars_after_gap": merged["min_bars_after_gap"],
        "b_step_size": STEP_SIZE,
        "b_step_trail": STEP_TRAIL,
        "b_hard_stop_ratio": HARD_STOP,
    }


async def main_async(rule_key: str) -> int:
    import os

    from src import db

    # db.connect()는 존재한 적이 없다 — 열기는 db.init(경로)이고, 경로는
    # real_readiness.py와 같은 DB_DIR 규약을 따른다.
    db_path = os.path.join(os.getenv("DB_DIR", "data/db"), "trading.db")
    await db.init(db_path)
    try:
        config_id = await db.upsert_strategy_config(
            build_config(rule_key, {}), kind="EXPLORATORY"
        )
    finally:
        await db.close()
    print(f"config_id = {config_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async(sys.argv[1])))
