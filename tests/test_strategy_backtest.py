"""전략 정책 백테스트 하네스 검증.

백테스트가 운영과 다른 판정을 하면 그 위에서 고른 파라미터는 근거가 아니라 착각이
된다. 그래서 (1) 기본 정책이 운영 f1_selector/f3_entry와 같은 답을 내는지,
(2) 정책을 바꿨을 때 의도한 방향으로만 바뀌는지, (3) 데이터가 없을 때 유리한 쪽으로
둔갑하지 않는지를 고정한다.
"""

import json
from dataclasses import replace

import pytest

from scripts.strategy_backtest import (
    BASELINE,
    FAST_BAR,
    LEGACY_BAR,
    evaluate_entry,
    load_probe_days,
    load_universes,
    rank,
    realized_pct,
    recheck_gate,
    score,
    selection_rejection,
    simulate_day,
    simulate_probe_day,
    summarize,
    tickers_needed,
)
from src.modules import f1_selector
from src.modules.f3_entry import _evaluate_order_gap

# ── 운영 코드와의 동치 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "gap, amount",
    [
        (0.019, 1_000_000_000),
        (0.020, 1_000_000_000),
        (0.050, 0),
        (0.079, 100_000),
        (0.080, 1_000_000_000),
        (0.080, 5_000_000_000),
        (0.099, 6_000_000_000),
        (0.100, 9_000_000_000),
        (0.160, 9_000_000_000),
        (float("nan"), 1_000_000_000),
    ],
)
def test_recheck_gate_matches_production_f3(gap, amount):
    """재검증 게이트는 f3_entry._evaluate_order_gap 과 사유까지 같아야 한다."""
    assert recheck_gate(gap, amount, BASELINE) == _evaluate_order_gap(gap, amount)


def test_recheck_gate_treats_missing_amount_as_fail_closed():
    """고갭 구간에서 대금이 없으면 통과시키지 않는다(운영과 동일한 fail-closed)."""
    assert recheck_gate(0.085, None, BASELINE) == (False, "HIGH_GAP_AMOUNT_LOW")
    assert recheck_gate(0.085, 0, BASELINE) == (False, "HIGH_GAP_AMOUNT_LOW")


def test_score_matches_production_selector_on_synthetic_rows():
    """기본 정책 점수는 f1_selector.f1_score 와 같은 값이어야 한다."""
    rows = [
        {"gap_pct": 0.03, "expected_amount": 2_000_000_000, "avg_amount_5d": 500_000_000},
        {"gap_pct": 0.075, "expected_amount": 300_000_000, "avg_amount_5d": 300_000_000},
        {"gap_pct": 0.09, "expected_amount": 9_000_000_000, "avg_amount_5d": 100_000_000},
    ]
    for row in rows:
        assert score(row, BASELINE) == pytest.approx(f1_selector.f1_score(row), abs=1e-4)


def test_baseline_ranking_matches_production_on_current_config_days(tmp_path):
    """현행 설정으로 찍힌 스냅샷에서는 운영 랭킹과 종목·순서가 같아야 한다.

    2026-08-12 이전 스냅샷은 F1_GAP_MIN 이 3% 이던 시절의 gap_allowed 를 품고 있어
    비교 대상이 아니다. 하네스는 모든 날에 같은 정책을 다시 적용하는 게 목적이다.
    """
    universes = load_universes()
    current = {d: u for d, u in universes.items() if d >= "20260812"}
    if not current:
        # 이 테스트는 운영이 남긴 data/f1_snapshots 를 읽는다. 20260903에
        # `git worktree remove` 가 정션을 따라가 그 디렉터리를 통째로 지웠고,
        # 09:00의 예상체결 상태라 재생성이 불가능하다. 장이 다시 스냅샷을
        # 쌓으면 저절로 돌아온다 — 그때까지 실패로 두면 스위트의 신호가 죽는다.
        pytest.skip("data/f1_snapshots 에 현행 설정(20260812 이후) 스냅샷이 없다")
    for date, universe in current.items():
        mine = [c["ticker"] for c in rank(universe, BASELINE)]
        prod = [c["ticker"] for c in f1_selector.rank_candidates(universe)]
        assert mine == prod, f"{date} 랭킹 불일치"


# ── 정책 파라미터가 의도한 방향으로만 움직이는가 ────────────────────────


