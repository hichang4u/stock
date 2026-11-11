"""REAL 신규 진입 AND 게이트와 증거의 버전·유효기간 (docs/REAL_VALIDATION_DESIGN.md §2.1, §5).

통과 상태는 합성 디렉터리로만 만든다. 실제 승인 증거는 존재하지 않는다.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from src.real_validation import evidence, gate, risk_halt
from src.real_validation.core import Finding, Verdict
from src.real_validation.risk_halt import Action, HaltKind
from src.real_validation.store import Store
from tests import real_validation_support as s

PASS = Finding(Verdict.PASS)
NON_PASS = [v for v in Verdict if v is not Verdict.PASS]


def _all_pass(**overrides):
    findings = {name: PASS for name in gate.COMPONENTS}
    findings.update(overrides)
    return gate.decide(**findings)


# ── 순수 AND ─────────────────────────────────────────────────────────


def test_every_component_passing_is_the_only_way_to_be_eligible():
    decision = _all_pass()

    assert decision.eligible_for_real is True
    assert decision.blockers() == []


@pytest.mark.parametrize("component", gate.COMPONENTS)
@pytest.mark.parametrize("verdict", NON_PASS)
def test_one_non_passing_component_blocks_whatever_the_others_say(component, verdict):
    decision = _all_pass(**{component: Finding(verdict, ("WHY",))})

    assert decision.eligible_for_real is False
    assert decision.blockers() == [f"{component}={verdict.value}: WHY"]


def test_gate_names_the_six_conditions_of_the_design():
    assert gate.COMPONENTS == (
        "operational_ready",
        "profitability_passed",
        "execution_parity_passed",
        "risk_policy_valid",
        "evidence_current",
        "risk_not_halted",
    )


def test_a_blocked_gate_stops_buys_but_never_protective_exits():
    blocked = _all_pass(risk_not_halted=Finding(Verdict.FAIL, ("RISK_HALTED_DAILY_LOSS",)))

    assert gate.permits(blocked, Action.NEW_BUY) is False
    assert gate.permits(blocked, Action.ADD_BUY) is False
    assert gate.permits(blocked, Action.PROTECTIVE_SELL) is True
    assert gate.permits(blocked, Action.CANCEL) is True
    assert gate.permits(blocked, Action.RECONCILE) is True
    assert gate.permits(_all_pass(), Action.NEW_BUY) is True


# ── 증거의 현재성 ────────────────────────────────────────────────────


def _row(expected, **overrides):
    row = {
        "id": 1,
        "verdict": "PASS",
        "refs": expected.as_dict(),
        "data_hash": "d",
        "evaluated_at": s.ts("20270401"),
        "expires_at": s.ts("20270501"),
        "reasons": [],
    }
    row.update(overrides)
    return row


def _now(day="20270410", hhmm="09:00"):
    return datetime.fromisoformat(s.ts(day, hhmm))


def test_matching_unexpired_evidence_is_current():
    expected = s.refs("exp-1")

    assert evidence.check_current(_row(expected), expected, _now()).verdict is Verdict.PASS


def test_missing_evidence_is_not_evaluated():
    finding = evidence.check_current(None, s.refs("exp-1"), _now())

    assert finding.verdict is Verdict.NOT_EVALUATED
    assert evidence.verdict_of(None).verdict is Verdict.NOT_EVALUATED


@pytest.mark.parametrize("field", [f.name for f in dataclasses.fields(evidence.EvidenceRefs)])
def test_any_version_that_differs_from_the_current_one_makes_evidence_stale(field):
    expected = s.refs("exp-1")
    recorded = dataclasses.replace(expected, **{field: "something-older"})

    finding = evidence.check_current(_row(recorded), expected, _now())

    assert finding.verdict is Verdict.FAIL
    assert finding.reasons == (f"STALE_{field.upper()}",)


@pytest.mark.parametrize("field", [f.name for f in dataclasses.fields(evidence.EvidenceRefs)])
def test_an_undetermined_current_version_matches_nothing(field):
    recorded = s.refs("exp-1")
    expected = dataclasses.replace(recorded, **{field: None})

    finding = evidence.check_current(_row(recorded), expected, _now())

    assert finding.verdict is Verdict.NOT_EVALUATED
    assert finding.reasons == (f"CURRENT_{field.upper()}_UNDETERMINED",)


def test_evidence_expires_exactly_at_its_expiry_time():
    expected = s.refs("exp-1")
    row = _row(expected)

    just_before = _now("20270501", "15:59")
    at_expiry = _now("20270501", "16:00")

    assert evidence.check_current(row, expected, just_before).verdict is Verdict.PASS
    assert evidence.check_current(row, expected, at_expiry).reasons == ("EVIDENCE_EXPIRED",)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"expires_at": None}, "EXPIRY_UNDETERMINED"),
        ({"evaluated_at": "2027-04-20T16:00:00+09:00"}, "EVALUATED_AT_IN_FUTURE"),
        ({"evaluated_at": "2027-04-01T16:00:00"}, "EVALUATED_AT_INVALID"),
        ({"data_hash": None}, "DATA_HASH_MISSING"),
    ],
)
def test_evidence_without_a_usable_validity_window_is_not_current(overrides, reason):
    expected = s.refs("exp-1")

    finding = evidence.check_current(_row(expected, **overrides), expected, _now())

    assert finding.verdict is Verdict.FAIL
    assert reason in finding.reasons


def test_now_must_carry_an_offset():
    with pytest.raises(ValueError):
        evidence.check_current(None, s.refs("exp-1"), datetime(2027, 4, 10, 9, 0))


def test_pass_evidence_cannot_be_recorded_without_references_expiry_or_data(tmp_path):
    incomplete = dataclasses.replace(s.refs("exp-1"), risk_policy_hash=None)
    common = {
        "kind": evidence.KIND_PROFITABILITY,
        "finding": PASS,
        "evaluated_at": s.ts("20270401"),
    }
    with Store.open(tmp_path / "gate.db", create=True) as store:
        with pytest.raises(ValueError):
            evidence.record(
                store, refs=incomplete, data_hash="d", expires_at=s.ts("20270501"), **common
            )
        with pytest.raises(ValueError):
            evidence.record(store, refs=s.refs("exp-1"), data_hash="d", expires_at=None, **common)
        with pytest.raises(ValueError):
            evidence.record(
                store, refs=s.refs("exp-1"), data_hash=None, expires_at=s.ts("20270501"), **common
            )
        with pytest.raises(ValueError):
            evidence.record(
                store, refs=s.refs("exp-1"), data_hash="d", expires_at=s.ts("20270501"),
                kind="SOMETHING_ELSE", finding=PASS, evaluated_at=s.ts("20270401"),
            )
        assert store.evidence(evidence.KIND_PROFITABILITY) == []


def test_recorded_evidence_is_never_overwritten(tmp_path):
    path = tmp_path / "gate.db"
    s.passing_directory(tmp_path)

    conn = sqlite3.connect(str(path))
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE evidence SET verdict='PASS'")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM evidence")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE registrations SET spec_json='{}'")
    finally:
        conn.close()


# ── 디렉터리에서 읽는 게이트 ─────────────────────────────────────────


def _gate(directory, *, now, fingerprint=s.FINGERPRINT, operational=s.OPERATIONAL_PASS):
    env = {} if directory is None else {gate.ENV_DIR: str(directory)}
    return gate.evaluate_from_environment(
        strategy_fingerprint=fingerprint, operational_ready=operational, now=now, env=env
    )


@pytest.fixture
def wired(monkeypatch):
    """주문 직전 집행이 연결된 미래 릴리스를 가정한다. 현재 코드는 False다."""
    monkeypatch.setattr(gate, "PRE_BUY_ENFORCEMENT_WIRED", True)


@pytest.fixture
def passing(tmp_path):
    directory = tmp_path / "real_validation"
    evaluated_at = s.passing_directory(directory)
    return directory, datetime.fromisoformat(evaluated_at) + timedelta(hours=1)


def test_without_a_configured_directory_everything_is_not_evaluated_and_nothing_is_read():
    decision = _gate(None, now=_now())

    assert decision.eligible_for_real is False
    for name in gate.COMPONENTS[1:]:
        assert decision.components[name].verdict is Verdict.NOT_EVALUATED
        assert decision.components[name].reasons == ("REAL_VALIDATION_DIR_UNSET",)


def test_empty_directory_blocks_every_condition_and_creates_no_store(tmp_path):
    decision = _gate(tmp_path, now=_now())

    assert decision.eligible_for_real is False
    assert decision.components["risk_policy_valid"].reasons == ("RISK_POLICY_FILE_MISSING",)
    assert decision.components["profitability_passed"].reasons == ("EVIDENCE_STORE_UNAVAILABLE",)
    assert decision.components["risk_not_halted"].reasons == ("HALT_STATE_UNREADABLE",)
    assert list(tmp_path.iterdir()) == []


def test_runtime_enforcement_is_not_wired_in_the_current_code():
    assert gate.PRE_BUY_ENFORCEMENT_WIRED is False


def test_complete_passing_evidence_still_blocks_while_pre_buy_enforcement_is_unwired(passing):
    directory, now = passing

    decision = _gate(directory, now=now)

    assert decision.eligible_for_real is False
    assert decision.blockers() == [
        "risk_policy_valid=NOT_EVALUATED: RUNTIME_PRE_BUY_ENFORCEMENT_NOT_WIRED"
    ]


def test_all_six_conditions_passing_opens_the_gate(passing, wired):
    directory, now = passing

    decision = _gate(directory, now=now)

    assert decision.blockers() == []
    assert decision.eligible_for_real is True
    assert decision.as_dict()["components"]["evidence_current"]["passed"] is True


def test_operational_failure_alone_blocks_a_fully_evidenced_gate(passing, wired):
    directory, now = passing

    decision = _gate(
        directory, now=now, operational=Finding(Verdict.FAIL, ("PAPER 정상 청산 20회",))
    )

    assert decision.eligible_for_real is False
    assert decision.blockers() == ["operational_ready=FAIL: PAPER 정상 청산 20회"]


def test_expired_evidence_closes_the_gate(passing, wired):
    directory, now = passing

    still_valid = _gate(directory, now=now + timedelta(days=29))
    expired = _gate(directory, now=now + timedelta(days=30))

    assert still_valid.eligible_for_real is True
    assert expired.eligible_for_real is False
    assert expired.components["evidence_current"].verdict is Verdict.FAIL
    assert "PROFITABILITY:EVIDENCE_EXPIRED" in expired.components["evidence_current"].reasons
    # 판정 자체는 과거 사실로 남는다.
    assert expired.components["profitability_passed"].verdict is Verdict.PASS


def test_a_new_strategy_fingerprint_invalidates_the_old_approval(passing, wired):
    directory, now = passing

    decision = _gate(directory, now=now, fingerprint="fp-after-a-change")

    assert decision.eligible_for_real is False
    assert not decision.components["evidence_current"].passed


def test_changing_the_execution_route_invalidates_the_old_approval(passing, wired):
    directory, now = passing
    s.write_json(directory / gate.EXECUTION_PROFILE_FILE, s.profile("FAST").as_dict())

    decision = _gate(directory, now=now)

    assert decision.eligible_for_real is False
    assert not decision.components["evidence_current"].passed


def test_an_undecided_route_blocks(passing, wired):
    directory, now = passing
    s.write_json(directory / gate.EXECUTION_PROFILE_FILE, s.profile(route=None).as_dict())

    decision = _gate(directory, now=now)

    assert decision.eligible_for_real is False
    assert "PROFITABILITY:CURRENT_EXECUTION_PROFILE_ID_UNDETERMINED" in (
        decision.components["evidence_current"].reasons
    )


def test_changing_a_risk_limit_invalidates_the_old_approval(passing, wired):
    directory, now = passing
    looser = s.risk_policy(per_trade_loss_budget_won=500_000.0)
    s.write_json(directory / gate.RISK_POLICY_FILE, looser.as_dict())

    decision = _gate(directory, now=now)

    assert decision.components["risk_policy_valid"].passed
    assert decision.eligible_for_real is False
    assert "PROFITABILITY:STALE_RISK_POLICY_HASH" in decision.components["evidence_current"].reasons


@pytest.mark.parametrize("filename", [gate.COST_POLICY_FILE, gate.FILL_MODEL_FILE])
def test_missing_cost_or_fill_model_version_blocks(passing, wired, filename):
    directory, now = passing
    (directory / filename).unlink()

    decision = _gate(directory, now=now)

    assert decision.eligible_for_real is False
    assert decision.components["evidence_current"].verdict is Verdict.NOT_EVALUATED


def test_incomplete_or_unreadable_risk_policy_blocks_without_raising(passing, wired):
    directory, now = passing
    path = directory / gate.RISK_POLICY_FILE

    s.write_json(path, s.risk_policy(daily_loss_limit_won=None).as_dict())
    undetermined = _gate(directory, now=now)
    s.write_json(path, s.risk_policy(absolute_order_cap=0).as_dict())
    unlimited = _gate(directory, now=now)
    path.write_bytes(b"{not json")
    unreadable = _gate(directory, now=now)
    path.write_bytes(json.dumps({**s.risk_policy().as_dict(), "typo_limit": 1}).encode())
    typo = _gate(directory, now=now)

    assert undetermined.components["risk_policy_valid"].reasons == (
        "DAILY_LOSS_LIMIT_UNDETERMINED",
    )
    assert unlimited.components["risk_policy_valid"].reasons == (
        "ABSOLUTE_ORDER_CAP_ZERO_MEANS_UNLIMITED",
    )
    for decision in (unreadable, typo):
        assert decision.components["risk_policy_valid"].reasons == (
            "RISK_POLICY_EVALUATION_ERROR",
        )
    assert not any(d.eligible_for_real for d in (undetermined, unlimited, unreadable, typo))


def test_risk_halt_closes_the_gate_across_restarts_and_leaves_exits_open(passing, wired):
    directory, now = passing
    with Store.open(directory / gate.STORE_FILE, create=True) as store:
        halt_id = risk_halt.raise_halt(
            store,
            kind=HaltKind.ORDER_STATE_UNKNOWN,
            reason="buy response lost",
            policy_hash=s.risk_policy().policy_hash(),
            trading_day="20270329",
            at=now.isoformat(),
        )

    # 판정할 때마다 저장소를 새로 연다 — 재시작과 같다.
    for _restart in range(2):
        halted = _gate(directory, now=now)
        assert halted.eligible_for_real is False
        assert halted.blockers() == ["risk_not_halted=FAIL: RISK_HALTED_ORDER_STATE_UNKNOWN"]
        assert gate.permits(halted, Action.NEW_BUY) is False
        assert gate.permits(halted, Action.ADD_BUY) is False
        assert gate.permits(halted, Action.PROTECTIVE_SELL) is True
        assert gate.permits(halted, Action.CANCEL) is True
        assert gate.permits(halted, Action.RECONCILE) is True

    with Store.open(directory / gate.STORE_FILE, create=True) as store:
        risk_halt.record_resume_decision(
            store, halt_id=halt_id, reconciled=True, investigation="order reconciled as unfilled",
            decided_by="operator", trading_day="20270330", at=now.isoformat(),
        )
    assert _gate(directory, now=now).eligible_for_real is True


def test_missing_store_blocks_evidence_and_halt_checks(passing, wired):
    directory, now = passing
    (directory / gate.STORE_FILE).unlink()

    decision = _gate(directory, now=now)

    assert decision.eligible_for_real is False
    assert decision.components["evidence_current"].reasons == ("EVIDENCE_STORE_UNAVAILABLE",)
    assert decision.components["risk_not_halted"].reasons == ("HALT_STATE_UNREADABLE",)
    assert not (directory / gate.STORE_FILE).exists()


def test_evaluating_the_gate_does_not_modify_the_directory(passing, wired):
    directory, now = passing
    before = {p.name: p.read_bytes() for p in directory.iterdir()}

    _gate(directory, now=now)

    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
