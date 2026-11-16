"""오프라인 분석기(scripts/real_validation_offline.py)의 계산 규칙.

입력은 전부 합성 로그·합성 DB다. 실제 운영 자료의 결과는
docs/REAL_VALIDATION_OFFLINE_20261113.md에 있다.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from scripts import real_validation_offline as offline

DAY = "2027-01-04"


def _event(hms: str, name: str, **fields):
    return {"ts": f"{DAY}T{hms}+09:00", "event": name, **fields}


def _order_day(*, with_latency=True, notices=((300, 10_050.0), (700, 10_060.0))):
    events = [
        _event("09:00:00.400000", "TARGET_LOCKED", ticker="000001"),
        _event(
            "09:00:04.000000", "ENTRY_PRICE_APPROVED",
            quote_age_ms=0, ask_price=10_050.0, ask_qty=200, entry_attempt=1,
        ),
    ]
    if with_latency:
        events.append(
            _event(
                "09:00:05.990000", "LATENCY_HIGH",
                api_endpoint="/uapi/domestic-stock/v1/trading/order-cash",
                network_ms=600, rate_wait_ms=1_000, total_ms=1_600,
            )
        )
    events.append(
        _event(
            "09:00:06.000000", "ENTRY_ORDER_SENT",
            order_id="0000000777", order_price=10_100.0, order_qty=1_000,
            entry_attempt=1, rt_cd="0", mode="PAPER",
        )
    )
    for index, (qty, price) in enumerate(notices):
        events.append(
            _event(
                f"09:00:0{7 + index}.000000", "FILL_NOTICE_RECEIVED",
                order_id="777", qty=qty, price=price, exchange_time="090007", side="02",
            )
        )
    events += [
        _event(
            "09:00:09.500000", "ENTRY_EXECUTED",
            order_id="0000000777", fill_qty=1_000, fill_price=10_060.0,
            partial_fill=False, fill_latency_ms=5_100,
        ),
        _event("09:00:09.600000", "ENTRY_PIPELINE_TIMING", selection_source="FAST_MULTI"),
    ]
    return events


# ── 지연 구간 ────────────────────────────────────────────────────────


def test_stage_times_use_one_clock_and_keep_send_and_ack_apart():
    timing = offline.entry_timing(_order_day())
    attempt = timing["attempts"][0]

    assert timing["path"] == "FAST_MULTI" and timing["entered"] is True
    assert attempt["accepted"] and attempt["filled"]
    # 접수(응답 기록) − 선정. 전송 시각은 여기서 network_ms를 뺀 값이다.
    assert attempt["select_to_ack_ms"] == pytest.approx(5_600)
    assert attempt["select_to_wire_ms"] == pytest.approx(5_000)
    assert attempt["send_to_ack_network_ms"] == 600
    assert attempt["send_rate_wait_ms"] == 1_000
    assert attempt["nominal_open_to_ack_ms"] == pytest.approx(6_000)
    assert attempt["nominal_open_to_select_ms"] == pytest.approx(400)
    assert attempt["ack_to_first_notice_ms"] == pytest.approx(1_000)
    assert attempt["ack_to_last_notice_ms"] == pytest.approx(2_000)
    assert attempt["ack_to_confirm_ms"] == pytest.approx(3_500)
    assert attempt["send_start_to_confirm_ms"] == 5_100


def test_quote_is_fresh_when_approved_but_old_by_the_time_the_order_is_on_the_wire():
    attempt = offline.entry_timing(_order_day())["attempts"][0]

    assert attempt["quote_age_at_approval_ms"] == 0
    assert attempt["quote_age_at_ack_ms"] == pytest.approx(2_000)
    assert attempt["quote_age_at_wire_ms"] == pytest.approx(1_400)


def test_partial_fills_report_the_weighted_price_and_the_last_price_separately():
    attempt = offline.entry_timing(_order_day())["attempts"][0]

    assert attempt["notice_fills"] == 2 and attempt["notice_qty"] == 1_000
    assert attempt["notice_vwap"] == pytest.approx(10_057.0)
    assert attempt["notice_last_price"] == 10_060.0
    assert attempt["confirmed_fill_price"] == 10_060.0


def test_missing_latency_record_is_censored_not_zero():
    """주문 호출의 건별 기록은 200ms를 넘을 때만 남는다. 없으면 '모른다'이지 0이 아니다."""
    attempt = offline.entry_timing(_order_day(with_latency=False))["attempts"][0]

    assert attempt["network_censored_below_floor"] is True
    assert attempt["send_to_ack_network_ms"] is None
    assert attempt["send_rate_wait_ms"] is None
    assert attempt["select_to_wire_ms"] is None
    assert attempt["quote_age_at_wire_ms"] is None
    assert attempt["quote_age_at_ack_ms"] == pytest.approx(2_000)


def test_old_latency_format_without_a_split_is_kept_apart():
    events = _order_day()
    events[2] = _event(
        "09:00:05.990000", "LATENCY_HIGH",
        api_endpoint="/uapi/domestic-stock/v1/trading/order-cash", latency_ms=1_600,
    )

    attempt = offline.entry_timing(events)["attempts"][0]

    assert attempt["send_latency_unsplit_ms"] == 1_600
    assert attempt["send_to_ack_network_ms"] is None
    assert attempt["network_censored_below_floor"] is False


def test_latency_of_another_endpoint_or_another_moment_is_not_attached_to_the_order():
    events = _order_day()
    events[2] = _event(
        "09:00:05.990000", "LATENCY_HIGH",
        api_endpoint="/uapi/domestic-stock/v1/trading/inquire-psbl-order",
        network_ms=600, rate_wait_ms=1_000,
    )
    far = _order_day()
    far[2] = {**far[2], "ts": f"{DAY}T09:00:05.000000+09:00"}

    assert offline.entry_timing(events)["attempts"][0]["send_to_ack_network_ms"] is None
    assert offline.entry_timing(far)["attempts"][0]["send_to_ack_network_ms"] is None


def test_unfilled_and_rejected_attempts_are_counted_but_not_timed_as_fills():
    events = [
        _event("09:00:00.400000", "TARGET_LOCKED", ticker="000001"),
        _event("09:00:06.000000", "ENTRY_ORDER_SENT", order_id="", rt_cd="1", entry_attempt=1),
        _event("09:00:09.000000", "ENTRY_ORDER_SENT", order_id="5", rt_cd="0", entry_attempt=2),
    ]

    timing = offline.entry_timing(events)
    report = offline.latency_report([{"date": "20270104", "fingerprint": "fp", "timing": timing}])

    assert timing["entered"] is False
    assert [a["accepted"] for a in timing["attempts"]] == [False, True]
    assert report["all"]["attempts"] == 2 and report["all"]["accepted_attempts"] == 1
    assert report["all"]["filled_attempts"] == 0
    assert report["all"]["metrics"]["select_to_ack_ms"]["n"] == 0
    assert offline.entry_timing([_event("09:00:00", "TARGET_LOCKED")]) is None


def test_fill_latency_logged_as_zero_is_treated_as_not_measured():
    events = _order_day()
    events[-2] = {**events[-2], "fill_latency_ms": 0}

    assert offline.entry_timing(events)["attempts"][0]["send_start_to_confirm_ms"] is None


def test_report_gives_counts_percentiles_and_missing_rate_per_path_and_fingerprint():
    days = [
        {"date": "20270104", "fingerprint": "fp1", "timing": offline.entry_timing(_order_day())},
        {
            "date": "20270105",
            "fingerprint": "fp2",
            "timing": offline.entry_timing(_order_day(with_latency=False, notices=())),
        },
    ]
    days[1]["timing"]["path"] = "LEGACY_SINGLE_QUOTE"

    report = offline.latency_report(days)
    network = report["all"]["metrics"]["send_to_ack_network_ms"]

    assert report["all"]["filled_attempts"] == 2
    assert network == {
        "n": 1, "p50": 600.0, "p95": 600.0, "max": 600.0,
        "expected": 2, "missing": 1, "missing_rate": 0.5,
    }
    assert report["all"]["network_at_or_below_log_floor"] == 1
    assert report["all"]["metrics"]["ack_to_first_notice_ms"]["missing"] == 1
    assert set(report["by_path"]) == {"FAST_MULTI", "LEGACY_SINGLE_QUOTE"}
    assert set(report["by_fingerprint"]) == {"fp1", "fp2"}


def test_sell_notice_weighting_ignores_buys_and_other_orders():
    def notice(hms, order_id, qty, price, side):
        return _event(
            hms, "FILL_NOTICE_RECEIVED", order_id=order_id, qty=qty, price=price, side=side
        )

    events = [
        notice("09:10:00.000000", "9", 1, 99.0, "02"),
        notice("09:10:01.000000", "8", 5, 50.0, "01"),
        notice("09:10:02.000000", "0009", 10, 100.0, "01"),
        notice("09:10:02.500000", "9", 30, 96.0, "01"),
    ]

    fills = offline.sell_notice_fills(events, "0000000009")

    assert fills == {"fills": 2, "qty": 40.0, "vwap": 97.0, "last_price": 96.0, "span_ms": 500.0}
    assert offline.sell_notice_fills(events, "7") is None


# ── 비용 ─────────────────────────────────────────────────────────────


def _trade(day, entry, exit_, *, market="KOSDAQ", reason="TRAILING", qty=100):
    return {
        "date": day, "ticker": "000001", "entry_price": entry, "exit_price": exit_,
        "entry_qty": qty, "exit_qty": qty, "close_reason": reason, "market": market,
        "pnl_pct": round((exit_ / entry - 1) * 100, 2), "strategy_fingerprint": "fp",
        "execution_mode": "PAPER", "etf_like": False, "orders": [],
    }


def test_net_is_gross_minus_fee_and_tax_only_never_slippage():
    trade = _trade("20261001", 10_000.0, 9_800.0, reason="HARD_STOP")

    result = offline.net_pct(trade, fee_per_side=0.00015, sell_tax=0.0020)

    assert result["gross_pct"] == pytest.approx(-2.0)
    assert result["fees"] == pytest.approx((1_000_000 + 980_000) * 0.00015)
    assert result["taxes"] == pytest.approx(980_000 * 0.0020)
    assert result["net_pct"] == pytest.approx(-2.0 - 0.0297 - 0.196)
    assert result["slippage_deducted_pct"] == 0.0


def test_tax_follows_the_market_only_in_the_statute_low_scenario():
    kospi, kosdaq, unknown = (
        _trade("20261001", 100.0, 100.0, market=m) for m in ("KOSPI", "KOSDAQ", None)
    )

    assert offline.tax_rate(kospi, "T020") == offline.tax_rate(kosdaq, "T020") == 0.0020
    assert offline.tax_rate(kospi, "T_STATUTE_LOW") == 0.0005
    assert offline.tax_rate(kosdaq, "T_STATUTE_LOW") == 0.0020
    assert offline.tax_rate(unknown, "T_STATUTE_LOW") == 0.0005


def test_cost_report_separates_actual_net_from_the_extra_stress_adjustment():
    trades = [
        _trade("20261001", 10_000.0, 10_300.0),
        _trade("20261002", 10_000.0, 9_800.0, reason="HARD_STOP", market="KOSPI"),
    ]

    report = offline.cost_report(trades, {"20261001": "FAST_MULTI"})
    zero_cost = report["grid"]["T020_fee0.000pct_per_side"]
    statute = report["grid"]["T_STATUTE_LOW_fee0.000pct_per_side"]

    assert report["gross"]["mean_pct"] == pytest.approx(0.5)
    assert report["gross"]["mdd_pctp_closed_trades"] == pytest.approx(2.0)
    # 세금은 매도금액에 붙는다: (1,030,000 + 980,000) × 0.20% ÷ 매수금액 평균.
    assert zero_cost["mean_cost_pctp"] == pytest.approx((0.206 + 0.196) / 2, abs=1e-4)
    assert statute["mean_cost_pctp"] == pytest.approx((0.206 + 0.049) / 2, abs=1e-4)
    assert report["research_constant_round_trip_0.18"]["mean_pct"] == pytest.approx(0.32)
    # 기존 conservative는 실제 손익에서 슬리피지를 한 번 더 뺀 값이다. 실제 순손익과 따로 둔다.
    extra = report["legacy_conservative_extra_slippage"]
    assert extra["mean_pct"] < report["research_constant_round_trip_0.18"]["mean_pct"]
    assert "추가 불리 조정" in extra["note"]
    assert set(report["by_path"]) == {"FAST_MULTI", "UNKNOWN"}
    assert set(report["by_market"]) == {"KOSDAQ", "KOSPI"}
    assert report["db_pnl_pct_mismatch"] == 0


# ── 위험 초안 ────────────────────────────────────────────────────────


def test_position_fraction_is_the_smaller_of_the_loss_budget_and_the_allocation_cap():
    assert offline.position_fraction(0.04) == (pytest.approx(0.025), "loss_budget")
    assert offline.position_fraction(0.01) == (0.05, "alloc_cap")
    # 손절 폭만 쓰면 정확히 상한 5%가 된다 — 설계가 금지한 기준선이다.
    assert offline.position_fraction(0.02)[0] == pytest.approx(0.05)


def test_static_path_scales_every_trade_and_counts_losses_beyond_the_budget():
    series = [("20261001", 3.0), ("20261002", -2.0), ("20261005", -5.0), ("20261006", 1.0)]

    path = offline.risk_path(series, stress=0.04, review_days=None)

    assert path["policy_applied"] is False and path["trades_taken"] == 4
    assert path["account_return_pct"] == pytest.approx(0.025 * -3.0)
    assert path["worst_trade_account_pct"] == pytest.approx(-0.125)
    assert path["worst_trade_vs_budget_multiple"] == 1.25
    # 손실 -2%는 스트레스 4% 안이라 예산 안, -5%는 넘는다.
    assert path["trades_over_loss_budget"] == 1
    assert path["account_mdd_pct_closed_trades"] == pytest.approx(0.175)
    assert path["mdd_limit_breached"] is False


def test_halt_path_skips_trades_after_three_net_losses_and_says_so():
    series = [
        ("20261001", -1.0), ("20261002", -1.0), ("20261005", -1.0),
        ("20261006", 9.0), ("20261007", 9.0), ("20261013", 1.0),
    ]

    static = offline.risk_path(series, stress=0.04, review_days=None)
    one_day = offline.risk_path(series, stress=0.04, review_days=1)
    five_days = offline.risk_path(series, stress=0.04, review_days=5)

    assert static["trades_taken"] == 6 and static["halts"] == 0
    # 10/05(월) 3연속 → 1영업일 검토면 10/06만, 5영업일이면 10/12까지 건너뛴다.
    assert (one_day["trades_taken"], one_day["trades_skipped_after_halt"]) == (5, 1)
    assert (five_days["trades_taken"], five_days["trades_skipped_after_halt"]) == (4, 2)
    assert one_day["halts"] == five_days["halts"] == 1
    assert five_days["max_consecutive_net_losses_taken"] == 3
    # 건너뛴 거래가 이익이었다. 중단 경로의 수익은 정책의 효과가 아니라 무엇을 건너뛰었는지에
    # 달렸다.
    assert five_days["account_return_pct"] < one_day["account_return_pct"] < (
        static["account_return_pct"]
    )


def test_drawdown_limit_ends_the_halt_path():
    series = [("20261001", -50.0), ("20261002", -50.0), ("20261005", 10.0)]

    path = offline.risk_path(series, stress=0.04, review_days=1)

    assert path["drawdown_halt_on"] == "20261002"
    assert path["trades_skipped_after_halt"] == 1
    assert path["mdd_limit_breached"] is True
    assert path["days_at_or_over_daily_stop"] == 2


def test_risk_report_states_what_cannot_be_computed():
    trades = [_trade("20261001", 10_000.0, 10_300.0), _trade("20261002", 500_000.0, 480_000.0)]

    report = offline.risk_report(trades, [])

    assert report["full_allowed_order_possible"] is False
    assert any("order book depth" in item for item in report["missing_for_allowed_order"])
    assert len(report["static_all_trades"]) == 1 + len(offline.STRESS_LOSS_GRID)
    assert len(report["halt_policy_paths"]) == 2 * (1 + len(offline.STRESS_LOSS_GRID))
    first = report["amount_examples"][0]
    assert first["example_capital_krw"] == 10_000_000 and first["loss_budget_krw"] == 10_000
    # 예시 자본 1천만 원에 스트레스 3%면 주문 상한 333,333원 — 50만 원짜리 주식은 한 주도 못 산다.
    assert first["max_order_amount_krw"] == 333_333
    assert first["past_entries_priced_above_order_amount"] == 1
    assert any("수익성이 좋아진 것이 아니다" in text for text in report["caveats"])


# ── 입력을 읽기만 한다 ───────────────────────────────────────────────


def _backup(tmp_path):
    backup = tmp_path / "data_20270105_154500"
    (backup / "db").mkdir(parents=True)
    (backup / "logs").mkdir()
    (backup / "paper_fast_probe").mkdir()
    conn = sqlite3.connect(str(backup / "db" / "trading.db"))
    conn.executescript(
        """CREATE TABLE trades (id INTEGER PRIMARY KEY, date TEXT, track TEXT, ticker TEXT,
               name TEXT, entry_price REAL, entry_qty INTEGER, exit_price REAL, exit_qty INTEGER,
               close_reason TEXT, pnl_pct REAL, status TEXT, execution_mode TEXT,
               strategy_fingerprint TEXT);
           CREATE TABLE orders (id INTEGER PRIMARY KEY, trade_id INTEGER, kis_order_id TEXT,
               order_type TEXT, name TEXT, fill_price REAL, fill_qty INTEGER,
               fill_latency_ms INTEGER, trigger_price REAL);"""
    )
    rows = [
        (1, "20270104", "A", "000001", "가나다", 10_000.0, 1_000, 10_300.0, 1_000, "TRAILING",
         3.0, "CLOSED", "PAPER", "fp1"),
        (2, "20270105", "A", "000002", "라마바", 10_000.0, 1_000, 9_800.0, 1_000, "HARD_STOP",
         -2.0, "CLOSED", "PAPER", "fp1"),
        (3, "20270106", "A", "000003", "컷오프뒤", 10_000.0, 1_000, 20_000.0, 1_000, "TRAILING",
         100.0, "CLOSED", "PAPER", "fp1"),
        (4, "20270105", "B", "000004", "다른트랙", 10_000.0, 1_000, 20_000.0, 1_000, "TRAILING",
         100.0, "CLOSED", "PAPER", "fp1"),
    ]
    conn.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.execute(
        "INSERT INTO orders VALUES (1, 2, '900', 'SELL', '라마바', 9800.0, 1000, 1500, 9900.0)"
    )
    conn.commit()
    conn.close()
    (backup / "logs" / "20270104.jsonl").write_bytes(
        "\n".join(json.dumps(e) for e in _order_day()).encode("utf-8")
    )
    (backup / "logs" / "20270106.jsonl").write_bytes(b'{"ts": "x", "event": "AFTER_CUTOFF"}\n')
    (backup / "logs" / "watchdog_20270104.log").write_bytes(b"not json")
    probe = {
        "ts": f"{DAY}T08:59:45+09:00", "event": "PAPER_FAST_PROBE_RANKING", "market": "Q",
        "response": {"output": [{"mksc_shrn_iscd": "000001"}]},
    }
    (backup / "paper_fast_probe" / "20270104.jsonl").write_bytes(json.dumps(probe).encode())
    return backup


def test_run_reads_a_backup_without_touching_it_and_writes_assumptions_first(tmp_path):
    backup = _backup(tmp_path)
    before = {p: p.read_bytes() for p in backup.rglob("*") if p.is_file()}
    out = tmp_path / "out"

    results = offline.run(backup, "20270105", out, {"status": "UNVERIFIED"})

    assert {p: p.read_bytes() for p in backup.rglob("*") if p.is_file()} == before
    assert sorted(p.name for p in (backup / "db").iterdir()) == ["trading.db"]
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["cutoff_inclusive"] == "20270105"
    assert manifest["db"]["sha256"] == offline.sha256_file(backup / "db" / "trading.db")
    assert manifest["db"]["opened"] == "mode=ro&immutable=1"
    assert manifest["logs"]["count"] == 1 and manifest["logs"]["last"] == "20270104.jsonl"
    assert manifest["assumptions_fixed_before_results"]["stress_loss_grid"] == [0.03, 0.04, 0.06]
    assert manifest["tax_rate_verification"] == {"status": "UNVERIFIED"}
    # 컷오프 뒤 거래와 다른 트랙은 들어가지 않는다.
    assert results["coverage"]["closed_trades"] == 2
    assert results["coverage"]["trade_dates_without_order_log"] == ["20270105"]
    assert results["integrity_check"] == "ok"
    assert set(results["cost"]["by_market"]) == {"KOSDAQ", "UNKNOWN"}
    sell = results["latency_sell"]["all"]
    assert sell["orders"] == 1
    assert sell["adverse_slip_vs_trigger_pct"]["max"] == pytest.approx(1.0101, abs=1e-4)
    # 결과물에 종목명이 나가지 않는다.
    assert "라마바" not in (out / "results.json").read_text(encoding="utf-8")


def test_database_is_opened_read_only(tmp_path):
    backup = _backup(tmp_path)
    conn = offline.open_readonly(backup / "db" / "trading.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM trades")
    finally:
        conn.close()
