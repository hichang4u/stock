"""PAPER·REAL 실행 경로 계약과 동일 입력 재생 비교 (설계 §3).

경로(기존/빠른)는 여기서 고르지 않는다. `route`가 비어 있으면 프로필은 무효이고
`execution_parity_passed`는 미평가다. 경로가 바뀌면 프로필 해시가 바뀌어 기존 승인
증거는 낡은 증거가 된다(`evidence.check_current`).

재생 비교는 '공통 판단 로직 + 모드별 어댑터' 구조를 전제로 한다. 판단 함수가 어댑터에서
받은 값으로 결론을 바꾸면 그 차이가 그대로 불일치로 드러난다. 환경 차이(지연·호출 한도·
체결 서버)는 재생으로 증명되지 않으므로 실측 표본으로 따로 판정한다.

**이 재생기는 아직 실제 판단부에 연결돼 있지 않다.** `f3_entry`의 선정·락업·재검증·주문
판단은 I/O와 섞여 있어 `decide`로 넘길 수 없다. 지금 검증된 것은 비교 절차뿐이고, 여기서
`matched=True`가 나와도 운영 경로의 PAPER·REAL 동일성이 검증된 것이 아니다. 실제 판단부를
연결한 재생 결과가 아니면 경로 동일성 증거로 기록하지 않는다(`replay`의 `decision_source`).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from src.real_validation import evidence
from src.real_validation.core import (
    Finding,
    Verdict,
    canonical_hash,
    finite_number,
    not_evaluated,
    parse_aware,
)
from src.real_validation.store import Store

REGISTRATION_KIND = "EXECUTION_PARITY"
ROUTES = ("LEGACY", "FAST")
MODES = ("PAPER", "REAL")
STAGES = ("select_to_send", "send_to_ack", "ack_to_fill")

# 재생에 쓴 판단 함수의 출처. 운영 판단부를 그대로 건 재생만 경로 동일성 증거가 된다.
DECISION_SOURCE_PRODUCTION = "PRODUCTION_DECISION_LOGIC"
DECISION_SOURCE_PARTIAL = "PARTIAL_PURE_FUNCTIONS"
DECISION_SOURCE_SYNTHETIC = "SYNTHETIC"
DECISION_SOURCES = (
    DECISION_SOURCE_PRODUCTION,
    DECISION_SOURCE_PARTIAL,
    DECISION_SOURCE_SYNTHETIC,
)

# 설계 §3.1이 프로필 해시에 넣으라고 한 항목. 값의 형식은 경로 확정 뒤 정한다 —
# 여기서는 '비어 있지 않다'만 요구한다.
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "selection": (
        "candidate_reference_time",
        "selection_lockup_revalidation_order",
        "open_first_trade_wait_policy",
    ),
    "entry": ("limit_price_cap", "quote_freshness", "vi_handling", "entry_deadline"),
    "orders": (
        "quantity_policy",
        "cash_budget_policy",
        "unfilled_cancel_policy",
        "partial_fill_policy",
        "response_loss_reconcile_policy",
    ),
    "calls": ("priority_policy", "retry_policy", "timeout_policy", "per_mode_call_limits"),
}


@dataclass(frozen=True)
class ExecutionProfile:
    route: str | None
    sections: dict[str, dict[str, Any]]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ExecutionProfile:
        unknown = set(raw) - {"route", *REQUIRED_FIELDS}
        if unknown:
            raise ValueError(f"unknown execution profile keys: {sorted(unknown)}")
        sections = {name: dict(raw.get(name) or {}) for name in REQUIRED_FIELDS}
        return cls(route=raw.get("route"), sections=sections)

    def as_dict(self) -> dict[str, Any]:
        return {"route": self.route, **self.sections}

    def problems(self) -> list[str]:
        found: list[str] = []
        if self.route not in ROUTES:
            found.append("ROUTE_UNDETERMINED")
        for section, names in REQUIRED_FIELDS.items():
            values = self.sections.get(section) or {}
            unknown = set(values) - set(names)
            if unknown:
                found.append(f"{section.upper()}_UNKNOWN_FIELD")
            for name in names:
                if values.get(name) in (None, "", {}, []):
                    found.append(f"{section.upper()}_{name.upper()}_UNDETERMINED")
        limits = (self.sections.get("calls") or {}).get("per_mode_call_limits")
        if isinstance(limits, dict) and set(limits) != set(MODES):
            # 모드별 호출 한도는 둘 다 적는다. 같다고 가정하지 않는다.
            found.append("CALLS_PER_MODE_CALL_LIMITS_NEEDS_BOTH_MODES")
        return sorted(set(found))

    def profile_hash(self) -> str:
        return canonical_hash(self.as_dict())

    def profile_id(self) -> str | None:
        """유효한 프로필에만 id가 있다. 미정 항목이 있으면 None."""
        return None if self.problems() else f"xp-{self.profile_hash()[:12]}"


@dataclass(frozen=True)
class OrderIntent:
    """한 입력에 대한 판단 결과 — 모드와 무관하게 같아야 하는 것."""

    input_id: str
    candidate: str | None
    entry_allowed: bool
    block_reason: str | None
    order_price_cap: float | None
    qty: int


_COMPARED = ("candidate", "entry_allowed", "block_reason", "order_price_cap", "qty")


def compare_intents(
    paper: Sequence[OrderIntent], real: Sequence[OrderIntent]
) -> list[dict[str, Any]]:
    """입력별로 후보·진입 허용 여부·주문 상한·수량을 비교한다. 빠진 입력도 불일치다."""
    by_paper = {intent.input_id: intent for intent in paper}
    by_real = {intent.input_id: intent for intent in real}
    if len(by_paper) != len(paper) or len(by_real) != len(real):
        raise ValueError("duplicate input_id in replay intents")
    mismatches: list[dict[str, Any]] = []
    for input_id in sorted(set(by_paper) | set(by_real)):
        left, right = by_paper.get(input_id), by_real.get(input_id)
        if left is None or right is None:
            mismatches.append(
                {"input_id": input_id, "field": "missing", "paper": left is not None,
                 "real": right is not None}
            )
            continue
        for name in _COMPARED:
            if getattr(left, name) != getattr(right, name):
                mismatches.append(
                    {"input_id": input_id, "field": name, "paper": getattr(left, name),
                     "real": getattr(right, name)}
                )
    return mismatches


def replay(
    inputs: Sequence[Mapping[str, Any]],
    decide: Callable[[Mapping[str, Any], Any], OrderIntent],
    adapters: Mapping[str, Any],
    *,
    decision_source: str,
) -> dict[str, Any]:
    """같은 입력을 두 모드 어댑터로 재생해 주문 의도를 비교한다.

    `decide(input, adapter)`가 공통 판단 로직이다. 입력은 재생 가능한 값(JSON)이어야 하며
    그 해시를 결과에 남겨 어떤 입력으로 비교했는지 추적한다.

    `decision_source`는 `decide`가 무엇인지 호출자가 밝히는 표시다. 운영 판단부를 그대로
    건 경우에만 `DECISION_SOURCE_PRODUCTION`을 쓴다. 일부 순수 함수만 건 재생이나 합성
    함수의 재생은 일치하더라도 `evaluate_parity`가 경로 동일성 증거로 받지 않는다.
    """
    if set(adapters) != set(MODES):
        raise ValueError("replay needs exactly the PAPER and REAL adapters")
    if decision_source not in DECISION_SOURCES:
        raise ValueError(f"unknown decision_source: {decision_source!r}")
    frozen = [dict(item) for item in inputs]
    intents = {mode: [decide(item, adapters[mode]) for item in frozen] for mode in MODES}
    mismatches = compare_intents(intents["PAPER"], intents["REAL"])
    return {
        "decision_source": decision_source,
        "inputs": len(frozen),
        "inputs_hash": canonical_hash(frozen),
        "mismatches": mismatches,
        "matched": not mismatches,
    }


def latency_summary(samples_ms: Sequence[float]) -> dict[str, Any]:
    """구간 지연의 표본 수와 p50/p95/max (nearest-rank)."""
    values = sorted(float(v) for v in samples_ms)
    if not values:
        return {"n": 0, "p50": None, "p95": None, "max": None}

    def rank(q: float) -> float:
        return values[max(1, math.ceil(q * len(values))) - 1]

    return {"n": len(values), "p50": rank(0.50), "p95": rank(0.95), "max": values[-1]}


@dataclass(frozen=True)
class ParityContract:
    """환경 차이를 어떻게 판정할지 **관측 전에** 등록하는 계약. None은 미정이다."""

    execution_profile_id: str | None = None
    registered_at: str | None = None
    min_replay_inputs: int | None = None
    min_latency_samples: int | None = None
    # 구간별 REAL p95가 PAPER p95보다 느려도 되는 최대 폭(ms).
    max_p95_excess_ms: dict[str, float] | None = None
    revalidation_days: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ParityContract:
        unknown = set(raw) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown parity contract keys: {sorted(unknown)}")
        return cls(**raw)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def contract_hash(self) -> str:
        return canonical_hash(self.as_dict())

    def problems(self) -> list[str]:
        found: list[str] = []
        if not self.execution_profile_id:
            found.append("EXECUTION_PROFILE_ID_UNDETERMINED")
        if parse_aware(self.registered_at) is None:
            found.append("REGISTERED_AT_UNDETERMINED")
        for name in ("min_replay_inputs", "min_latency_samples", "revalidation_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                found.append(f"{name.upper()}_UNDETERMINED")
        tolerances = self.max_p95_excess_ms
        if not isinstance(tolerances, dict) or set(tolerances) != set(STAGES):
            found.append("MAX_P95_EXCESS_MS_UNDETERMINED")
        elif any((finite_number(v) is None or float(v) < 0) for v in tolerances.values()):
            found.append("MAX_P95_EXCESS_MS_UNDETERMINED")
        return sorted(set(found))


@dataclass(frozen=True)
class ParityObservations:
    """재생·실측 결과. None은 '아직 하지 않았다'이며 통과가 아니다."""

    observation_started_at: str | None
    replay: dict[str, Any] | None
    latency_ms: Mapping[str, Mapping[str, Sequence[float]]]
    readonly_check_passed: bool | None = None
    smoke_reconciled: bool | None = None
    # REAL 관측 지연을 체결 모델에 넣고도 §2 수익·위험 조건을 만족했는가.
    profitability_with_real_latency_passed: bool | None = None
    unexplained_differences: Sequence[str] = ()


def contract_experiment_id(contract: ParityContract) -> str:
    return f"parity-{contract.contract_hash()[:12]}"


def register_contract(store: Store, contract: ParityContract) -> str:
    """환경 차이 계약을 저장소에 잠근다. 미정 항목이 있으면 거부한다."""
    problems = contract.problems()
    if problems:
        raise ValueError(f"parity contract has undetermined values: {problems}")
    experiment_id = contract_experiment_id(contract)
    with store.transaction():
        if store.registration(experiment_id) is None:
            store.insert_registration(
                experiment_id,
                REGISTRATION_KIND,
                contract.as_dict(),
                contract.contract_hash(),
                str(contract.registered_at),
            )
    return experiment_id


def judge_parity(
    store: Store,
    profile: ExecutionProfile,
    experiment_id: str,
    observed: ParityObservations,
    *,
    refs: evidence.EvidenceRefs,
    evaluated_at: str,
) -> Finding:
    """잠긴 계약으로 판정하고 최종 판정이면 증거로 남긴다. 유효기간은 재검증 주기다."""
    registered = store.registration(experiment_id)
    if registered is None or registered["kind"] != REGISTRATION_KIND:
        return not_evaluated("PARITY_CONTRACT_NOT_REGISTERED")
    contract = ParityContract.from_dict(registered["spec"])
    bound = (
        refs.experiment_id == experiment_id
        and refs.execution_profile_id == profile.profile_id()
        and refs.spec_hash == registered["spec_hash"] == contract.contract_hash()
        and refs.rules_code_hash == evidence.rules_code_hash(evidence.KIND_EXECUTION_PARITY)
        and refs.evaluation_rule_version == evidence.EVALUATION_RULE_VERSION
    )
    if not bound:
        return not_evaluated("REFS_DIFFER_FROM_REGISTERED_CONTRACT")
    finding = evaluate_parity(profile, contract, observed)
    if finding.verdict in (Verdict.PASS, Verdict.FAIL, Verdict.INSUFFICIENT):
        evaluated = parse_aware(evaluated_at)
        if evaluated is None:
            raise ValueError("evaluated_at must be an offset-aware ISO timestamp")
        expires = evaluated + timedelta(days=int(contract.revalidation_days or 0))
        inputs = observation_inputs(observed)
        evidence.record(
            store,
            kind=evidence.KIND_EXECUTION_PARITY,
            finding=finding,
            refs=refs,
            data_hash=canonical_hash(inputs),
            evaluated_at=evaluated_at,
            expires_at=expires.isoformat(),
            inputs=inputs,
        )
    return finding


def observation_inputs(observed: ParityObservations) -> dict[str, Any]:
    """판정에 쓴 관측 원본. 증거에 그대로 남겨 나중에 대조할 수 있게 한다."""
    return {
        "observation_started_at": observed.observation_started_at,
        "replay": observed.replay,
        "latency_ms": {
            mode: {stage: [float(v) for v in values] for stage, values in stages.items()}
            for mode, stages in observed.latency_ms.items()
        },
        "readonly_check_passed": observed.readonly_check_passed,
        "smoke_reconciled": observed.smoke_reconciled,
        "profitability_with_real_latency_passed": (
            observed.profitability_with_real_latency_passed
        ),
        "unexplained_differences": list(observed.unexplained_differences),
    }


def evaluate_parity(
    profile: ExecutionProfile, contract: ParityContract, observed: ParityObservations
) -> Finding:
    """`execution_parity_passed` 판정.

    미정 → NOT_EVALUATED, 표본·점검 부족 → INSUFFICIENT, 불일치·허용 범위 초과·미해명
    차이 → FAIL. 전부 채워지고 어긋남이 없을 때만 PASS다.
    """
    undetermined = [f"PROFILE_{p}" for p in profile.problems()]
    undetermined += [f"CONTRACT_{p}" for p in contract.problems()]
    if undetermined:
        return not_evaluated(*undetermined)
    if contract.execution_profile_id != profile.profile_id():
        return not_evaluated("CONTRACT_BOUND_TO_OTHER_PROFILE")

    registered = parse_aware(contract.registered_at)
    started = parse_aware(observed.observation_started_at)
    if started is None:
        return not_evaluated("OBSERVATION_START_UNDETERMINED")
    assert registered is not None
    if registered >= started:
        # 결과를 본 뒤 허용 범위를 정한 계약은 계약이 아니다.
        return not_evaluated("CONTRACT_REGISTERED_AFTER_OBSERVATION_START")

    summary = {
        mode: {
            stage: latency_summary((observed.latency_ms.get(mode) or {}).get(stage) or ())
            for stage in STAGES
        }
        for mode in MODES
    }
    detail: dict[str, Any] = {
        "execution_profile_id": profile.profile_id(),
        "contract_hash": contract.contract_hash(),
        "latency_ms": summary,
        "replay": observed.replay,
    }

    failures: list[str] = []
    shortfalls: list[str] = []
    replay_report = observed.replay
    if replay_report is None:
        shortfalls.append("REPLAY_NOT_RUN")
    else:
        if replay_report.get("mismatches"):
            failures.append("REPLAY_INTENT_MISMATCH")
        if replay_report.get("decision_source") != DECISION_SOURCE_PRODUCTION:
            # 운영 판단부를 걸지 않은 재생은 일치해도 경로 동일성을 말해 주지 않는다.
            shortfalls.append("REPLAY_NOT_ON_PRODUCTION_DECISION_LOGIC")
        if int(replay_report.get("inputs") or 0) < int(contract.min_replay_inputs or 0):
            shortfalls.append("REPLAY_INPUTS_BELOW_MINIMUM")

    tolerances = contract.max_p95_excess_ms or {}
    for stage in STAGES:
        paper, real = summary["PAPER"][stage], summary["REAL"][stage]
        if min(paper["n"], real["n"]) < int(contract.min_latency_samples or 0):
            shortfalls.append(f"LATENCY_SAMPLES_BELOW_MINIMUM_{stage.upper()}")
            continue
        if real["p95"] - paper["p95"] > float(tolerances[stage]):
            failures.append(f"LATENCY_P95_EXCESS_{stage.upper()}")

    for name, value in (
        ("READONLY_CHECK", observed.readonly_check_passed),
        ("SMOKE_RECONCILE", observed.smoke_reconciled),
        ("PROFITABILITY_WITH_REAL_LATENCY", observed.profitability_with_real_latency_passed),
    ):
        if value is None:
            shortfalls.append(f"{name}_NOT_DONE")
        elif value is not True:
            failures.append(f"{name}_FAILED")
    if observed.unexplained_differences:
        failures.append("UNEXPLAINED_MODE_DIFFERENCE")
        detail["unexplained_differences"] = list(observed.unexplained_differences)

    if failures:
        return Finding(Verdict.FAIL, tuple(failures + shortfalls), detail)
    if shortfalls:
        return Finding(Verdict.INSUFFICIENT, tuple(shortfalls), detail)
    return Finding(Verdict.PASS, (), detail)
