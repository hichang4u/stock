import json

import pytest

pytest.importorskip("fastapi")

import src.api.server as server  # noqa: E402 — fastapi 미설치 시 모듈 스킵 이후 임포트
from src import db, perf_costs  # noqa: E402
from src.modules.f4_tracking import (  # noqa: E402
    FORCE_TRAILING_HOUR,
    FORCE_TRAILING_MINUTE,
)
from src.schedule_times import F5_EXEC_H, F5_EXEC_M  # noqa: E402


def _trade(**over):
    base = {
        "date": "20260701", "ticker": "005930", "name": "삼성전자",
        "entry_price": 10_000.0, "high_price": None, "highest_step": 0.0,
        "pnl_pct": 0.0, "close_reason": "TIMEOUT",
        "entry_at": "2026-07-01T09:12:00+09:00",
        "exit_at": "2026-07-01T11:00:00+09:00",
    }
    base.update(over)
    return base


def _order(phase, order_price, fill_price, latency=500, trigger_price=None):
    row = {
        "order_phase": phase, "order_price": order_price,
        "fill_price": fill_price, "fill_latency_ms": latency,
    }
    if trigger_price is not None:
        row["trigger_price"] = trigger_price
    return row


def test_improve_empty_rows_returns_zero_structure():
    payload = server._improve_from_rows([], [], {})

    assert payload["overall"] == {
        "total": 0, "wins": 0, "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
        "payoff_ratio": 0.0, "expectancy": 0.0,
        "cur_loss_streak": 0, "max_loss_streak": 0,
    }
    assert payload["mfe_rows"] == []
    assert payload["hold_time"] == {}
    assert payload["params"]["step_size_pct"] == 2.5
    assert payload["params"]["step_trail_pct"] == 2.0
    assert payload["params"]["hard_stop_pct"] == 2.0
    assert payload["params"]["gap_max_order_pct"] == 10.0
    assert payload["params"]["gap_max_fill_pct"] == 10.0
    # 시각은 schedule_times/f4_tracking이 단일 출처 — 리터럴로 고정하지 않는다.
    assert payload["params"]["timeout_time"] == f"{F5_EXEC_H:02d}:{F5_EXEC_M:02d}"
    assert payload["params"]["force_trailing_time"] == (
        f"{FORCE_TRAILING_HOUR:02d}:{FORCE_TRAILING_MINUTE:02d}"
    )
    # 강제 trailing은 청산 10분 전이라는 관계가 깨지면 안 된다.
    assert (FORCE_TRAILING_HOUR * 60 + FORCE_TRAILING_MINUTE) == (
        F5_EXEC_H * 60 + F5_EXEC_M - 10
    )


def test_improve_overall_payoff_and_expectancy():
    trades = [
        _trade(date="20260701", pnl_pct=2.0, close_reason="TRAILING"),
        _trade(date="20260702", pnl_pct=-1.0, close_reason="HARD_STOP"),
        _trade(date="20260703", pnl_pct=-1.0, close_reason="HARD_STOP"),
    ]

    o = server._improve_from_rows(trades, [], {})["overall"]

    assert o["total"] == 3
    assert o["wins"] == 1
    assert o["win_rate"] == 33.3
    assert o["avg_win"] == 2.0
    assert o["avg_loss"] == -1.0
    assert o["payoff_ratio"] == 2.0
    # 기대값 = 1/3*2.0 + 2/3*(-1.0) = 0.0
    assert o["expectancy"] == 0.0


def test_improve_loss_streaks_track_current_and_max():
    # 순서: 패 패 승 패 → 최대 2, 현재 1
    trades = [
        _trade(date="20260701", pnl_pct=-1.0),
        _trade(date="20260702", pnl_pct=-1.0),
        _trade(date="20260703", pnl_pct=1.0),
        _trade(date="20260704", pnl_pct=-1.0),
    ]

    o = server._improve_from_rows(trades, [], {})["overall"]

    assert o["max_loss_streak"] == 2
    assert o["cur_loss_streak"] == 1


