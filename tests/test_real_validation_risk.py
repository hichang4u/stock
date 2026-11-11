"""위험 정책·주문 예산·예약·지속 중단 (docs/REAL_VALIDATION_DESIGN.md §4.2~4.3).

정책 숫자는 합성 입력이다. 승인된 한도가 아니다.
"""

from __future__ import annotations

import dataclasses
import sqlite3

import pytest

from src.real_validation import risk_halt, sizing
from src.real_validation.core import Verdict
from src.real_validation.risk_halt import AccountRiskState, Action, HaltKind
from src.real_validation.risk_policy import RiskPolicy
from src.real_validation.sizing import SizingInput
from src.real_validation.store import Store
from tests import real_validation_support as s

DAY = "20270104"


# ── 정책 유효성 ──────────────────────────────────────────────────────


def test_synthetic_policy_is_valid_and_empty_policy_is_entirely_undetermined():
    assert s.risk_policy().problems() == []

    problems = RiskPolicy().problems()

    for expected in (
        "POLICY_VERSION_UNDETERMINED",
        "APPROVED_ACCOUNT_CAPITAL_UNDETERMINED",
        "PER_TRADE_LOSS_BUDGET_UNDETERMINED",
        "DAILY_LOSS_LIMIT_UNDETERMINED",
        "MAX_DRAWDOWN_RATIO_UNDETERMINED",
        "CVAR_LIMIT_RATIO_UNDETERMINED",
        "CVAR_INSUFFICIENT_ACTION_UNDETERMINED",
        "MAX_CONSECUTIVE_LOSSES_UNDETERMINED",
        "ABSOLUTE_ORDER_CAP_UNDETERMINED",
        "ALLOC_RATIO_UNDETERMINED",
        "USABLE_DEPTH_RATIO_UNDETERMINED",
        "MAX_QUOTE_AGE_MS_UNDETERMINED",
        "STRESS_STOP_LOSS_RATIO_UNDETERMINED",
        "ACCOUNT_DATA_MAX_AGE_SEC_UNDETERMINED",
    ):
        assert expected in problems


_SINGLE_FIELDS = [
    f.name
    for f in dataclasses.fields(RiskPolicy)
    if f.name
    not in {
        "per_trade_loss_budget_won",
        "per_trade_loss_budget_ratio",
        "daily_loss_limit_won",
        "daily_loss_limit_ratio",
    }
]


@pytest.mark.parametrize("field", _SINGLE_FIELDS)
def test_each_missing_required_value_invalidates_the_policy(field):
    assert s.risk_policy(**{field: None}).problems()


@pytest.mark.parametrize(
    "field",
    [
        "approved_account_capital",
        "per_trade_loss_budget_won",
        "daily_loss_limit_won",
        "max_drawdown_ratio",
        "cvar_limit_ratio",
        "max_consecutive_losses",
        "absolute_order_cap",
        "alloc_ratio",
        "usable_depth_ratio",
        "max_participation_ratio",
        "max_quote_age_ms",
        "max_entry_delay_ms",
        "stress_stop_loss_ratio",
        "account_data_max_age_sec",
    ],
)
def test_zero_is_never_read_as_unlimited(field):
    problems = s.risk_policy(**{field: 0}).problems()

    assert any(p.endswith("ZERO_MEANS_UNLIMITED") for p in problems), problems


def test_allocation_ratio_keeps_the_existing_twenty_percent_ceiling():
    assert s.risk_policy(alloc_ratio=0.20).problems() == []
    assert "ALLOC_RATIO_ABOVE_20_PERCENT" in s.risk_policy(alloc_ratio=0.2001).problems()


def test_hard_stop_alone_cannot_be_the_stress_loss_rate():
    stop_only = s.risk_policy(
        stress_delay_gap_ratio=0.0, stress_adverse_fill_ratio=0.0, stress_cost_ratio=0.0
    )

    assert stop_only.problems() == ["STRESS_LOSS_EQUALS_STOP_ONLY"]
    assert s.risk_policy().stress_loss_ratio() == pytest.approx(0.0375)
    assert s.risk_policy(stress_cost_ratio=None).stress_loss_ratio() is None


def test_loss_budget_is_won_or_ratio_but_not_both():
    by_ratio = s.risk_policy(per_trade_loss_budget_won=None, per_trade_loss_budget_ratio=0.005)
    both = s.risk_policy(per_trade_loss_budget_ratio=0.005)

    assert by_ratio.problems() == []
    assert by_ratio.per_trade_loss_budget() == pytest.approx(50_000.0)
    assert "PER_TRADE_LOSS_BUDGET_BOTH_WON_AND_RATIO" in both.problems()
    assert both.per_trade_loss_budget() is None


