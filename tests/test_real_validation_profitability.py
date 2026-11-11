"""잠금 미래 검증의 절대 수익성 판정 (docs/REAL_VALIDATION_DESIGN.md §2.2).

입력은 전부 합성이다. 여기서 통과한 표본은 실제 PAPER 60거래일 검증이 아니다.
"""

from __future__ import annotations

import dataclasses

import pytest

from src.real_validation import evidence, profitability
from src.real_validation.core import Verdict
from src.real_validation.profitability import DayRecord
from src.real_validation.store import Store
from tests import real_validation_support as s

REQUIRED_FIELDS = [f.name for f in dataclasses.fields(profitability.ProfitabilitySpec)]


def _evaluate(spec, days, trades, *, as_of=None, locked_at=s.LOCKED_AT):
    return profitability.evaluate(
        spec, days, trades, as_of=as_of or str(spec.judgment_date), locked_at=locked_at
    )


# ── 사전등록값: 미정이면 판정하지 않는다 ──────────────────────────────


@pytest.mark.parametrize(
    "field", [name for name in REQUIRED_FIELDS if name != "multiple_testing_n"]
)
def test_any_undetermined_spec_value_blocks_evaluation(field):
    spec = s.spec(**{field: None})
    days, trades = s.sample(s.GOOD_PCTS)

    finding, data_hash = _evaluate(spec, days, trades, as_of="20991231")

    assert spec.problems(), field
    assert finding.verdict is Verdict.NOT_EVALUATED
    assert data_hash is None


def test_default_spec_is_entirely_undetermined():
    problems = profitability.ProfitabilitySpec().problems()

    assert "ACCOUNT_CAPITAL_UNDETERMINED" in problems
    assert "JUDGMENT_DATE_UNDETERMINED" in problems
    assert "SEED_UNDETERMINED" in problems
    assert "CI_SIDES_UNDETERMINED" in problems


def test_min_trade_days_cannot_be_registered_below_sixty():
    assert "MIN_TRADE_DAYS_BELOW_FLOOR" in s.spec(min_trade_days=59).problems()
    assert s.spec(min_trade_days=60).problems() == []


def test_zero_adverse_adjustment_is_not_a_stress_scenario():
    assert "ADVERSE_EXTRA_PCT_NOT_POSITIVE" in s.spec(adverse_extra_pct=0.0).problems()


def test_bonferroni_needs_the_number_of_tests_and_tightens_the_tail():
    assert "MULTIPLE_TESTING_N_UNDETERMINED" in s.spec(
        multiple_testing_method="BONFERRONI"
    ).problems()
    corrected = s.spec(multiple_testing_method="BONFERRONI", multiple_testing_n=5)

    assert corrected.problems() == []
    assert corrected.tail_alpha() == pytest.approx(0.005)
    assert s.spec().tail_alpha() == pytest.approx(0.025)
    assert s.spec(ci_sides="ONE_SIDED").tail_alpha() == pytest.approx(0.05)


def test_too_few_resamples_for_the_tail_is_rejected():
    assert "N_RESAMPLES_TOO_FEW_FOR_TAIL" in s.spec(n_resamples=39).problems()
    assert s.spec(n_resamples=40).problems() == []


def test_unknown_spec_key_is_rejected_instead_of_dropped():
    with pytest.raises(ValueError):
        profitability.ProfitabilitySpec.from_dict({"min_trade_dayz": 60})


# ── 고정 판정일 ──────────────────────────────────────────────────────


def test_before_the_judgment_date_no_statistic_is_computed():
    spec = s.spec()
    days, trades = s.sample(s.GOOD_PCTS[:30])

    finding, data_hash = _evaluate(spec, days, trades, as_of=days[-1].date)

    assert finding.verdict is Verdict.COLLECTING
    assert data_hash is None
    assert finding.detail["trade_days"] == 30
    assert "ci_lower_mean_net_pct" not in finding.detail
    assert "base" not in finding.detail


