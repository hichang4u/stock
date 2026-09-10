"""트랙 B 규칙 비교 하네스 검증.

선택 규칙이 미래를 보면 백테스트가 실시간에 없는 정보를 쓴다. 진입가가 신호 봉
종가면 봉이 닫히는 순간을 미리 안 것이 된다. 두 가지가 이 파일의 핵심이다.
"""

import json
from unittest.mock import patch

import pytest

from scripts import track_b_backtest
from scripts.track_b_backtest import (
    bootstrap_ci,
    correlation,
    find_signal,
    gate_report,
    simulate_day,
)
from scripts.track_b_rules import DEFAULT_PARAMS


def _bars(prices: list[tuple[str, float]]) -> list[dict]:
    return [
        {"date": "20260820", "time": t, "open": p, "high": p, "low": p,
         "close": p, "volume": 1000.0}
        for t, p in prices
    ]


def test_signal_takes_earliest_bar_not_best_rank():
    """랭크 1이 나중에 신호를 내도 기다리지 않는다. 실시간에 불가능하다."""
    bars_by_ticker = {
        # 랭크 1 — 09:38 에 고가 돌파
        "AAA": _bars([("093500", 100), ("093600", 100), ("093700", 100),
                      ("093800", 130)]),
        # 랭크 2 — 09:37 에 먼저 돌파한다
        "BBB": _bars([("093500", 100), ("093600", 100), ("093700", 130),
                      ("093800", 100)]),
    }
    signal = find_signal(bars_by_ticker, ["AAA", "BBB"], "R1", DEFAULT_PARAMS)
    assert signal["ticker"] == "BBB"
    assert signal["signal_time"] == "093700"


def test_same_bar_tie_goes_to_higher_rank():
    bars_by_ticker = {
        "AAA": _bars([("093500", 100), ("093600", 100), ("093700", 130)]),
        "BBB": _bars([("093500", 100), ("093600", 100), ("093700", 130)]),
    }
    signal = find_signal(bars_by_ticker, ["AAA", "BBB"], "R1", DEFAULT_PARAMS)
    assert signal["ticker"] == "AAA"
    assert signal["rank"] == 1


def test_signal_ignores_bars_before_0935_and_after_1400():
    early = _bars([("091000", 100), ("091100", 130)])
    late = _bars([("135900", 100), ("140000", 100), ("140100", 130)])
    assert find_signal({"AAA": early}, ["AAA"], "R1", DEFAULT_PARAMS) is None
    assert find_signal({"AAA": late}, ["AAA"], "R1", DEFAULT_PARAMS) is None


def test_entry_price_is_next_bar_open_not_signal_close():
    """신호 봉 종가에 샀다고 하면 봉이 닫히는 순간을 미리 안 것이 된다."""
    bars = _bars([("093500", 100), ("093600", 130)])
    bars.append({"date": "20260820", "time": "093700", "open": 125,
                 "high": 125, "low": 125, "close": 125, "volume": 1000.0})
    bars.append({"date": "20260820", "time": "151500", "open": 125,
                 "high": 125, "low": 125, "close": 125, "volume": 1000.0})
    universe = [{"ticker": "AAA", "gap_pct": 0.05, "prev_close": 95,
                 "expected_amount": 5_000_000_000,
                 "avg_amount_5d": 1_000_000_000}]
    result = simulate_day("20260820", universe, {"AAA": bars}, "R1",
                          DEFAULT_PARAMS)
    assert result["entry_price"] == 125.0
    assert result["entry_time"] == "093700"


def test_no_entry_when_signal_is_the_last_bar():
    """다음 봉이 없으면 진입가가 없다. 종가로 대체하지 않는다."""
    bars = _bars([("093500", 100), ("093600", 130)])
    universe = [{"ticker": "AAA", "gap_pct": 0.05, "prev_close": 95,
                 "expected_amount": 5_000_000_000,
                 "avg_amount_5d": 1_000_000_000}]
    assert simulate_day("20260820", universe, {"AAA": bars}, "R1",
                        DEFAULT_PARAMS) is None


