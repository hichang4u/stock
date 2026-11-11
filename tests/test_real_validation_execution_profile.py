"""실행 프로필 계약·동일 입력 재생·환경 차이 판정 (docs/REAL_VALIDATION_DESIGN.md §3).

지연 표본과 점검 결과는 합성이다. 실제 서버 실측이나 스모크를 수행한 것이 아니다.
"""

from __future__ import annotations

import dataclasses

import pytest

from src.real_validation import evidence, execution_profile
from src.real_validation.core import Verdict
from src.real_validation.execution_profile import (
    MODES,
    STAGES,
    ExecutionProfile,
    OrderIntent,
    ParityContract,
    ParityObservations,
)
from src.real_validation.store import Store
from tests import real_validation_support as s

OBSERVED_FROM = s.ts("20270104", "09:00")


# ── 프로필 ───────────────────────────────────────────────────────────


def test_undecided_route_makes_the_profile_invalid_and_gives_it_no_id():
    undecided = s.profile(route=None)

    assert undecided.problems() == ["ROUTE_UNDETERMINED"]
    assert undecided.profile_id() is None
    assert s.profile(route="HYBRID").profile_id() is None


def test_empty_profile_lists_every_contract_field_as_undetermined():
    problems = ExecutionProfile.from_dict({}).problems()

    expected = 1 + sum(len(names) for names in execution_profile.REQUIRED_FIELDS.values())
    assert len(problems) == expected
    assert "ENTRY_QUOTE_FRESHNESS_UNDETERMINED" in problems
    assert "ORDERS_RESPONSE_LOSS_RECONCILE_POLICY_UNDETERMINED" in problems
    assert "CALLS_PER_MODE_CALL_LIMITS_UNDETERMINED" in problems


def test_route_or_any_policy_change_gives_a_different_profile_id():
    legacy = s.profile("LEGACY")
    fast = s.profile("FAST")
    stricter = s.profile("LEGACY", entry={"quote_freshness": "500ms"})

    assert legacy.profile_id() == s.profile("LEGACY").profile_id()
    assert len({legacy.profile_id(), fast.profile_id(), stricter.profile_id()}) == 3
    assert str(legacy.profile_id()).startswith("xp-")


def test_call_limits_must_be_stated_for_both_modes():
    one_sided = s.profile(calls={"per_mode_call_limits": {"PAPER": "1/s"}})

    assert one_sided.problems() == ["CALLS_PER_MODE_CALL_LIMITS_NEEDS_BOTH_MODES"]


def test_unknown_profile_keys_are_rejected_or_reported():
    with pytest.raises(ValueError):
        ExecutionProfile.from_dict({"route": "LEGACY", "extras": {}})
    assert "ENTRY_UNKNOWN_FIELD" in s.profile(entry={"surprise": 1}).problems()


# ── 동일 입력 재생 ───────────────────────────────────────────────────


def _intent(input_id, **overrides):
    values = {
        "input_id": input_id,
        "candidate": "005930",
        "entry_allowed": True,
        "block_reason": None,
        "order_price_cap": 10_030.0,
        "qty": 132,
    }
    values.update(overrides)
    return OrderIntent(**values)


def test_identical_intents_have_no_mismatch():
    intents = [_intent("a"), _intent("b", entry_allowed=False, block_reason="QUOTE_STALE", qty=0)]

    assert execution_profile.compare_intents(intents, list(intents)) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate", "000660"),
        ("entry_allowed", False),
        ("block_reason", "VI_ACTIVE"),
        ("order_price_cap", 10_040.0),
        ("qty", 131),
    ],
)
def test_any_difference_in_the_order_intent_is_a_mismatch(field, value):
    paper = [_intent("a")]
    real = [dataclasses.replace(paper[0], **{field: value})]

    mismatches = execution_profile.compare_intents(paper, real)

    assert mismatches == [
        {"input_id": "a", "field": field, "paper": getattr(paper[0], field), "real": value}
    ]


def test_input_decided_in_only_one_mode_is_a_mismatch():
    mismatches = execution_profile.compare_intents([_intent("a"), _intent("b")], [_intent("a")])

    assert mismatches == [{"input_id": "b", "field": "missing", "paper": True, "real": False}]
    with pytest.raises(ValueError):
        execution_profile.compare_intents([_intent("a"), _intent("a")], [_intent("a")])


INPUTS = [
    {"input_id": "t1", "ticker": "005930", "ask": 10_037.0, "quote_age_ms": 300},
    {"input_id": "t2", "ticker": "000660", "ask": 20_512.0, "quote_age_ms": 900},
    {"input_id": "t3", "ticker": "035420", "ask": 30_190.0, "quote_age_ms": 1_600},
]
ADAPTERS = {
    "PAPER": {"buy_tr": "VTTC0012U", "quote_max_age_ms": 1_500},
    "REAL": {"buy_tr": "TTTC0012U", "quote_max_age_ms": 500},
}