def test_fifty_nine_trade_days_on_the_judgment_date_is_insufficient():
    spec = s.spec()
    days, trades = s.sample(s.GOOD_PCTS)
    # 판정일 창은 60일인데 하루는 거래가 없었다.
    days[-1] = DayRecord(days[-1].date, no_trade_reason="NO_TARGET")
    trades = trades[:-1]

    finding, _ = _evaluate(spec, days, trades)

    assert finding.verdict is Verdict.INSUFFICIENT
    assert finding.reasons == ("TRADE_DAYS_BELOW_MINIMUM",)
    assert finding.detail["trade_days"] == 59
    assert "ci_lower_mean_net_pct" not in finding.detail


def test_sixty_trade_days_with_positive_expectancy_passes():
    spec = s.spec()
    days, trades = s.sample(s.GOOD_PCTS)

    finding, data_hash = _evaluate(spec, days, trades)

    assert finding.verdict is Verdict.PASS
    assert finding.reasons == ()
    assert finding.detail["trade_days"] == 60
    assert finding.detail["ci_lower_mean_net_pct"] > 0
    assert data_hash == finding.detail["data_hash"]


def test_result_is_reproducible_no_matter_when_it_is_run_after_the_judgment_date():
    spec = s.spec()
    days, trades = s.sample(s.GOOD_PCTS)
    on_time, hash_on_time = _evaluate(spec, days, trades)

    # 판정일 이후에 큰 손실이 이어졌어도 판정일 기준 결과는 바뀌지 않는다.
    later_days = s.weekdays(s.next_weekday(str(spec.judgment_date)), 5)
    days_later = days + [DayRecord(day) for day in later_days]
    trades_later = trades + [s.trade(day, -9.0) for day in later_days]
    late, hash_late = _evaluate(spec, days_later, trades_later, as_of=later_days[-1])

    assert late.verdict is on_time.verdict is Verdict.PASS
    assert hash_late == hash_on_time
    assert late.detail["ci_lower_mean_net_pct"] == on_time.detail["ci_lower_mean_net_pct"]
    assert late.detail["ignored_after_judgment"] == 10


def test_bootstrap_is_fixed_by_the_registered_seed():
    days, trades = s.sample(s.GOOD_PCTS)

    first, _ = _evaluate(s.spec(seed=7), days, trades)
    again, _ = _evaluate(s.spec(seed=7), days, trades)

    assert first.detail["ci_lower_mean_net_pct"] == again.detail["ci_lower_mean_net_pct"]


# ── 절대 수익성 조건 ─────────────────────────────────────────────────


def test_twenty_clean_exits_that_all_lost_are_not_profitable():
    """운영 안전성 표본(정상 청산 20회)이 전부 손실이어도 그 자체로는 통과할 수 없다."""
    spec = s.spec(judgment_days=20)
    days, trades = s.sample([-1.0] * 20)

    finding, _ = _evaluate(spec, days, trades)

    assert finding.verdict is Verdict.INSUFFICIENT


def test_sixty_losing_days_fail():
    spec = s.spec()
    days, trades = s.sample([-1.0] * 60)

    finding, _ = _evaluate(spec, days, trades)

    assert finding.verdict is Verdict.FAIL
    assert "CI_LOWER_NOT_POSITIVE" in finding.reasons
    assert "ACCOUNT_NET_NOT_POSITIVE" in finding.reasons
    assert "BASE_CONSECUTIVE_LOSSES_OVER_LIMIT" in finding.reasons


def test_zero_expectancy_is_not_positive():
    days, trades = s.sample([0.0] * 60)

    finding, _ = _evaluate(s.spec(), days, trades)

    assert finding.verdict is Verdict.FAIL
    assert "BASE_MEAN_NOT_POSITIVE" in finding.reasons
    assert "ACCOUNT_NET_NOT_POSITIVE" in finding.reasons


def test_profit_that_depends_on_the_single_best_trade_fails():
    pcts = [-0.05] * 59 + [40.0]
    days, trades = s.sample(pcts)

    finding, _ = _evaluate(s.spec(max_consecutive_losses=100), days, trades)

    assert finding.detail["base"]["mean_net_pct"] > 0
    assert finding.verdict is Verdict.FAIL
    assert "TOP1_REMOVED_MEAN_NOT_POSITIVE" in finding.reasons


