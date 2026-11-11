"""REAL 신규 진입 자격 — 여섯 조건의 AND (설계 §2.1).

    eligible_for_real = operational_ready AND profitability_passed
        AND execution_parity_passed AND risk_policy_valid AND evidence_current
        AND NOT risk_halted

점수가 아니다. 한 항목의 통과가 다른 항목의 실패를 상쇄하지 못한다. 미평가·수집 중·
표본 부족·만료·읽기 실패는 전부 차단이다.

이 게이트는 매수 자격만 판정한다. 보호 청산·취소·대사는 `permits`가 늘 허용한다.

설정은 `REAL_VALIDATION_DIR` 디렉터리 하나에서 읽는다(읽기 전용). 환경변수가 없으면
아무 파일도 읽지 않고 전부 미평가로 둔다 — 현재 PAPER 운영의 동작은 바뀌지 않는다.
이 변수 이름은 전략 지문 접두어(`src/release.py`)에 걸리지 않는다. 정책이 바뀌면 지문이
아니라 증거의 정책 해시가 어긋나서 자격이 내려간다.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.real_validation import evidence, execution_profile, profitability, risk_halt
from src.real_validation.core import Finding, Verdict, canonical_hash, not_evaluated
from src.real_validation.costs import CostPolicy
from src.real_validation.risk_policy import RiskPolicy
from src.real_validation.store import Store, StoreUnavailable

ENV_DIR = "REAL_VALIDATION_DIR"
STORE_FILE = "gate.db"
RISK_POLICY_FILE = "risk_policy.json"
EXECUTION_PROFILE_FILE = "execution_profile.json"
COST_POLICY_FILE = "cost_policy.json"
FILL_MODEL_FILE = "fill_model.json"

# 주문 직전 위험 예산 확인(`sizing.allowed_order`·예약)과 매수 전 게이트 재검사가 실제
# 매수 경로에 연결됐는가. 연결은 전략 파일(f3_entry 등) 변경이라 별도 릴리스이고, 그
# 릴리스가 이 값을 True로 바꾼다. False인 동안에는 정책 파일이 완벽해도
# `risk_policy_valid`가 통과하지 않는다 — 계산만 있고 집행이 없는 한도를 믿지 않는다.
PRE_BUY_ENFORCEMENT_WIRED = False

COMPONENTS = (
    "operational_ready",
    "profitability_passed",
    "execution_parity_passed",
    "risk_policy_valid",
    "evidence_current",
    "risk_not_halted",
)


@dataclass(frozen=True)
class GateDecision:
    components: dict[str, Finding]

    @property
    def eligible_for_real(self) -> bool:
        return all(self.components[name].passed for name in COMPONENTS)

    def blockers(self) -> list[str]:
        found: list[str] = []
        for name in COMPONENTS:
            finding = self.components[name]
            if finding.passed:
                continue
            reasons = ",".join(finding.reasons) or "NO_REASON"
            found.append(f"{name}={finding.verdict.value}: {reasons}")
        return found

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible_for_real": self.eligible_for_real,
            "formula": " AND ".join(COMPONENTS),
            "components": {name: self.components[name].as_dict() for name in COMPONENTS},
            "blockers": self.blockers(),
        }


def decide(
    *,
    operational_ready: Finding,
    profitability_passed: Finding,
    execution_parity_passed: Finding,
    risk_policy_valid: Finding,
    evidence_current: Finding,
    risk_not_halted: Finding,
) -> GateDecision:
    """순수 AND. 가중치도 예외 경로도 없다."""
    return GateDecision(
        {
            "operational_ready": operational_ready,
            "profitability_passed": profitability_passed,
            "execution_parity_passed": execution_parity_passed,
            "risk_policy_valid": risk_policy_valid,
            "evidence_current": evidence_current,
            "risk_not_halted": risk_not_halted,
        }
    )


def permits(decision: GateDecision, action: risk_halt.Action) -> bool:
    """자격이 없으면 매수만 막는다. 청산·취소·대사는 항상 True다."""
    return risk_halt.action_allowed(action, eligible_for_real=decision.eligible_for_real)


def _guard(compute: Callable[[], Finding], label: str) -> Finding:
    """항목 하나의 계산이 터져도 게이트 전체가 열리거나 죽지 않게 한다."""
    try:
        return compute()
    except Exception as exc:
        return not_evaluated(f"{label}_EVALUATION_ERROR", error=repr(exc))


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path.name} must hold a JSON object")
    return loaded


def _risk_policy(directory: Path) -> tuple[Finding, str | None]:
    raw = _read_json(directory / RISK_POLICY_FILE)
    if raw is None:
        return not_evaluated("RISK_POLICY_FILE_MISSING"), None
    policy = RiskPolicy.from_dict(raw)
    problems = policy.problems()
    if problems:
        return Finding(Verdict.FAIL, tuple(problems)), None
    detail = {"policy_version": policy.policy_version, "policy_hash": policy.policy_hash()}
    if not PRE_BUY_ENFORCEMENT_WIRED:
        return (
            not_evaluated("RUNTIME_PRE_BUY_ENFORCEMENT_NOT_WIRED", **detail),
            policy.policy_hash(),
        )
    return Finding(Verdict.PASS, (), detail), policy.policy_hash()


def _current_registration(
    store: Store, kind: str, matches: Callable[[dict[str, Any]], bool]
) -> str | None:
    """현재 버전에 묶인 가장 최근 등록의 실험 id. 없으면 None(미정)."""
    for row in reversed(store.registrations(kind)):
        if matches(row["spec"]):
            return str(row["experiment_id"])
    return None


def _combine(parts: Mapping[str, Finding]) -> Finding:
    reasons = tuple(f"{name}:{r}" for name, f in parts.items() for r in f.reasons)
    detail = {name: f.detail for name, f in parts.items()}
    verdicts = {f.verdict for f in parts.values()}
    if verdicts == {Verdict.PASS}:
        return Finding(Verdict.PASS, (), detail)
    if Verdict.NOT_EVALUATED in verdicts:
        return Finding(Verdict.NOT_EVALUATED, reasons, detail)
    return Finding(Verdict.FAIL, reasons, detail)


def evaluate_from_environment(
    *,
    strategy_fingerprint: str,
    operational_ready: Finding,
    now: datetime,
    env: Mapping[str, str],
) -> GateDecision:
    """디렉터리의 정책·프로필·증거를 읽어 게이트를 판정한다. 아무것도 쓰지 않는다."""
    directory_raw = (env.get(ENV_DIR) or "").strip()
    if not directory_raw:
        unset = not_evaluated("REAL_VALIDATION_DIR_UNSET")
        return decide(
            operational_ready=operational_ready,
            profitability_passed=unset,
            execution_parity_passed=unset,
            risk_policy_valid=unset,
            evidence_current=unset,
            risk_not_halted=unset,
        )
    directory = Path(directory_raw)
    store_path = directory / STORE_FILE

    risk_hash: str | None = None

    def risk() -> Finding:
        nonlocal risk_hash
        finding, risk_hash = _risk_policy(directory)
        return finding

    risk_finding = _guard(risk, "RISK_POLICY")

    profile_id: str | None = None
    profile_hash: str | None = None
    cost_hash: str | None = None
    fill_hash: str | None = None

    def versions() -> Finding:
        nonlocal profile_id, profile_hash, cost_hash, fill_hash
        raw_profile = _read_json(directory / EXECUTION_PROFILE_FILE)
        if raw_profile is not None:
            profile = execution_profile.ExecutionProfile.from_dict(raw_profile)
            profile_id = profile.profile_id()
            profile_hash = profile.profile_hash() if profile_id else None
        raw_cost = _read_json(directory / COST_POLICY_FILE)
        if raw_cost is not None:
            cost_policy = CostPolicy.from_dict(raw_cost)
            cost_hash = None if cost_policy.problems() else cost_policy.policy_hash()
        raw_fill = _read_json(directory / FILL_MODEL_FILE)
        if raw_fill:
            fill_hash = canonical_hash(raw_fill)
        return Finding(Verdict.PASS)

    versions_finding = _guard(versions, "VERSIONS")

    def from_store() -> tuple[Finding, Finding, Finding]:
        with Store.open(store_path) as store:
            def bound(spec: dict[str, Any]) -> bool:
                return bool(profile_id) and spec.get("execution_profile_id") == profile_id

            def bound_profit(spec: dict[str, Any]) -> bool:
                return bound(spec) and spec.get("strategy_fingerprint") == strategy_fingerprint

            experiments = {
                evidence.KIND_PROFITABILITY: _current_registration(
                    store, profitability.REGISTRATION_KIND, bound_profit
                ),
                evidence.KIND_EXECUTION_PARITY: _current_registration(
                    store, execution_profile.REGISTRATION_KIND, bound
                ),
            }
            current: dict[str, Finding] = {}
            verdicts: dict[str, Finding] = {}
            for kind, experiment_id in experiments.items():
                row = evidence.latest(store, kind)
                verdicts[kind] = evidence.verdict_of(row)
                expected = evidence.EvidenceRefs(
                    strategy_fingerprint=strategy_fingerprint,
                    experiment_id=experiment_id,
                    execution_profile_id=profile_id,
                    execution_profile_hash=profile_hash,
                    cost_policy_hash=cost_hash,
                    fill_model_hash=fill_hash,
                    risk_policy_hash=risk_hash,
                    evaluation_rule_version=evidence.EVALUATION_RULE_VERSION,
                )
                current[kind] = evidence.check_current(row, expected, now)
        return (
            verdicts[evidence.KIND_PROFITABILITY],
            verdicts[evidence.KIND_EXECUTION_PARITY],
            _combine(current),
        )

    try:
        profit_finding, parity_finding, current_finding = from_store()
    except StoreUnavailable as exc:
        missing = not_evaluated("EVIDENCE_STORE_UNAVAILABLE", error=str(exc))
        profit_finding = parity_finding = current_finding = missing
    except Exception as exc:
        broken = not_evaluated("EVIDENCE_EVALUATION_ERROR", error=repr(exc))
        profit_finding = parity_finding = current_finding = broken

    if not versions_finding.passed and current_finding.passed:
        current_finding = versions_finding

    return decide(
        operational_ready=operational_ready,
        profitability_passed=profit_finding,
        execution_parity_passed=parity_finding,
        risk_policy_valid=risk_finding,
        evidence_current=current_finding,
        risk_not_halted=risk_halt.halt_finding(str(store_path)),
    )