def _shared_decision(item, _adapter):
    """공통 판단 로직: 어댑터 값을 판단에 쓰지 않는다."""
    fresh = item["quote_age_ms"] <= 500
    return OrderIntent(
        input_id=item["input_id"],
        candidate=item["ticker"],
        entry_allowed=fresh,
        block_reason=None if fresh else "QUOTE_STALE",
        order_price_cap=float(int(item["ask"] // 10) * 10),
        qty=10 if fresh else 0,
    )


def _mode_dependent_decision(item, adapter):
    """어댑터의 모드별 신선도 한도가 판단에 새어 들어간 경우."""
    fresh = item["quote_age_ms"] <= adapter["quote_max_age_ms"]
    return dataclasses.replace(
        _shared_decision(item, adapter),
        entry_allowed=fresh,
        block_reason=None if fresh else "QUOTE_STALE",
        qty=10 if fresh else 0,
    )


def test_replay_of_shared_logic_matches_across_mode_adapters():
    report = execution_profile.replay(INPUTS, _shared_decision, ADAPTERS)

    assert report["matched"] is True
    assert report["inputs"] == 3
    assert report["inputs_hash"] == execution_profile.replay(
        INPUTS, _shared_decision, ADAPTERS
    )["inputs_hash"]


def test_replay_exposes_a_decision_that_depends_on_the_mode():
    report = execution_profile.replay(INPUTS, _mode_dependent_decision, ADAPTERS)

    assert report["matched"] is False
    assert {(m["input_id"], m["field"]) for m in report["mismatches"]} == {
        ("t2", "entry_allowed"), ("t2", "block_reason"), ("t2", "qty"),
    }


def test_replay_needs_both_mode_adapters():
    with pytest.raises(ValueError):
        execution_profile.replay(INPUTS, _shared_decision, {"PAPER": {}})


# ── 지연 요약 ────────────────────────────────────────────────────────


def test_latency_summary_reports_count_and_nearest_rank_percentiles():
    summary = execution_profile.latency_summary(range(1, 101))

    assert summary == {"n": 100, "p50": 50.0, "p95": 95.0, "max": 100.0}
    assert execution_profile.latency_summary([]) == {
        "n": 0, "p50": None, "p95": None, "max": None,
    }
    assert execution_profile.latency_summary([7])["p95"] == 7.0


# ── 환경 차이 판정 ───────────────────────────────────────────────────


def _latency(real_p95_extra=0.0, n=5):
    paper = [100.0] * n
    real = [100.0] * (n - 1) + [100.0 + real_p95_extra]
    return {
        "PAPER": {stage: list(paper) for stage in STAGES},
        "REAL": {stage: list(real) for stage in STAGES},
    }


def _observed(**overrides):
    values = {
        "observation_started_at": OBSERVED_FROM,
        "replay": execution_profile.replay(INPUTS, _shared_decision, ADAPTERS),
        "latency_ms": _latency(),
        "readonly_check_passed": True,
        "smoke_reconciled": True,
        "profitability_with_real_latency_passed": True,
    }
    values.update(overrides)
    return ParityObservations(**values)


def _parity(observed=None, *, contract=None, profile=None):
    return execution_profile.evaluate_parity(
        profile or s.profile(), contract or s.contract(), observed or _observed()
    )


def test_complete_matching_observations_pass():
    finding = _parity()

    assert finding.verdict is Verdict.PASS
    for mode in MODES:
        for stage in STAGES:
            assert finding.detail["latency_ms"][mode][stage]["n"] == 5


def test_empty_contract_is_not_evaluated():
    finding = _parity(contract=ParityContract())

    assert finding.verdict is Verdict.NOT_EVALUATED
    assert "CONTRACT_MAX_P95_EXCESS_MS_UNDETERMINED" in finding.reasons
    assert "CONTRACT_MIN_LATENCY_SAMPLES_UNDETERMINED" in finding.reasons
    assert "CONTRACT_REVALIDATION_DAYS_UNDETERMINED" in finding.reasons


def test_undecided_route_is_not_evaluated():
    finding = _parity(profile=s.profile(route=None))

    assert finding.verdict is Verdict.NOT_EVALUATED
    assert "PROFILE_ROUTE_UNDETERMINED" in finding.reasons


def test_contract_for_another_route_does_not_apply():
    finding = _parity(profile=s.profile("FAST"))

    assert finding.reasons == ("CONTRACT_BOUND_TO_OTHER_PROFILE",)


def test_tolerances_registered_after_observation_started_do_not_count():
    late = s.contract(registered_at=OBSERVED_FROM)

    assert _parity(contract=late).reasons == ("CONTRACT_REGISTERED_AFTER_OBSERVATION_START",)
    assert _parity(_observed(observation_started_at=None)).reasons == (
        "OBSERVATION_START_UNDETERMINED",
    )


def test_intent_mismatch_fails():
    mismatched = execution_profile.replay(INPUTS, _mode_dependent_decision, ADAPTERS)

    finding = _parity(_observed(replay=mismatched))

    assert finding.verdict is Verdict.FAIL
    assert "REPLAY_INTENT_MISMATCH" in finding.reasons


def test_missing_or_small_samples_are_insufficient():
    no_replay = _parity(_observed(replay=None))
    few_inputs = _parity(contract=s.contract(min_replay_inputs=4))
    few_latency = _parity(_observed(latency_ms=_latency(n=4)))
    one_mode = _parity(_observed(latency_ms={"PAPER": _latency()["PAPER"]}))

    assert no_replay.verdict is Verdict.INSUFFICIENT and no_replay.reasons == ("REPLAY_NOT_RUN",)
    assert few_inputs.reasons == ("REPLAY_INPUTS_BELOW_MINIMUM",)
    assert few_latency.verdict is Verdict.INSUFFICIENT
    assert set(few_latency.reasons) == {
        f"LATENCY_SAMPLES_BELOW_MINIMUM_{stage.upper()}" for stage in STAGES
    }
    assert one_mode.verdict is Verdict.INSUFFICIENT


def test_latency_excess_passes_at_the_tolerance_and_fails_above_it():
    at_tolerance = _parity(_observed(latency_ms=_latency(real_p95_extra=200.0)))
    above = _parity(_observed(latency_ms=_latency(real_p95_extra=200.5)))

    assert at_tolerance.verdict is Verdict.PASS
    assert above.verdict is Verdict.FAIL
    assert set(above.reasons) == {f"LATENCY_P95_EXCESS_{stage.upper()}" for stage in STAGES}


@pytest.mark.parametrize(
    ("field", "label"),
    [
        ("readonly_check_passed", "READONLY_CHECK"),
        ("smoke_reconciled", "SMOKE_RECONCILE"),
        ("profitability_with_real_latency_passed", "PROFITABILITY_WITH_REAL_LATENCY"),
    ],
)
def test_checks_not_done_are_insufficient_and_failed_checks_fail(field, label):
    not_done = _parity(_observed(**{field: None}))
    failed = _parity(_observed(**{field: False}))

    assert not_done.verdict is Verdict.INSUFFICIENT and not_done.reasons == (f"{label}_NOT_DONE",)
    assert failed.verdict is Verdict.FAIL and failed.reasons == (f"{label}_FAILED",)


def test_unexplained_difference_fails():
    finding = _parity(_observed(unexplained_differences=["REAL fills 40% less often"]))

    assert finding.verdict is Verdict.FAIL
    assert finding.reasons == ("UNEXPLAINED_MODE_DIFFERENCE",)


# ── 계약 잠금과 증거 기록 ────────────────────────────────────────────


def test_parity_verdict_is_recorded_with_the_revalidation_period_as_expiry(tmp_path):
    current = s.profile()
    with Store.open(tmp_path / "gate.db", create=True) as store:
        experiment_id = execution_profile.register_contract(store, s.contract())
        assert execution_profile.register_contract(store, s.contract()) == experiment_id
        with pytest.raises(ValueError):
            execution_profile.register_contract(store, ParityContract())

        finding = execution_profile.judge_parity(
            store, current, experiment_id, _observed(),
            refs=s.refs(experiment_id), evaluated_at=s.ts("20270201"),
        )
        rows = store.evidence(evidence.KIND_EXECUTION_PARITY)

        assert finding.verdict is Verdict.PASS
        assert [row["verdict"] for row in rows] == ["PASS"]
        assert rows[0]["expires_at"] == "2027-03-03T16:00:00+09:00"
        with pytest.raises(evidence.DuplicateVerdictError):
            execution_profile.judge_parity(
                store, current, experiment_id, _observed(),
                refs=s.refs(experiment_id), evaluated_at=s.ts("20270202"),
            )


def test_parity_is_not_judged_for_an_unregistered_contract_or_another_route(tmp_path):
    with Store.open(tmp_path / "gate.db", create=True) as store:
        experiment_id = execution_profile.register_contract(store, s.contract())

        unregistered = execution_profile.judge_parity(
            store, s.profile(), "parity-unknown", _observed(),
            refs=s.refs("parity-unknown"), evaluated_at=s.ts("20270201"),
        )
        other_route = execution_profile.judge_parity(
            store, s.profile("FAST"), experiment_id, _observed(),
            refs=s.refs(experiment_id), evaluated_at=s.ts("20270201"),
        )

        assert unregistered.reasons == ("PARITY_CONTRACT_NOT_REGISTERED",)
        assert other_route.reasons == ("REFS_DIFFER_FROM_REGISTERED_CONTRACT",)
        assert store.evidence(evidence.KIND_EXECUTION_PARITY) == []
