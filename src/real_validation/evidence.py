"""승인 증거 — 무엇을 근거로 통과했는지와 그 근거가 지금도 유효한지 (설계 §5).

증거는 자신이 평가된 전략 지문·실험·실행 프로필·비용/체결/위험 정책·평가 규칙 버전을
전부 참조한다. 그중 하나라도 현재 값과 다르거나, 만료됐거나, 비어 있으면 현재 증거가
아니다. 판정값(PASS/FAIL)은 과거 사실로 남기고, 유효성은 따로 판단한다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from src.real_validation.core import Finding, Verdict, not_evaluated, parse_aware
from src.real_validation.store import Store

# 판정 규칙(통계 절차·경계 처리)이 바뀌면 올린다. 올리면 기존 증거는 전부 낡은 증거가 된다.
EVALUATION_RULE_VERSION = "real-validation-rules-1"

KIND_PROFITABILITY = "PROFITABILITY"
KIND_EXECUTION_PARITY = "EXECUTION_PARITY"
EVIDENCE_KINDS = (KIND_PROFITABILITY, KIND_EXECUTION_PARITY)

# 한 실험에 한 번만 남길 수 있는 최종 판정. COLLECTING은 판정이 아니라 진행 상태다.
_FINAL_VERDICTS = (Verdict.PASS, Verdict.FAIL, Verdict.INSUFFICIENT)


@dataclass(frozen=True)
class EvidenceRefs:
    """증거가 묶인 버전들. 하나라도 None이면 '미정'이며 일치로 보지 않는다."""

    strategy_fingerprint: str | None
    experiment_id: str | None
    execution_profile_id: str | None
    execution_profile_hash: str | None
    cost_policy_hash: str | None
    fill_model_hash: str | None
    risk_policy_hash: str | None
    evaluation_rule_version: str | None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)

    def missing(self) -> list[str]:
        return [name for name, value in self.as_dict().items() if not value]


class DuplicateVerdictError(Exception):
    """같은 실험에 최종 판정을 다시 남기려 했다 — 연장·재시험은 새 실험으로 등록한다."""


def record(
    store: Store,
    *,
    kind: str,
    finding: Finding,
    refs: EvidenceRefs,
    data_hash: str | None,
    evaluated_at: str,
    expires_at: str | None,
) -> int:
    """증거 한 건을 추가한다. 기존 행은 건드리지 않는다.

    PASS는 참조가 전부 채워지고 만료일이 있을 때만 남길 수 있다. 같은 실험의 최종 판정은
    한 번뿐이다 — 유리한 결과가 나올 때까지 다시 판정하는 것을 저장 단계에서 막는다.
    """
    if kind not in EVIDENCE_KINDS:
        raise ValueError(f"unknown evidence kind: {kind}")
    if parse_aware(evaluated_at) is None:
        raise ValueError("evaluated_at must be an offset-aware ISO timestamp")
    if expires_at is not None and parse_aware(expires_at) is None:
        raise ValueError("expires_at must be an offset-aware ISO timestamp")
    if finding.verdict is Verdict.PASS:
        if refs.missing():
            raise ValueError(f"PASS evidence needs every reference: missing {refs.missing()}")
        if expires_at is None or data_hash is None:
            raise ValueError("PASS evidence needs expires_at and data_hash")

    with store.transaction():
        if finding.verdict in _FINAL_VERDICTS and refs.experiment_id:
            for previous in store.evidence(kind):
                same = previous["refs"].get("experiment_id") == refs.experiment_id
                if same and Verdict(previous["verdict"]) in _FINAL_VERDICTS:
                    raise DuplicateVerdictError(
                        f"{kind} verdict already recorded for {refs.experiment_id}: "
                        f"{previous['verdict']} (id={previous['id']})"
                    )
        return store.insert_evidence(
            {
                "kind": kind,
                "verdict": finding.verdict.value,
                "refs": refs.as_dict(),
                "data_hash": data_hash,
                "evaluated_at": evaluated_at,
                "expires_at": expires_at,
                "reasons": list(finding.reasons),
                "detail": finding.detail,
            }
        )


def latest(store: Store, kind: str) -> dict[str, Any] | None:
    rows = store.evidence(kind)
    return rows[-1] if rows else None


def verdict_of(record_row: dict[str, Any] | None) -> Finding:
    """기록된 판정을 그대로 옮긴다. 기록이 없으면 미평가다."""
    if record_row is None:
        return not_evaluated("NO_EVIDENCE")
    return Finding(
        Verdict(record_row["verdict"]),
        tuple(record_row.get("reasons") or ()),
        {"evidence_id": record_row.get("id"), "evaluated_at": record_row.get("evaluated_at")},
    )


def check_current(
    record_row: dict[str, Any] | None, expected: EvidenceRefs, now: datetime
) -> Finding:
    """증거가 현재 버전에 묶여 있고 유효기간 안인지 판정한다.

    판정값이 PASS인지는 여기서 보지 않는다(그건 `verdict_of`). 여기서 FAIL은 '증거가
    낡았다'는 뜻이다.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be offset-aware")
    if record_row is None:
        return not_evaluated("NO_EVIDENCE")

    reasons: list[str] = []
    undetermined = expected.missing()
    if undetermined:
        # 현재 버전을 알 수 없으면 무엇과도 '일치'라고 할 수 없다.
        return not_evaluated(*(f"CURRENT_{name.upper()}_UNDETERMINED" for name in undetermined))

    refs = record_row.get("refs") or {}
    for name, current in expected.as_dict().items():
        recorded = refs.get(name)
        if not recorded:
            reasons.append(f"EVIDENCE_{name.upper()}_MISSING")
        elif recorded != current:
            reasons.append(f"STALE_{name.upper()}")

    evaluated_at = parse_aware(record_row.get("evaluated_at"))
    expires_at = parse_aware(record_row.get("expires_at"))
    if evaluated_at is None:
        reasons.append("EVALUATED_AT_INVALID")
    elif evaluated_at > now:
        reasons.append("EVALUATED_AT_IN_FUTURE")
    if expires_at is None:
        reasons.append("EXPIRY_UNDETERMINED")
    elif now >= expires_at:
        reasons.append("EVIDENCE_EXPIRED")
    if not record_row.get("data_hash"):
        reasons.append("DATA_HASH_MISSING")

    detail = {
        "evidence_id": record_row.get("id"),
        "evaluated_at": record_row.get("evaluated_at"),
        "expires_at": record_row.get("expires_at"),
    }
    if reasons:
        return Finding(Verdict.FAIL, tuple(reasons), detail)
    return Finding(Verdict.PASS, (), detail)