def test_policy_hash_tracks_every_value_and_unknown_keys_are_rejected():
    assert s.risk_policy().policy_hash() == s.risk_policy().policy_hash()
    changed = s.risk_policy(absolute_order_cap=2_000_001.0)
    assert s.risk_policy().policy_hash() != changed.policy_hash()
    with pytest.raises(ValueError):
        RiskPolicy.from_dict({**s.risk_policy().as_dict(), "daily_loss_limt_won": 1.0})


# ── 주문 예산 ────────────────────────────────────────────────────────


def _input(**overrides):
    values = {
        "price": 10_037.0,
        "tick_size": 10,
        "available_cash": 50_000_000.0,
        "account_data_age_sec": 5.0,
        "ask_depth_amount": 100_000_000.0,
        "expected_trade_amount": 10_000_000_000.0,
        "quote_age_ms": 100.0,
        "entry_delay_ms": 500.0,
    }
    values.update(overrides)
    return SizingInput(**values)


def test_order_is_the_minimum_of_the_four_limits_floored_to_tick_and_shares():
    decision = sizing.allowed_order(s.risk_policy(), _input())

    # 손실 예산 50,000 ÷ 스트레스 3.75% = 1,333,333원이 가장 작다.
    assert decision.limits == pytest.approx(
        {
            "cash": 10_000_000.0,
            "absolute_cap": 2_000_000.0,
            "liquidity": 50_000_000.0,
            "loss_budget": 50_000.0 / 0.0375,
        }
    )
    assert decision.binding == "loss_budget"
    assert decision.limit_price == 10_030.0
    assert decision.qty == 132
    assert decision.amount == 132 * 10_030.0
    assert decision.amount <= decision.limits["loss_budget"] < decision.amount + 10_030.0
    assert decision.loss_budget_used == pytest.approx(decision.amount * 0.0375)
    assert decision.allowed


@pytest.mark.parametrize(
    ("overrides", "policy_overrides", "binding"),
    [
        ({"available_cash": 3_000_000.0}, {}, "cash"),
        ({}, {"absolute_order_cap": 500_000.0}, "absolute_cap"),
        ({"ask_depth_amount": 1_000_000.0}, {}, "liquidity"),
        ({"expected_trade_amount": 40_000_000.0}, {}, "liquidity"),
        ({}, {}, "loss_budget"),
    ],
)
def test_each_limit_can_be_the_binding_one(overrides, policy_overrides, binding):
    decision = sizing.allowed_order(s.risk_policy(**policy_overrides), _input(**overrides))

    assert decision.binding == binding
    assert decision.amount <= min(decision.limits.values())


def test_worse_cost_delay_or_liquidity_shrinks_the_order_then_abandons_it():
    base = sizing.allowed_order(s.risk_policy(), _input())
    worse_stress = sizing.allowed_order(
        s.risk_policy(stress_delay_gap_ratio=0.03, stress_cost_ratio=0.01), _input()
    )
    thin_book = sizing.allowed_order(s.risk_policy(), _input(ask_depth_amount=300_000.0))
    empty_book = sizing.allowed_order(s.risk_policy(), _input(ask_depth_amount=15_000.0))

    assert worse_stress.qty < base.qty
    assert thin_book.qty == 14 and thin_book.binding == "liquidity"
    assert empty_book.qty == 0 and not empty_book.allowed
    assert empty_book.blocked == ("BUDGET_BELOW_ONE_SHARE_LIQUIDITY",)


def test_existing_exposure_and_pending_buys_consume_the_loss_budget():
    fresh = sizing.allowed_order(s.risk_policy(), _input())
    with_exposure = sizing.allowed_order(
        s.risk_policy(), _input(open_exposure_loss=20_000.0, reserved_loss=15_000.0)
    )
    exhausted = sizing.allowed_order(
        s.risk_policy(), _input(open_exposure_loss=30_000.0, reserved_loss=20_000.0)
    )

    assert with_exposure.limits["loss_budget"] == pytest.approx(15_000.0 / 0.0375)
    assert with_exposure.qty < fresh.qty
    assert exhausted.qty == 0
    assert exhausted.blocked == ("BUDGET_BELOW_ONE_SHARE_LOSS_BUDGET",)


