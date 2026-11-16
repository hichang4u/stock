"""비용 정책 버전과 실제 순손익 — 미끄러짐을 두 번 빼지 않는다 (설계 §4.1).

실제 체결가로 계산한 손익에는 가격 미끄러짐이 이미 들어 있다. 그래서 `realized_pnl`은
수수료·세금만 빼고, 결정가 대비 슬리피지는 **참고값**으로만 돌려준다. 추가로 불리하게
조정한 스트레스 손익은 `stress_pnl`이 따로 만들며 그 사실을 결과에 표시한다.

기존 `src/modules/cost_model.py`·`src/perf_costs.py`의 conservative 손익은 실제 체결
손익에서 사유별 슬리피지를 한 번 더 뺀 값이다. 그 값은 연구용 스트레스 수치이고 실제
순손익이 아니므로, 수익성 판정 입력으로 받지 않는다(`profitability.PNL_BASIS`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from src.modules import cost_model
from src.real_validation.core import canonical_hash, finite_number, is_yyyymmdd


class CostBasis(str, Enum):
    """비용 수치의 출처. 시나리오 가정을 실계좌 요율이나 정산값으로 부르지 않는다."""

    SCENARIO_ASSUMPTION = "SCENARIO_ASSUMPTION"
    ACCOUNT_SCHEDULE = "ACCOUNT_SCHEDULE"
    SETTLED = "SETTLED"


@dataclass(frozen=True)
class CostPolicyVersion:
    """적용일·시장·계좌 수수료 조건 하나에 대한 요율.

    요율은 분리형(매수 수수료·매도 수수료·매도 세금) 또는 왕복 합계형 중 하나만 준다.
    왕복 합계형은 매수금액에 곱한다 — 기존 연구 상수 0.18%p가 그 형태다.
    """

    effective_from: str
    market: str
    account_condition: str
    basis: CostBasis
    buy_fee_rate: float | None = None
    sell_fee_rate: float | None = None
    sell_tax_rate: float | None = None
    round_trip_rate: float | None = None

    def problems(self) -> list[str]:
        found: list[str] = []
        if not is_yyyymmdd(self.effective_from):
            found.append("EFFECTIVE_FROM_INVALID")
        if not self.market or not self.account_condition:
            found.append("MARKET_OR_ACCOUNT_CONDITION_UNDETERMINED")
        split = (self.buy_fee_rate, self.sell_fee_rate, self.sell_tax_rate)
        has_split = any(value is not None for value in split)
        has_round = self.round_trip_rate is not None
        if has_split == has_round:
            found.append("RATE_FORM_AMBIGUOUS")
        rates = split if has_split else (self.round_trip_rate,)
        for value in rates:
            number = finite_number(value)
            if number is None:
                found.append("RATE_UNDETERMINED")
            elif number < 0 or number >= 1:
                found.append("RATE_OUT_OF_RANGE")
        return found

    def as_dict(self) -> dict[str, Any]:
        return {
            "effective_from": self.effective_from,
            "market": self.market,
            "account_condition": self.account_condition,
            "basis": self.basis.value,
            "buy_fee_rate": self.buy_fee_rate,
            "sell_fee_rate": self.sell_fee_rate,
            "sell_tax_rate": self.sell_tax_rate,
            "round_trip_rate": self.round_trip_rate,
        }


@dataclass(frozen=True)
class CostPolicy:
    policy_id: str
    versions: tuple[CostPolicyVersion, ...]

    def problems(self) -> list[str]:
        found: list[str] = []
        if not self.policy_id:
            found.append("POLICY_ID_UNDETERMINED")
        if not self.versions:
            found.append("NO_VERSIONS")
        seen: set[tuple[str, str, str]] = set()
        for version in self.versions:
            found.extend(version.problems())
            key = (version.effective_from, version.market, version.account_condition)
            if key in seen:
                found.append("DUPLICATE_VERSION")
            seen.add(key)
        return sorted(set(found))

    def policy_hash(self) -> str:
        return canonical_hash(
            {"policy_id": self.policy_id, "versions": [v.as_dict() for v in self.versions]}
        )

    def version_for(
        self, trade_date: str, market: str, account_condition: str
    ) -> CostPolicyVersion:
        """거래일에 적용되는 버전. 적용일 이전 거래에 뒤 버전을 소급하지 않는다."""
        candidates = [
            v
            for v in self.versions
            if v.market == market
            and v.account_condition == account_condition
            and v.effective_from <= trade_date
        ]
        if not candidates:
            raise LookupError(
                f"no cost policy version for {trade_date} {market} {account_condition}"
            )
        return max(candidates, key=lambda v: v.effective_from)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CostPolicy:
        allowed = set(CostPolicyVersion.__dataclass_fields__)
        versions = []
        for item in raw.get("versions") or []:
            unknown = set(item) - allowed
            if unknown:
                raise ValueError(f"unknown cost policy keys: {sorted(unknown)}")
            versions.append(CostPolicyVersion(**{**item, "basis": CostBasis(item["basis"])}))
        return cls(policy_id=str(raw.get("policy_id") or ""), versions=tuple(versions))


def research_scenario_policy() -> CostPolicy:
    """기존 연구 상수(왕복 0.18%p)를 시나리오 가정으로 보존한다.

    값은 `cost_model`에서 읽는다 — 상수가 바뀌면 정책 해시도 바뀐다. 이 정책의 출처는
    늘 SCENARIO_ASSUMPTION이며 실계좌 요율로 승격되지 않는다.
    """
    return CostPolicy(
        policy_id=f"research-{cost_model.COST_MODEL_VERSION}",
        versions=(
            CostPolicyVersion(
                effective_from="20260101",
                market="KRX",
                account_condition="RESEARCH",
                basis=CostBasis.SCENARIO_ASSUMPTION,
                round_trip_rate=cost_model.BASE_ROUND_TRIP_COST_PCT / 100,
            ),
        ),
    )


@dataclass(frozen=True)
class Fill:
    """체결 한 건. 부분체결은 여러 건으로 준다."""

    qty: int
    price: float


def _amount(fills: Sequence[Fill]) -> tuple[int, float]:
    qty = 0
    amount = 0.0
    for fill in fills:
        if fill.qty <= 0 or fill.price <= 0:
            raise ValueError("fill qty and price must be positive")
        qty += fill.qty
        amount += fill.qty * fill.price
    return qty, amount


def realized_pnl(
    buys: Sequence[Fill],
    sells: Sequence[Fill],
    version: CostPolicyVersion,
    *,
    decision_price: float | None = None,
    settled_fees: float | None = None,
    settled_taxes: float | None = None,
) -> dict[str, Any]:
    """실제 체결로 확정된 순손익. 수수료·세금만 뺀다.

    이 함수에는 슬리피지를 받는 인자가 없다 — 체결가에 이미 들어 있는 것을 다시 뺄 길을
    열어 두지 않는다. 매수·매도 수량이 다르면 아직 끝나지 않은 포지션이므로 거부한다.
    정산 수수료·세금을 주면 그 값을 쓰고 출처를 SETTLED로 표시한다.
    """
    if version.problems():
        raise ValueError(f"cost policy version invalid: {version.problems()}")
    buy_qty, buy_amount = _amount(buys)
    sell_qty, sell_amount = _amount(sells)
    if buy_qty == 0 or buy_qty != sell_qty:
        raise ValueError(
            f"position not flat: buy_qty={buy_qty} sell_qty={sell_qty} — not a realized result"
        )
    if (settled_fees is None) != (settled_taxes is None):
        raise ValueError("settled fees and taxes must be given together")

    if settled_fees is not None and settled_taxes is not None:
        fees, taxes, basis = float(settled_fees), float(settled_taxes), CostBasis.SETTLED
    elif version.round_trip_rate is not None:
        fees, taxes, basis = buy_amount * version.round_trip_rate, 0.0, version.basis
    else:
        fees = buy_amount * float(version.buy_fee_rate or 0.0) + sell_amount * float(
            version.sell_fee_rate or 0.0
        )
        taxes = sell_amount * float(version.sell_tax_rate or 0.0)
        basis = version.basis

    gross_amount = sell_amount - buy_amount
    net_amount = gross_amount - fees - taxes
    avg_buy = buy_amount / buy_qty
    return {
        "qty": buy_qty,
        "buy_amount": buy_amount,
        "sell_amount": sell_amount,
        "avg_buy_price": avg_buy,
        "avg_sell_price": sell_amount / sell_qty,
        "gross_amount": gross_amount,
        "gross_pct": gross_amount / buy_amount * 100,
        "fees": fees,
        "taxes": taxes,
        "net_amount": net_amount,
        "net_pct": net_amount / buy_amount * 100,
        "cost_basis": basis.value,
        "is_estimate": basis is not CostBasis.SETTLED,
        "slippage_deducted_pct": 0.0,
        # 참고값: 결정가보다 얼마나 불리하게 샀는가. gross에 이미 반영돼 있다.
        "entry_slippage_vs_decision_pct": (
            None if not decision_price or decision_price <= 0
            else (avg_buy / decision_price - 1) * 100
        ),
        "pnl_basis": "ACTUAL_FILL_NET",
    }


def stress_pnl(realized: dict[str, Any], *, extra_adverse_pct: float) -> dict[str, Any]:
    """실제 순손익에 사전등록한 불리 조정을 **추가로** 얹은 스트레스 손익.

    결과는 실제 손익이 아니다. 조정폭과 '추가 불리 조정'이라는 사실을 함께 돌려준다.
    """
    if realized.get("pnl_basis") != "ACTUAL_FILL_NET":
        raise ValueError("stress adjustment applies to an actual-fill net result only")
    extra = finite_number(extra_adverse_pct)
    if extra is None or extra < 0:
        raise ValueError("extra_adverse_pct must be a non-negative finite number")
    return {
        "stress_net_pct": realized["net_pct"] - extra,
        "stress_net_amount": realized["net_amount"] - realized["buy_amount"] * extra / 100,
        "additional_adverse_adjustment_pct": extra,
        "is_additional_adverse_adjustment": True,
        "pnl_basis": "STRESS_ON_ACTUAL_FILL",
    }
