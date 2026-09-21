"""C1 오버나이트 체 — 순수 함수만 검사한다. 파일·DB는 읽지 않는다."""

from scripts.overnight_sieve import (
    BASE_ROUND_TRIP_COST_PCT,
    HARD_STOP_SLIPPAGE_PCT,
    TRAILING_SLIPPAGE_PCT,
    apply_costs,
    simulate_overnight,
)


def _bar(time_: str, o: float, h: float, lo: float, c: float) -> dict:
    return {"date": "20260922", "time": time_, "open": o, "high": h, "low": lo, "close": c,
            "volume": 100.0}


def _session(first: dict, *rest: dict) -> list[dict]:
    """첫 봉 + 나머지 + 15:15 타임아웃 봉까지 채워 covers_session을 만족시킨다."""
    bars = [first, *rest]
    last_min = int(bars[-1]["time"][:2]) * 60 + int(bars[-1]["time"][2:4])
    close = bars[-1]["close"]
    for m in range(last_min + 1, 15 * 60 + 31):
        bars.append(_bar(f"{m // 60:02d}{m % 60:02d}00", close, close, close, close))
    return bars


def test_gap_down_beyond_hard_stop_exits_at_the_open():
    bars = _session(_bar("090000", 97.0, 99.0, 96.0, 98.0))
    r = simulate_overnight(bars, entry_price=100.0)
    assert r["exit_reason"] == "GAP_HARD_STOP" and r["exit_time"] == "090000"
    assert r["exit_price"] == 97.0 and r["gross_pct"] == -3.0
    assert r["gap_pct"] == -3.0 and r["bars_complete"] is True


def test_gap_up_then_trailing_uses_track_b_rule_low_first():
    # 시가 +1%, 09:01 고가 103(스텝 2.5%), 09:02 저가 100.4(스탑 100.5 이탈)
    bars = _session(
        _bar("090000", 101.0, 101.5, 100.8, 101.2),
        _bar("090100", 101.2, 103.0, 101.0, 102.8),
        _bar("090200", 102.8, 103.0, 100.4, 100.6),
    )
    r = simulate_overnight(bars, entry_price=100.0)
    assert r["gap_pct"] == 1.0
    assert r["exit_reason"] == "TRAILING" and r["exit_time"] == "090200"
    assert round(r["exit_price"], 2) == 100.5 and round(r["gross_pct"], 2) == 0.5


def test_hard_stop_inside_the_day_and_timeout():
    stopped = simulate_overnight(
        _session(_bar("090000", 100.5, 100.8, 97.9, 98.0)), entry_price=100.0
    )
    assert stopped["exit_reason"] == "HARD_STOP" and stopped["gross_pct"] == -2.0

    flat = simulate_overnight(_session(_bar("090000", 100.5, 100.8, 100.2, 100.5)),
                              entry_price=100.0)
    assert flat["exit_reason"] == "TIMEOUT" and flat["exit_time"] == "151500"


def test_incomplete_session_is_flagged_and_ends_with_data_end():
    bars = [_bar("090000", 100.5, 100.8, 100.2, 100.5), _bar("090100", 100.5, 100.6, 100.4, 100.5)]
    r = simulate_overnight(bars, entry_price=100.0)
    assert r["bars_complete"] is False and r["exit_reason"] == "DATA_END"


def test_costs_follow_the_improvement_plan_constants():
    assert (BASE_ROUND_TRIP_COST_PCT, HARD_STOP_SLIPPAGE_PCT, TRAILING_SLIPPAGE_PCT) == (
        0.18, 0.30, 0.15
    )
    c = apply_costs(2.5, "TRAILING")
    assert round(c["net_cost_pct"], 2) == 2.32 and round(c["net_slip_pct"], 2) == 2.17
    g = apply_costs(-3.0, "GAP_HARD_STOP")
    assert round(g["net_slip_pct"], 2) == -3.48   # 시가 하드스탑도 하드스탑 슬리피지
    t = apply_costs(0.0, "TIMEOUT")
    assert round(t["net_slip_pct"], 2) == -0.38