def test_profit_that_depends_on_the_two_best_trades_fails():
    pcts = [-0.05] * 58 + [3.2, 3.0]
    days, trades = s.sample(pcts)

    finding, _ = _evaluate(s.spec(max_consecutive_losses=100), days, trades)

    assert finding.detail["top1_removed_mean_net_pct"] > 0
    assert finding.detail["top2_removed_mean_net_pct"] < 0
    assert "TOP2_REMOVED_MEAN_NOT_POSITIVE" in finding.reasons
    assert "TOP1_REMOVED_MEAN_NOT_POSITIVE" not in finding.reasons


def test_positive_base_that_turns_negative_under_adverse_cost_and_delay_fails():
    days, trades = s.sample([0.25, 0.15, 0.2] * 20)

    finding, _ = _evaluate(s.spec(adverse_extra_pct=0.25), days, trades)

    assert finding.detail["base"]["mean_net_pct"] > 0
    assert finding.detail["stress"]["mean_net_pct"] < 0
    assert finding.verdict is Verdict.FAIL
    assert "STRESS_MEAN_NOT_POSITIVE" in finding.reasons
    assert "BASE_MEAN_NOT_POSITIVE" not in finding.reasons


def test_stress_result_is_one_extra_adjustment_on_the_actual_net_not_two():
    days, trades = s.sample(s.GOOD_PCTS)

    finding, _ = _evaluate(s.spec(adverse_extra_pct=0.10), days, trades)

    base, stress = finding.detail["base"], finding.detail["stress"]
    assert base["additional_adverse_adjustment_pct"] == 0.0
    assert stress["additional_adverse_adjustment_pct"] == 0.10
    assert stress["mean_net_pct"] == pytest.approx(base["mean_net_pct"] - 0.10)
    assert stress["account_net_amount"] == pytest.approx(
        base["account_net_amount"] - 60 * 1_000_000 * 0.10 / 100
    )


def test_conservative_pnl_that_already_deducted_slippage_is_refused():
    days, trades = s.sample(s.GOOD_PCTS)
    trades[0] = dataclasses.replace(trades[0], pnl_basis="CONSERVATIVE_NET")

    finding, data_hash = _evaluate(s.spec(), days, trades)

    assert finding.verdict is Verdict.NOT_EVALUATED
    assert finding.reasons == ("PNL_BASIS_NOT_ACTUAL_FILL_NET",)
    assert data_hash is None


def test_risk_limits_pass_at_the_limit_and_fail_just_above():
    days, trades = s.sample([-0.5] + s.GOOD_PCTS[1:])
    loose, _ = _evaluate(s.spec(), days, trades)
    assert loose.verdict is Verdict.PASS
    stress_mdd = loose.detail["stress"]["account_mdd_pct"]
    stress_cvar = loose.detail["stress"]["cvar_loss_pct"]
    assert stress_mdd > loose.detail["base"]["account_mdd_pct"] > 0

    at_limit, _ = _evaluate(
        s.spec(max_account_mdd_pct=stress_mdd, max_cvar_loss_pct=stress_cvar), days, trades
    )
    mdd_over, _ = _evaluate(s.spec(max_account_mdd_pct=stress_mdd * 0.999), days, trades)
    cvar_over, _ = _evaluate(s.spec(max_cvar_loss_pct=stress_cvar * 0.999), days, trades)

    assert at_limit.verdict is Verdict.PASS
    assert mdd_over.reasons == ("STRESS_MDD_OVER_LIMIT",)
    assert cvar_over.reasons == ("STRESS_CVAR_OVER_LIMIT",)


def test_consecutive_loss_limit_boundary():
    pcts = [-0.3, -0.3, -0.3] + [1.2, 0.8, 0.9] * 19
    days, trades = s.sample(pcts)

    at_limit, _ = _evaluate(s.spec(max_consecutive_losses=3), days, trades)
    over, _ = _evaluate(s.spec(max_consecutive_losses=2), days, trades)

    assert at_limit.verdict is Verdict.PASS
    assert over.verdict is Verdict.FAIL
    assert "BASE_CONSECUTIVE_LOSSES_OVER_LIMIT" in over.reasons


# ── 계좌 집계에서 아무것도 빼지 않는다 ───────────────────────────────


