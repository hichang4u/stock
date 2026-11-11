"""지속성 있는 신규 진입 중단과 그 해제 (설계 §4.3).

중단은 저장소에 남는다. 프로세스를 다시 띄워도 풀리지 않는다. 풀 수 있는 길은 둘뿐이다.
- 일일 손실 중단: 거래일이 바뀐 뒤 승인된 전환 절차로.
- 낙폭·Critical 등 나머지: 대사와 원인 조사를 마치고 기록된 재개 결정으로.

중단은 **신규 매수·추가매수만** 막는다. 보호 청산(F4/F5)·취소·주문 대사는 어떤 중단
상태에서도 허용한다(`action_allowed`). 반대로 중단이 매도 재전송을 '허가'하지도 않는다 —
미확인 매도의 재전송 여부는 기존 청산 복구 대사가 정한다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.real_validation.core import Finding, Verdict, finite_number, is_yyyymmdd, parse_aware
from src.real_validation.risk_policy import RiskPolicy
from src.real_validation.store import Store, StoreUnavailable


class HaltKind(str, Enum):
    DAILY_LOSS = "DAILY_LOSS"
    DRAWDOWN = "DRAWDOWN"
    CVAR = "CVAR"
    CONSECUTIVE_LOSS = "CONSECUTIVE_LOSS"
    ORDER_STATE_UNKNOWN = "ORDER_STATE_UNKNOWN"
    EXIT_IMPOSSIBLE = "EXIT_IMPOSSIBLE"


# 거래일 전환 절차로 풀 수 있는 것은 일일 손실 중단뿐이다.
_DAILY_KINDS = frozenset({HaltKind.DAILY_LOSS})


class Action(str, Enum):
    NEW_BUY = "NEW_BUY"
    ADD_BUY = "ADD_BUY"
    PROTECTIVE_SELL = "PROTECTIVE_SELL"
    CANCEL = "CANCEL"
    RECONCILE = "RECONCILE"


_ENTRY_ACTIONS = frozenset({Action.NEW_BUY, Action.ADD_BUY})


def action_allowed(action: Action, *, eligible_for_real: bool) -> bool:
    """게이트가 막는 것은 매수뿐이다. 청산·취소·대사는 자격과 무관하게 항상 허용한다."""
    if action in _ENTRY_ACTIONS:
        return bool(eligible_for_real)
    return True


def halt_authorizes_sell_resend(_kinds: Sequence[HaltKind]) -> bool:
    """중단은 미확인 매도의 재전송 근거가 아니다. 늘 False다.

    일일 손실선을 넘었다는 이유만으로 응답이 유실된 매도를 다시 보내면 이중 매도가 된다.
    """
    return False


def exit_failure_status() -> dict[str, Any]:
    """청산 불능의 기록 방식. CLOSED나 '손실 제한 성공'으로 적지 않는다."""
    return {
        "level": "CRIT",
        "response": "MANUAL",
        "trade_status": "OPEN",
        "halt_kind": HaltKind.EXIT_IMPOSSIBLE.value,
        "loss_limited": False,
    }


def raise_halt(
    store: Store,
    *,
    kind: HaltKind,
    reason: str,
    policy_hash: str | None,
    trading_day: str,
    at: str,
) -> int:
    """중단을 남긴다. 같은 거래일·같은 종류의 활성 중단이 있으면 그 id를 돌려준다."""
    if not is_yyyymmdd(trading_day) or parse_aware(at) is None or not reason:
        raise ValueError("halt needs a trading_day, an offset-aware timestamp and a reason")
    with store.transaction():
        for halt in active_halts(store):
            if halt["kind"] == kind.value and halt["trading_day"] == trading_day:
                return int(halt["id"])
        return store.insert_halt_event(
            event="HALT",
            halt_id=None,
            kind=kind.value,
            reason=reason,
            policy_hash=policy_hash,
            trading_day=trading_day,
            at=at,
        )


def active_halts(store: Store) -> list[dict[str, Any]]:
    """해제 기록이 없는 중단. 재시작해도 같은 답이 나온다."""
    events = store.halt_events()
    resumed = {row["halt_id"] for row in events if row["event"] == "RESUME"}
    return [row for row in events if row["event"] == "HALT" and row["id"] not in resumed]


def release_daily(
    store: Store, *, new_trading_day: str, procedure_ref: str, at: str
) -> list[int]:
    """거래일 전환 절차로 **이전 거래일의** 일일 손실 중단만 푼다.

    같은 날 재시작으로는 풀리지 않는다. 낙폭·Critical 중단은 여기서 건드리지 않는다.
    """
    if not is_yyyymmdd(new_trading_day) or parse_aware(at) is None:
        raise ValueError("release needs a trading day and an offset-aware timestamp")
    if not procedure_ref:
        raise ValueError("daily release needs a reference to the approved day-change procedure")
    released: list[int] = []
    with store.transaction():
        for halt in active_halts(store):
            if HaltKind(halt["kind"]) not in _DAILY_KINDS:
                continue
            if halt["trading_day"] >= new_trading_day:
                continue
            store.insert_halt_event(
                event="RESUME",
                halt_id=int(halt["id"]),
                kind=halt["kind"],
                reason="TRADING_DAY_CHANGE",
                policy_hash=halt["policy_hash"],
                trading_day=new_trading_day,
                at=at,
                decision={"procedure_ref": procedure_ref},
            )
            released.append(int(halt["id"]))
    return released


def record_resume_decision(
    store: Store,
    *,
    halt_id: int,
    reconciled: bool,
    investigation: str,
    decided_by: str,
    trading_day: str,
    at: str,
) -> None:
    """낙폭·Critical 중단의 재개 결정을 남긴다. 대사·조사·결정자가 모두 있어야 한다."""
    if not is_yyyymmdd(trading_day) or parse_aware(at) is None:
        raise ValueError("resume needs a trading day and an offset-aware timestamp")
    if reconciled is not True:
        raise ValueError("resume requires a completed reconciliation")
    if not investigation.strip() or not decided_by.strip():
        raise ValueError("resume requires an investigation note and a named decision maker")
    with store.transaction():
        target = next((h for h in active_halts(store) if int(h["id"]) == halt_id), None)
        if target is None:
            raise KeyError(f"no active halt with id {halt_id}")
        if HaltKind(target["kind"]) in _DAILY_KINDS:
            raise ValueError("daily halts are released by the trading-day procedure only")
        store.insert_halt_event(
            event="RESUME",
            halt_id=halt_id,
            kind=target["kind"],
            reason="RECORDED_RESUME_DECISION",
            policy_hash=target["policy_hash"],
            trading_day=trading_day,
            at=at,
            decision={
                "reconciled": True,
                "investigation": investigation.strip(),
                "decided_by": decided_by.strip(),
            },
        )


def halt_finding(store_path: str | None) -> Finding:
    """`NOT risk_halted` 항목의 판정. PASS는 '활성 중단이 없음을 확인했다'는 뜻이다.

    저장소가 없거나 읽히지 않으면 중단 여부를 모르는 것이다 — 통과시키지 않는다.
    """
    if not store_path:
        return Finding(Verdict.NOT_EVALUATED, ("HALT_STORE_UNDETERMINED",))
    try:
        with Store.open(store_path) as store:
            halts = active_halts(store)
    except StoreUnavailable as exc:
        return Finding(Verdict.NOT_EVALUATED, ("HALT_STATE_UNREADABLE",), {"error": str(exc)})
    except Exception as exc:  # 손상된 행 등 — 모르면 막는다
        return Finding(Verdict.NOT_EVALUATED, ("HALT_STATE_UNREADABLE",), {"error": repr(exc)})
    if halts:
        summary = [
            {k: h[k] for k in ("id", "kind", "reason", "trading_day", "at", "policy_hash")}
            for h in halts
        ]
        return Finding(
            Verdict.FAIL, tuple(f"RISK_HALTED_{h['kind']}" for h in halts), {"halts": summary}
        )
    return Finding(Verdict.PASS)


@dataclass(frozen=True)
class AccountRiskState:
    """한도 판단에 쓰는 계좌 상태. 손익은 원, 손실은 음수다."""

    trading_day: str
    realized_pnl_today: float
    unrealized_pnl: float
    costs_today: float
    # 보유 중 평가손익을 포함한 현재 자산.
    equity: float
    # 입출금을 보정한 자산 고점(`adjusted_high_water`).
    equity_high_water: float
    # 최근 계좌 일별 수익률(비율, 오래된 것부터).
    daily_returns: Sequence[float] = field(default_factory=tuple)
    # 최근 거래별 순손익(원, 오래된 것부터).
    trade_pnls: Sequence[float] = field(default_factory=tuple)
    order_state_unknown: bool = False


def adjusted_high_water(points: Sequence[tuple[float, float]]) -> float:
    """입출금 보정 자산 고점. `points`는 (그 시점 자산, 직전 시점 이후 순입금액)이다.

    입금은 고점을 같은 금액만큼 올리고 출금은 내린다 — 입금으로 낙폭이 가려지거나
    출금이 낙폭으로 잡히지 않게 한다.
    """
    high = 0.0
    for equity, net_deposit in points:
        high = max(high + net_deposit, equity)
    return high


def evaluate_limits(
    policy: RiskPolicy, state: AccountRiskState
) -> tuple[list[tuple[HaltKind, str]], list[str]]:
    """(남겨야 할 중단, 영속 중단은 아니지만 진입을 막는 사유).

    경계는 닫힌 쪽이 차단이다 — 손실이 한도와 **같으면** 중단한다.
    정책이 유효하지 않으면 한도를 판단할 수 없으므로 차단 사유로 돌려준다.
    """
    problems = policy.problems()
    if problems:
        return [], [f"RISK_POLICY_{p}" for p in problems]

    halts: list[tuple[HaltKind, str]] = []
    blocks: list[str] = []
    numbers = (
        state.realized_pnl_today,
        state.unrealized_pnl,
        state.costs_today,
        state.equity,
        state.equity_high_water,
    )
    if any(finite_number(value) is None for value in numbers):
        return [], ["ACCOUNT_RISK_STATE_UNKNOWN"]

    if state.order_state_unknown:
        halts.append((HaltKind.ORDER_STATE_UNKNOWN, "unconfirmed order state"))

    daily_limit = policy.daily_loss_limit()
    assert daily_limit is not None
    day_pnl = state.realized_pnl_today + state.unrealized_pnl - abs(state.costs_today)
    if -day_pnl >= daily_limit:
        halts.append((HaltKind.DAILY_LOSS, f"day pnl {day_pnl:.0f} <= -{daily_limit:.0f}"))

    if state.equity_high_water <= 0:
        blocks.append("EQUITY_HIGH_WATER_UNKNOWN")
    else:
        drawdown = (state.equity_high_water - state.equity) / state.equity_high_water
        if drawdown >= float(policy.max_drawdown_ratio or 0.0):
            halts.append((HaltKind.DRAWDOWN, f"drawdown {drawdown:.4f}"))

    window = list(state.daily_returns)[-int(policy.cvar_window_days or 0):]
    if len(window) < int(policy.cvar_min_samples or 0):
        blocks.append("CVAR_SAMPLE_INSUFFICIENT")
    else:
        tail = sorted(window)[: max(1, math.ceil(float(policy.cvar_alpha or 0.0) * len(window)))]
        cvar_loss = max(0.0, -sum(tail) / len(tail))
        if cvar_loss >= float(policy.cvar_limit_ratio or 0.0):
            halts.append((HaltKind.CVAR, f"cvar loss {cvar_loss:.4f}"))

    run = 0
    for pnl in reversed(list(state.trade_pnls)):
        if pnl >= 0:
            break
        run += 1
    if run >= int(policy.max_consecutive_losses or 0):
        halts.append((HaltKind.CONSECUTIVE_LOSS, f"{run} consecutive losses"))
    return halts, blocks