def test_improve_mfe_rows_newest_first_with_giveback():
    trades = [
        _trade(date="20260701", high_price=10_210.0, pnl_pct=-2.13,
               close_reason="HARD_STOP"),
        _trade(date="20260702", high_price=10_400.0, pnl_pct=1.0,
               close_reason="TRAILING", highest_step=0.025),
    ]

    rows = server._improve_from_rows(trades, [], {})["mfe_rows"]

    assert [r["date"] for r in rows] == ["20260702", "20260701"]
    assert rows[1]["mfe_pct"] == 2.1
    assert rows[1]["giveback_pp"] == 4.23
    assert rows[0]["mfe_pct"] == 4.0
    assert rows[0]["giveback_pp"] == 3.0


def test_improve_mfe_null_when_high_price_missing():
    rows = server._improve_from_rows([_trade(high_price=None)], [], {})["mfe_rows"]

    assert rows[0]["mfe_pct"] is None
    assert rows[0]["giveback_pp"] is None


def test_improve_step_counts_near_miss_and_step1():
    trades = [
        # 근접 이탈: 스텝1 미도달 + MFE 2.1% + 손실
        _trade(date="20260701", high_price=10_210.0, pnl_pct=-2.13,
               close_reason="HARD_STOP"),
        # 스텝1 도달
        _trade(date="20260702", high_price=10_400.0, pnl_pct=1.0,
               close_reason="TRAILING", highest_step=0.025),
        # MFE 1.5% + 손실 → 근접 이탈 (경계값 포함)
        _trade(date="20260703", high_price=10_150.0, pnl_pct=-0.5,
               close_reason="TIMEOUT"),
        # MFE 1.4% → 근접 이탈 아님
        _trade(date="20260704", high_price=10_140.0, pnl_pct=-0.5,
               close_reason="TIMEOUT"),
        # MFE 2.0%지만 수익 마감 → 근접 이탈 아님
        _trade(date="20260705", high_price=10_200.0, pnl_pct=0.3,
               close_reason="TIMEOUT"),
    ]

    s = server._improve_from_rows(trades, [], {})["step"]

    assert s["step1_n"] == 1
    assert s["step1_rate"] == 20.0
    assert s["near_miss_n"] == 2


def test_improve_hard_stop_slip_and_fast_stop():
    trades = [
        # -2.13% 체결, 8분 보유 → 편차 0.13%p, 빠른 손절
        _trade(date="20260701", pnl_pct=-2.13, close_reason="HARD_STOP",
               entry_at="2026-07-01T09:12:00+09:00",
               exit_at="2026-07-01T09:20:00+09:00"),
        # -2.07% 체결, 30분 보유
        _trade(date="20260702", pnl_pct=-2.07, close_reason="HARD_STOP",
               entry_at="2026-07-02T09:12:00+09:00",
               exit_at="2026-07-02T09:42:00+09:00"),
    ]

    h = server._improve_from_rows(trades, [], {})["hard_stop"]

    assert h["n"] == 2
    assert h["share_pct"] == 100.0
    assert h["avg_fill_pnl"] == -2.1
    assert h["avg_slip_pp"] == 0.1
    assert h["fast_stop_n"] == 1
    assert h["avg_min_to_stop"] == 19.0


def test_improve_trailing_and_timeout_sections():
    trades = [
        _trade(date="20260701", high_price=10_400.0, pnl_pct=1.0,
               close_reason="TRAILING", highest_step=0.025),
        _trade(date="20260702", high_price=10_150.0, pnl_pct=-0.5,
               close_reason="TIMEOUT"),
    ]

    payload = server._improve_from_rows(trades, [], {})

    assert payload["trailing"] == {"n": 1, "avg_giveback_pp": 3.0, "avg_pnl": 1.0}
    assert payload["timeout_exit"] == {
        "n": 1, "avg_pnl": -0.5, "avg_mfe": 1.5,
        "avg_pnl_net": round(perf_costs.apply_costs(-0.5, "TIMEOUT")["net_conservative_pct"], 2),
    }