def test_overheat_penalty_removal_lifts_high_gap_candidate():
    """과열 페널티를 끄면 8%대 후보가 저갭 후보를 앞선다.

    대금·거래량급증을 같게 두고 갭만 다르게 한다. 그래야 순서가 뒤집히는 원인이
    페널티 하나로 좁혀진다.
    """
    high = {"ticker": "HIGH", "gap_pct": 0.095, "expected_amount": 6_000_000_000,
            "avg_amount_5d": 6_000_000_000}
    low = {"ticker": "LOW", "gap_pct": 0.030, "expected_amount": 6_000_000_000,
           "avg_amount_5d": 6_000_000_000}
    universe = [high, low]

    assert [c["ticker"] for c in rank(universe, BASELINE)] == ["LOW", "HIGH"]

    no_penalty = replace(BASELINE, name="X", overheat_penalty=0.0)
    assert [c["ticker"] for c in rank(universe, no_penalty)] == ["HIGH", "LOW"]


def test_high_gap_amount_floor_gates_selection():
    """고갭 대금 하한을 낮추면 걸러지던 8%대 중소형주가 후보로 들어온다."""
    row = {"ticker": "A", "gap_pct": 0.084, "expected_amount": 2_600_000_000,
           "avg_amount_5d": 1_000_000_000}
    assert selection_rejection(row, BASELINE) == "HIGH_GAP"

    loosened = replace(BASELINE, name="X", high_gap_min_amount=500_000_000)
    assert selection_rejection(row, loosened) is None


def test_gap_hard_max_bounds_the_universe():
    """상한을 올리기 전에는 10% 이상이 후보에 없어야 한다."""
    row = {"ticker": "A", "gap_pct": 0.12, "expected_amount": 6_000_000_000,
           "avg_amount_5d": 1_000_000_000}
    assert selection_rejection(row, BASELINE) == "GAP"

    loosened = replace(BASELINE, name="X", gap_hard_max=0.150)
    assert selection_rejection(row, loosened) is None


def test_excluded_products_never_enter_the_universe():
    row = {"ticker": "A", "name": "KODEX 레버리지", "gap_pct": 0.04,
           "expected_amount": 9_000_000_000, "avg_amount_5d": 1_000_000_000}
    assert selection_rejection(row, BASELINE) == "PRODUCT"


# ── 하루 시뮬레이션 ─────────────────────────────────────────────────────


def _bars(open_0900, open_0901, *, high, low, close):
    """09:00/09:01 시가만 다르고 나머지는 공유하는 최소 분봉 두 개."""
    return [
        {"date": "20260820", "time": "090000", "open": open_0900, "high": high,
         "low": low, "close": close},
        {"date": "20260820", "time": "090100", "open": open_0901, "high": high,
         "low": low, "close": close},
    ]


def test_entry_bar_changes_the_recheck_gap():
    """82초 지연을 봉으로 재현한다: 같은 종목이 0900은 통과, 0901은 상한 초과."""
    universe = [{"ticker": "A", "gap_pct": 0.07, "prev_close": 10_000.0,
                 "expected_amount": 1_000_000_000, "avg_amount_5d": 1_000_000_000}]
    bars = {"A": _bars(10_700, 11_600, high=11_800, low=10_600, close=11_500)}

    fast = simulate_day("20260820", universe, replace(BASELINE, name="F", entry_bar=FAST_BAR), bars)
    assert fast["entered"] is True
    assert fast["recheck_gap_pct"] == pytest.approx(7.0)

    legacy = simulate_day(
        "20260820", universe, replace(BASELINE, name="L", entry_bar=LEGACY_BAR), bars
    )
    assert legacy["entered"] is False
    assert legacy["attempts"][0]["reason"] == "ABOVE_MAX"


def test_depth_enables_candidate_replacement():
    """1순위가 갭 상한에 막히면 depth 안의 다음 후보로 넘어간다."""
    universe = [
        {"ticker": "BLOCK", "gap_pct": 0.078, "prev_close": 10_000.0,
         "expected_amount": 9_000_000_000, "avg_amount_5d": 100_000_000},
        {"ticker": "OK", "gap_pct": 0.030, "prev_close": 10_000.0,
         "expected_amount": 9_000_000_000, "avg_amount_5d": 9_000_000_000},
    ]
    bars = {
        "BLOCK": _bars(11_600, 11_600, high=11_800, low=11_500, close=11_700),
        "OK": _bars(10_400, 10_400, high=10_900, low=10_300, close=10_800),
    }

    shallow = simulate_day("20260820", universe, replace(BASELINE, name="D1", depth=1), bars)
    assert shallow["entered"] is False

    deep = simulate_day("20260820", universe, replace(BASELINE, name="D2", depth=2), bars)
    assert deep["entered"] is True
    assert deep["ticker"] == "OK"
    assert deep["attempts"][0]["reason"] == "ABOVE_MAX"


