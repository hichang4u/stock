"""오늘 화면 표시 모델 검사(tests/js/today_view_checks.js)를 pytest에서 돌린다.

청산 후 관측가와 거래 결과(비용 전)를 나누는 카드 모델, 지난 거래일 표시, 연결 상태를
미관측일 때 정상으로 그리지 않는 규칙, F1 지난 거래 표시, 화면 순서·대비 토큰이 그 안에 있다
(2026-11-21 화면 점검: 11/20 123690 청산 +5.22%가 관측 −3.46%로 보이던 문제).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

CHECKS_JS = Path(__file__).resolve().parent / "js" / "today_view_checks.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node 미설치")


def test_today_view_js_checks_all_pass():
    result = subprocess.run(
        ["node", str(CHECKS_JS)], capture_output=True, text=True, encoding="utf-8", check=False
    )

    failed = [line for line in result.stdout.splitlines() if line.startswith("FAIL")]
    assert result.returncode == 0, "\n".join(failed) or result.stderr
    assert "ALL CHECKS PASSED" in result.stdout