def test_improve_hold_time_grouped_by_reason():
    trades = [
        _trade(date="20260701", close_reason="HARD_STOP",
               entry_at="2026-07-01T09:12:00+09:00",
               exit_at="2026-07-01T09:20:00+09:00"),
        _trade(date="20260702", close_reason="TIMEOUT",
               entry_at="2026-07-02T09:12:00+09:00",
               exit_at="2026-07-02T11:00:00+09:00"),
    ]

    ht = server._improve_from_rows(trades, [], {})["hold_time"]

    assert ht["HARD_STOP"] == {"n": 1, "avg_min": 8.0}
    assert ht["TIMEOUT"] == {"n": 1, "avg_min": 108.0}


def test_improve_slippage_adverse_is_positive_for_both_sides():
    orders = [
        # 매수: 비싸게 체결 = 불리 → +0.3
        _order("FIRST_BUY", 10_000.0, 10_030.0, latency=400),
        # 매도: 싸게 체결 = 불리 → +0.2
        _order("CLOSE_SELL", 10_000.0, 9_980.0, latency=600),
        # 매도: 비싸게 체결 = 유리 → -0.1
        _order("TIMEOUT_SELL", 10_000.0, 10_010.0, latency=800),
    ]

    sl = server._improve_from_rows([], orders, {})["slippage"]

    assert sl["buy"] == {"n": 1, "avg_pp": 0.3, "max_pp": 0.3, "avg_latency_ms": 400}
    assert sl["sell"]["n"] == 2
    assert sl["sell"]["avg_pp"] == 0.05
    assert sl["sell"]["max_pp"] == 0.2
    assert sl["sell"]["avg_latency_ms"] == 700
    assert sl["by_phase"]["FIRST_BUY"]["n"] == 1
    assert sl["by_phase"]["CLOSE_SELL"]["avg_pp"] == 0.2


def test_improve_slippage_skips_rows_without_prices():
    orders = [
        _order("FIRST_BUY", None, 10_030.0),
        _order("FIRST_BUY", 0, 10_030.0),
        _order("FIRST_BUY", 10_000.0, None),
    ]

    sl = server._improve_from_rows([], orders, {})["slippage"]

    assert sl["buy"]["n"] == 0
    assert sl["by_phase"] == {}


def test_improve_slippage_prefers_trigger_price_for_market_order():
    orders = [
        _order("CLOSE_SELL", 0.0, 9_980.0, trigger_price=10_000.0),
    ]

    sl = server._improve_from_rows([], orders, {})["slippage"]

    assert sl["sell"]["n"] == 1
    assert sl["sell"]["avg_pp"] == 0.2


def test_improve_guard_count_and_skips():
    # SLIPPAGE_GUARD는 거래를 열기 전에 daily_skips에 기록되고 반환된다 —
    # 종료 거래(close_reason)에는 존재할 수 없으므로 skips에서 집계해야 한다.
    trades = [_trade(date="20260701", close_reason="TIMEOUT", pnl_pct=-1.2)]
    skips = {"NO_TARGET": 3, "GAP_CHANGED": 1, "SLIPPAGE_GUARD": 1}

    payload = server._improve_from_rows(trades, [], skips)

    assert payload["slippage"]["guard_n"] == 1
    assert payload["candidates"] == {
        "skips": {"NO_TARGET": 3, "GAP_CHANGED": 1, "SLIPPAGE_GUARD": 1},
        "skip_days": 5,
        "trade_days": 1,
    }


def test_improve_echoes_f1_conditional_hard_max():
    payload = server._improve_from_rows([], [], {})

    assert payload["params"]["f1_gap_hard_max_pct"] == 10.0


def test_improve_excludes_manual_trade_from_strategy_diagnostics():
    trades = [
        _trade(date="20260701", pnl_pct=2.0, close_reason="TRAILING", highest_step=0.025),
        _trade(date="20260702", pnl_pct=-5.0, close_reason="MANUAL"),
    ]

    payload = server._improve_from_rows(trades, [], {})

    assert payload["overall"]["total"] == 1
    assert payload["overall"]["expectancy"] == 2.0
    assert payload["data_quality"]["source_closed_n"] == 2
    assert payload["data_quality"]["excluded_manual_n"] == 1


