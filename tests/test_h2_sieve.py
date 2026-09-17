"""H2 소급 탐색 — 09:01봉 시가 진입 + A 청산(저가 먼저)을 라벨별로 요약한다."""

from scripts.h2_sieve import simulate_open_entry, summarize


def _bar(time: str, o: float, h: float, l: float, c: float) -> dict:
    return {"date": "20260910", "time": time, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


def test_enters_at_the_0901_bar_open_and_hard_stops_on_low_first():
    bars = [
        _bar("090000", 100, 103, 99, 102),
        _bar("090100", 102, 104, 101, 103),   # 진입가 102
        _bar("090200", 103, 105, 99, 100),    # 저가 먼저: 99 <= 102*0.98 → HARD_STOP
    ]
    result = simulate_open_entry(bars)
    assert result["entry_price"] == 102
    assert result["reason"] == "HARD_STOP"
    assert result["pct"] == -2.0


def test_returns_none_without_a_0901_bar():
    """09:01봉이 없으면(VI·결측) 그 쌍은 진입 불가로 센다 — 다른 봉으로 대체하지 않는다."""
    assert simulate_open_entry([_bar("090000", 100, 101, 99, 100), _bar("090300", 100, 101, 99, 100)]) is None


def test_summarize_reports_hard_stop_rate_per_label_and_the_two_predictions():
    rows = [
        {"label": "MATERIAL", "reason": "TRAILING", "pct": 3.0},
        {"label": "MATERIAL", "reason": "HARD_STOP", "pct": -2.0},
        {"label": "MATERIAL", "reason": "TIMEOUT", "pct": 1.0},
        {"label": "NONE", "reason": "HARD_STOP", "pct": -2.0},
        {"label": "NONE", "reason": "HARD_STOP", "pct": -2.0},
        {"label": "NONE", "reason": "TRAILING", "pct": 4.0},
        {"label": "OTHER", "reason": "HARD_STOP", "pct": -2.0},
    ]
    report = summarize(rows, seed=1)
    assert report["by_label"]["MATERIAL"]["n"] == 3
    assert report["by_label"]["MATERIAL"]["hard_stop_rate"] == 1 / 3
    assert report["by_label"]["NONE"]["hard_stop_rate"] == 2 / 3
    assert report["by_label"]["OTHER"]["n"] == 1
    assert report["p1_material_below_none"] is True
    assert report["p2_material_below_half"] is True


def test_predictions_are_none_when_a_side_is_empty():
    report = summarize([{"label": "NONE", "reason": "HARD_STOP", "pct": -2.0}], seed=1)
    assert report["p1_material_below_none"] is None
    assert report["p2_material_below_half"] is None


def test_summarize_takes_treatment_and_control_labels_for_h3():
    rows = [
        {"label": "BID_DOMINANT", "reason": "TRAILING", "pct": 3.0},
        {"label": "BID_DOMINANT", "reason": "HARD_STOP", "pct": -2.0},
        {"label": "ASK_DOMINANT", "reason": "HARD_STOP", "pct": -2.0},
        {"label": "ASK_DOMINANT", "reason": "HARD_STOP", "pct": -2.0},
        {"label": "ASK_DOMINANT", "reason": "TRAILING", "pct": 1.0},
    ]
    report = summarize(rows, treatment="BID_DOMINANT", control="ASK_DOMINANT", seed=1)
    assert report["by_label"]["BID_DOMINANT"]["hard_stop_rate"] == 0.5
    assert report["by_label"]["ASK_DOMINANT"]["hard_stop_rate"] == 2 / 3
    assert report["p1_treatment_below_control"] is True
    assert report["p2_treatment_below_half"] is False
