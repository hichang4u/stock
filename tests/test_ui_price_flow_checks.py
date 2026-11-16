"""가격흐름 차트의 브라우저 쪽 로직 검사(tests/js/price_flow_checks.js)를 pytest에서 돌린다.

그 스크립트는 docs/html/assets/app.js에서 실제 함수를 뽑아 node로 실행한다. 손으로만 돌리면
승격 전 검사(preflight의 pytest)에 걸리지 않으므로 여기서 한 번 부른다. 저장 분봉 시드를
메모리 분·틱과 합치는 기준, 새로고침 뒤 시드 유지, 종목·거래일 전환, 도움말 문구가 그 안에 있다
(2026-11-16: '거래 전체'를 골라도 오전 가격선이 없던 결함).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

CHECKS_JS = Path(__file__).resolve().parent / "js" / "price_flow_checks.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node 미설치")


def test_price_flow_js_checks_all_pass():
    result = subprocess.run(
        ["node", str(CHECKS_JS)], capture_output=True, text=True, encoding="utf-8", check=False
    )

    failed = [line for line in result.stdout.splitlines() if line.startswith("FAIL")]
    assert result.returncode == 0, "\n".join(failed) or result.stderr
    assert "ALL CHECKS PASSED" in result.stdout