def test_improve_step_rate_excludes_unobservable_legacy_rows():
    trades = [
        _trade(date="20260701", high_price=None, highest_step=0.0),
        _trade(date="20260702", high_price=None, highest_step=0.025),
        _trade(date="20260703", high_price=10_100.0, highest_step=0.0),
    ]

    payload = server._improve_from_rows(trades, [], {})
    step = payload["step"]

    assert step["observed_n"] == 2
    assert step["step1_n"] == 1
    assert step["step1_rate"] == 50.0
    assert step["coverage_pct"] == 66.7
    assert payload["data_quality"]["mfe_observed_n"] == 1


def test_improve_trailing_diagnostic_compares_exit_with_active_step_stop():
    trades = [
        _trade(
            date="20260701", high_price=10_988.0, pnl_pct=5.95,
            close_reason="TRAILING", highest_step=0.075,
        ),
        _trade(
            date="20260702", high_price=11_226.0, pnl_pct=8.58,
            close_reason="TRAILING", highest_step=0.10,
        ),
    ]

    payload = server._improve_from_rows(trades, [], {})
    diag = payload["trailing_diagnostics"]

    assert diag["giveback_n"] == 2
    assert diag["stop_eval_n"] == 2
    assert diag["avg_stop_slip_pp"] == 0.0
    assert diag["max_stop_slip_pp"] == 0.0
    assert diag["structural_giveback_min_pp"] == 2.0
    assert diag["structural_giveback_max_pp"] == 4.5


def test_improve_candidate_supply_uses_no_target_only_and_deduplicates_dates():
    trades = [_trade(date="20260703", close_reason="TIMEOUT", pnl_pct=1.0)]
    skips = {"NO_TARGET": 2, "ENTRY_FAIL": 1, "MARKET_CLOSED": 1}
    skip_rows = [
        {"date": "20260701", "reason": "NO_TARGET"},
        {"date": "20260703", "reason": "NO_TARGET"},
        {"date": "20260704", "reason": "ENTRY_FAIL"},
        {"date": "20260705", "reason": "MARKET_CLOSED"},
    ]

    supply = server._improve_from_rows(
        trades, [], skips, skip_rows=skip_rows,
    )["candidate_supply"]

    assert supply == {
        "trade_days": 1,
        "no_target_days": 1,
        "evaluated_days": 2,
        "operational_skip_n": 1,
        "market_closed_days": 1,
        "overlap_days": 1,
    }


def test_improve_marks_zero_fill_latency_as_unmeasured():
    orders = [_order("FIRST_BUY", 10_000.0, 10_010.0, latency=0)]

    quality = server._improve_from_rows([], orders, {})["data_quality"]

    assert quality["fill_latency_measured_n"] == 0

@pytest.mark.asyncio
async def test_api_improve_empty_db_returns_default_structure(tmp_path):
    await db.init(str(tmp_path / "improve.db"))

    resp = await server.api_improve()
    payload = json.loads(resp.body.decode("utf-8"))

    assert payload["overall"]["total"] == 0
    assert payload["mfe_rows"] == []
    assert payload["slippage"]["buy"]["n"] == 0
    assert payload["candidates"]["skips"] == {}
    assert payload["params"]["hard_stop_pct"] == 2.0
    await db.close()