def test_pending_buy_amount_is_taken_out_of_the_cash_limit():
    decision = sizing.allowed_order(
        s.risk_policy(), _input(available_cash=6_000_000.0, reserved_amount=5_000_000.0)
    )

    assert decision.limits["cash"] == pytest.approx(200_000.0)
    assert decision.binding == "cash"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"available_cash": None}, "CASH_UNKNOWN"),
        ({"ask_depth_amount": None}, "LIQUIDITY_UNKNOWN"),
        ({"expected_trade_amount": None}, "LIQUIDITY_UNKNOWN"),
        ({"account_data_age_sec": None}, "ACCOUNT_DATA_AGE_UNKNOWN"),
        ({"account_data_age_sec": 30.001}, "STALE_ACCOUNT_DATA"),
        ({"quote_age_ms": None}, "QUOTE_AGE_UNKNOWN"),
        ({"quote_age_ms": 500.001}, "QUOTE_STALE"),
        ({"entry_delay_ms": None}, "ENTRY_DELAY_UNKNOWN"),
        ({"entry_delay_ms": 3_000.001}, "ENTRY_DELAY_EXCEEDED"),
        ({"price": 0.0}, "PRICE_INVALID"),
        ({"tick_size": 0}, "TICK_SIZE_INVALID"),
        ({"reserved_loss": float("nan")}, "EXPOSURE_OR_RESERVATION_UNKNOWN"),
    ],
)
def test_unknown_or_stale_input_blocks_the_order(overrides, reason):
    decision = sizing.allowed_order(s.risk_policy(), _input(**overrides))

    assert decision.qty == 0
    assert reason in decision.blocked


def test_freshness_limits_are_inclusive_at_the_boundary():
    decision = sizing.allowed_order(
        s.risk_policy(),
        _input(account_data_age_sec=30.0, quote_age_ms=500.0, entry_delay_ms=3_000.0),
    )

    assert decision.allowed


def test_invalid_policy_blocks_every_order():
    decision = sizing.allowed_order(s.risk_policy(absolute_order_cap=0), _input())

    assert decision.qty == 0
    assert decision.blocked == ("RISK_POLICY_ABSOLUTE_ORDER_CAP_ZERO_MEANS_UNLIMITED",)


# ── 미체결 예산 예약 ─────────────────────────────────────────────────


@pytest.fixture
def path(tmp_path):
    return tmp_path / "gate.db"


@pytest.fixture
def store(path):
    with Store.open(path, create=True) as opened:
        yield opened


def _reserve(store, order_id, decision, *, exposure=0.0):
    sizing.reserve(
        store,
        s.risk_policy(),
        client_order_id=order_id,
        decision=decision,
        open_exposure_loss=exposure,
        at=s.ts(DAY, "09:00"),
    )


def test_pending_buy_reserves_the_budget_so_a_second_order_cannot_spend_it_again(store):
    policy = s.risk_policy()
    first = sizing.allowed_order(policy, _input())
    _reserve(store, "buy-1", first)
    amount, loss = sizing.reserved_totals(store)

    assert (amount, loss) == (first.amount, pytest.approx(first.loss_budget_used))
    second = sizing.allowed_order(policy, _input(reserved_amount=amount, reserved_loss=loss))
    assert second.qty == 0

    # 예약을 보지 않고 계산한 낡은 수량으로 들어와도 예약 단계에서 막힌다.
    with pytest.raises(sizing.LossBudgetExceeded):
        _reserve(store, "buy-2", first)
    assert set(sizing.active_reservations(store)) == {"buy-1"}


def test_same_order_id_cannot_reserve_twice_even_after_release(store):
    decision = sizing.allowed_order(s.risk_policy(), _input(ask_depth_amount=300_000.0))
    _reserve(store, "buy-1", decision)

    with pytest.raises(sizing.DuplicateReservation):
        _reserve(store, "buy-1", decision)
    sizing.release(store, client_order_id="buy-1", reason="CANCEL_CONFIRMED", at=s.ts(DAY))
    with pytest.raises(sizing.DuplicateReservation):
        _reserve(store, "buy-1", decision)


