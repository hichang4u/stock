import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_blank_new_float_env_values_do_not_crash_module_import():
    env = os.environ.copy()
    env["BALANCE_SNAPSHOT_TTL_SEC"] = ""
    env["F3_FAST_RECHECK_MAX_AGE_SEC"] = ""
    env["F4_WS_STALE_SEC"] = ""
    env["F4_REST_POLL_INTERVAL_SEC"] = ""
    env["F4_FILL_POLL_INTERVAL_SEC"] = ""
    env["F4_STATE_PERSIST_INTERVAL_SEC"] = ""
    env["F4_HEARTBEAT_INTERVAL_SEC"] = ""
    env["PAPER_FAST_PROBE_OPEN_TIMEOUT_SEC"] = ""

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import main;"
                "from src.modules import f3_entry, f4_tracking;"
                "assert f3_entry.BALANCE_SNAPSHOT_TTL_SEC == 90.0;"
                "assert f3_entry.F3_FAST_RECHECK_MAX_AGE_SEC == 15.0;"
                "assert f4_tracking.F4_WS_STALE_SEC == 2.0;"
                "assert f4_tracking.F4_REST_POLL_INTERVAL_SEC == 1.0;"
                "assert f4_tracking.F4_FILL_POLL_INTERVAL_SEC == 0.5;"
                "assert f4_tracking.F4_STATE_PERSIST_INTERVAL_SEC == 1.0;"
                "assert f4_tracking.F4_HEARTBEAT_INTERVAL_SEC == 30.0;"
                "assert main.PAPER_FAST_PROBE_OPEN_TIMEOUT_SEC == 2.5"
            ),
        ],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_negative_f4_intervals_are_clamped_at_zero():
    env = os.environ.copy()
    env["F4_WS_STALE_SEC"] = "-2"
    env["F4_REST_POLL_INTERVAL_SEC"] = "-5"
    env["F4_FILL_POLL_INTERVAL_SEC"] = "-0.5"
    env["F4_STATE_PERSIST_INTERVAL_SEC"] = "-1"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from src.modules import f4_tracking;"
                "assert f4_tracking.F4_WS_STALE_SEC == 0.0;"
                "assert f4_tracking.F4_REST_POLL_INTERVAL_SEC == 0.0;"
                "assert f4_tracking.F4_FILL_POLL_INTERVAL_SEC == 0.0;"
                "assert f4_tracking.F4_STATE_PERSIST_INTERVAL_SEC == 0.0"
            ),
        ],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_pytest_import_mode_does_not_load_local_dotenv_values():
    env = os.environ.copy()
    env["STOCK_SKIP_DOTENV"] = "1"
    env.pop("F4_POST_CLOSE_OBSERVE_UNTIL", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import api_tests._helper;"
                "import main;"
                "from src.modules import f4_tracking;"
                "assert f4_tracking.F4_POST_CLOSE_OBSERVE_UNTIL == '09:10'"
            ),
        ],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def _script_modules_that_load_dotenv() -> list[str]:
    return [
        f"scripts.{path.stem}"
        for path in sorted((ROOT / "scripts").glob("*.py"))
        if "load_dotenv" in path.read_text(encoding="utf-8")
    ]


@pytest.mark.parametrize("module", _script_modules_that_load_dotenv())
def test_scripts_do_not_load_dotenv_when_imported_during_tests(module):
    """수집 중 import만으로 개발 머신의 .env가 os.environ에 실리면 안 된다.

    pytest는 테스트 모듈을 전부 import한 뒤 실행한다. 스크립트 하나가 가드 없이
    load_dotenv를 부르면 그 뒤 모든 테스트가 실계좌 자격증명·실 URL을 들고 돌아
    실제 KIS를 때린다. 목록은 소스에서 뽑으므로 새 스크립트도 자동으로 걸린다.
    """
    env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "SystemRoot", "TEMP", "TMP", "COMSPEC")
        if key in os.environ
    }
    env["STOCK_SKIP_DOTENV"] = "1"
    env["PYTHONPATH"] = str(ROOT)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib, json, os;"
                "before = set(os.environ);"
                f"importlib.import_module({module!r});"
                "print(json.dumps(sorted(set(os.environ) - before)))"
            ),
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    leaked = json.loads(result.stdout.strip().splitlines()[-1])
    assert leaked == [], f"{module} import가 .env 키를 실었다: {leaked}"