def test_barrier_window_starts_at_the_entry_bar():
    """09:01 진입은 09:00 봉의 저가로 손절 판정을 받으면 안 된다.

    09:00 봉만 -2% 아래로 찔렀다가 이후 +2.5%로 올라가는 날, 09:00 진입은 손절이지만
    09:01 진입은 아직 들고 있지도 않았으므로 익절이어야 한다. 측정 시작을 진입 봉에
    묶지 않으면 지연이 있는 정책이 부당하게 불리해진다.
    """
    bars = [
        {"date": "20260820", "time": "090000", "open": 10_000, "high": 10_050,
         "low": 9_700, "close": 10_000},
        {"date": "20260820", "time": "090100", "open": 10_000, "high": 10_300,
         "low": 9_950, "close": 10_280},
    ]
    universe = [{"ticker": "A", "gap_pct": 0.04, "prev_close": 10_000 / 1.04,
                 "expected_amount": 1_000_000_000, "avg_amount_5d": 1_000_000_000}]

    fast_policy = replace(BASELINE, name="F", entry_bar=FAST_BAR)
    fast = simulate_day("20260820", universe, fast_policy, {"A": bars})
    assert fast["outcome"] == "DOWN_FIRST"

    legacy_policy = replace(BASELINE, name="L", entry_bar=LEGACY_BAR)
    legacy = simulate_day("20260820", universe, legacy_policy, {"A": bars})
    assert legacy["outcome"] == "UP_FIRST"


def test_realized_pct_marks_to_last_close_within_the_entry_window():
    """NONE 청산가도 진입 봉 이후 구간에서만 찾는다."""
    bars = [
        {"date": "20260820", "time": "090000", "open": 10_000, "high": 10_050,
         "low": 9_950, "close": 10_000},
        {"date": "20260820", "time": "090100", "open": 10_000, "high": 10_100,
         "low": 9_990, "close": 10_060},
    ]
    result = {"outcome": "NONE", "entry_price": 10_000}
    assert realized_pct(result, bars, LEGACY_BAR) == pytest.approx(0.6)


def test_missing_bars_never_count_as_an_entry():
    """분봉이 없는 날을 '장벽 미접촉'으로 처리하면 데이터 없음이 성과로 둔갑한다."""
    universe = [{"ticker": "A", "gap_pct": 0.04, "prev_close": 10_000.0,
                 "expected_amount": 1_000_000_000, "avg_amount_5d": 1_000_000_000}]
    row = simulate_day("20260817", universe, BASELINE, {})
    assert row["entered"] is False
    assert row["attempts"][0]["reason"] == "NO_BARS"


# ── 손익 환산 / 집계 ────────────────────────────────────────────────────


def test_realized_pct_uses_approved_barriers():
    bars = _bars(10_000, 10_000, high=10_400, low=9_700, close=10_300)
    assert realized_pct({"outcome": "UP_FIRST", "entry_price": 10_000}, bars) == pytest.approx(2.5)
    down = {"outcome": "DOWN_FIRST", "entry_price": 10_000}
    assert realized_pct(down, bars) == pytest.approx(-2.0)


def test_realized_pct_is_none_for_ambiguous():
    """같은 봉에서 양쪽 장벽에 닿으면 순서를 모른다. 0%로 세면 표본이 오염된다."""
    bars = _bars(10_000, 10_000, high=10_400, low=9_700, close=10_300)
    assert realized_pct({"outcome": "AMBIGUOUS", "entry_price": 10_000}, bars) is None


def test_realized_pct_marks_untouched_window_to_last_close():
    bars = _bars(10_000, 10_000, high=10_100, low=9_950, close=10_050)
    assert realized_pct({"outcome": "NONE", "entry_price": 10_000}, bars) == pytest.approx(0.5)


def test_summarize_excludes_undecidable_from_returns():
    rows = [
        {"entered": True, "outcome": "UP_FIRST",
         "realized_pct": 2.5, "mfe_pct": 3.0, "mae_pct": -0.5},
        {"entered": True, "outcome": "AMBIGUOUS",
         "realized_pct": None, "mfe_pct": 4.0, "mae_pct": -3.0},
        {"entered": False},
    ]
    s = summarize(rows)
    assert s["days"] == 3
    assert s["entered"] == 2
    assert s["scored"] == 1
    assert s["undecidable"] == 1
    assert s["total_pct"] == pytest.approx(2.5)
    assert s["win_rate_pct"] == pytest.approx(100.0)


def test_evaluate_entry_returns_none_without_window_bars():
    assert evaluate_entry([], 10_000) is None


# ── 분봉 수집 대상 ──────────────────────────────────────────────────────


def test_tickers_needed_covers_every_policy_depth():
    universe = [
        {"ticker": f"T{i}", "gap_pct": 0.03 + i * 0.001, "prev_close": 10_000.0,
         "expected_amount": 9_000_000_000, "avg_amount_5d": 1_000_000_000}
        for i in range(6)
    ]
    policies = [replace(BASELINE, name="A", depth=1), replace(BASELINE, name="B", depth=5)]
    needed = tickers_needed({"20260820": universe}, policies)
    assert len(needed["20260820"]) == 5