def test_release_needs_a_reconciled_outcome_and_frees_the_budget(store):
    policy = s.risk_policy()
    first = sizing.allowed_order(policy, _input())
    _reserve(store, "buy-1", first)

    with pytest.raises(ValueError):
        sizing.release(store, client_order_id="buy-1", reason="TIMEOUT", at=s.ts(DAY))
    with pytest.raises(KeyError):
        sizing.release(store, client_order_id="nope", reason="CANCEL_CONFIRMED", at=s.ts(DAY))
    assert sizing.reserved_totals(store)[1] > 0

    assert sizing.release(
        store, client_order_id="buy-1", reason="CANCEL_CONFIRMED", at=s.ts(DAY)
    ) is True
    assert sizing.release(
        store, client_order_id="buy-1", reason="CANCEL_CONFIRMED", at=s.ts(DAY)
    ) is False
    assert sizing.reserved_totals(store) == (0, 0)
    _reserve(store, "buy-2", first)


def test_filled_reservation_becomes_exposure_that_still_counts(store):
    policy = s.risk_policy()
    first = sizing.allowed_order(policy, _input())
    _reserve(store, "buy-1", first)
    sizing.release(store, client_order_id="buy-1", reason="FILL_RECONCILED", at=s.ts(DAY))

    with pytest.raises(sizing.LossBudgetExceeded):
        _reserve(store, "buy-2", first, exposure=first.loss_budget_used)


def test_reservations_survive_a_restart(path):
    with Store.open(path, create=True) as store:
        _reserve(store, "buy-1", sizing.allowed_order(s.risk_policy(), _input()))

    with Store.open(path) as reopened:
        assert set(sizing.active_reservations(reopened)) == {"buy-1"}


def test_blocked_decision_cannot_be_reserved(store):
    blocked = sizing.allowed_order(s.risk_policy(), _input(available_cash=None))

    with pytest.raises(ValueError):
        _reserve(store, "buy-1", blocked)


# ── 한도 판단 ────────────────────────────────────────────────────────


def _state(**overrides):
    values = {
        "trading_day": DAY,
        "realized_pnl_today": 0.0,
        "unrealized_pnl": 0.0,
        "costs_today": 0.0,
        "equity": 10_000_000.0,
        "equity_high_water": 10_000_000.0,
        "daily_returns": [0.001] * 20,
        "trade_pnls": [1_000.0] * 5,
    }
    values.update(overrides)
    return AccountRiskState(**values)


def _kinds(policy, state):
    halts, blocks = risk_halt.evaluate_limits(policy, state)
    return [kind for kind, _ in halts], blocks


def test_healthy_account_raises_nothing():
    assert _kinds(s.risk_policy(), _state()) == ([], [])


def test_daily_loss_halts_at_the_limit_and_counts_unrealized_loss_and_costs():
    policy = s.risk_policy()

    just_under = _state(realized_pnl_today=-60_000.0, unrealized_pnl=-39_000.0, costs_today=999.0)
    at_limit = _state(realized_pnl_today=-60_000.0, unrealized_pnl=-39_000.0, costs_today=1_000.0)

    assert _kinds(policy, just_under) == ([], [])
    assert _kinds(policy, at_limit) == ([HaltKind.DAILY_LOSS], [])


def test_drawdown_halts_at_the_limit_including_open_loss():
    policy = s.risk_policy()

    assert _kinds(policy, _state(equity=9_500_001.0)) == ([], [])
    assert _kinds(policy, _state(equity=9_500_000.0)) == ([HaltKind.DRAWDOWN], [])


def test_deposits_do_not_hide_a_drawdown_and_withdrawals_do_not_create_one():
    # 10,000,000 → 손실로 9,400,000 → 1,000,000 입금 후 10,400,000.
    after_deposit = risk_halt.adjusted_high_water(
        [(10_000_000.0, 0.0), (9_400_000.0, 0.0), (10_400_000.0, 1_000_000.0)]
    )
    # 10,000,000 → 3,000,000 출금 후 7,000,000. 손실은 없었다.
    after_withdrawal = risk_halt.adjusted_high_water(
        [(10_000_000.0, 0.0), (7_000_000.0, -3_000_000.0)]
    )
    policy = s.risk_policy()

    assert after_deposit == 11_000_000.0
    assert _kinds(policy, _state(equity=10_400_000.0, equity_high_water=after_deposit)) == (
        [HaltKind.DRAWDOWN], [],
    )
    assert after_withdrawal == 7_000_000.0
    assert _kinds(policy, _state(equity=7_000_000.0, equity_high_water=after_withdrawal)) == (
        [], [],
    )


def test_cvar_with_too_few_samples_blocks_instead_of_passing():
    kinds, blocks = _kinds(s.risk_policy(), _state(daily_returns=[0.001] * 9))

    assert kinds == []
    assert blocks == ["CVAR_SAMPLE_INSUFFICIENT"]


