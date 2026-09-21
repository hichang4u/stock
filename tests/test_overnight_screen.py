"""C1 종가 스크리닝 — 순수 함수만 검사한다. 네트워크는 부르지 않는다.

규칙 임계값은 스펙 §2.2에 고정돼 있다. 여기서 값을 바꾸면 스펙부터 바꿔야 한다.
"""

from scripts.overnight_screen import (
    AMOUNT_MULTIPLE_MIN,
    CHANGE_MAX,
    CHANGE_MIN,
    CLOSE_POSITION_MIN,
    close_position,
    evaluate,
    is_excluded_name,
    rank_candidates,
)


def _row(**over) -> dict:
    base = {
        "ticker": "000001", "name": "정상전자", "change_pct": 5.0,
        "open": 10000.0, "high": 11000.0, "low": 9900.0, "close": 10800.0,
        "amount": 5_000_000_000.0, "avg_amount_20d": 1_000_000_000.0,
    }
    base.update(over)
    base["close_position"] = close_position(base["high"], base["low"], base["close"])
    base["amount_multiple"] = (
        base["amount"] / base["avg_amount_20d"] if base["avg_amount_20d"] else None
    )
    return base


def test_thresholds_match_spec():
    assert (CHANGE_MIN, CHANGE_MAX, CLOSE_POSITION_MIN, AMOUNT_MULTIPLE_MIN) == (
        3.0, 25.0, 0.8, 2.0
    )


def test_close_position_is_fraction_of_range_and_none_when_flat():
    assert close_position(110.0, 100.0, 108.0) == 0.8
    assert close_position(100.0, 100.0, 100.0) is None


def test_evaluate_passes_a_textbook_close():
    assert evaluate(_row()) is None


def test_evaluate_rejects_each_rule_with_its_reason():
    assert evaluate(_row(change_pct=2.9)) == "CHANGE_PCT"
    assert evaluate(_row(change_pct=25.0)) == "CHANGE_PCT"
    assert evaluate(_row(close=10700.0)) == "CLOSE_POSITION"       # (10700-9900)/1100 = 0.727
    assert evaluate(_row(high=100.0, low=100.0, close=100.0)) == "CLOSE_POSITION"
    assert evaluate(_row(amount=1_900_000_000.0)) == "AMOUNT_MULTIPLE"
    assert evaluate(_row(amount=900_000_000.0, avg_amount_20d=100_000_000.0)) == "AMOUNT_FLOOR"
    assert evaluate(_row(close=999.0, high=1000.0, low=990.0)) == "PRICE_FLOOR"
    assert evaluate(_row(name="KODEX 200")) == "NAME_EXCLUDED"
    assert evaluate(_row(avg_amount_20d=None)) == "NO_HISTORY"


def test_excluded_names_cover_etf_etn_spac():
    assert is_excluded_name("TIGER 반도체")
    assert is_excluded_name("삼성 KRX 2X ETN")
    assert is_excluded_name("하나32호스팩")
    assert not is_excluded_name("성호전자")


def test_rank_orders_by_amount_multiple_then_change_and_caps_at_five():
    rows = [
        _row(ticker="A", amount=3_000_000_000.0, change_pct=4.0),
        _row(ticker="B", amount=5_000_000_000.0, change_pct=4.0),
        _row(ticker="C", amount=5_000_000_000.0, change_pct=9.0),
        _row(ticker="D", change_pct=2.0),                      # 거부, 순위 밖
        _row(ticker="E", amount=2_500_000_000.0),
        _row(ticker="F", amount=2_400_000_000.0),
        _row(ticker="G", amount=2_300_000_000.0),
    ]
    ranked = rank_candidates(rows)
    assert [r["ticker"] for r in ranked] == ["C", "B", "A", "E", "F"]
    assert [r["rank"] for r in ranked] == [1, 2, 3, 4, 5]
