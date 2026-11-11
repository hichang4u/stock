"""실전 검증 게이트는 전략 지문 밖에 있다.

게이트 코드나 설정 디렉터리 변수가 지문에 잡히면, 게이트를 고치거나 증거 위치를 바꾸는
것만으로 PAPER 정상 청산 20건 카운터가 0이 된다. 정책 변경은 지문이 아니라 증거의 정책
해시로 자격을 내린다.
"""

from __future__ import annotations

from pathlib import Path

from src import release
from src.real_validation import gate

ROOT = Path(__file__).resolve().parents[1]


def test_gate_code_is_not_a_strategy_file():
    strategy_files = set(release._STRATEGY_FILES)
    gate_files = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src" / "real_validation").glob("*.py")
    }

    assert gate_files
    assert not strategy_files & gate_files
    assert "src/readiness.py" not in strategy_files
    assert "scripts/execution_mode_diff.py" not in strategy_files


def test_gate_directory_setting_does_not_change_the_strategy_fingerprint(monkeypatch, tmp_path):
    monkeypatch.delenv(gate.ENV_DIR, raising=False)
    release.strategy_fingerprint.cache_clear()
    before = release.strategy_fingerprint()
    environment_before = release._strategy_environment()

    monkeypatch.setenv(gate.ENV_DIR, str(tmp_path))
    release.strategy_fingerprint.cache_clear()
    try:
        assert release.strategy_fingerprint() == before
        assert release._strategy_environment() == environment_before
    finally:
        release.strategy_fingerprint.cache_clear()