def test_slippage_raises_entry_price_only():
    bars = _bars([("093500", 100), ("093600", 130)])
    bars.append({"date": "20260820", "time": "093700", "open": 100,
                 "high": 100, "low": 100, "close": 100, "volume": 1000.0})
    bars.append({"date": "20260820", "time": "151500", "open": 100,
                 "high": 100, "low": 100, "close": 100, "volume": 1000.0})
    universe = [{"ticker": "AAA", "gap_pct": 0.05, "prev_close": 95,
                 "expected_amount": 5_000_000_000,
                 "avg_amount_5d": 1_000_000_000}]
    result = simulate_day("20260820", universe, {"AAA": bars}, "R1",
                          DEFAULT_PARAMS, slippage=0.004)
    assert result["entry_price"] == pytest.approx(100.4)
    assert result["pct"] == pytest.approx((100 / 100.4 - 1) * 100)


def test_bootstrap_ci_is_deterministic_and_brackets_the_mean():
    values = [1.0, -2.0, 3.0, -1.0, 2.0]
    low, high = bootstrap_ci(values)
    assert low < sum(values) / len(values) < high
    assert (low, high) == bootstrap_ci(values)   # 시드 고정


def test_bootstrap_ci_of_empty_is_none():
    assert bootstrap_ci([]) == (None, None)


