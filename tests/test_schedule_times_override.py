"""스케줄 시각 오버라이드 — 개발 트리를 운영 진입 창 밖으로 밀어내기 위한 것."""

import importlib

from src import schedule_times


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
