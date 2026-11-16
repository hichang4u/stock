"""계좌 위험 정책 — 필수 한도와 그 유효성 (설계 §4.2).

숫자는 여기서 정하지 않는다. 사용자의 실제 자본·허용 손실을 모르기 때문이다. 정책 파일에
값이 없으면 '미정', 0이면 '0으로 무제한'으로 보고 둘 다 REAL 신규 진입 차단 사유로 낸다.

기존 `F3_ALLOC_RATIO <= 0.20` 조건은 그대로 유지한다(`MAX_ALLOC_RATIO`). 이 정책의
투입비율은 그 상한 안에서만 유효하다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from src.real_validation.core import canonical_hash, finite_number

# 기존 준비도 게이트의 초기 실전 투입비율 상한(src/readiness.py `initial_allocation`).
MAX_ALLOC_RATIO = 0.20

CVAR_INSUFFICIENT_ACTIONS = ("BLOCK",)

_RATIO_FIELDS = (
    "max_drawdown_ratio",
    "cvar_alpha",
    "cvar_limit_ratio",
    "alloc_ratio",
    "usable_depth_ratio",
    "max_participation_ratio",
)
_POSITIVE_FIELDS = (
    "approved_account_capital",
    "absolute_order_cap",
    "max_quote_age_ms",
    "max_entry_delay_ms",
    "account_data_max_age_sec",
)
_POSITIVE_INT_FIELDS = ("cvar_window_days", "cvar_min_samples", "max_consecutive_losses")
_STRESS_COMPONENTS = (
    "stress_stop_loss_ratio",
    "stress_delay_gap_ratio",
    "stress_adverse_fill_ratio",
    "stress_cost_ratio",
)


@dataclass(frozen=True)
class RiskPolicy:
    """필수 정책값. None은 미정이다. 금액은 원, 비율은 0~1이다.

    거래당 손실 예산과 일일 손실 한도는 원 또는 승인 자본 대비 비율 중 **하나만** 준다.
    스트레스 손실률은 구성요소의 합이다 — 손절 폭만으로 채울 수 없게 나눠 받는다.
    """

    policy_version: str | None = None
    approved_account_capital: float | None = None
    per_trade_loss_budget_won: float | None = None
    per_trade_loss_budget_ratio: float | None = None
    daily_loss_limit_won: float | None = None
    daily_loss_limit_ratio: float | None = None
    max_drawdown_ratio: float | None = None
    cvar_alpha: float | None = None
    cvar_window_days: int | None = None
    cvar_min_samples: int | None = None
    cvar_limit_ratio: float | None = None
    cvar_insufficient_action: str | None = None
    max_consecutive_losses: int | None = None
    absolute_order_cap: float | None = None
    alloc_ratio: float | None = None
    usable_depth_ratio: float | None = None
    max_participation_ratio: float | None = None
    max_quote_age_ms: float | None = None
    max_entry_delay_ms: float | None = None
    stress_stop_loss_ratio: float | None = None
    stress_delay_gap_ratio: float | None = None
    stress_adverse_fill_ratio: float | None = None
    stress_cost_ratio: float | None = None
    account_data_max_age_sec: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def policy_hash(self) -> str:
        return canonical_hash(self.as_dict())

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RiskPolicy:
        # 오타 난 키를 조용히 버리면 한도 하나가 사라진 채로 통과한다.
        unknown = set(raw) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown risk policy keys: {sorted(unknown)}")
        return cls(**raw)

    def _won_or_ratio(self, name: str) -> tuple[float | None, list[str]]:
        label = name.upper()
        won = getattr(self, f"{name}_won")
        ratio = getattr(self, f"{name}_ratio")
        if won is None and ratio is None:
            return None, [f"{label}_UNDETERMINED"]
        if won is not None and ratio is not None:
            return None, [f"{label}_BOTH_WON_AND_RATIO"]
        if won is not None:
            number = finite_number(won)
            if number is None:
                return None, [f"{label}_UNDETERMINED"]
            if number == 0:
                return None, [f"{label}_ZERO_MEANS_UNLIMITED"]
            return (number, []) if number > 0 else (None, [f"{label}_NEGATIVE"])
        number = finite_number(ratio)
        capital = finite_number(self.approved_account_capital)
        if number is None:
            return None, [f"{label}_UNDETERMINED"]
        if number == 0:
            return None, [f"{label}_ZERO_MEANS_UNLIMITED"]
        if not 0 < number < 1:
            return None, [f"{label}_RATIO_OUT_OF_RANGE"]
        if capital is None or capital <= 0:
            return None, []  # 자본 미정은 아래에서 따로 보고한다
        return number * capital, []

    def per_trade_loss_budget(self) -> float | None:
        return self._won_or_ratio("per_trade_loss_budget")[0]

    def daily_loss_limit(self) -> float | None:
        return self._won_or_ratio("daily_loss_limit")[0]

    def stress_loss_ratio(self) -> float | None:
        """수량 산정에 쓰는 스트레스 손실률. 구성요소가 하나라도 미정이면 None."""
        parts = [finite_number(getattr(self, name)) for name in _STRESS_COMPONENTS]
        if any(part is None or part < 0 for part in parts):
            return None
        return sum(part for part in parts if part is not None)

    def problems(self) -> list[str]:
        """차단 사유. 비어 있어야 유효한 정책이다."""
        found: list[str] = []
        if not self.policy_version:
            found.append("POLICY_VERSION_UNDETERMINED")
        found.extend(self._won_or_ratio("per_trade_loss_budget")[1])
        found.extend(self._won_or_ratio("daily_loss_limit")[1])

        for name in _POSITIVE_FIELDS:
            number = finite_number(getattr(self, name))
            if number is None:
                found.append(f"{name.upper()}_UNDETERMINED")
            elif number == 0:
                found.append(f"{name.upper()}_ZERO_MEANS_UNLIMITED")
            elif number < 0:
                found.append(f"{name.upper()}_NEGATIVE")
        for name in _RATIO_FIELDS:
            number = finite_number(getattr(self, name))
            if number is None:
                found.append(f"{name.upper()}_UNDETERMINED")
            elif number == 0:
                found.append(f"{name.upper()}_ZERO_MEANS_UNLIMITED")
            elif not 0 < number <= 1:
                found.append(f"{name.upper()}_OUT_OF_RANGE")
        for name in _POSITIVE_INT_FIELDS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                found.append(f"{name.upper()}_UNDETERMINED")
            elif value == 0:
                found.append(f"{name.upper()}_ZERO_MEANS_UNLIMITED")
            elif value < 0:
                found.append(f"{name.upper()}_NEGATIVE")

        alloc = finite_number(self.alloc_ratio)
        if alloc is not None and alloc > MAX_ALLOC_RATIO:
            found.append("ALLOC_RATIO_ABOVE_20_PERCENT")
        if self.cvar_insufficient_action not in CVAR_INSUFFICIENT_ACTIONS:
            found.append("CVAR_INSUFFICIENT_ACTION_UNDETERMINED")
        if (
            isinstance(self.cvar_min_samples, int)
            and isinstance(self.cvar_window_days, int)
            and self.cvar_min_samples > self.cvar_window_days > 0
        ):
            found.append("CVAR_MIN_SAMPLES_ABOVE_WINDOW")

        for name in _STRESS_COMPONENTS:
            number = finite_number(getattr(self, name))
            if number is None:
                found.append(f"{name.upper()}_UNDETERMINED")
            elif number < 0 or number >= 1:
                found.append(f"{name.upper()}_OUT_OF_RANGE")
        stop = finite_number(self.stress_stop_loss_ratio)
        total = self.stress_loss_ratio()
        if stop is not None and stop == 0:
            found.append("STRESS_STOP_LOSS_RATIO_ZERO_MEANS_UNLIMITED")
        if stop is not None and total is not None and stop > 0 and total <= stop:
            # 설계: 하드스탑 폭만을 최대 손실률로 쓰지 않는다.
            found.append("STRESS_LOSS_EQUALS_STOP_ONLY")
        if total is not None and total >= 1:
            found.append("STRESS_LOSS_RATIO_OUT_OF_RANGE")
        return sorted(set(found))