def test_cvar_halts_at_the_limit_over_the_registered_window():
    policy = s.risk_policy()
    # 창 20일의 하위 10%(2일) 평균 손실이 정확히 2%다.
    at_limit = [0.001] * 18 + [-0.03, -0.01]
    under = [0.001] * 18 + [-0.03, -0.0099]
    old_losses_outside_window = [-0.09] * 5 + [0.001] * 20

    assert _kinds(policy, _state(daily_returns=at_limit)) == ([HaltKind.CVAR], [])
    assert _kinds(policy, _state(daily_returns=under)) == ([], [])
    assert _kinds(policy, _state(daily_returns=old_losses_outside_window)) == ([], [])


def test_consecutive_losses_halt_at_the_limit():
    policy = s.risk_policy()

    assert _kinds(policy, _state(trade_pnls=[-1.0, 5.0, -1.0, -1.0, -1.0])) == ([], [])
    assert _kinds(policy, _state(trade_pnls=[5.0, -1.0, -1.0, -1.0, -1.0])) == (
        [HaltKind.CONSECUTIVE_LOSS], [],
    )


def test_unknown_order_state_is_a_halt_and_unknown_numbers_block():
    policy = s.risk_policy()

    assert _kinds(policy, _state(order_state_unknown=True)) == (
        [HaltKind.ORDER_STATE_UNKNOWN], [],
    )
    assert _kinds(policy, _state(equity=float("nan"))) == ([], ["ACCOUNT_RISK_STATE_UNKNOWN"])
    assert _kinds(policy, _state(equity_high_water=0.0)) == ([], ["EQUITY_HIGH_WATER_UNKNOWN"])


def test_limits_cannot_be_judged_with_an_invalid_policy():
    kinds, blocks = _kinds(s.risk_policy(daily_loss_limit_won=0), _state())

    assert kinds == []
    assert blocks == ["RISK_POLICY_DAILY_LOSS_LIMIT_ZERO_MEANS_UNLIMITED"]


# ── 지속 중단과 해제 ─────────────────────────────────────────────────


def _halt(store, kind, day=DAY):
    return risk_halt.raise_halt(
        store,
        kind=kind,
        reason="synthetic",
        policy_hash=s.risk_policy().policy_hash(),
        trading_day=day,
        at=s.ts(day, "10:00"),
    )


def test_halt_survives_a_restart_and_records_reason_time_and_policy(path):
    with Store.open(path, create=True) as store:
        halt_id = _halt(store, HaltKind.DAILY_LOSS)

    finding = risk_halt.halt_finding(str(path))

    assert finding.verdict is Verdict.FAIL
    assert finding.reasons == ("RISK_HALTED_DAILY_LOSS",)
    assert finding.detail["halts"] == [
        {
            "id": halt_id,
            "kind": "DAILY_LOSS",
            "reason": "synthetic",
            "trading_day": DAY,
            "at": s.ts(DAY, "10:00"),
            "policy_hash": s.risk_policy().policy_hash(),
        }
    ]


def test_repeating_the_same_halt_on_the_same_day_does_not_duplicate_it(store):
    first = _halt(store, HaltKind.DAILY_LOSS)

    assert _halt(store, HaltKind.DAILY_LOSS) == first
    assert len(risk_halt.active_halts(store)) == 1


def test_daily_halt_is_not_released_on_the_same_day(store):
    _halt(store, HaltKind.DAILY_LOSS)

    released = risk_halt.release_daily(
        store, new_trading_day=DAY, procedure_ref="day-change", at=s.ts(DAY, "15:40")
    )

    assert released == []
    assert len(risk_halt.active_halts(store)) == 1


def test_day_change_procedure_releases_only_the_daily_halt(store):
    daily = _halt(store, HaltKind.DAILY_LOSS)
    _halt(store, HaltKind.DRAWDOWN)
    _halt(store, HaltKind.ORDER_STATE_UNKNOWN)
    next_day = s.next_weekday(DAY)

    with pytest.raises(ValueError):
        risk_halt.release_daily(
            store, new_trading_day=next_day, procedure_ref="", at=s.ts(next_day, "08:00")
        )
    released = risk_halt.release_daily(
        store, new_trading_day=next_day, procedure_ref="day-change", at=s.ts(next_day, "08:00")
    )

    assert released == [daily]
    assert sorted(h["kind"] for h in risk_halt.active_halts(store)) == [
        "DRAWDOWN", "ORDER_STATE_UNKNOWN",
    ]


