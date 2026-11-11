"""REAL 신규 진입 AND 게이트와 증거의 버전·유효기간 (docs/REAL_VALIDATION_DESIGN.md §2.1, §5).

통과 상태는 합성 디렉터리로만 만든다. 실제 승인 증거는 존재하지 않는다.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.real_validation import evidence, gate, profitability, risk_halt
from src.real_validation.core import Finding, Verdict, canonical_hash
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
    inputs = {"synthetic": True}
    good = {
        "refs": s.refs("exp-1"),
        "data_hash": canonical_hash(inputs),
        "inputs": inputs,
        "expires_at": s.ts("20270501"),
    }
    with Store.open(tmp_path / "gate.db", create=True) as store:
        for broken in (
            {"refs": incomplete},
            {"expires_at": None},
            {"inputs": None},
            {"data_hash": None, "inputs": None},
            {"data_hash": "not-the-hash-of-the-inputs"},
        ):
            with pytest.raises(ValueError):
                evidence.record(store, **{**good, **broken}, **common)
        with pytest.raises(ValueError):
            evidence.record(
                store, **good, kind="SOMETHING_ELSE", finding=PASS,
                evaluated_at=s.ts("20270401"),
            )
        assert store.evidence() == []

        # 같은 값에서 깨진 것이 없으면 기록된다 — 위 거부가 다른 이유 때문이 아니다.
        assert evidence.record(store, **good, **common) == 1


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


# ── 판정 코드가 바뀌면 옛 PASS는 재사용되지 않는다 ───────────────────


def test_rule_code_hash_follows_the_bytes_of_the_files_that_make_verdicts(tmp_path, monkeypatch):
    package = Path(evidence.__file__).resolve().parent
    for names in evidence.RULE_FILES.values():
        for name in names:
            shutil.copyfile(package / name, tmp_path / name)
    monkeypatch.setattr(evidence, "_PACKAGE_DIR", tmp_path)
    evidence.rules_code_hash.cache_clear()
    try:
        profit_before = evidence.rules_code_hash(evidence.KIND_PROFITABILITY)
        parity_before = evidence.rules_code_hash(evidence.KIND_EXECUTION_PARITY)
        assert profit_before == evidence.rules_code_hash(evidence.KIND_PROFITABILITY)

        with (tmp_path / "profitability.py").open("ab") as handle:
            handle.write(b"\n# one more line\n")
        evidence.rules_code_hash.cache_clear()

        assert evidence.rules_code_hash(evidence.KIND_PROFITABILITY) != profit_before
        # 수익성 판정 파일만 바뀌었으므로 경로 증거는 낡지 않는다.
        assert evidence.rules_code_hash(evidence.KIND_EXECUTION_PARITY) == parity_before

        (tmp_path / "core.py").unlink()
        evidence.rules_code_hash.cache_clear()
        with pytest.raises(OSError):
            evidence.rules_code_hash(evidence.KIND_PROFITABILITY)
    finally:
        evidence.rules_code_hash.cache_clear()


def test_gate_package_files_are_covered_by_a_rule_hash_or_named_as_wiring():
    """새 파일이 생기면 판정 파일인지 배선인지 여기서 정해야 한다."""
    package = Path(evidence.__file__).resolve().parent
    verdict_files = {name for names in evidence.RULE_FILES.values() for name in names}
    wiring = {
        "__init__.py", "evidence.py", "gate.py", "store.py",
        # 주문 직전 계산·중단 기록: 증거 판정이 아니라 런타임 집행이다.
        "risk_policy.py", "sizing.py", "risk_halt.py",
    }

    assert {path.name for path in package.glob("*.py")} == verdict_files | wiring
    assert not verdict_files & wiring


def test_changed_rule_code_makes_the_old_pass_stale_without_a_fingerprint_change(
    passing, wired, monkeypatch
):
    directory, now = passing
    assert _gate(directory, now=now).eligible_for_real is True
    real_hash = evidence.rules_code_hash

    monkeypatch.setattr(
        evidence,
        "rules_code_hash",
        lambda kind: "edited" if kind == evidence.KIND_PROFITABILITY else real_hash(kind),
    )
    decision = _gate(directory, now=now)

    assert decision.eligible_for_real is False
    assert decision.components["evidence_current"].reasons == (
        "PROFITABILITY:STALE_RULES_CODE_HASH",
    )
    # 판정 자체는 과거 사실로 남는다. 재사용만 막힌다.
    assert decision.components["profitability_passed"].verdict is Verdict.PASS


def test_bumped_rule_version_also_makes_old_evidence_stale(passing, wired, monkeypatch):
    directory, now = passing
    monkeypatch.setattr(evidence, "EVALUATION_RULE_VERSION", "real-validation-rules-next")

    decision = _gate(directory, now=now)

    assert decision.eligible_for_real is False
    assert "PROFITABILITY:STALE_EVALUATION_RULE_VERSION" in (
        decision.components["evidence_current"].reasons
    )


# ── 손으로 만든 증거 ─────────────────────────────────────────────────


def _forge(directory, *, chain=True, **overrides):
    """마지막 수익성 증거를 베껴 고친 PASS 행을 직접 끼워 넣는다(기록 함수를 거치지 않고)."""
    with Store.open(directory / gate.STORE_FILE, create=True) as store:
        row = dict(store.evidence(evidence.KIND_PROFITABILITY)[-1])
        row.pop("id")
        row["verdict"] = "PASS"
        row["reasons"] = []
        refs = dict(row["refs"])
        refs.update(overrides.pop("refs", {}))
        row.update(overrides)
        row["refs"] = refs
        row["prev_hash"] = evidence.chain_head(store)
        row["row_hash"] = evidence._row_hash(row["prev_hash"], row) if chain else "f" * 64
        store.insert_evidence(row)


def _current(decision):
    return decision.components["evidence_current"]


def test_forged_pass_without_a_valid_chain_hash_is_rejected(passing, wired):
    directory, now = passing
    _forge(directory, chain=False)

    decision = _gate(directory, now=now)

    assert decision.eligible_for_real is False
    assert any("EVIDENCE_ROW_ALTERED" in reason for reason in _current(decision).reasons)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"inputs": None}, "PROFITABILITY:INPUTS_MISSING"),
        ({"inputs": {"days": [], "trades": []}}, "PROFITABILITY:INPUTS_DO_NOT_MATCH_DATA_HASH"),
        ({"refs": {"spec_hash": "0" * 64}}, "PROFITABILITY:STALE_SPEC_HASH"),
        ({"evaluated_at": s.ts("20270115")}, "PROFITABILITY:EVALUATED_BEFORE_JUDGMENT_DATE"),
        ({"refs": {"experiment_id": "never-registered"}}, "PROFITABILITY:STALE_EXPERIMENT_ID"),
    ],
)
def test_forged_pass_that_is_not_bound_to_the_locked_registration_is_rejected(
    passing, wired, overrides, reason
):
    directory, now = passing
    if "evaluated_at" in overrides:
        overrides = {**overrides, "expires_at": s.ts("20270601")}
    _forge(directory, **overrides)

    decision = _gate(directory, now=now)

    assert decision.eligible_for_real is False
    assert reason in _current(decision).reasons


def _without_triggers(directory, statement):
    """추가 전용 트리거를 지우고 행을 고친다 — 파일 쓰기 권한이 있으면 할 수 있는 일이다."""
    conn = sqlite3.connect(str(directory / gate.STORE_FILE))
    try:
        for table in ("evidence", "registrations"):
            conn.execute(f"DROP TRIGGER {table}_no_update")
        conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def test_verdict_edited_in_place_breaks_the_chain(passing, wired):
    directory, now = passing
    _without_triggers(directory, "UPDATE evidence SET expires_at='2099-01-01T00:00:00+09:00'")

    decision = _gate(directory, now=now + timedelta(days=400))

    assert decision.eligible_for_real is False
    assert any("EVIDENCE_ROW_ALTERED" in reason for reason in _current(decision).reasons)


def test_locked_spec_relaxed_in_place_is_detected(passing, wired):
    directory, now = passing
    relaxed = json.dumps({**s.spec().as_dict(), "max_account_mdd_pct": 99.0}, sort_keys=True)
    _without_triggers(
        directory,
        f"UPDATE registrations SET spec_json='{relaxed}' WHERE kind='PROFITABILITY'",
    )

    decision = _gate(directory, now=now)

    assert decision.eligible_for_real is False
    assert "PROFITABILITY:REGISTRATION_SPEC_ALTERED" in _current(decision).reasons


def test_consistent_forgery_passes_the_gate_and_only_the_offline_audit_catches_it(
    passing, wired
):
    """게이트의 한계를 그대로 적는다: 해시까지 맞춘 가짜 PASS는 구조 검사를 통과한다.

    입력 원본이 실제로는 손실 표본이면 재계산 감사가 잡는다. 입력까지 지어낸 경우는
    저장소 밖의 대조(거래 DB, git에 남긴 사슬 머리 해시)로만 잡힌다.
    """
    directory, now = passing
    days, trades = s.sample([-1.0] * 60)
    losing = profitability.window_inputs(s.spec(), days, trades)
    _forge(directory, inputs=losing, data_hash=canonical_hash(losing))

    decision = _gate(directory, now=now)
    with Store.open(directory / gate.STORE_FILE) as store:
        forged_id = store.evidence(evidence.KIND_PROFITABILITY)[-1]["id"]
        audit = profitability.audit(store, forged_id)

    assert decision.eligible_for_real is True
    assert audit.verdict is Verdict.FAIL
    assert "RECOMPUTED_VERDICT_DIFFERS" in audit.reasons
    assert audit.detail == {"recorded": "PASS", "recomputed": "FAIL"}


def test_chain_head_changes_with_every_recorded_verdict(tmp_path):
    with Store.open(tmp_path / "gate.db", create=True) as store:
        assert evidence.chain_head(store) == evidence.GENESIS_HASH
        assert evidence.verify_chain(store) == []
    s.passing_directory(tmp_path / "rv")
    with Store.open(tmp_path / "rv" / gate.STORE_FILE) as store:
        rows = store.evidence()
        assert [row["prev_hash"] for row in rows] == [evidence.GENESIS_HASH, rows[0]["row_hash"]]
        assert evidence.chain_head(store) == rows[1]["row_hash"]
        assert evidence.verify_chain(store) == []


def test_store_from_another_schema_version_is_unavailable_and_left_untouched(tmp_path):
    path = tmp_path / "gate.db"
    with Store.open(path, create=True):
        pass
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    before = path.read_bytes()

    for create in (False, True):
        with pytest.raises(gate.StoreUnavailable):
            Store.open(path, create=create)
    assert path.read_bytes() == before
    assert _gate(tmp_path, now=_now()).components["risk_not_halted"].reasons == (
        "HALT_STATE_UNREADABLE",
    )


def test_evaluating_the_gate_does_not_modify_the_directory(passing, wired):
    directory, now = passing
    before = {p.name: p.read_bytes() for p in directory.iterdir()}

    _gate(directory, now=now)

    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