@pytest.mark.asyncio
async def test_api_improve_aggregates_seeded_trades_orders_skips(tmp_path):
    await db.init(str(tmp_path / "improve.db"))
    conn = db.get()

    # 거래 1: 손절 (MFE 2.1%, 8분 보유 → 근접 이탈 + 빠른 손절)
    t1 = await db.open_trade("20260701", "005930", 10_000.0, 10, name="삼성전자")
    await db.update_trade_progress(t1, 10_210.0, 0.0)
    await db.close_trade(
        t1, 9_787.0, "HARD_STOP", -2.13, 0.0,
        exit_qty=10, high_price=10_210.0,
    )
    # 거래 2: 트레일링 (스텝1 도달, MFE 4.0%)
    t2 = await db.open_trade("20260702", "000660", 10_000.0, 10, name="SK하이닉스")
    await db.update_trade_progress(t2, 10_400.0, 0.025)
    await db.close_trade(
        t2, 10_100.0, "TRAILING", 1.0, 0.025,
        exit_qty=10, high_price=10_400.0,
    )
    # 진입·청산 시각을 결정적으로 고정 (close_trade는 now를 기록)
    await conn.execute(
        "UPDATE trades SET entry_at=?, exit_at=? WHERE id=?",
        ("2026-07-01T09:12:00+09:00", "2026-07-01T09:20:00+09:00", t1))
    await conn.execute(
        "UPDATE trades SET entry_at=?, exit_at=? WHERE id=?",
        ("2026-07-02T09:12:00+09:00", "2026-07-02T10:30:00+09:00", t2))
    await conn.commit()

    # 주문: 매수 불리 +0.3%p 체결
    o1 = await db.record_order(t1, "KIS001", "BUY", 10, 10_000.0,
                               "FIRST_BUY", "005930", name="삼성전자")
    await db.update_order_fill(o1, 10_030.0, 10, 400)
    # 미체결(PENDING) 주문은 집계에서 제외되어야 함
    await db.record_order(t1, "KIS002", "SELL", 10, 10_000.0,
                          "CLOSE_SELL", "005930")

    await db.record_skip("20260703", "NO_TARGET", "후보 없음")
    await db.record_skip("20260704", "SLIPPAGE_GUARD", "expected=10000,fill=10800")

    resp = await server.api_improve()
    payload = json.loads(resp.body.decode("utf-8"))

    assert payload["overall"]["total"] == 2
    assert payload["step"]["step1_n"] == 1
    assert payload["step"]["near_miss_n"] == 1
    assert payload["hard_stop"]["n"] == 1
    assert payload["hard_stop"]["fast_stop_n"] == 1
    assert payload["mfe_rows"][0]["date"] == "20260702"  # 최신 먼저
    assert payload["slippage"]["buy"]["n"] == 1
    assert payload["slippage"]["buy"]["avg_pp"] == 0.3
    assert payload["slippage"]["guard_n"] == 1
    assert payload["candidates"]["skips"] == {"NO_TARGET": 1, "SLIPPAGE_GUARD": 1}
    assert payload["candidates"]["trade_days"] == 2
    await db.close()


# ---------------------------------------------------------------------------
# 2026-10-28: 판정이 지문 21개에 걸친 62건을 현재 파라미터로 섞어 계산하고 비용을 빼지 않았다
# (docs/IMPROVE_MENU_FOLLOWUP_20261028.md 가 ~ 다).
# ---------------------------------------------------------------------------

_SEG = {"name": "파서 수정 후", "start": "20261002", "end": None}


def _scoped_inputs():
    trades = [
        _trade(id=1, date="20260915", strategy_fingerprint="old", pnl_pct=1.0),
        _trade(id=2, date="20261020", strategy_fingerprint="prev", pnl_pct=-2.0,
               close_reason="HARD_STOP"),
        _trade(id=3, date="20261028", strategy_fingerprint="cur", pnl_pct=0.23),
    ]
    orders = [
        {**_order("FIRST_BUY", 100.0, 101.0), "trade_id": 1},
        {**_order("FIRST_BUY", 100.0, 100.5), "trade_id": 2},
        {**_order("FIRST_BUY", 100.0, 100.0), "trade_id": 3},
    ]
    skips = [
        {"date": "20260916", "reason": "NO_TARGET", "created_at": "2026-09-16T09:00:01+09:00"},
        {"date": "20261027", "reason": "ENTRY_FAIL", "created_at": "2026-10-27T09:00:19+09:00"},
        {"date": "20261029", "reason": "NO_TARGET", "created_at": "2026-10-29T09:00:01+09:00"},
    ]
    return trades, orders, skips


def test_fingerprint_scope_keeps_only_the_current_fingerprint():
    trades, orders, skips = _scoped_inputs()

    t, o, s, meta = server._scope_rows(
        "fingerprint", trades, orders, skips, fingerprint="cur",
        fingerprint_since="2026-10-27T15:52:34+09:00", segment=_SEG,
    )

    assert [r["id"] for r in t] == [3]
    assert [r["trade_id"] for r in o] == [3]
    # 승격(15:52) 전 같은 날 진입 무산은 이전 지문의 일이다
    assert [r["date"] for r in s] == ["20261029"]
    assert meta["verdict"] is True
    assert meta["fingerprint"] == "cur"
    assert meta["fingerprints_n"] == 1