def test_failed_and_manual_trades_stay_in_the_account_total_and_are_disclosed():
    days, trades = s.sample(s.GOOD_PCTS)
    # 같은 날 일어난 수동 청산 한 건이 그간의 수익을 모두 지웠다.
    trades.append(
        s.trade(days[10].date, -40.0, outcome="MANUAL_CLOSE", cause="EXIT_IMPOSSIBLE")
    )
    # 진입 실패로 포지션은 없었지만 비용만 나간 건.
    trades.append(
        s.trade(
            days[11].date, 0.0, invested=0.0, net_pnl_amount=-1_500.0, net_pnl_pct=None,
            outcome="FAILED", cause="ENTRY_FAIL",
        )
    )

    finding, _ = _evaluate(s.spec(), days, trades)

    assert finding.verdict is Verdict.FAIL
    assert "ACCOUNT_NET_NOT_POSITIVE" in finding.reasons
    assert finding.detail["by_outcome"]["MANUAL_CLOSE:EXIT_IMPOSSIBLE"] == {
        "trades": 1, "net_amount": -400_000.0,
    }
    assert finding.detail["by_outcome"]["FAILED:ENTRY_FAIL"]["net_amount"] == -1_500.0
    assert finding.detail["base"]["account_net_amount"] == pytest.approx(
        330_000 - 400_000 - 1_500
    )
    # 투입금이 없던 실패 건은 거래당 평균에는 들어가지 않는다.
    assert finding.detail["base"]["trades_in_mean"] == 61


def test_incomplete_price_path_does_not_remove_the_trade():
    days, trades = s.sample(s.GOOD_PCTS)
    trades[2] = dataclasses.replace(trades[2], path_complete=False)

    finding, _ = _evaluate(s.spec(), days, trades)

    assert finding.detail["path_incomplete_trades"] == 1
    assert finding.detail["base"]["trades_in_mean"] == 60


def test_unresolved_position_defers_the_verdict_without_ending_the_experiment():
    """손실이 확정되지 않은 포지션은 빼고 판정하지도, 최종 판정으로 태우지도 않는다."""
    days, trades = s.sample(s.GOOD_PCTS)
    trades[-1] = dataclasses.replace(
        trades[-1], resolved=False, net_pnl_amount=None, net_pnl_pct=None
    )

    finding, data_hash = _evaluate(s.spec(), days, trades)

    assert finding.verdict is Verdict.NOT_EVALUATED
    assert finding.reasons == ("UNRESOLVED_POSITION_OR_PNL",)
    assert finding.detail["unresolved_trades"] == 1
    assert data_hash is None
    assert "base" not in finding.detail


def test_unresolved_loss_is_counted_once_it_is_known(store):
    """보류 뒤 손실이 확정되면 그 손실을 넣어 판정한다 — 보류가 손실을 지우지 않는다."""
    spec = s.spec()
    profitability.register(store, spec, locked_at=s.LOCKED_AT)
    days, trades = s.sample(s.GOOD_PCTS)
    pending = list(trades)
    pending[-1] = dataclasses.replace(
        trades[-1], resolved=False, net_pnl_amount=None, net_pnl_pct=None
    )
    resolved = list(trades)
    resolved[-1] = s.trade(trades[-1].trade_date, -45.0, outcome="MANUAL_CLOSE", cause="HALTED")
    kwargs = {
        "as_of": str(spec.judgment_date),
        "evaluated_at": s.ts(s.next_weekday(str(spec.judgment_date))),
        "refs": s.refs(str(spec.experiment_id)),
    }

    deferred = profitability.judge(store, str(spec.experiment_id), days, pending, **kwargs)
    assert deferred.verdict is Verdict.NOT_EVALUATED
    assert store.evidence(evidence.KIND_PROFITABILITY) == []

    final = profitability.judge(store, str(spec.experiment_id), days, resolved, **kwargs)
    assert final.verdict is Verdict.FAIL
    assert "ACCOUNT_NET_NOT_POSITIVE" in final.reasons
    assert final.detail["by_outcome"]["MANUAL_CLOSE:HALTED"]["net_amount"] == -450_000.0


