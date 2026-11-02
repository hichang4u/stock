"""H1 재평가 — 정의 1(09:01봉 시가 ÷ 전일종가 − 1)로 규칙·대조군을 고르고 §3 기준으로 판정한다."""

from scripts.h1_reevaluation import evaluate_day, judge


def _bars(open_0901: float, *, low: float | None = None) -> list[dict]:
    o = open_0901
    return [
        {"time": "090000", "open": o, "high": o, "low": o, "close": o},
        {"time": "090100", "open": o, "high": o * 1.01, "low": o if low is None else low,
         "close": o},
        {"time": "151500", "open": o, "high": o, "low": o, "close": o},
    ]


CANDS = [{"rank": 1, "ticker": "A"}, {"rank": 2, "ticker": "B"}, {"rank": 3, "ticker": "C"}]
PREV = {"A": 100.0, "B": 100.0, "C": 100.0}


def test_rule_takes_the_highest_ranked_candidate_at_or_above_7pct():
    bars = {"A": _bars(106.9), "B": _bars(107.0), "C": _bars(109.0)}
    day = evaluate_day(CANDS, bars, PREV)
    assert day["rule"]["ticker"] == "B"           # 7.0%는 문턱 이상, 랭크 3의 9%보다 랭크 2가 먼저
    assert round(day["rule"]["gap_pct"], 2) == 7.0
    assert day["control"]["ticker"] == "A"        # 대조군은 갭과 무관하게 랭크 1


def test_no_rule_entry_when_every_gap_is_below_7pct():
    bars = {"A": _bars(103.0), "B": _bars(106.99), "C": _bars(101.0)}
    day = evaluate_day(CANDS, bars, PREV)
    assert day["rule"] is None
    assert day["control"]["ticker"] == "A"


def test_candidate_without_a_0901_bar_is_excluded_and_counted_not_replaced():
    """정의 4 — 그 후보만 뺀다. 랭크 1이 빠지면 대조군도 그날은 없다(랭크 2로 내려가지 않는다)."""
    bars = {"A": [{"time": "090000", "open": 110, "high": 110, "low": 110, "close": 110}],
            "B": _bars(108.0), "C": None}
    day = evaluate_day(CANDS, bars, PREV)
    assert day["excluded"]["no_bar"] == 2
    assert day["control"] is None
    assert day["rule"]["ticker"] == "B"


def test_control_does_not_need_prev_close():
    """대조군은 갭을 보지 않는다 — 전일종가가 없어도 랭크 1의 09:01봉이 있으면 진입한다."""
    bars = {"A": _bars(108.0), "B": _bars(108.0), "C": _bars(100.0)}
    day = evaluate_day(CANDS, bars, {"B": 100.0, "C": 100.0})
    assert day["control"]["ticker"] == "A"
    assert day["rule"]["ticker"] == "B"
    assert day["excluded"]["no_prev_close"] == 1


def test_entry_and_exit_follow_section_3():
    """09:01봉 시가 진입, 저가 먼저 — 같은 봉 저가가 −2%를 찍으면 하드스탑."""
    bars = {"A": _bars(108.0, low=105.0), "B": _bars(100.0), "C": _bars(100.0)}
    day = evaluate_day(CANDS, bars, PREV)
    assert day["rule"]["entry_price"] == 108.0
    assert day["rule"]["reason"] == "HARD_STOP"
    assert day["rule"]["pct"] == -2.0


def test_judge_is_interim_below_n50():
    verdict = judge([1.0, 2.0, 3.0], [0.0, 0.5], seed=1)
    assert verdict["rule"]["n"] == 3
    assert verdict["control"]["n"] == 2
    assert verdict["diff_pct"] == 2.0 - 0.25
    assert verdict["verdict"] == "INTERIM"


def test_judge_adopts_only_when_ci_low_and_diff_are_both_positive():
    rule = [1.0] * 50
    assert judge(rule, [0.5] * 50, seed=1)["verdict"] == "ADOPT"
    assert judge(rule, [1.5] * 50, seed=1)["verdict"] == "REJECT"          # 대조군보다 못함
    assert judge([1.0, -1.2] * 25, [-1.0] * 50, seed=1)["verdict"] == "REJECT"  # CI 하한 ≤ 0
