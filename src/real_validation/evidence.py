"""승인 증거 — 무엇을 근거로 통과했는지와 그 근거가 지금도 유효한지 (설계 §5).

증거는 자신이 평가된 전략 지문·실험·잠긴 판정식·실행 프로필·비용/체결/위험 정책·판정
코드를 전부 참조한다. 그중 하나라도 현재 값과 다르거나, 만료됐거나, 비어 있으면 현재
증거가 아니다. 판정값(PASS/FAIL)은 과거 사실로 남기고, 유효성은 따로 판단한다.

판정 코드 바인딩이 둘인 이유: 이 패키지는 전략 지문 대상이 아니다. 그래서 판정 로직을
고쳐도 지문은 그대로이고, 사람이 `EVALUATION_RULE_VERSION`을 올리는 것을 잊으면 옛
로직으로 낸 PASS가 계속 현재 증거로 남는다. `rules_code_hash`는 판정에 참여하는 파일의
바이트 해시라 잊을 수 없다. 버전 문자열은 사람이 읽는 이름으로 함께 둔다.

위조에 대해: 저장소는 로컬 파일이다. 해시 사슬·등록 대조·입력 원본 대조는 실수와 서툰
수정을 드러낼 뿐, 쓰기 권한을 가진 사람이 일관된 가짜 저장소를 만드는 것까지 막지는
못한다. 그건 저장소 밖의 통제(판정 시 사슬 머리 해시를 git에 커밋)로 다룬다.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.real_validation.core import (
    Finding,
    Verdict,
    canonical_hash,
    not_evaluated,
    parse_aware,
)
from src.real_validation.store import Store

KST = ZoneInfo("Asia/Seoul")

# 판정 규칙(통계 절차·경계 처리)의 사람이 읽는 이름. 2: 빈 재표집을 0이 아니라 '정의
# 안 됨'으로 처리, 입력 결함·미해결 포지션은 최종 판정이 아니라 판정 보류.
EVALUATION_RULE_VERSION = "real-validation-rules-2"

KIND_PROFITABILITY = "PROFITABILITY"
KIND_EXECUTION_PARITY = "EXECUTION_PARITY"
EVIDENCE_KINDS = (KIND_PROFITABILITY, KIND_EXECUTION_PARITY)

# 판정값을 만드는 파일. 여기 든 파일이 한 바이트라도 바뀌면 그 종류의 기존 증거는 낡는다.
# 저장·게이트 배선(store/gate/evidence)은 판정값을 만들지 않으므로 넣지 않는다.
RULE_FILES: dict[str, tuple[str, ...]] = {
    KIND_PROFITABILITY: ("core.py", "costs.py", "profitability.py"),
    KIND_EXECUTION_PARITY: ("core.py", "execution_profile.py"),
}
_PACKAGE_DIR = Path(__file__).resolve().parent

GENESIS_HASH = "0" * 64

# 한 실험에 한 번만 남길 수 있는 최종 판정. COLLECTING은 판정이 아니라 진행 상태다.
_FINAL_VERDICTS = (Verdict.PASS, Verdict.FAIL, Verdict.INSUFFICIENT)


@lru_cache(maxsize=None)
def rules_code_hash(kind: str) -> str:
    """그 종류의 판정에 참여하는 소스 파일의 SHA-256. `release.strategy_fingerprint`와 같은 방식.

    파일을 읽지 못하면 예외가 난다 — 알 수 없는 코드로 '현재 증거'를 만들지 않는다.
    """
    digest = hashlib.sha256()
    for name in RULE_FILES[kind]:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((_PACKAGE_DIR / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def runtime_info() -> dict[str, str]:
    """판정이 돌아간 실행 환경의 기록.

    판정 파일 해시가 잡지 못하는 것(인터프리터·표준 라이브러리)을 남긴다.

    일치를 강제하지는 않는다 — 환경이 달라졌으면 재계산 감사로 결과가 같은지 확인한다.
    """
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": sys.platform,
        "sqlite": sqlite3.sqlite_version,
    }


@dataclass(frozen=True)
class EvidenceRefs:
    """증거가 묶인 버전들. 하나라도 None이면 '미정'이며 일치로 보지 않는다."""

    strategy_fingerprint: str | None
    experiment_id: str | None
    spec_hash: str | None
    execution_profile_id: str | None
    execution_profile_hash: str | None
    cost_policy_hash: str | None
    fill_model_hash: str | None
    risk_policy_hash: str | None
    evaluation_rule_version: str | None
    rules_code_hash: str | None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)

    def missing(self) -> list[str]:
        return [name for name, value in self.as_dict().items() if not value]


class DuplicateVerdictError(Exception):
    """같은 실험에 최종 판정을 다시 남기려 했다 — 연장·재시험은 새 실험으로 등록한다."""


def _normalized(value: Any) -> Any:
    """저장했다 읽은 것과 같은 모양으로 만든다(튜플→리스트). 해시가 왕복 뒤에도 같게."""
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))


def _row_hash(prev_hash: str, row: dict[str, Any]) -> str:
    return canonical_hash(
        {
            "prev_hash": prev_hash,
            "kind": row["kind"],
            "verdict": row["verdict"],
            "refs": row["refs"],
            "data_hash": row.get("data_hash"),
            "evaluated_at": row["evaluated_at"],
            "expires_at": row.get("expires_at"),
            "reasons": row.get("reasons") or [],
            "detail": row.get("detail") or {},
            "inputs": row.get("inputs"),
        }
    )


def verify_chain(store: Store) -> list[str]:
    """증거 행 전체의 해시 사슬을 다시 계산한다. 고쳐지거나 빠진 행이 있으면 사유를 낸다."""
    problems: list[str] = []
    previous = GENESIS_HASH
    for row in store.evidence():
        if row["prev_hash"] != previous:
            problems.append(f"EVIDENCE_CHAIN_BROKEN_AT_{row['id']}")
        if _row_hash(row["prev_hash"], row) != row["row_hash"]:
            problems.append(f"EVIDENCE_ROW_ALTERED_{row['id']}")
        previous = row["row_hash"]
    return problems


def chain_head(store: Store) -> str:
    """마지막 증거 행의 해시. 판정 직후 이 값을 git에 남기면 저장소 밖에서 대조할 수 있다."""
    rows = store.evidence()
    return rows[-1]["row_hash"] if rows else GENESIS_HASH


def record(
    store: Store,
    *,
    kind: str,
    finding: Finding,
    refs: EvidenceRefs,
    data_hash: str | None,
    evaluated_at: str,
    expires_at: str | None,
    inputs: Any = None,
    rerun_reason: str | None = None,
) -> int:
    """증거 한 건을 추가한다. 기존 행은 건드리지 않는다.

    PASS는 참조가 전부 채워지고 만료일·입력 원본이 있을 때만 남길 수 있다.

    같은 실험의 최종 판정은 한 번뿐이다 — 유리한 결과가 나올 때까지 다시 판정하는 것을
    저장 단계에서 막는다. 예외는 하나다: **판정 코드가 바뀌었고**(`rules_code_hash`가
    다름) **입력이 같으며**(`data_hash`가 같음) 사유를 적은 재판정. 이전 판정은 그대로
    남고 새 행이 그것을 가리킨다. 자료를 바꾸거나 사유 없이 다시 돌릴 수는 없다.
    """
    if kind not in EVIDENCE_KINDS:
        raise ValueError(f"unknown evidence kind: {kind}")
    if parse_aware(evaluated_at) is None:
        raise ValueError("evaluated_at must be an offset-aware ISO timestamp")
    if expires_at is not None and parse_aware(expires_at) is None:
        raise ValueError("expires_at must be an offset-aware ISO timestamp")
    if inputs is not None and canonical_hash(_normalized(inputs)) != data_hash:
        raise ValueError("data_hash must be the canonical hash of the stored inputs")
    if finding.verdict is Verdict.PASS:
        if refs.missing():
            raise ValueError(f"PASS evidence needs every reference: missing {refs.missing()}")
        if expires_at is None or data_hash is None or inputs is None:
            raise ValueError("PASS evidence needs expires_at, data_hash and the inputs")

    detail = dict(finding.detail)
    detail["runtime"] = runtime_info()
    with store.transaction():
        if finding.verdict in _FINAL_VERDICTS and refs.experiment_id:
            prior = last_final(store, kind, refs.experiment_id)
            if prior is not None:
                rule_changed = prior["refs"].get("rules_code_hash") != refs.rules_code_hash
                same_data = data_hash is not None and prior.get("data_hash") == data_hash
                reason = (rerun_reason or "").strip()
                if not (rule_changed and same_data and reason):
                    raise DuplicateVerdictError(
                        f"{kind} verdict already recorded for {refs.experiment_id}: "
                        f"{prior['verdict']} (id={prior['id']}); rejudging needs changed "
                        "rule code, identical data and a rerun reason"
                    )
                detail["rejudged_from"] = prior["id"]
                detail["previous_verdict"] = prior["verdict"]
                detail["rerun_reason"] = reason
        row = {
            "kind": kind,
            "verdict": finding.verdict.value,
            "refs": refs.as_dict(),
            "data_hash": data_hash,
            "evaluated_at": evaluated_at,
            "expires_at": expires_at,
            "reasons": list(finding.reasons),
            "detail": _normalized(detail),
            "inputs": None if inputs is None else _normalized(inputs),
        }
        row["prev_hash"] = chain_head(store)
        row["row_hash"] = _row_hash(row["prev_hash"], row)
        return store.insert_evidence(row)


def last_final(store: Store, kind: str, experiment_id: str) -> dict[str, Any] | None:
    """그 실험에 남은 가장 최근 최종 판정."""
    for row in reversed(store.evidence(kind)):
        same = row["refs"].get("experiment_id") == experiment_id
        if same and Verdict(row["verdict"]) in _FINAL_VERDICTS:
            return row
    return None


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


def check_binding(
    record_row: dict[str, Any] | None,
    registration: dict[str, Any] | None,
    *,
    not_before: str | None,
) -> Finding:
    """증거가 잠긴 등록과 자기 입력 원본에 실제로 묶여 있는지 본다.

    참조 문자열만 맞춘 행을 걸러낸다: 등록이 있어야 하고, 등록의 해시가 그 내용과 맞아야
    하고, 증거가 그 해시를 가리켜야 하고, 입력 원본의 해시가 `data_hash`여야 하고,
    `not_before`(판정일, YYYYMMDD) 전에 내려진 판정이 아니어야 한다.
    """
    if record_row is None:
        return not_evaluated("NO_EVIDENCE")
    if registration is None:
        return Finding(Verdict.FAIL, ("REGISTRATION_MISSING",))
    reasons: list[str] = []
    if canonical_hash(registration["spec"]) != registration["spec_hash"]:
        reasons.append("REGISTRATION_SPEC_ALTERED")
    if (record_row.get("refs") or {}).get("spec_hash") != registration["spec_hash"]:
        reasons.append("EVIDENCE_NOT_BOUND_TO_REGISTERED_SPEC")
    inputs = record_row.get("inputs")
    if inputs is None:
        reasons.append("INPUTS_MISSING")
    elif canonical_hash(inputs) != record_row.get("data_hash"):
        reasons.append("INPUTS_DO_NOT_MATCH_DATA_HASH")
    evaluated = parse_aware(record_row.get("evaluated_at"))
    if not_before is not None and evaluated is not None:
        if evaluated.astimezone(KST).strftime("%Y%m%d") < not_before:
            reasons.append("EVALUATED_BEFORE_JUDGMENT_DATE")
    if reasons:
        return Finding(Verdict.FAIL, tuple(reasons))
    return Finding(Verdict.PASS)
