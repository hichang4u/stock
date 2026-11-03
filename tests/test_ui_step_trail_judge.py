"""docs/html/assets/app.js의 judgeStepTrail() 판정 테스트.

STEP_TRAIL 카드의 조언("주문 실행 경로를 먼저 점검")은 우리 코드 몫인 감지(활성 스탑 → 발동가)를
가리킨다. 장 초반 급락이 스탑을 관통해 생기는 체결 편차(발동가 → 체결가)는 시장 몫이라 참고로만
보인다(docs/STEP_TRAIL_SLIPPAGE_FOLLOWUP_20261029.md 7절 다, 2026-11-03). 문턱(평균 0.3%p · 최대
0.5%p)은 그대로다.

실제 JS 함수를 node로 실행해 검증한다.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "docs" / "html" / "assets" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node 미설치")


def _judge(diag: dict) -> list:
    src = APP_JS.read_text(encoding="utf-8")
    m = re.search(r"function judgeStepTrail\([^)]*\)\s*\{.*?\n\}", src, re.S)
    assert m, "app.js에서 judgeStepTrail 함수를 찾지 못함"
    d = {"trailing": {"n": 17, "avg_giveback_pp": 3.1}, "trailing_diagnostics": diag}
    script = (
        m.group(0)
        + f"\nprocess.stdout.write(JSON.stringify(judgeStepTrail({json.dumps(d)})));"
    )
    result = subprocess.run(
        ["node"], input=script, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return json.loads(result.stdout)


# 2026-11-03 운영 17건과 같은 모양: 합계 최대 1.2%p(0828 급락 관통), 감지는 최대 0.22%p.
OPENING_CRASH = {
    "stop_eval_n": 17, "avg_stop_slip_pp": 0.14, "max_stop_slip_pp": 1.2,
    "detect_eval_n": 17, "avg_detect_slip_pp": 0.09, "max_detect_slip_pp": 0.22,
    "avg_fill_slip_pp": 0.04, "max_fill_slip_pp": 0.99,
    "giveback_n": 17, "structural_giveback_min_pp": 2.0, "structural_giveback_max_pp": 4.5,
}


def test_fill_slip_alone_does_not_raise_the_card():
    state, evidence, _ = _judge(OPENING_CRASH)
    assert state == "ok"
    assert "감지" in evidence and "체결" in evidence


def test_detection_slip_over_threshold_still_raises_the_card():
    state, _, advice = _judge({**OPENING_CRASH, "max_detect_slip_pp": 0.6})
    assert state == "adjust"
    assert "실행 경로" in advice


def test_needs_five_trades_with_a_known_trigger():
    state, _, _ = _judge({**OPENING_CRASH, "detect_eval_n": 4})
    assert state == "nodata"