def test_pct_and_amount_on_different_bases_are_refused():
    """비용 차감 전 손익률과 차감 후 금액을 섞으면 평균만 부풀려진다."""
    days, trades = s.sample(s.GOOD_PCTS)
    trades[3] = dataclasses.replace(trades[3], net_pnl_pct=trades[3].net_pnl_pct + 0.18)

    finding, data_hash = _evaluate(s.spec(), days, trades)

    assert finding.verdict is Verdict.NOT_EVALUATED
    assert finding.reasons == ("PNL_PCT_AND_AMOUNT_INCONSISTENT",)
    assert finding.detail["inconsistent_trades"] == 1
    assert data_hash is None


def test_no_trade_days_need_a_reason_and_count_in_the_account_calendar():
    spec = s.spec(judgment_days=62)
    all_days = s.weekdays(s.START, 62)
    idle = {all_days[5], all_days[40]}
    traded = [day for day in all_days if day not in idle]
    trades = [s.trade(day, pct) for day, pct in zip(traded, s.GOOD_PCTS)]

    missing_reason = [DayRecord(day) for day in all_days]
    with_reason = [
        DayRecord(day, "NO_TARGET" if day in idle else None) for day in all_days
    ]
    blocked, _ = _evaluate(spec, missing_reason, trades)
    finding, _ = _evaluate(spec, with_reason, trades)

    # 입력 결함은 고쳐서 다시 판정할 수 있어야 한다 — 최종 판정이 아니다.
    assert blocked.verdict is Verdict.NOT_EVALUATED
    assert blocked.reasons == ("NO_TRADE_DAY_WITHOUT_REASON",)
    assert finding.verdict is Verdict.PASS
    assert finding.detail["calendar_days"] == 62
    assert finding.detail["trade_days"] == 60
    assert finding.detail["no_trade_days"] == {day: "NO_TARGET" for day in sorted(idle)}


def test_trade_on_a_day_missing_from_the_calendar_defers_the_verdict():
    days, trades = s.sample(s.GOOD_PCTS)

    finding, data_hash = _evaluate(s.spec(), days[:-1], trades)

    assert finding.verdict is Verdict.NOT_EVALUATED
    assert "TRADE_DATE_MISSING_FROM_CALENDAR" in finding.reasons
    assert data_hash is None


def test_no_trade_days_keep_their_place_in_the_bootstrap_calendar():
    """무거래일도 달력에 남아 블록 재표집과 계좌 일별 수익률에 들어간다."""
    spec = s.spec(judgment_days=90, block_length_days=5)
    all_days = s.weekdays(s.START, 90)
    traded = [day for i, day in enumerate(all_days) if i % 3 != 2]
    trades = [s.trade(day, pct) for day, pct in zip(traded, s.GOOD_PCTS)]
    full = [DayRecord(d, None if d in set(traded) else "NO_TARGET") for d in all_days]

    with_idle, _ = _evaluate(spec, full, trades)

    assert with_idle.detail["calendar_days"] == 90
    assert with_idle.detail["trade_days"] == 60
    assert with_idle.detail["empty_resamples"] == 0
    # 계좌 일별 수익률에는 0인 날 30개가 들어간다.
    assert with_idle.detail["base"]["account_net_amount"] == 330_000.0


# ── 거래가 없는 재표집 ───────────────────────────────────────────────


def test_empty_resamples_are_never_given_an_invented_value():
    """거래 없는 재표집은 0이 아니라 '모든 값보다 아래'다. 하한은 남은 값에서 나온다."""
    per_day = [[1.0]] + [[] for _ in range(9)]

    lower, empty = profitability._block_bootstrap_lower(
        per_day, block=1, resamples=2_000, seed=3, alpha=0.4
    )
    undefined, empty_again = profitability._block_bootstrap_lower(
        per_day, block=1, resamples=2_000, seed=3, alpha=0.3
    )

    # 10일 중 1일만 거래 → 재표집의 약 35%가 비어 있다.
    assert 600 < empty == empty_again < 800
    assert lower == 1.0
    # 하한 순위가 빈 재표집 구간에 들면 하한은 정해지지 않는다. 0이라고 하지 않는다.
    assert undefined is None


