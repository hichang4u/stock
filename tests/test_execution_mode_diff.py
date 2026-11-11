"""현재 코드의 PAPER·REAL 모드 차이 열거 (scripts/execution_mode_diff.py).

현재 전략 코드를 읽기만 한다. 여기서 드러난 차이는 `execution_parity_passed`가 지금은
성립할 수 없는 이유의 증거이며, 차이를 없애는 것은 전략 변경(별도 릴리스)이다.
"""

from __future__ import annotations

import os

from scripts import execution_mode_diff
from src.real_validation.execution_profile import MODES


def test_current_code_differs_by_mode_in_the_known_places():
    differences = execution_mode_diff.mode_differences()

    assert differences["final_quote_max_age_default_ms"] == {"PAPER": 1_500, "REAL": 500}
    assert differences["rest_rate_interval_default_sec"] == {"PAPER": 1.1, "REAL": 0.2}
    # 빠른 경로는 PAPER 전용이다 — REAL에서는 켜도 동작하지 않는다.
    assert differences["fast_path_available_when_enabled"] == {"PAPER": True, "REAL": False}
    assert set(differences["buy_tr_id"]) == set(MODES)


def test_real_compatible_quote_freshness_cannot_be_applied_to_paper_today():
    """PAPER는 500ms를 설정해도 1,500ms로 올려 쓴다. 같은 설정으로 표본을 모을 수 없다."""
    effective = execution_mode_diff.mode_differences()[
        "final_quote_max_age_effective_ms_when_configured_500"
    ]

    assert effective == {"PAPER": 1_500, "REAL": 500}


def test_same_quote_age_gives_a_different_entry_decision_per_mode():
    report = execution_mode_diff.quote_freshness_replay(500)

    assert report["matched"] is False
    disagreeing = {
        m["input_id"] for m in report["mismatches"] if m["field"] == "entry_allowed"
    }
    # 500ms 이하와 1,500ms 초과는 두 모드가 같고, 그 사이에서만 갈린다.
    assert disagreeing == {"age-501", "age-1000", "age-1500"}
    assert all(
        m["paper"] is True and m["real"] is False
        for m in report["mismatches"]
        if m["field"] == "entry_allowed"
    )


def test_enumeration_leaves_the_process_environment_untouched():
    before = dict(os.environ)

    execution_mode_diff.mode_differences()
    execution_mode_diff.quote_freshness_replay()

    assert dict(os.environ) == before
