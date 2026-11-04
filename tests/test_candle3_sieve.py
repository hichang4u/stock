"""3양봉 진입·3음봉 청산 체 — 등록 표본 전용, 진입과 청산을 따로 잰다."""

import pytest

from scripts.candle3_sieve import (
    END,
    assert_registration_sample,
    candle_exit,
    three_bull_entry,
)


def _bar(time: str, o: float, c: float, *, h: float | None = None, lo: float | None = None) -> dict:
    return {"time": time, "open": o, "close": c,
            "high": max(o, c) if h is None else h, "low": min(o, c) if lo is None else lo}


def test_enters_at_the_open_of_the_bar_after_three_bullish_bars():
    bars = [
        _bar("090000", 100, 101), _bar("090100", 101, 102), _bar("090200", 102, 103),
        _bar("090300", 103.5, 104),
    ]
    assert three_bull_entry(bars) == 3


def test_flat_or_bearish_bar_resets_the_count():
    bars = [
        _bar("090000", 100, 101), _bar("090100", 101, 102),
        _bar("090200", 102, 102),                               # 보합은 양봉이 아니다
        _bar("090300", 102, 103), _bar("090400", 103, 104), _bar("090500", 104, 103),
        _bar("090600", 103, 104), _bar("090700", 104, 105), _bar("090800", 105, 106),
        _bar("090900", 106, 107),
    ]
    assert three_bull_entry(bars) == 9


def test_no_signal_without_a_following_bar_before_the_timeout():
    bars = [
        _bar("151100", 100, 101), _bar("151200", 101, 102), _bar("151300", 102, 103),
        _bar("151500", 103, 104),                               # 다음 봉이 15:15면 진입하지 않는다
    ]
    assert three_bull_entry(bars) is None


def test_exits_at_the_open_of_the_bar_after_three_bearish_bars():
    bars = [
        _bar("090100", 100, 99.8), _bar("090200", 99.8, 99.6), _bar("090300", 99.6, 99.4),
        _bar("090400", 99.3, 99.9),
    ]
    result = candle_exit(bars, 0, 100.0)
    assert result["reason"] == "BEARISH3"
    assert result["exit_time"] == "090400"
    assert result["pct"] == pytest.approx(-0.7)


def test_hard_stop_comes_first_inside_a_bar():
    bars = [_bar("090100", 100, 99.9, lo=97.5), _bar("090200", 99.9, 99.8)]
    result = candle_exit(bars, 0, 100.0)
    assert result["reason"] == "HARD_STOP"
    assert result["pct"] == -2.0


def test_a_bullish_bar_resets_the_bearish_count_and_timeout_closes():
    bars = [
        _bar("090100", 100, 99.8), _bar("090200", 99.8, 99.6), _bar("090300", 99.6, 99.9),
        _bar("090400", 99.9, 99.7), _bar("151500", 99.7, 100.5),
    ]
    result = candle_exit(bars, 0, 100.0)
    assert result["reason"] == "TIMEOUT"
    assert result["pct"] == pytest.approx(0.5)


def test_post_registration_dates_are_refused():
    """등록 이후 표본은 나중 판정용이다. 체로 먼저 보지 않는다."""
    assert END == "20260910"
    assert_registration_sample("20260701", "20260910")
    with pytest.raises(SystemExit):
        assert_registration_sample("20260701", "20260914")
