"""현재 코드의 PAPER·REAL 모드 차이를 읽기 전용으로 열거한다 (설계 §3).

주문·시세 호출·DB 접근이 없다. 전략 모듈의 순수 함수와 상수만 읽고, 같은 입력을 두 모드로
재생해 판단이 어디서 갈리는지 보여준다. 결과는 '지금 코드가 이렇다'는 증거일 뿐이며
실제 서버 실측(지연·체결·호출 한도)을 대신하지 않는다.

    python scripts/execution_mode_diff.py

`KIS_MODE`를 잠깐 바꿔 읽는 값이 있으므로 운영 프로세스 안에서 임포트해 쓰지 않는다.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.api import kis_rest  # noqa: E402
from src.modules import f3_entry  # noqa: E402
from src.real_validation import execution_profile  # noqa: E402
from src.real_validation.execution_profile import MODES, OrderIntent  # noqa: E402

# 재생에 쓰는 최종 호가 나이(ms) 격자. 두 모드의 기본 한도(500·1,500) 양쪽을 덮는다.
QUOTE_AGES_MS = (0, 250, 500, 501, 1_000, 1_500, 1_501, 3_000)


def _default_rate_interval(mode: str) -> float:
    with mock.patch.dict(os.environ, {"KIS_MODE": mode}):
        return kis_rest.default_rate_interval()


def _fast_path_available(mode: str) -> bool:
    env = {"KIS_MODE": mode, "PAPER_FAST_PROBE": "1", "DRY_RUN": "0"}
    with mock.patch.dict(os.environ, env):
        return f3_entry.paper_fast_probe.enabled()


def mode_differences() -> dict[str, dict[str, Any]]:
    """모드에 따라 값이 달라지는 지점과 그 값. 같으면 목록에서 빠진다."""
    observed: dict[str, dict[str, Any]] = {
        "final_quote_max_age_default_ms": {
            mode: f3_entry._default_final_quote_max_age_ms(mode) for mode in MODES
        },
        # REAL 호환값 500ms를 PAPER에 설정했을 때 실제로 적용되는 값.
        "final_quote_max_age_effective_ms_when_configured_500": {
            mode: f3_entry._effective_final_quote_max_age_ms(mode, 500) for mode in MODES
        },
        "rest_rate_interval_default_sec": {mode: _default_rate_interval(mode) for mode in MODES},
        "fast_path_available_when_enabled": {mode: _fast_path_available(mode) for mode in MODES},
        "buy_tr_id": dict(f3_entry._BUY_TR),
    }
    return {
        name: values for name, values in observed.items() if len(set(values.values())) > 1
    }


def _quote_freshness_decision(item: Mapping[str, Any], adapter: Any) -> OrderIntent:
    """f3_entry `_quote_is_fresh`와 같은 비교를 모드별 실효 한도로 한다."""
    limit = f3_entry._effective_final_quote_max_age_ms(
        str(adapter["mode"]), int(item["configured_ms"])
    )
    fresh = limit <= 0 or int(item["quote_age_ms"]) <= limit
    return OrderIntent(
        input_id=str(item["input_id"]),
        candidate=None,
        entry_allowed=fresh,
        block_reason=None if fresh else "FINAL_QUOTE_STALE",
        order_price_cap=None,
        qty=0,
    )


def quote_freshness_replay(configured_ms: int = 500) -> dict[str, Any]:
    """같은 호가 나이를 두 모드로 재생한다. 불일치가 있으면 판단이 모드에 의존한다."""
    inputs = [
        {"input_id": f"age-{age}", "configured_ms": configured_ms, "quote_age_ms": age}
        for age in QUOTE_AGES_MS
    ]
    adapters = {mode: {"mode": mode} for mode in MODES}
    return execution_profile.replay(inputs, _quote_freshness_decision, adapters)


def main() -> None:
    report = {
        "mode_differences": mode_differences(),
        "quote_freshness_replay_configured_500ms": quote_freshness_replay(500),
        "note": "코드 열거일 뿐이며 실제 서버 실측·스모크가 아니다",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