def test_segment_scope_uses_the_performance_report_segment():
    trades, orders, skips = _scoped_inputs()

    t, o, s, meta = server._scope_rows(
        "segment", trades, orders, skips, fingerprint="cur",
        fingerprint_since="2026-10-27T15:52:34+09:00", segment=_SEG,
    )

    assert [r["id"] for r in t] == [2, 3]
    assert [r["trade_id"] for r in o] == [2, 3]
    assert [r["date"] for r in s] == ["20261027", "20261029"]
    assert meta["verdict"] is False  # 구간 안에도 지문이 여럿이다
    assert meta["segment"] == "파서 수정 후"
    assert meta["fingerprints_n"] == 2


def test_all_scope_keeps_everything_without_verdict():
    trades, orders, skips = _scoped_inputs()

    t, o, s, meta = server._scope_rows(
        "all", trades, orders, skips, fingerprint="cur", fingerprint_since=None, segment=_SEG,
    )

    assert len(t) == 3 and len(o) == 3 and len(s) == 3
    assert meta["verdict"] is False
    assert meta["fingerprints_n"] == 3
    assert meta["unversioned_n"] == 0


def test_trades_before_fingerprints_are_counted_separately():
    trades, orders, skips = _scoped_inputs()
    trades.append(_trade(id=4, date="20260710", strategy_fingerprint=None))

    *_, meta = server._scope_rows(
        "all", trades, orders, skips, fingerprint="cur", fingerprint_since=None, segment=_SEG,
    )

    assert meta["fingerprints_n"] == 3
    assert meta["unversioned_n"] == 1


def test_fingerprint_scope_without_registry_starts_at_the_first_trade():
    trades, orders, skips = _scoped_inputs()

    _, _, s, _ = server._scope_rows(
        "fingerprint", trades, orders, skips, fingerprint="cur",
        fingerprint_since=None, segment=_SEG,
    )

    # 현재 지문의 첫 진입(2026-07-01T09:12 — _trade 기본값) 뒤 스킵
    assert [r["date"] for r in s] == ["20260916", "20261027", "20261029"]


def test_unknown_scope_falls_back_to_fingerprint():
    trades, orders, skips = _scoped_inputs()

    _, _, _, meta = server._scope_rows(
        "weird", trades, orders, skips, fingerprint="cur", fingerprint_since=None, segment=_SEG,
    )

    assert meta["name"] == "fingerprint"


def test_overall_net_uses_the_performance_report_cost_model():
    trades = [
        _trade(date="20261020", pnl_pct=2.0, close_reason="TRAILING"),
        _trade(date="20261021", pnl_pct=-2.0, close_reason="HARD_STOP"),
    ]

    payload = server._improve_from_rows(trades, [], {})

    win = perf_costs.apply_costs(2.0, "TRAILING")["net_conservative_pct"]
    loss = perf_costs.apply_costs(-2.0, "HARD_STOP")["net_conservative_pct"]
    assert payload["overall"]["expectancy"] == 0.0  # 체결 그대로는 이전과 같다
    assert payload["overall_net"]["basis"] == "conservative"
    assert payload["overall_net"]["expectancy"] == round((win + loss) / 2, 2)
    assert payload["overall_net"]["avg_loss"] == round(loss, 2)
    by_basis = payload["expectancy_by_basis"]
    assert by_basis["gross"] == 0.0
    assert by_basis["cost"] == round(-perf_costs.BASE_ROUND_TRIP_COST_PCT, 2)
    assert by_basis["conservative"] == payload["overall_net"]["expectancy"]


def test_timeout_exit_reports_the_conservative_average():
    trades = [_trade(date="20261028", pnl_pct=0.23, close_reason="TIMEOUT")]

    to = server._improve_from_rows(trades, [], {})["timeout_exit"]

    assert to["avg_pnl"] == 0.23
    assert to["avg_pnl_net"] == round(
        perf_costs.apply_costs(0.23, "TIMEOUT")["net_conservative_pct"], 2)


