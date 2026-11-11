"""신규 주문 허용 금액·수량과 미체결 예산 예약 (설계 §4.2).

허용 금액 = min(현금 한도, 절대 주문 상한, 유동성 한도, 남은 거래당 손실 예산 ÷ 스트레스
손실률). 가격은 호가단위로, 수량은 정수로 **내림**한다. 넷 중 하나라도 알 수 없으면
수량은 0이다 — 모르는 한도를 무제한으로 보지 않는다.

순수 계산과 예약 장부만 있다. 기존 `f3_entry.entry_budget`(투입비율·주문금액 상한)을
대체하지 않으며, 런타임 연동은 전략 파일 변경이라 별도 릴리스다.

스트레스 손실률은 수량 산정 가정일 뿐 실제 손실 상한의 보장이 아니다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from src.real_validation.core import finite_number
from src.real_validation.risk_policy import RiskPolicy
from src.real_validation.store import Store

# 예약은 대사로 확인된 결과로만 푼다. '시간이 지났다'는 해제 사유가 아니다.
RELEASE_REASONS = ("CANCEL_CONFIRMED", "FILL_RECONCILED", "REJECT_CONFIRMED")


@dataclass(frozen=True)
class SizingInput:
    """주문 직전 스냅샷. None은 '모른다'이며 0과 다르다."""

    price: float
    tick_size: int
    available_cash: float | None
    account_data_age_sec: float | None
    # 지정가 상한 이내 매도호가 잔량의 금액 합. 최우선 호가 하나만 넣지 않는다.
    ask_depth_amount: float | None
    # 참여율 기준이 되는 예상 거래대금.
    expected_trade_amount: float | None
    quote_age_ms: float | None
    entry_delay_ms: float | None
    # 보유 포지션에 이미 걸린 스트레스 손실(원).
    open_exposure_loss: float = 0.0
    # 아직 대사되지 않은 미체결 매수의 예약 금액과 손실 예산(원).
    reserved_amount: float = 0.0
    reserved_loss: float = 0.0


@dataclass(frozen=True)
class SizingDecision:
    qty: int
    limit_price: float
    amount: float
    loss_budget_used: float
    binding: str | None
    limits: dict[str, float]
    blocked: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return self.qty > 0 and not self.blocked

    def as_dict(self) -> dict[str, Any]:
        return {
            "qty": self.qty,
            "limit_price": self.limit_price,
            "amount": self.amount,
            "loss_budget_used": self.loss_budget_used,
            "binding": self.binding,
            "limits": self.limits,
            "blocked": list(self.blocked),
        }


def _blocked(*reasons: str, limits: dict[str, float] | None = None) -> SizingDecision:
    return SizingDecision(0, 0.0, 0.0, 0.0, None, limits or {}, tuple(reasons))


def allowed_order(policy: RiskPolicy, inp: SizingInput) -> SizingDecision:
    """네 한도의 최솟값으로 수량을 정한다. 한 주도 못 사면 거래를 포기한다."""
    problems = policy.problems()
    if problems:
        return _blocked(*(f"RISK_POLICY_{p}" for p in problems))

    reasons: list[str] = []
    price = finite_number(inp.price)
    if price is None or price <= 0:
        reasons.append("PRICE_INVALID")
    if isinstance(inp.tick_size, bool) or not isinstance(inp.tick_size, int) or inp.tick_size < 1:
        reasons.append("TICK_SIZE_INVALID")

    cash = finite_number(inp.available_cash)
    depth = finite_number(inp.ask_depth_amount)
    expected = finite_number(inp.expected_trade_amount)
    account_age = finite_number(inp.account_data_age_sec)
    quote_age = finite_number(inp.quote_age_ms)
    delay = finite_number(inp.entry_delay_ms)
    carried = [
        finite_number(v) for v in (inp.open_exposure_loss, inp.reserved_amount, inp.reserved_loss)
    ]
    if cash is None:
        reasons.append("CASH_UNKNOWN")
    if depth is None or expected is None:
        # 잔량·거래대금 없이 전량 체결을 가정하지 않는다.
        reasons.append("LIQUIDITY_UNKNOWN")
    if any(value is None or value < 0 for value in carried):
        reasons.append("EXPOSURE_OR_RESERVATION_UNKNOWN")
    if account_age is None:
        reasons.append("ACCOUNT_DATA_AGE_UNKNOWN")
    elif account_age > float(policy.account_data_max_age_sec or 0.0):
        reasons.append("STALE_ACCOUNT_DATA")
    if quote_age is None:
        reasons.append("QUOTE_AGE_UNKNOWN")
    elif quote_age > float(policy.max_quote_age_ms or 0.0):
        reasons.append("QUOTE_STALE")
    if delay is None:
        reasons.append("ENTRY_DELAY_UNKNOWN")
    elif delay > float(policy.max_entry_delay_ms or 0.0):
        reasons.append("ENTRY_DELAY_EXCEEDED")
    if reasons:
        return _blocked(*reasons)

    assert price is not None and cash is not None and depth is not None and expected is not None
    budget = policy.per_trade_loss_budget()
    stress = policy.stress_loss_ratio()
    assert budget is not None and stress is not None and stress > 0

    remaining_loss = max(0.0, budget - inp.open_exposure_loss - inp.reserved_loss)
    limits = {
        "cash": max(0.0, cash - inp.reserved_amount) * float(policy.alloc_ratio or 0.0),
        "absolute_cap": float(policy.absolute_order_cap or 0.0),
        "liquidity": min(
            max(0.0, depth) * float(policy.usable_depth_ratio or 0.0),
            max(0.0, expected) * float(policy.max_participation_ratio or 0.0),
        ),
        "loss_budget": remaining_loss / stress,
    }
    binding = min(limits, key=lambda name: limits[name])
    amount = limits[binding]

    limit_price = float(math.floor(price / inp.tick_size) * inp.tick_size)
    if limit_price <= 0:
        return _blocked("PRICE_BELOW_ONE_TICK", limits=limits)
    qty = int(math.floor(amount / limit_price))
    if qty < 1:
        reason = f"BUDGET_BELOW_ONE_SHARE_{binding.upper()}"
        return SizingDecision(0, limit_price, 0.0, 0.0, binding, limits, (reason,))
    order_amount = qty * limit_price
    return SizingDecision(
        qty, limit_price, order_amount, order_amount * stress, binding, limits, ()
    )


class DuplicateReservation(Exception):
    """같은 주문 식별자로 예산을 두 번 잡으려 했다 — 중복 집행 시도다."""


class LossBudgetExceeded(Exception):
    """예약을 더하면 거래당 손실 예산을 넘는다."""


def active_reservations(store: Store) -> dict[str, dict[str, float]]:
    """아직 대사되지 않은 미체결 매수 예약."""
    active: dict[str, dict[str, float]] = {}
    for row in store.reservation_events():
        if row["event"] == "RESERVE":
            active[row["client_order_id"]] = {
                "amount": float(row["amount"]),
                "loss_budget": float(row["loss_budget"]),
            }
        else:
            active.pop(row["client_order_id"], None)
    return active


def reserved_totals(store: Store) -> tuple[float, float]:
    """(예약 금액 합, 예약 손실 예산 합). `SizingInput`에 그대로 넣는다."""
    active = active_reservations(store).values()
    return sum(r["amount"] for r in active), sum(r["loss_budget"] for r in active)


def reserve(
    store: Store,
    policy: RiskPolicy,
    *,
    client_order_id: str,
    decision: SizingDecision,
    open_exposure_loss: float,
    at: str,
) -> None:
    """매수 전송 **전에** 손실 예산을 잡는다.

    잠금을 잡은 채로 다시 합산한다. 수량을 계산한 뒤 다른 주문이 먼저 예약했다면 여기서
    걸린다. 한 번 쓴 식별자는 해제된 뒤에도 다시 쓸 수 없다.
    """
    if not client_order_id:
        raise ValueError("client_order_id is required")
    if not decision.allowed:
        raise ValueError(f"sizing decision is not an allowed order: {decision.blocked}")
    budget = policy.per_trade_loss_budget()
    if policy.problems() or budget is None:
        raise ValueError(f"risk policy invalid: {policy.problems()}")

    with store.transaction():
        if any(r["client_order_id"] == client_order_id for r in store.reservation_events()):
            raise DuplicateReservation(client_order_id)
        _, reserved_loss = reserved_totals(store)
        total = open_exposure_loss + reserved_loss + decision.loss_budget_used
        if total > budget + 1e-6:
            raise LossBudgetExceeded(
                f"loss budget {budget:.0f} < exposure {open_exposure_loss:.0f} "
                f"+ reserved {reserved_loss:.0f} + new {decision.loss_budget_used:.0f}"
            )
        store.insert_reservation_event(
            event="RESERVE",
            client_order_id=client_order_id,
            amount=decision.amount,
            loss_budget=decision.loss_budget_used,
            reason="BUY_PENDING",
            at=at,
        )


def release(store: Store, *, client_order_id: str, reason: str, at: str) -> bool:
    """취소·체결·거부가 대사로 확인된 뒤 예약을 푼다. 이미 풀렸으면 False.

    체결로 풀린 금액은 이제 보유 노출이다 — 호출자가 `open_exposure_loss`에 넣는다.
    """
    if reason not in RELEASE_REASONS:
        raise ValueError(f"release needs a reconciled outcome, got {reason!r}")
    with store.transaction():
        current = active_reservations(store).get(client_order_id)
        if current is None:
            if any(r["client_order_id"] == client_order_id for r in store.reservation_events()):
                return False
            raise KeyError(f"no reservation for {client_order_id}")
        store.insert_reservation_event(
            event="RELEASE",
            client_order_id=client_order_id,
            amount=current["amount"],
            loss_budget=current["loss_budget"],
            reason=reason,
            at=at,
        )
    return True