def test_drawdown_and_critical_halts_need_reconciliation_and_a_recorded_decision(store):
    drawdown = _halt(store, HaltKind.DRAWDOWN)
    daily = _halt(store, HaltKind.DAILY_LOSS)
    common = {"trading_day": DAY, "at": s.ts(DAY, "16:00")}

    with pytest.raises(ValueError):
        risk_halt.record_resume_decision(
            store, halt_id=drawdown, reconciled=False, investigation="x", decided_by="op",
            **common,
        )
    with pytest.raises(ValueError):
        risk_halt.record_resume_decision(
            store, halt_id=drawdown, reconciled=True, investigation=" ", decided_by="op",
            **common,
        )
    with pytest.raises(ValueError):
        risk_halt.record_resume_decision(
            store, halt_id=drawdown, reconciled=True, investigation="x", decided_by="",
            **common,
        )
    with pytest.raises(ValueError):
        risk_halt.record_resume_decision(
            store, halt_id=daily, reconciled=True, investigation="x", decided_by="op", **common
        )
    assert len(risk_halt.active_halts(store)) == 2

    risk_halt.record_resume_decision(
        store, halt_id=drawdown, reconciled=True, investigation="cause found",
        decided_by="operator", **common,
    )

    assert [h["kind"] for h in risk_halt.active_halts(store)] == ["DAILY_LOSS"]
    decision = store.halt_events()[-1]
    assert decision["event"] == "RESUME" and decision["halt_id"] == drawdown
    assert decision["decision"] == {
        "reconciled": True, "investigation": "cause found", "decided_by": "operator",
    }
    with pytest.raises(KeyError):
        risk_halt.record_resume_decision(
            store, halt_id=drawdown, reconciled=True, investigation="x", decided_by="op",
            **common,
        )


def test_halt_history_cannot_be_edited_or_deleted(path):
    with Store.open(path, create=True) as store:
        _halt(store, HaltKind.DRAWDOWN)
        _reserve(store, "buy-1", sizing.allowed_order(s.risk_policy(), _input()))

    conn = sqlite3.connect(str(path))
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM halt_events")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE halt_events SET event='RESUME'")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM reservation_events")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE reservation_events SET loss_budget=0")
    finally:
        conn.close()
    assert risk_halt.halt_finding(str(path)).verdict is Verdict.FAIL
    with Store.open(path) as reopened:
        assert set(sizing.active_reservations(reopened)) == {"buy-1"}


def test_unknown_halt_state_is_not_treated_as_not_halted(tmp_path, path):
    with Store.open(path, create=True):
        pass
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a sqlite database")

    assert risk_halt.halt_finding(str(path)).verdict is Verdict.PASS
    assert risk_halt.halt_finding(None).reasons == ("HALT_STORE_UNDETERMINED",)
    assert risk_halt.halt_finding(str(tmp_path / "missing.db")).reasons == (
        "HALT_STATE_UNREADABLE",
    )
    assert risk_halt.halt_finding(str(corrupt)).reasons == ("HALT_STATE_UNREADABLE",)
    assert not (tmp_path / "missing.db").exists()


def test_read_only_store_cannot_write(path):
    with Store.open(path, create=True):
        pass

    with Store.open(path) as read_only:
        with pytest.raises(sqlite3.DatabaseError):
            _halt(read_only, HaltKind.DRAWDOWN)


# ── 중단 중에도 보호 청산·취소·대사는 허용 ───────────────────────────


@pytest.mark.parametrize("eligible", [True, False])
def test_only_buys_depend_on_eligibility(eligible):
    assert risk_halt.action_allowed(Action.NEW_BUY, eligible_for_real=eligible) is eligible
    assert risk_halt.action_allowed(Action.ADD_BUY, eligible_for_real=eligible) is eligible
    for action in (Action.PROTECTIVE_SELL, Action.CANCEL, Action.RECONCILE):
        assert risk_halt.action_allowed(action, eligible_for_real=eligible) is True


def test_a_halt_never_authorizes_resending_an_unconfirmed_sell():
    assert risk_halt.halt_authorizes_sell_resend([HaltKind.DAILY_LOSS]) is False
    assert risk_halt.halt_authorizes_sell_resend(list(HaltKind)) is False


def test_exit_failure_is_recorded_as_critical_manual_not_closed():
    status = risk_halt.exit_failure_status()

    assert status["level"] == "CRIT" and status["response"] == "MANUAL"
    assert status["trade_status"] != "CLOSED"
    assert status["loss_limited"] is False
