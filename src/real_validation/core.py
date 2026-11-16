"""판정값·해시·시각 — 게이트 전 모듈이 공유하는 최소 공통부."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    """설계 §2.1 판정값. PASS만 통과다 — 나머지 넷은 전부 차단이다."""

    NOT_EVALUATED = "NOT_EVALUATED"
    COLLECTING = "COLLECTING"
    INSUFFICIENT = "INSUFFICIENT"
    FAIL = "FAIL"
    PASS = "PASS"


@dataclass(frozen=True)
class Finding:
    """게이트 한 항목의 판정과 차단 사유."""

    verdict: Verdict
    reasons: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.verdict is Verdict.PASS

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "detail": self.detail,
        }


def not_evaluated(*reasons: str, **detail: Any) -> Finding:
    return Finding(Verdict.NOT_EVALUATED, tuple(reasons), dict(detail))


def canonical_hash(value: Any) -> str:
    """정규화 SHA-256. `src/db.py`의 `_canonical_config_hash`와 같은 직렬화 규약이다.

    NaN·무한대는 JSON으로 안정적으로 직렬화되지 않으므로 거부한다 — 해시가 같은데
    값이 다른 증거를 만들지 않기 위해서다.
    """
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_aware(value: object) -> datetime | None:
    """오프셋이 있는 ISO 시각만 받는다. 없으면 None — 추정한 시간대로 만료를 판단하지 않는다."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def is_yyyymmdd(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        return False
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return False
    return True


def finite_number(value: object) -> float | None:
    """유한한 실수만 돌려준다. bool·None·문자열·NaN·무한대는 미정으로 본다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