def test_order_slippage_compares_with_the_order_price_and_skips_market_orders():
    orders = [
        # 0828 043200: 기준가 1,384 · 주문 1,437 · 체결 1,420 → 기준가 대비 +2.6, 주문가 대비 −1.18
        _order("FIRST_BUY", 1437.0, 1420.0, trigger_price=1384.0),
        _order("FIRST_BUY", 10_000.0, 10_017.0, trigger_price=9_990.0),
        _order("CLOSE_SELL", 0.0, 9_980.0, trigger_price=10_000.0),  # 시장가 매도
    ]

    payload = server._improve_from_rows([], orders, {})

    assert payload["slippage"]["basis"] == "trigger"
    assert payload["slippage"]["buy"]["max_pp"] == 2.601
    osl = payload["order_slippage"]["buy"]
    assert osl["n"] == 2
    assert osl["max_pp"] == 0.17
    assert payload["order_slippage"]["sell"]["n"] == 0


def test_payload_carries_scope_and_marks_params_versioned():
    meta = {"name": "fingerprint", "verdict": True, "fingerprint": "cur",
            "since": None, "segment": "파서 수정 후", "fingerprints_n": 1, "unversioned_n": 0}

    payload = server._improve_from_rows([], [], {}, scope=meta)

    assert payload["scope"] == meta
    assert payload["data_quality"]["params_versioned"] is True


@pytest.mark.asyncio
async def test_api_improve_defaults_to_the_current_fingerprint(tmp_path, monkeypatch):
    await db.init(str(tmp_path / "improve.db"))
    conn = db.get()
    t1 = await db.open_trade("20261020", "005930", 10_000.0, 10, name="삼성전자")
    await db.close_trade(t1, 9_800.0, "HARD_STOP", -2.0, 0.0, exit_qty=10, high_price=10_000.0)
    t2 = await db.open_trade("20261028", "009830", 26_650.0, 260, name="한화솔루션")
    await db.close_trade(t2, 26_710.0, "TIMEOUT", 0.23, 0.0, exit_qty=260,
                         high_price=27_175.0)
    await conn.execute("UPDATE trades SET strategy_fingerprint='prev' WHERE id=?", (t1,))
    await conn.execute("UPDATE trades SET strategy_fingerprint='cur' WHERE id=?", (t2,))
    await conn.commit()
    monkeypatch.setattr(server, "_current_fingerprint", lambda: "cur")

    try:
        default = json.loads((await server.api_improve()).body.decode("utf-8"))
        everything = json.loads((await server.api_improve(scope="all")).body.decode("utf-8"))
    finally:
        await db.close()  # 실패해도 닫는다 — 열린 연결은 pytest 종료를 막는다

    assert default["scope"]["name"] == "fingerprint"
    assert default["overall"]["total"] == 1
    assert everything["overall"]["total"] == 2
    assert everything["scope"]["verdict"] is False


# ---------------------------------------------------------------------------
# 같은 파라미터 표본(2026-10-28). 지문은 진입 경로·파서 같은 다른 조건으로도 바뀐다. 카드가 보는
# 파라미터 값이 지금과 같았던 거래는 그 카드의 판정에 쓸 수 있다(설정 기록은 strategy_configs).
# ---------------------------------------------------------------------------

_EXIT = {"hard_stop_ratio": 0.02, "first_step": 0.025, "step_trail": 0.02,
         "f5_exec": "15:15", "force_trailing": "15:05"}
_F1_OLD = {"f1_gap_min": 0.025, "f1_gap_core_max": 0.08, "f1_gap_hard_max": 0.1,
           "f1_min_expected_amount": 1e8, "f1_high_gap_min_expected_amount": 5e9}
_F1_NEW = {**_F1_OLD, "f1_high_gap_min_expected_amount": 1e9}


def _configs():
    def cfg(fp, at, config):
        return {"fingerprint": fp, "created_at": at, "config": config}

    return [
        cfg("A", "2026-08-14T16:00:00+09:00", {**_EXIT, **_F1_OLD}),
        cfg("D", "2026-09-01T16:00:00+09:00", {**_EXIT, "step_trail": 0.015, **_F1_OLD}),
        cfg("B", "2026-09-16T16:00:00+09:00", {**_EXIT, **_F1_NEW}),
        cfg("C", "2026-10-27T15:52:00+09:00", {**_EXIT, **_F1_NEW}),
    ]


