"""스케줄 시각 오버라이드 — 개발 트리를 운영 진입 창 밖으로 밀어내기 위한 것."""

import importlib
import os

import pytest

from src import schedule_times


@pytest.fixture(autouse=True)
def _reset_schedule_times_module():
    """테스트마다 reload한 모듈을 세션에 남기지 않는다.

    monkeypatch는 env는 복원해도 이미 reload된 모듈 객체(F1_H 등)는 되돌리지
    않는다 — 이 파일의 마지막 테스트가 우연히 기본값으로 reload하고 끝나서
    지금까지 들키지 않았을 뿐, 다른 테스트 파일과 순서를 바꿔 실행하면
    schedule_times를 import하는 다른 모듈(scheduler 등)이 오염된 상수를 본다.
    os.environ을 직접 지우고 reload해 monkeypatch의 되돌림 순서와 무관하게
    기본값으로 확실히 되돌린다.
    """
    yield
    for name in ("SCHEDULE_F1", "SCHEDULE_F2", "SCHEDULE_F3"):
        os.environ.pop(name, None)
    importlib.reload(schedule_times)


def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(schedule_times)


def test_defaults_match_the_production_schedule(monkeypatch):
    """오버라이드가 없으면 값이 바뀌면 안 된다 — 운영 동작 불변."""
    monkeypatch.delenv("SCHEDULE_F1", raising=False)
    monkeypatch.delenv("SCHEDULE_F2", raising=False)
    monkeypatch.delenv("SCHEDULE_F3", raising=False)
    st = importlib.reload(schedule_times)
    assert (st.F1_H, st.F1_M) == (9, 0)
    assert (st.F2_H, st.F2_M) == (9, 10)
    assert (st.F3_H, st.F3_M, st.F3_S) == (9, 10, 10)


def test_override_shifts_the_entry_chain(monkeypatch):
    st = _reload(monkeypatch, SCHEDULE_F1="09:15:00", SCHEDULE_F2="09:25:00",
                 SCHEDULE_F3="09:25:10")
    assert (st.F1_H, st.F1_M) == (9, 15)
    assert (st.F2_H, st.F2_M) == (9, 25)
    assert (st.F3_H, st.F3_M, st.F3_S) == (9, 25, 10)


def test_malformed_override_falls_back_to_the_default(monkeypatch):
    """잘못된 값으로 스케줄이 사라지면 하루를 통째로 잃는다. fail-safe."""
    st = _reload(monkeypatch, SCHEDULE_F1="아홉시")
    assert (st.F1_H, st.F1_M) == (9, 0)


def test_out_of_range_override_falls_back(monkeypatch):
    st = _reload(monkeypatch, SCHEDULE_F1="25:00:00")
    assert (st.F1_H, st.F1_M) == (9, 0)