def test_correlation_of_identical_series_is_one():
    assert correlation([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_correlation_needs_two_points():
    assert correlation([1.0], [1.0]) is None


def test_gate1_rejects_axis_with_fewer_than_three_entry_days():
    axis = {
        "R1": {
            "rows": [{"date": "20260820", "pct": 1.0, "ambiguous": False}] * 2,
            "slippage_signs": [1, 1, 1],
        }
    }
    report = gate_report(axis, a_daily={})
    assert report["R1"]["gate1_pass"] is False
    assert "진입일" in report["R1"]["gate1_reason"]


def test_gate2_rejects_axis_whose_sign_flips_under_slippage():
    axis = {
        "R1": {
            "rows": [{"date": f"2026082{i}", "pct": 1.0, "ambiguous": False}
                     for i in range(4)],
            "slippage_signs": [1, 1, -1],
        }
    }
    report = gate_report(axis, a_daily={})
    assert report["R1"]["gate2_pass"] is False


def test_ambiguous_days_are_excluded_but_counted():
    axis = {
        "R1": {
            "rows": [
                {"date": "20260818", "pct": 1.0, "ambiguous": False},
                {"date": "20260819", "pct": None, "ambiguous": True},
                {"date": "20260820", "pct": 2.0, "ambiguous": False},
                {"date": "20260821", "pct": 3.0, "ambiguous": False},
            ],
            "slippage_signs": [1, 1, 1],
        }
    }
    report = gate_report(axis, a_daily={})
    assert report["R1"]["ambiguous_days"] == 1
    assert report["R1"]["judged_days"] == 3
    assert report["R1"]["total_pct"] == pytest.approx(6.0)


def test_previous_trading_date_uses_the_universe_not_the_calendar():
    """08-17은 대체공휴일이라 유니버스에 없다. 달력을 쓰면 안 된다."""
    dates = ["20260814", "20260818", "20260819"]

    assert track_b_backtest.previous_trading_date(dates, "20260818") == "20260814"
    assert track_b_backtest.previous_trading_date(dates, "20260814") is None
    assert track_b_backtest.previous_trading_date(dates, "20260901") is None


def test_load_warmup_returns_empty_when_the_previous_day_is_missing(tmp_path):
    dates = ["20260814", "20260818"]

    assert track_b_backtest.load_warmup(
        "20260818", "005930", dates, days=1, cache_dir=tmp_path
    ) == []


def test_load_warmup_reads_the_previous_day_in_time_order(tmp_path):
    import json
    (tmp_path / "20260814_005930.json").write_text(json.dumps([
        {"time": "091000", "open": 2, "high": 2, "low": 2, "close": 2, "volume": 1},
        {"time": "090000", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
    ]), encoding="utf-8")
    dates = ["20260814", "20260818"]

    rows = track_b_backtest.load_warmup(
        "20260818", "005930", dates, days=1, cache_dir=tmp_path
    )

    assert [r["time"] for r in rows] == ["090000", "091000"]


def test_load_warmup_zero_days_reads_nothing(tmp_path):
    import json
    (tmp_path / "20260814_005930.json").write_text(json.dumps([
        {"time": "090000", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
    ]), encoding="utf-8")

    assert track_b_backtest.load_warmup(
        "20260818", "005930", ["20260814", "20260818"], days=0, cache_dir=tmp_path
    ) == []


def test_simulate_day_row_carries_warmup_state_for_the_entered_ticker():
    """산출물만 보고 어느 모드로 나온 값인지 알 수 있어야 한다(스펙 §8)."""
    bars = _bars([("093500", 100), ("093600", 130)])
    bars.append({"date": "20260820", "time": "093700", "open": 125,
                 "high": 125, "low": 125, "close": 125, "volume": 1000.0})
    bars.append({"date": "20260820", "time": "151500", "open": 125,
                 "high": 125, "low": 125, "close": 125, "volume": 1000.0})
    universe = [{"ticker": "AAA", "gap_pct": 0.05, "prev_close": 95,
                 "expected_amount": 5_000_000_000,
                 "avg_amount_5d": 1_000_000_000}]
    # 한 세션치(381봉) — 09:00부터 15:20까지라 개장~마감을 덮는다.
    warm = [{"time": f"{9 + m // 60:02d}{m % 60:02d}00", "open": 1, "high": 1,
             "low": 1, "close": 1, "volume": 1}
            for m in range(381)]

    result = simulate_day(
        "20260820", universe, {"AAA": bars}, "R1", DEFAULT_PARAMS,
        warmup_by_ticker={"AAA": warm},
    )

    assert result["warmup_bars"] == 381
    assert result["warmed"] is True


def test_simulate_day_row_reports_unwarmed_when_no_warmup_was_supplied():
    bars = _bars([("093500", 100), ("093600", 130)])
    bars.append({"date": "20260820", "time": "093700", "open": 125,
                 "high": 125, "low": 125, "close": 125, "volume": 1000.0})
    bars.append({"date": "20260820", "time": "151500", "open": 125,
                 "high": 125, "low": 125, "close": 125, "volume": 1000.0})
    universe = [{"ticker": "AAA", "gap_pct": 0.05, "prev_close": 95,
                 "expected_amount": 5_000_000_000,
                 "avg_amount_5d": 1_000_000_000}]

    result = simulate_day("20260820", universe, {"AAA": bars}, "R1", DEFAULT_PARAMS)

    assert result["warmup_bars"] == 0
    assert result["warmed"] is False


def test_out_json_records_the_warmup_days_the_run_used(tmp_path, monkeypatch):
    """22일 표본을 대체하는 산출물은 어떤 모드에서 나왔는지 스스로 말해야 한다(스펙 §8)."""
    monkeypatch.setattr(track_b_backtest, "load_universes", lambda: {})
    monkeypatch.setattr(
        track_b_backtest, "load_bars_for",
        lambda universes, depth=5, **kwargs: ({}, {"pairs": 0, "missing": 0, "partial": 0}),
    )
    monkeypatch.setattr(track_b_backtest, "a_daily_from_baseline", lambda: {})
    out_path = tmp_path / "result.json"

    rc = track_b_backtest.main(["--warmup-days", "2", "--out", str(out_path)])

    assert rc == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["warmup_days"] == 2
    assert "report" in payload
    assert "axes" in payload


def test_count_warmed_uses_the_session_predicate_not_a_bar_count():
    """산출물의 "실제 데운 쌍"이 개수를 세면 오전만 긴 파일을 데운 것으로 과다 보고한다."""
    def bars_from(n):
        return [{"time": f"{9 + i // 60:02d}{i % 60:02d}00"} for i in range(n)]

    whole = bars_from(380) + [{"time": "153000"}]
    morning_only = bars_from(300)                     # 09:00~13:59, 하한은 넘는다

    assert len(morning_only) > track_b_backtest.warmup_mod.WARMUP_MIN_BARS
    assert track_b_backtest.count_warmed({"20260831": {"AAA": whole}}) == 1
    assert track_b_backtest.count_warmed({"20260831": {"AAA": morning_only}}) == 0


def _bars_rising(n: int = 40) -> list[dict]:
    """09:00부터 1분 간격. 종가가 계속 올라 R1(고점 회복)이 반드시 걸린다."""
    rows = []
    for i in range(n):
        price = 10_000 + i * 10
        rows.append({
            "date": "20260910",
            "time": f"{9 + i // 60:02d}{i % 60:02d}00",
            "open": float(price), "high": float(price + 5),
            "low": float(price - 5), "close": float(price),
            "volume": 1_000,
        })
    return rows


def test_ranked_tickers_bypasses_the_f1_ranking():
    """복원된 유니버스에는 순위를 다시 매길 속성이 없다. 부르면 안 된다."""
    bars = {"111111": _bars_rising()}

    with patch("scripts.track_b_backtest.f1_selector.rank_candidates") as ranker:
        result = simulate_day(
            "20260910", [], bars, "R1", {},
            ranked_tickers=["111111"],
        )

    ranker.assert_not_called()
    # 신호가 실제로 잡혀야 주입 경로 이후 코드(진입가·청산)까지 실행된다.
    assert result is not None
    assert result["ticker"] == "111111"


def test_ranked_tickers_is_truncated_to_depth():
    """depth 를 넘는 종목은 보지 않는다 — 캐시에 없는 종목을 찾지 않게."""
    bars = {"111111": _bars_rising()}

    result = simulate_day(
        "20260910", [], bars, "R1", {},
        ranked_tickers=["111111", "222222", "333333"], depth=1,
    )
    # 222222/333333 의 분봉이 없어도 KeyError 없이 끝나야 한다.
    assert result is not None
    assert result["ticker"] == "111111"


def test_sign_stability_uses_the_restored_order_too():
    """관문 2가 조용히 옛 경로로 돌면 4일치로 부호를 판정하게 된다."""
    from scripts.track_b_backtest import sign_stability

    bars = {"20260910": {"111111": _bars_rising()}}
    universes = {"20260910": []}

    with patch("scripts.track_b_backtest.f1_selector.rank_candidates") as ranker:
        sign_stability(
            universes, bars, "R1", {},
            ranked_by_date={"20260910": ["111111"]},
        )

    ranker.assert_not_called()


def _write_cached_bars(cache_dir, date: str, ticker: str) -> None:
    """load_bars_for 가 09:00~14:00를 덮은 것으로 보도록 최소 두 봉을 쓴다."""
    rows = [
        {"date": date, "time": "090000", "open": 1.0, "high": 1.0,
         "low": 1.0, "close": 1.0, "volume": 1.0},
        {"date": date, "time": "140000", "open": 1.0, "high": 1.0,
         "low": 1.0, "close": 1.0, "volume": 1.0},
    ]
    (cache_dir / f"{date}_{ticker}.json").write_text(
        json.dumps(rows), encoding="utf-8"
    )


def test_load_bars_for_uses_the_restored_order_when_universe_is_empty(tmp_path):
    """복원 경로는 universes[date] 가 비어 있다. ranked_by_date 로 골라야 한다."""
    from scripts.track_b_backtest import load_bars_for

    _write_cached_bars(tmp_path, "20260910", "111111")
    _write_cached_bars(tmp_path, "20260910", "222222")

    bars, stats = load_bars_for(
        {"20260910": []}, cache_dir=tmp_path,
        ranked_by_date={"20260910": ["111111", "222222"]},
    )

    assert set(bars["20260910"]) == {"111111", "222222"}
    assert stats["pairs"] == 2
    assert stats["missing"] == 0


def test_load_bars_for_with_ranked_by_date_skips_the_f1_ranking():
    """빈 universe 에 rank_candidates([]) 를 돌리면 항상 0건이다. 부르면 안 된다."""
    from scripts.track_b_backtest import load_bars_for

    with patch("scripts.track_b_backtest.f1_selector.rank_candidates") as ranker:
        load_bars_for(
            {"20260910": []},
            ranked_by_date={"20260910": ["111111"]},
        )

    ranker.assert_not_called()


def test_load_bars_for_restored_path_still_counts_missing_pairs(tmp_path):
    """캐시가 없는 쌍도 옛 경로처럼 missing 으로 세어야 경고가 계속 뜬다."""
    from scripts.track_b_backtest import load_bars_for

    _write_cached_bars(tmp_path, "20260910", "111111")
    # 222222 는 캐시에 없다.

    bars, stats = load_bars_for(
        {"20260910": []}, cache_dir=tmp_path,
        ranked_by_date={"20260910": ["111111", "222222"]},
    )

    assert stats["pairs"] == 2
    assert stats["missing"] == 1
    assert set(bars["20260910"]) == {"111111"}