def test_dense_sample_has_no_empty_resample_and_reports_it():
    days, trades = s.sample(s.GOOD_PCTS)

    finding, _ = _evaluate(s.spec(), days, trades)

    assert finding.detail["empty_resamples"] == 0


def test_undefined_lower_bound_is_insufficient_not_a_pass_or_a_zero(monkeypatch):
    days, trades = s.sample(s.GOOD_PCTS)
    monkeypatch.setattr(profitability, "_block_bootstrap_lower", lambda *a, **k: (None, 399))

    finding, data_hash = _evaluate(s.spec(), days, trades)

    assert finding.verdict is Verdict.INSUFFICIENT
    assert finding.reasons == ("BOOTSTRAP_LOWER_BOUND_UNDEFINED",)
    assert finding.detail["empty_resamples"] == 399
    assert data_hash is not None


def test_regime_without_its_registered_sample_gets_no_eligibility():
    days, trades = s.sample(s.GOOD_PCTS)
    trades = [
        dataclasses.replace(t, regime="UP" if i < 50 else "DOWN") for i, t in enumerate(trades)
    ]

    finding, _ = _evaluate(
        s.spec(regime_min_trades={"UP": 30, "DOWN": 30, "HIGH_VOL": 10}), days, trades
    )

    regimes = finding.detail["regimes"]
    assert regimes["UP"]["trades"] == 50 and regimes["UP"]["sample_requirement_met"] is True
    assert regimes["DOWN"]["trades"] == 10 and regimes["DOWN"]["sample_requirement_met"] is False
    assert regimes["HIGH_VOL"]["trades"] == 0
    assert regimes["HIGH_VOL"]["sample_requirement_met"] is False


# ── 미래정보 누출 ────────────────────────────────────────────────────


def test_data_from_before_the_collection_start_is_a_leak():
    days, trades = s.sample(s.GOOD_PCTS)
    trades.append(s.trade("20261230", 5.0))

    finding, data_hash = _evaluate(s.spec(), days, trades)

    assert finding.verdict is Verdict.NOT_EVALUATED
    assert finding.reasons == ("LOOKAHEAD_DATA_BEFORE_COLLECTION_START",)
    assert data_hash is None


@pytest.mark.parametrize("locked_at", [s.ts(s.START, "08:00"), s.ts("20270301")])
def test_spec_locked_on_or_after_the_start_date_is_a_leak(locked_at):
    days, trades = s.sample(s.GOOD_PCTS)

    finding, _ = _evaluate(s.spec(), days, trades, locked_at=locked_at)

    assert finding.verdict is Verdict.NOT_EVALUATED
    assert finding.reasons == ("LOOKAHEAD_SPEC_LOCKED_AFTER_COLLECTION_START",)


# ── 잠금과 단일 판정 ─────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "gate.db", create=True) as opened:
        yield opened


def test_register_refuses_incomplete_spec_and_late_lock(store):
    with pytest.raises(ValueError):
        profitability.register(store, s.spec(account_capital=None), locked_at=s.LOCKED_AT)
    with pytest.raises(ValueError):
        profitability.register(store, s.spec(), locked_at=s.ts(s.START, "08:00"))
    assert store.registrations(profitability.REGISTRATION_KIND) == []


def test_locked_spec_cannot_be_relaxed_after_the_fact(store):
    first = profitability.register(store, s.spec(), locked_at=s.LOCKED_AT)

    assert profitability.register(store, s.spec(), locked_at=s.LOCKED_AT) == first
    with pytest.raises(ValueError):
        profitability.register(store, s.spec(max_account_mdd_pct=50.0), locked_at=s.LOCKED_AT)
    assert store.registration("real-future-synthetic-1")["spec"]["max_account_mdd_pct"] == 5.0