# ── 프로브 실측 경로 ────────────────────────────────────────────────────


def test_load_probe_days_uses_production_candidate_builder(tmp_path):
    """후보는 운영 _candidate_from_multi 로 만든다.

    여기서 직접 계산하면 expected_amount 가 '예상체결대금'이 아니라 '누적거래대금'이
    되어 고갭 대금 게이트가 실제와 다른 답을 낸다.
    """
    row = {
        "inter_shrn_iscd": "005930",
        "inter_kor_isnm": "삼성전자",
        "inter2_prpr": "10500",
        "inter2_prdy_clpr": "10000",
        "inter2_askp": "10550",
        "intr_antc_vol": "1000",
        "intr_antc_cntg_prdy_ctrt": "5.0",
        "intr_antc_cntg_vrss": "500",
        "acml_tr_pbmn": "123",
    }
    day = tmp_path / "20260820.jsonl"
    day.write_text(
        json.dumps({"event": "PAPER_FAST_PROBE_OPEN_MULTI", "response": {"output": [row]}})
        + "\n"
        + json.dumps({"event": "PAPER_FAST_PROBE_OPEN_DONE", "shadow_tickers": ["005930"]})
        + "\n",
        encoding="utf-8",
    )

    days = load_probe_days(tmp_path)
    candidate = days["20260820"][0]
    assert candidate["ticker"] == "005930"
    assert candidate["ask_price"] == pytest.approx(10_550)
    assert candidate["gap_pct"] == pytest.approx(0.05)
    # 예상체결대금 = 예상체결가 10500 * 예상수량 1000. 누적거래대금 123 이 아니다.
    assert candidate["expected_amount"] == pytest.approx(10_500_000)


def test_load_probe_days_prefers_shadow_compare_order(tmp_path):
    """순위는 SHADOW_COMPARE 의 fast_tickers 가 OPEN_DONE 보다 우선한다."""
    def row(ticker):
        return {
            "inter_shrn_iscd": ticker,
            "inter_kor_isnm": f"종목{ticker}",
            "inter2_prpr": "10300",
            "inter2_prdy_clpr": "10000",
            "inter2_askp": "10310",
            "intr_antc_vol": "500",
            "intr_antc_cntg_prdy_ctrt": "3.0",
            "intr_antc_cntg_vrss": "300",
        }

    day = tmp_path / "20260820.jsonl"
    day.write_text(
        json.dumps({"event": "PAPER_FAST_PROBE_OPEN_MULTI",
                    "response": {"output": [row("000111"), row("000222")]}})
        + "\n"
        + json.dumps({"event": "PAPER_FAST_PROBE_OPEN_DONE",
                      "shadow_tickers": ["000111", "000222"]})
        + "\n"
        + json.dumps({"event": "PAPER_FAST_SHADOW_COMPARE",
                      "fast_tickers": ["000222", "000111"]})
        + "\n",
        encoding="utf-8",
    )

    assert [c["ticker"] for c in load_probe_days(tmp_path)["20260820"]] == ["000222", "000111"]


def test_simulate_probe_day_fills_at_the_recorded_ask():
    """진입가는 09:00:00.3 에 실제로 호가된 매도호가여야 한다. 봉 시가가 아니다."""
    candidates = [{
        "ticker": "A", "name": "테스트", "ask_price": 10_300.0, "prev_close": 10_000.0,
        "gap_pct": 0.03, "expected_amount": 1_000_000_000,
    }]
    bars = [
        {"date": "20260820", "time": "090000", "open": 10_100, "high": 10_600,
         "low": 10_200, "close": 10_580},
    ]
    row = simulate_probe_day("20260820", candidates, BASELINE, {"A": bars})
    assert row["entered"] is True
    assert row["entry_price"] == pytest.approx(10_300.0)
    assert row["outcome"] == "UP_FIRST"


def test_simulate_probe_day_applies_the_same_gate_as_production():
    """프로브 후보도 F3 갭 게이트를 통과해야 한다."""
    candidates = [{
        "ticker": "A", "ask_price": 11_500.0, "prev_close": 10_000.0,
        "gap_pct": 0.15, "expected_amount": 9_000_000_000,
    }]
    bars = [{"date": "20260820", "time": "090000", "open": 11_500, "high": 11_900,
             "low": 11_400, "close": 11_800}]
    row = simulate_probe_day("20260820", candidates, BASELINE, {"A": bars})
    assert row["entered"] is False
    assert row["attempts"][0]["reason"] == "ABOVE_MAX"
