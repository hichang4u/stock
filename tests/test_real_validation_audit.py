"""증거 감사(scripts/real_validation_audit.py)와 1·2차 테스트가 덮지 않은 공백.

전부 합성 저장소다. 실제 PASS 증거는 존재하지 않는다. 여기서 확인하는 것은 '증거가 생겼을 때
감사와 게이트가 어떻게 반응하는가'이지 실제 검증 결과가 아니다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from scripts import real_validation_audit as audit_script
from src.real_validation import evidence, execution_profile, gate, profitability, risk_halt
from src.real_validation.core import Verdict
from src.real_validation.costs import research_scenario_policy
from src.real_validation.risk_halt import Action, HaltKind
from src.real_validation.store import Store
from tests import real_validation_support as s


@pytest.fixture
def passing(tmp_path):
    directory = tmp_path / "rv"
    evaluated_at = s.passing_directory(directory)
    return directory, datetime.fromisoformat(evaluated_at) + timedelta(hours=1)


@pytest.fixture
def wired(monkeypatch):
    """집행이 연결된 미래 릴리스를 가정한다. 현재 코드는 False이고 이 테스트 밖에서는 그대로다."""
    monkeypatch.setattr(gate, "PRE_BUY_ENFORCEMENT_WIRED", True)


def _audit(directory, now):
    return audit_script.audit(directory, fingerprint=s.FINGERPRINT, now=now)


def _gate(directory, now):
    return gate.evaluate_from_environment(
        strategy_fingerprint=s.FINGERPRINT, operational_ready=s.OPERATIONAL_PASS, now=now,
        env={gate.ENV_DIR: str(directory)},
    )


# ── 입력 → 판정 재현, 해시 사슬 ──────────────────────────────────────


def test_clean_synthetic_store_reproduces_its_verdict_and_chain(passing):
    directory, now = passing
    before = {p.name: p.read_bytes() for p in directory.iterdir()}

    report = _audit(directory, now)

    assert report["ok"] is True and report["problems"] == []
    assert report["integrity_check"] == "ok"
    assert report["chain"]["problems"] == []
    profit, parity = report["evidence"]
    assert profit["kind"] == "PROFITABILITY" and profit["verdict"] == "PASS"
    assert profit["recomputed"] == {"verdict": "PASS", "reasons": []}
    assert profit["binding"] == {"verdict": "PASS", "reasons": []}
    assert profit["rules_code_current"] and profit["runtime_same"]
    assert parity["kind"] == "EXECUTION_PARITY" and "recomputed" not in parity
    # 사슬: 첫 행은 기원 해시에서, 둘째 행은 첫 행에서 이어지고 머리는 마지막 행이다.
    with Store.open(directory / gate.STORE_FILE) as store:
        rows = store.evidence()
    assert rows[0]["prev_hash"] == evidence.GENESIS_HASH
    assert rows[1]["prev_hash"] == rows[0]["row_hash"] == profit["row_hash"]
    assert report["chain"]["head"] == rows[1]["row_hash"] == parity["row_hash"]
    # 감사는 읽기만 한다.
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before


def test_audit_does_not_open_the_gate_that_enforcement_keeps_closed(passing):
    """증거가 깨끗해도 감사가 자격을 주지 않는다. 집행 미연결은 그대로 차단 사유다."""
    directory, now = passing

    report = _audit(directory, now)

    assert report["pre_buy_enforcement_wired"] is False
    assert report["gate"]["eligible_for_real"] is False
    assert "RUNTIME_PRE_BUY_ENFORCEMENT_NOT_WIRED" in json.dumps(report["gate"])
    assert report["exits_permitted_whatever_the_gate_says"] == {
        "PROTECTIVE_SELL": True, "CANCEL": True, "RECONCILE": True,
    }


def test_evidence_records_the_runtime_it_was_judged_on(passing):
    directory, _now = passing
    with Store.open(directory / gate.STORE_FILE) as store:
        runtime = store.evidence()[0]["detail"]["runtime"]

    assert runtime == evidence.runtime_info()
    assert set(runtime) == {"python", "implementation", "platform", "sqlite"}


def test_audit_flags_a_runtime_change_without_calling_it_a_failure(passing, monkeypatch):
    directory, now = passing
    changed = {**evidence.runtime_info(), "python": "9.9.9"}
    monkeypatch.setattr(evidence, "runtime_info", lambda: changed)

    report = _audit(directory, now)

    assert report["evidence"][0]["runtime_same"] is False
    # 같은 입력에서 같은 판정이 다시 나왔으므로 문제로 올리지 않는다.
    assert report["evidence"][0]["recomputed"]["verdict"] == "PASS"
    assert report["ok"] is True


def test_audit_catches_a_recorded_pass_whose_inputs_do_not_pass(passing):
    directory, now = passing
    days, trades = s.sample([-1.0] * 60)
    losing = profitability.window_inputs(s.spec(), days, trades)
    with Store.open(directory / gate.STORE_FILE, create=True) as store:
        row = dict(store.evidence(evidence.KIND_PROFITABILITY)[-1])
        row.pop("id")
        row.update(inputs=losing, data_hash=evidence.canonical_hash(losing))
        row["prev_hash"] = evidence.chain_head(store)
        row["row_hash"] = evidence._row_hash(row["prev_hash"], row)
        forged_id = store.insert_evidence(row)

    report = _audit(directory, now)

    forged = next(item for item in report["evidence"] if item["id"] == forged_id)
    assert report["ok"] is False
    assert forged["recomputed"]["verdict"] == "FAIL"
    assert "RECOMPUTED_VERDICT_DIFFERS" in forged["recomputed"]["reasons"]
    assert report["chain"]["problems"] == []  # 사슬만으로는 잡히지 않는 위조다


# ── 코드·정책 변경, 만료 ─────────────────────────────────────────────


def _rewrite(directory, name, mutate):
    path = directory / name
    value = json.loads(path.read_bytes().decode("utf-8"))
    mutate(value)
    s.write_json(path, value)


def test_changed_cost_policy_content_makes_the_evidence_stale(passing, wired):
    directory, now = passing
    assert _gate(directory, now).eligible_for_real is True

    _rewrite(
        directory, gate.COST_POLICY_FILE,
        lambda raw: raw["versions"][0].update(round_trip_rate=0.0023),
    )
    decision = _gate(directory, now)

    assert decision.eligible_for_real is False
    assert "PROFITABILITY:STALE_COST_POLICY_HASH" in decision.components["evidence_current"].reasons


def test_changed_fill_model_content_makes_the_evidence_stale(passing, wired):
    directory, now = passing

    _rewrite(directory, gate.FILL_MODEL_FILE, lambda raw: raw.update(depth_levels=3))
    decision = _gate(directory, now)

    assert decision.eligible_for_real is False
    assert "PROFITABILITY:STALE_FILL_MODEL_HASH" in decision.components["evidence_current"].reasons


def test_parity_rule_code_change_stales_only_the_parity_evidence(passing, wired, monkeypatch):
    directory, now = passing
    real_hash = evidence.rules_code_hash
    monkeypatch.setattr(
        evidence,
        "rules_code_hash",
        lambda kind: "edited" if kind == evidence.KIND_EXECUTION_PARITY else real_hash(kind),
    )

    decision = _gate(directory, now)
    report = _audit(directory, now)

    assert decision.eligible_for_real is False
    assert decision.components["evidence_current"].reasons == (
        "EXECUTION_PARITY:STALE_RULES_CODE_HASH",
    )
    assert [item["rules_code_current"] for item in report["evidence"]] == [True, False]


def test_each_evidence_kind_expires_on_its_own_clock(tmp_path, wired):
    """수익성 증거와 경로 증거의 유효기간이 다르면 먼저 끝나는 쪽이 게이트를 닫는다."""
    directory = tmp_path / "rv"
    directory.mkdir()
    s.write_config(directory)
    spec = s.spec(evidence_valid_days=90)
    evaluated_at = s.ts(str(spec.judgment_date))
    days, trades = s.sample(s.GOOD_PCTS)
    with Store.open(directory / gate.STORE_FILE, create=True) as store:
        profitability.register(store, spec, locked_at=s.LOCKED_AT)
        refs = s.refs(str(spec.experiment_id), spec_hash=spec.spec_hash())
        profitability.judge(
            store, str(spec.experiment_id), days, trades, as_of=str(spec.judgment_date),
            evaluated_at=evaluated_at, refs=refs,
        )
        parity_id = execution_profile.register_contract(store, s.contract())
        inputs = {"synthetic": "parity"}
        evidence.record(
            store, kind=evidence.KIND_EXECUTION_PARITY,
            finding=evidence.Finding(Verdict.PASS),
            refs=s.refs(parity_id, kind=evidence.KIND_EXECUTION_PARITY),
            data_hash=evidence.canonical_hash(inputs), inputs=inputs,
            evaluated_at=evaluated_at,
            expires_at=(datetime.fromisoformat(evaluated_at) + timedelta(days=30)).isoformat(),
        )
    base = datetime.fromisoformat(evaluated_at)

    assert _gate(directory, base + timedelta(days=29)).eligible_for_real is True
    parity_expired = _gate(directory, base + timedelta(days=31))
    both_expired = _gate(directory, base + timedelta(days=91))

    assert parity_expired.components["evidence_current"].reasons == (
        "EXECUTION_PARITY:EVIDENCE_EXPIRED",
    )
    assert set(both_expired.components["evidence_current"].reasons) == {
        "PROFITABILITY:EVIDENCE_EXPIRED", "EXECUTION_PARITY:EVIDENCE_EXPIRED",
    }


# ── 저장소 손상 ──────────────────────────────────────────────────────


def test_truncated_store_blocks_the_gate_and_fails_the_audit(passing, wired):
    directory, now = passing
    path = directory / gate.STORE_FILE
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])

    decision = _gate(directory, now)
    report = _audit(directory, now)

    assert decision.eligible_for_real is False
    assert not decision.components["evidence_current"].passed
    assert not decision.components["risk_not_halted"].passed
    assert report["ok"] is False
    assert gate.permits(decision, Action.PROTECTIVE_SELL) is True
    assert path.read_bytes() == data[: len(data) // 2]  # 손상된 파일을 고쳐 쓰지 않는다


def test_overwritten_page_in_the_store_is_reported(passing, wired):
    directory, now = passing
    path = directory / gate.STORE_FILE
    data = bytearray(path.read_bytes())
    data[4096:8192] = b"\x00" * 4096
    path.write_bytes(bytes(data))

    report = _audit(directory, now)

    assert report["ok"] is False
    assert _gate(directory, now).eligible_for_real is False


def test_deleted_registration_leaves_the_evidence_unbound(passing, wired):
    directory, now = passing
    conn = sqlite3.connect(str(directory / gate.STORE_FILE))
    conn.execute("DROP TRIGGER registrations_no_delete")
    conn.execute("DELETE FROM registrations WHERE kind='PROFITABILITY'")
    conn.commit()
    conn.close()

    decision = _gate(directory, now)
    report = _audit(directory, now)

    assert decision.eligible_for_real is False
    assert "PROFITABILITY:CURRENT_EXPERIMENT_ID_UNDETERMINED" in (
        decision.components["evidence_current"].reasons
    )
    assert report["ok"] is False
    assert any("REGISTRATION_MISSING" in problem for problem in report["problems"])


def test_missing_directory_or_store_is_reported_not_raised(tmp_path):
    report = _audit(tmp_path / "nowhere", datetime.fromisoformat(s.ts("20270401")))

    assert report["ok"] is False
    assert report["integrity_check"] == "MISSING"
    assert report["gate"]["eligible_for_real"] is False
    assert all(not state["present"] for state in report["files"].values())


# ── 중단 중에도 청산·취소·대사는 허용 ────────────────────────────────


def test_audit_lists_active_halts_unsettled_orders_and_keeps_exits_open(passing, wired):
    directory, now = passing
    with Store.open(directory / gate.STORE_FILE, create=True) as store:
        risk_halt.raise_halt(
            store, kind=HaltKind.EXIT_IMPOSSIBLE, reason="sell rejected, manual response",
            policy_hash=s.risk_policy().policy_hash(), trading_day="20270329",
            at=now.isoformat(),
        )
        store.insert_reservation_event(
            event="RESERVE", client_order_id="buy-1", amount=1_000_000.0, loss_budget=37_500.0,
            reason="BUY_PENDING", at=now.isoformat(),
        )

    report = _audit(directory, now)

    assert [h["kind"] for h in report["active_halts"]] == ["EXIT_IMPOSSIBLE"]
    assert report["unsettled_orders"]["buy-1"] == {
        "pending_amount": 1_000_000.0, "exposure_amount": 0.0, "loss_in_use": 37_500.0,
        "acked": False,
    }
    assert report["gate"]["eligible_for_real"] is False
    assert report["exits_permitted_whatever_the_gate_says"] == {
        "PROTECTIVE_SELL": True, "CANCEL": True, "RECONCILE": True,
    }
    # 중단과 미결 예약은 '증거가 깨졌다'는 뜻이 아니다. 감사의 무결성 판정과 따로다.
    assert report["ok"] is True


# ── 감사 범위 ────────────────────────────────────────────────────────


def test_verdict_code_imports_stay_within_the_reviewed_set():
    """판정 파일이 새 저장소 모듈을 임포트하면 해시 범위에 넣을지 여기서 정해야 한다."""
    scope = audit_script.code_scope()

    assert scope["PROFITABILITY"]["imported_but_not_hashed"] == [
        # 연구 시나리오 비용 정책의 상수. 판정 로직이 아니고 비용 정책 해시로 잡힌다.
        "src.modules.cost_model",
        # 배선: 증거 기록과 저장소.
        "src.real_validation.evidence",
        "src.real_validation.store",
    ]
    assert scope["EXECUTION_PARITY"]["imported_but_not_hashed"] == [
        "src.real_validation.evidence",
        "src.real_validation.store",
    ]
    for kind in evidence.EVIDENCE_KINDS:
        assert scope[kind]["rules_code_hash"] == evidence.rules_code_hash(kind)


def test_research_cost_constant_change_is_caught_by_the_cost_policy_hash(monkeypatch):
    """`cost_model`은 판정 코드 해시 밖이다. 그 상수가 바뀌면 비용 정책 해시가 대신 어긋난다."""
    before = research_scenario_policy().policy_hash()
    monkeypatch.setattr("src.modules.cost_model.BASE_ROUND_TRIP_COST_PCT", 0.23)

    assert research_scenario_policy().policy_hash() != before


def test_audit_states_what_it_does_not_cover(passing):
    directory, now = passing

    report = _audit(directory, now)

    assert len(report["not_covered"]) == len(audit_script.NOT_COVERED) == 5
    assert any("거래 DB" in item for item in report["not_covered"])
    assert any("인터프리터" in item for item in report["not_covered"])


def test_cli_exit_code_follows_the_audit_result(passing, tmp_path):
    directory, now = passing
    out = tmp_path / "audit.json"
    args = ["--dir", str(directory), "--fingerprint", s.FINGERPRINT, "--now", now.isoformat()]

    assert audit_script.main([*args, "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["ok"] is True
    (directory / gate.STORE_FILE).write_bytes(b"not a database")
    assert audit_script.main([*args, "--out", str(out)]) == 1