def test_judge_records_one_final_verdict_and_refuses_a_second_look(store):
    spec = s.spec()
    profitability.register(store, spec, locked_at=s.LOCKED_AT)
    days, trades = s.sample([-1.0] * 60)
    kwargs = {
        "as_of": str(spec.judgment_date),
        "evaluated_at": s.ts(str(spec.judgment_date)),
        "refs": s.refs(str(spec.experiment_id)),
    }

    finding = profitability.judge(store, str(spec.experiment_id), days, trades, **kwargs)
    assert finding.verdict is Verdict.FAIL

    better_days, better_trades = s.sample(s.GOOD_PCTS)
    with pytest.raises(evidence.DuplicateVerdictError):
        profitability.judge(
            store, str(spec.experiment_id), better_days, better_trades, **kwargs
        )
    rows = store.evidence(evidence.KIND_PROFITABILITY)
    assert [row["verdict"] for row in rows] == ["FAIL"]
    assert rows[0]["expires_at"] is not None


def test_judge_does_not_record_while_collecting(store):
    spec = s.spec()
    profitability.register(store, spec, locked_at=s.LOCKED_AT)
    days, trades = s.sample(s.GOOD_PCTS[:10])

    finding = profitability.judge(
        store,
        str(spec.experiment_id),
        days,
        trades,
        as_of=days[-1].date,
        evaluated_at=s.ts(days[-1].date),
        refs=s.refs(str(spec.experiment_id)),
    )

    assert finding.verdict is Verdict.COLLECTING
    assert store.evidence(evidence.KIND_PROFITABILITY) == []


def test_judge_refuses_a_sample_from_another_fingerprint_or_unregistered_experiment(store):
    spec = s.spec()
    profitability.register(store, spec, locked_at=s.LOCKED_AT)
    days, trades = s.sample(s.GOOD_PCTS)
    common = {"as_of": str(spec.judgment_date), "evaluated_at": s.ts(str(spec.judgment_date))}

    other = profitability.judge(
        store, str(spec.experiment_id), days, trades,
        refs=s.refs(str(spec.experiment_id), fingerprint="another-fingerprint"), **common,
    )
    unknown = profitability.judge(
        store, "never-registered", days, trades, refs=s.refs("never-registered"), **common
    )

    assert other.reasons == ("REFS_DIFFER_FROM_LOCKED_SPEC_STRATEGY_FINGERPRINT",)
    assert unknown.reasons == ("EXPERIMENT_NOT_REGISTERED",)
    assert store.evidence(evidence.KIND_PROFITABILITY) == []


def test_judge_refuses_refs_that_do_not_name_the_locked_spec_or_current_rule_code(store):
    spec = s.spec()
    profitability.register(store, spec, locked_at=s.LOCKED_AT)
    days, trades = s.sample(s.GOOD_PCTS)
    good = s.refs(str(spec.experiment_id))
    common = {"as_of": str(spec.judgment_date), "evaluated_at": s.ts(str(spec.judgment_date))}

    wrong_spec = profitability.judge(
        store, str(spec.experiment_id), days, trades,
        refs=dataclasses.replace(good, spec_hash="0" * 64), **common,
    )
    wrong_code = profitability.judge(
        store, str(spec.experiment_id), days, trades,
        refs=dataclasses.replace(good, rules_code_hash="0" * 64), **common,
    )

    assert wrong_spec.reasons == ("REFS_DIFFER_FROM_LOCKED_SPEC_SPEC_HASH",)
    assert wrong_code.reasons == ("REFS_DIFFER_FROM_LOCKED_SPEC_RULES_CODE_HASH",)
    assert store.evidence(evidence.KIND_PROFITABILITY) == []


def test_final_verdict_cannot_be_dated_before_the_judgment_date(store):
    spec = s.spec()
    profitability.register(store, spec, locked_at=s.LOCKED_AT)
    days, trades = s.sample(s.GOOD_PCTS)

    with pytest.raises(ValueError):
        profitability.judge(
            store, str(spec.experiment_id), days, trades,
            as_of=str(spec.judgment_date), evaluated_at=s.ts(s.START),
            refs=s.refs(str(spec.experiment_id)),
        )
    assert store.evidence(evidence.KIND_PROFITABILITY) == []


# ── 판정 코드가 바뀐 뒤의 재판정 ─────────────────────────────────────


def _judged(store, pcts):
    spec = s.spec()
    profitability.register(store, spec, locked_at=s.LOCKED_AT)
    days, trades = s.sample(pcts)
    kwargs = {
        "as_of": str(spec.judgment_date),
        "evaluated_at": s.ts(str(spec.judgment_date)),
    }
    profitability.judge(
        store, str(spec.experiment_id), days, trades,
        refs=s.refs(str(spec.experiment_id)), **kwargs,
    )
    return spec, days, trades, kwargs