def _pool_trades():
    return [
        _trade(id=1, date="20260710", strategy_fingerprint=None),
        _trade(id=2, date="20260820", strategy_fingerprint="A", close_reason="HARD_STOP"),
        _trade(id=3, date="20260905", strategy_fingerprint="D"),
        _trade(id=4, date="20260920", strategy_fingerprint="B"),
        _trade(id=5, date="20261028", strategy_fingerprint="C"),
    ]


def _pool_skips():
    return [
        {"date": "20260815", "reason": "NO_TARGET", "created_at": "2026-08-15T09:00:01+09:00"},
        {"date": "20260917", "reason": "NO_TARGET", "created_at": "2026-09-17T09:00:01+09:00"},
        {"date": "20261029", "reason": "ENTRY_FAIL", "created_at": "2026-10-29T09:00:20+09:00"},
    ]


def test_param_pools_keep_trades_whose_card_parameters_match_today():
    pools = server._param_pools(_pool_trades(), _pool_skips(), _configs(), "C")

    exit_pool, f1_pool = pools["exit"], pools["f1"]
    assert [t["id"] for t in exit_pool["trades"]] == [2, 4, 5]  # D는 트레일이 다르고 1은 기록 없음
    assert exit_pool["meta"]["n"] == 3
    assert exit_pool["meta"]["first_date"] == "20260820"
    assert exit_pool["meta"]["fingerprints_n"] == 3
    assert exit_pool["meta"]["params"] == _EXIT
    assert [t["id"] for t in f1_pool["trades"]] == [4, 5]
    # 스킵은 그 시각에 돌던 지문의 설정으로 가른다
    assert [r["date"] for r in f1_pool["skips"]] == ["20260917", "20261029"]


def test_no_config_for_the_current_fingerprint_means_no_pools():
    assert server._param_pools(_pool_trades(), _pool_skips(), _configs(), "Z") == {}


def test_pooled_cards_replace_only_their_sections():
    trades = _pool_trades()
    pools = server._param_pools(trades, _pool_skips(), _configs(), "C")
    base = server._improve_from_rows([trades[-1]], [], {})

    payload = server._with_param_pools(base, pools)

    assert payload["exit_pool"]["meta"]["n"] == 3
    assert payload["exit_pool"]["hard_stop"]["n"] == 1
    assert payload["exit_pool"]["overall_total"] == 3
    assert payload["f1_pool"]["candidate_supply"]["trade_days"] == 2
    assert payload["overall"]["total"] == 1  # 전략 종합은 현재 지문 그대로


@pytest.mark.asyncio
async def test_api_improve_adds_pools_only_for_the_fingerprint_scope(tmp_path, monkeypatch):
    await db.init(str(tmp_path / "improve.db"))
    conn = db.get()
    try:
        for i, (fp, day, cfg) in enumerate([("old", "20260820", {**_EXIT, **_F1_NEW}),
                                            ("cur", "20261028", {**_EXIT, **_F1_NEW})]):
            tid = await db.open_trade(day, f"00000{i}", 10_000.0, 10, name="x")
            await db.close_trade(tid, 9_800.0, "HARD_STOP", -2.0, 0.0, exit_qty=10,
                                 high_price=10_000.0)
            await conn.execute("UPDATE trades SET strategy_fingerprint=? WHERE id=?", (fp, tid))
            await db.upsert_strategy_config({**cfg, "strategy_fingerprint": fp},
                                            kind="PRIMARY", code_fingerprint=fp)
        await conn.commit()
        monkeypatch.setattr(server, "_current_fingerprint", lambda: "cur")

        scoped = json.loads((await server.api_improve()).body.decode("utf-8"))
        everything = json.loads((await server.api_improve(scope="all")).body.decode("utf-8"))
    finally:
        await db.close()

    assert scoped["overall"]["total"] == 1
    assert scoped["exit_pool"]["meta"]["n"] == 2
    assert "exit_pool" not in everything
