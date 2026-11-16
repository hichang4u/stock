"""신규 주문 허용 금액·수량과 주문 예산 장부 (설계 §4.2).

허용 금액 = min(현금 한도, 절대 주문 상한, 유동성 한도, 남은 거래당 손실 예산 ÷ 스트레스
손실률). 가격은 호가단위로, 수량은 정수로 **내림**한다. 넷 중 하나라도 알 수 없으면
수량은 0이다 — 모르는 한도를 무제한으로 보지 않는다.

순수 계산과 장부만 있다. 기존 `f3_entry.entry_budget`(투입비율·주문금액 상한)을 대체하지
않으며, 런타임 연동은 전략 파일 변경이라 별도 릴리스다.

현금과 예약의 관계 — 이중 차감하지 않는다:
증권사의 주문가능현금은 **접수된** 미체결 매수를 이미 뺀 값이다. 그래서 예약을 무조건
현금에서 또 빼면 같은 돈을 두 번 뺀다. 여기서는 현금 스냅샷의 기준 시각(as-of)과 주문의
접수 시각을 비교해, 스냅샷이 아직 모르는 주문만 뺀다. 접수 확인이 없거나 스냅샷보다
늦게 접수된 주문이 그것이다. 손실 예산은 증권사 수치가 아니므로 늘 장부 전체로 센다.

체결된 주문은 예산을 돌려주지 않는다. 예약이 보유 노출로 바뀔 뿐이고, 포지션이 청산돼
대사된 뒤(`close_exposure`)에야 풀린다. 그 전환이 장부 안에서 일어나므로 '예약은 풀렸는데
노출은 아직 안 잡힌' 틈이 없다.

스트레스 손실률은 수량 산정 가정일 뿐 실제 손실 상한의 보장이 아니다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from src.real_validation.core import finite_number, parse_aware
from src.real_validation.risk_policy import RiskPolicy
from src.real_validation.store import Store

# 주문가능현금만 받는다. 예수금 잔고는 미체결·미결제 매수를 반영하지 않아 기준이 될 수 없다.
CASH_BASIS_ORDERABLE = "ORDERABLE_CASH"

# 미체결 잔량은 대사로 확인된 결과로만 푼다. '시간이 지났다'는 해제 사유가 아니다.
RELEASE_REASONS = ("CANCEL_CONFIRMED", "REJECT_CONFIRMED", "NOT_SENT_CONFIRMED", "FULLY_FILLED")
_NO_FILL_REASONS = ("REJECT_CONFIRMED", "NOT_SENT_CONFIRMED")
CLOSE_REASON = "POSITION_FLAT_RECONCILED"

_EPSILON = 1e-6


@dataclass(frozen=True)
class BudgetUse:
    """장부가 본 예산 사용량. `cash_as_of` 기준 현금 스냅샷과 짝이다."""

    cash_as_of: str
    # 미체결 예약 + 보유 노출에 묶인 손실 예산(원).
    loss_in_use: float
    # 현금 스냅샷이 아직 반영하지 못한 주문의 금액(원). 현금에서 이것만 뺀다.
    amount_not_in_cash: float
    # 참고: 미체결 잔량 금액 전체와 보유 노출 금액.
    pending_amount: float
    exposure_amount: float


@dataclass(frozen=True)
class SizingInput:
    """주문 직전 스냅샷. None은 '모른다'이며 0과 다르다."""

    price: float
    tick_size: int
    available_cash: float | None
    cash_basis: str | None
    # 현금 스냅샷의 기준 시각과 지금(오프셋 있는 ISO). 둘의 차가 계좌 자료의 나이다.
    cash_as_of: str | None
    now: str | None
    # 지정가 상한 이내 매도호가 잔량의 금액 합. 최우선 호가 하나만 넣지 않는다.
    ask_depth_amount: float | None
    # 참여율 기준이 되는 예상 거래대금.
    expected_trade_amount: float | None
    quote_age_ms: float | None
    entry_delay_ms: float | None
    # 장부(`budget_use`)의 사용량. 같은 `cash_as_of`로 계산한 것이어야 한다.
    use: BudgetUse | None
    # 장부 밖 노출(수동 매수 등)에 걸린 스트레스 손실(원).
    external_exposure_loss: float = 0.0


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
    quote_age = finite_number(inp.quote_age_ms)
    delay = finite_number(inp.entry_delay_ms)
    external = finite_number(inp.external_exposure_loss)
    if cash is None:
        reasons.append("CASH_UNKNOWN")
    if inp.cash_basis != CASH_BASIS_ORDERABLE:
        reasons.append("CASH_BASIS_NOT_ORDERABLE_CASH")
    if depth is None or expected is None:
        # 잔량·거래대금 없이 전량 체결을 가정하지 않는다.
        reasons.append("LIQUIDITY_UNKNOWN")
    if external is None or external < 0:
        reasons.append("EXTERNAL_EXPOSURE_UNKNOWN")

    as_of = parse_aware(inp.cash_as_of)
    now = parse_aware(inp.now)
    if as_of is None or now is None:
        reasons.append("ACCOUNT_DATA_AS_OF_UNKNOWN")
    else:
        age = (now - as_of).total_seconds()
        if age < 0:
            reasons.append("ACCOUNT_DATA_AS_OF_IN_FUTURE")
        elif age > float(policy.account_data_max_age_sec or 0.0):
            reasons.append("STALE_ACCOUNT_DATA")
    use = inp.use
    if use is None:
        reasons.append("BUDGET_USE_UNKNOWN")
    elif use.cash_as_of != inp.cash_as_of:
        # 다른 시점의 현금으로 계산한 사용량과 섞으면 이중 차감이나 누락이 생긴다.
        reasons.append("BUDGET_USE_AS_OF_MISMATCH")
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
    assert use is not None and external is not None
    budget = policy.per_trade_loss_budget()
    stress = policy.stress_loss_ratio()
    assert budget is not None and stress is not None and stress > 0

    remaining_loss = max(0.0, budget - external - use.loss_in_use)
    limits = {
        "cash": max(0.0, cash - use.amount_not_in_cash) * float(policy.alloc_ratio or 0.0),
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


class LedgerStateError(Exception):
    """장부 순서에 맞지 않는 기록이다(예약 없는 체결, 체결량 감소 등)."""


@dataclass
class OrderState:
    """한 주문의 장부 상태. 이벤트를 순서대로 접어 만든다."""

    amount: float
    loss_budget: float
    reserved_at: str
    acked_at: str | None = None
    filled_amount: float = 0.0
    released: bool = False
    closed: bool = False

    @property
    def pending_amount(self) -> float:
        return 0.0 if self.released else max(0.0, self.amount - self.filled_amount)

    @property
    def exposure_amount(self) -> float:
        return 0.0 if self.closed else self.filled_amount

    @property
    def loss_in_use(self) -> float:
        if self.amount <= 0:
            return 0.0
        return self.loss_budget * (self.pending_amount + self.exposure_amount) / self.amount

    @property
    def settled(self) -> bool:
        """더 묶인 예산이 없다 — 잔량은 풀렸고 노출도 닫혔다."""
        return self.pending_amount <= 0 and self.exposure_amount <= 0


def orders(store: Store) -> dict[str, OrderState]:
    """장부의 모든 주문 상태. 끝난 주문도 남긴다(식별자 재사용을 막기 위해)."""
    states: dict[str, OrderState] = {}
    for row in store.reservation_events():
        order_id = row["client_order_id"]
        event = row["event"]
        if event == "RESERVE":
            states[order_id] = OrderState(
                float(row["amount"]), float(row["loss_budget"]), str(row["at"])
            )
            continue
        state = states[order_id]
        if event == "ACK":
            state.acked_at = str(row["at"])
        elif event == "FILL":
            state.filled_amount = float(row["amount"])
        elif event == "RELEASE":
            state.released = True
        elif event == "CLOSE":
            state.closed = True
    return states


def budget_use(store: Store, *, cash_as_of: str) -> BudgetUse:
    """`cash_as_of` 시점의 주문가능현금과 짝지을 예산 사용량.

    그 시각까지 접수가 확인된 주문은 증권사가 이미 현금에서 뺐다고 본다. 접수 확인이 없거나
    그 뒤에 접수된 주문은 스냅샷이 모르는 주문이므로 미체결 잔량과 체결분을 함께 뺀다.
    """
    as_of = parse_aware(cash_as_of)
    if as_of is None:
        raise ValueError("cash_as_of must be an offset-aware ISO timestamp")
    loss = not_in_cash = pending = exposure = 0.0
    for state in orders(store).values():
        if state.settled:
            continue
        loss += state.loss_in_use
        pending += state.pending_amount
        exposure += state.exposure_amount
        acked = parse_aware(state.acked_at)
        if acked is None or acked > as_of:
            not_in_cash += state.pending_amount + state.exposure_amount
    return BudgetUse(cash_as_of, loss, not_in_cash, pending, exposure)


def _require(store: Store, client_order_id: str) -> OrderState:
    state = orders(store).get(client_order_id)
    if state is None:
        raise KeyError(f"no reservation for {client_order_id}")
    return state


def _stamp(at: str) -> None:
    if parse_aware(at) is None:
        raise ValueError("at must be an offset-aware ISO timestamp")


def reserve(
    store: Store,
    policy: RiskPolicy,
    *,
    client_order_id: str,
    decision: SizingDecision,
    external_exposure_loss: float,
    at: str,
) -> None:
    """매수 전송 **전에** 손실 예산을 잡는다.

    쓰기 잠금을 잡은 채로 장부를 다시 합산한다. 수량을 계산한 뒤 다른 주문이 먼저 예약했다면
    여기서 걸린다. 한 번 쓴 식별자는 주문이 끝난 뒤에도 다시 쓸 수 없다.
    """
    if not client_order_id:
        raise ValueError("client_order_id is required")
    _stamp(at)
    if not decision.allowed:
        raise ValueError(f"sizing decision is not an allowed order: {decision.blocked}")
    budget = policy.per_trade_loss_budget()
    if policy.problems() or budget is None:
        raise ValueError(f"risk policy invalid: {policy.problems()}")
    external = finite_number(external_exposure_loss)
    if external is None or external < 0:
        raise ValueError("external_exposure_loss must be a non-negative finite number")

    with store.transaction():
        states = orders(store)
        if client_order_id in states:
            raise DuplicateReservation(client_order_id)
        in_use = sum(state.loss_in_use for state in states.values())
        total = external + in_use + decision.loss_budget_used
        if total > budget + _EPSILON:
            raise LossBudgetExceeded(
                f"loss budget {budget:.0f} < external {external:.0f} "
                f"+ ledger {in_use:.0f} + new {decision.loss_budget_used:.0f}"
            )
        store.insert_reservation_event(
            event="RESERVE",
            client_order_id=client_order_id,
            amount=decision.amount,
            loss_budget=decision.loss_budget_used,
            reason="BUY_PENDING",
            at=at,
        )


def acknowledge(store: Store, *, client_order_id: str, at: str) -> bool:
    """증권사 접수 확인 시각을 남긴다. 이 시각 이후의 현금 스냅샷부터 이 주문을 반영한다."""
    _stamp(at)
    with store.transaction():
        state = _require(store, client_order_id)
        if state.acked_at is not None:
            return False
        store.insert_reservation_event(
            event="ACK", client_order_id=client_order_id, amount=0.0, loss_budget=0.0,
            reason="BROKER_ACKNOWLEDGED", at=at,
        )
    return True


def record_fill(store: Store, *, client_order_id: str, cumulative_amount: float, at: str) -> None:
    """대사로 확인된 **누적** 체결 금액. 예약의 그만큼이 보유 노출로 바뀐다."""
    _stamp(at)
    filled = finite_number(cumulative_amount)
    if filled is None or filled <= 0:
        raise ValueError("cumulative_amount must be a positive finite number")
    with store.transaction():
        state = _require(store, client_order_id)
        if state.closed:
            raise LedgerStateError(f"{client_order_id} is already closed")
        if filled + _EPSILON < state.filled_amount:
            raise LedgerStateError("cumulative fill cannot decrease")
        if filled > state.amount + _EPSILON:
            # 지정가 상한으로 잡은 금액보다 많이 체결될 수 없다. 장부가 틀렸다는 신호다.
            raise LedgerStateError("fill exceeds the reserved amount")
        if state.released and filled > state.filled_amount + _EPSILON:
            raise LedgerStateError("released remainder cannot fill")
        store.insert_reservation_event(
            event="FILL", client_order_id=client_order_id, amount=filled,
            loss_budget=state.loss_budget * filled / state.amount,
            reason="FILL_RECONCILED", at=at,
        )


def release(store: Store, *, client_order_id: str, reason: str, at: str) -> bool:
    """미체결 잔량을 푼다. 취소·거부·미전송·전량 체결이 대사로 확인된 뒤에만. 이미 풀렸으면 False.

    체결분은 풀리지 않는다 — 보유 노출로 남아 `close_exposure`까지 손실 예산을 쓴다.
    """
    if reason not in RELEASE_REASONS:
        raise ValueError(f"release needs a reconciled outcome, got {reason!r}")
    _stamp(at)
    with store.transaction():
        state = _require(store, client_order_id)
        if state.released:
            return False
        if reason in _NO_FILL_REASONS and state.filled_amount > 0:
            raise LedgerStateError(f"{reason} contradicts a recorded fill")
        if reason == "FULLY_FILLED" and state.filled_amount <= 0:
            raise LedgerStateError("FULLY_FILLED needs a recorded fill")
        store.insert_reservation_event(
            event="RELEASE", client_order_id=client_order_id, amount=state.pending_amount,
            loss_budget=state.loss_budget * state.pending_amount / state.amount,
            reason=reason, at=at,
        )
    return True


def close_exposure(store: Store, *, client_order_id: str, at: str) -> bool:
    """포지션이 전량 청산돼 대사된 뒤 보유 노출을 닫는다. 이미 닫혔으면 False."""
    _stamp(at)
    with store.transaction():
        state = _require(store, client_order_id)
        if state.closed:
            return False
        if not state.released:
            raise LedgerStateError("unfilled remainder must be released before closing")
        if state.filled_amount <= 0:
            raise LedgerStateError("nothing was filled — there is no exposure to close")
        store.insert_reservation_event(
            event="CLOSE", client_order_id=client_order_id, amount=state.filled_amount,
            loss_budget=state.loss_budget * state.filled_amount / state.amount,
            reason=CLOSE_REASON, at=at,
        )
    return True