def _after_rule_change(monkeypatch):
    """판정 파일이 바뀐 상황을 만든다(파일을 건드리지 않고 해시만 바꾼다)."""
    monkeypatch.setattr(evidence, "rules_code_hash", lambda kind: f"changed-{kind}")


def test_rule_code_change_allows_one_recorded_rejudge_on_identical_data(store, monkeypatch):
    spec, days, trades, kwargs = _judged(store, s.GOOD_PCTS)
    _after_rule_change(monkeypatch)
    new_refs = s.refs(str(spec.experiment_id))
    assert new_refs.rules_code_hash == "changed-PROFITABILITY"

    with pytest.raises(evidence.DuplicateVerdictError):
        profitability.judge(store, str(spec.experiment_id), days, trades, refs=new_refs, **kwargs)
    finding = profitability.judge(
        store, str(spec.experiment_id), days, trades, refs=new_refs,
        rerun_reason="bootstrap empty-resample handling fixed", **kwargs,
    )

    rows = store.evidence(evidence.KIND_PROFITABILITY)
    assert finding.verdict is Verdict.PASS
    assert [row["verdict"] for row in rows] == ["PASS", "PASS"]
    assert rows[1]["detail"]["rejudged_from"] == rows[0]["id"]
    assert rows[1]["detail"]["previous_verdict"] == "PASS"
    assert rows[1]["detail"]["rerun_reason"] == "bootstrap empty-resample handling fixed"
    assert rows[1]["data_hash"] == rows[0]["data_hash"]
    # 같은 코드로 또 돌릴 수는 없다.
    with pytest.raises(evidence.DuplicateVerdictError):
        profitability.judge(
            store, str(spec.experiment_id), days, trades, refs=new_refs,
            rerun_reason="again", **kwargs,
        )


def test_rejudge_cannot_swap_in_better_data(store, monkeypatch):
    spec, _days, _trades, kwargs = _judged(store, [-1.0] * 60)
    _after_rule_change(monkeypatch)
    better_days, better_trades = s.sample(s.GOOD_PCTS)

    with pytest.raises(evidence.DuplicateVerdictError):
        profitability.judge(
            store, str(spec.experiment_id), better_days, better_trades,
            refs=s.refs(str(spec.experiment_id)), rerun_reason="rule change", **kwargs,
        )
    assert [r["verdict"] for r in store.evidence(evidence.KIND_PROFITABILITY)] == ["FAIL"]


def test_same_rule_code_cannot_rejudge_even_with_a_reason(store):
    spec, days, trades, kwargs = _judged(store, [-1.0] * 60)

    with pytest.raises(evidence.DuplicateVerdictError):
        profitability.judge(
            store, str(spec.experiment_id), days, trades,
            refs=s.refs(str(spec.experiment_id)), rerun_reason="I would like a pass", **kwargs,
        )


# ── 저장된 입력으로 다시 계산해 대조 ─────────────────────────────────


def test_audit_recomputes_the_recorded_verdict_from_its_stored_inputs(store):
    _judged(store, s.GOOD_PCTS)
    row = store.evidence(evidence.KIND_PROFITABILITY)[0]

    finding = profitability.audit(store, row["id"])

    assert len(row["inputs"]["days"]) == 60 and len(row["inputs"]["trades"]) == 60
    assert finding.verdict is Verdict.PASS
    assert finding.detail == {"recorded": "PASS", "recomputed": "PASS"}
    assert profitability.audit(store, 999).reasons == ("EVIDENCE_NOT_FOUND",)


def test_audit_cannot_vouch_for_a_verdict_made_by_other_rule_code(store, monkeypatch):
    _judged(store, s.GOOD_PCTS)
    row = store.evidence(evidence.KIND_PROFITABILITY)[0]
    _after_rule_change(monkeypatch)

    assert profitability.audit(store, row["id"]).reasons == ("RULES_CODE_CHANGED_SINCE_VERDICT",)
