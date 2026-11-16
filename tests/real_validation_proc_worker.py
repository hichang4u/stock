"""별도 프로세스에서 게이트 저장소를 건드리는 테스트 일꾼.

`python -m tests.real_validation_proc_worker <명령> ...`으로만 쓴다. 임시 디렉터리의 저장소만
다루고, 결과를 표준출력에 JSON 한 줄로 낸다. 부모 테스트가 시간 제한과 강제 종료를 맡는다.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

from src.real_validation import gate, risk_halt, sizing
from src.real_validation.risk_halt import Action, HaltKind
from src.real_validation.sizing import BudgetUse, SizingInput
from src.real_validation.store import Store
from tests import real_validation_support as support

DAY = "20270104"
CASH_AS_OF = support.ts(DAY, "08:59")
HANG_SEC = 120.0
WAIT_SEC = 30.0


def full_budget_decision() -> sizing.SizingDecision:
    """합성 정책의 손실 예산을 거의 다 쓰는 주문. 두 번째 같은 주문은 들어갈 수 없다."""
    return sizing.allowed_order(
        support.risk_policy(),
        SizingInput(
            price=10_037.0,
            tick_size=10,
            available_cash=50_000_000.0,
            cash_basis=sizing.CASH_BASIS_ORDERABLE,
            cash_as_of=CASH_AS_OF,
            now="2027-01-04T08:59:05+09:00",
            ask_depth_amount=100_000_000.0,
            expected_trade_amount=10_000_000_000.0,
            quote_age_ms=100.0,
            entry_delay_ms=500.0,
            use=BudgetUse(CASH_AS_OF, 0.0, 0.0, 0.0, 0.0),
        ),
    )


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + WAIT_SEC
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"go file never appeared: {path}")
        time.sleep(0.005)


def _reserve(store: Store, order_id: str) -> None:
    sizing.reserve(
        store,
        support.risk_policy(),
        client_order_id=order_id,
        decision=full_budget_decision(),
        external_exposure_loss=0.0,
        at=support.ts(DAY, "09:00"),
    )


def _emit(value: object) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _hang(ready: Path) -> None:
    ready.write_bytes(b"ready")
    time.sleep(HANG_SEC)


def main(argv: list[str]) -> int:
    command, db = argv[0], Path(argv[1])
    if command == "reserve":
        order_id, go = argv[2], Path(argv[3])
        with Store.open(db, create=True) as store:
            _wait_for(go)
            try:
                _reserve(store, order_id)
                _emit({"result": "OK"})
            except (sizing.LossBudgetExceeded, sizing.DuplicateReservation) as exc:
                _emit({"result": type(exc).__name__})
    elif command == "release":
        order_id, reason, go = argv[2], argv[3], Path(argv[4])
        with Store.open(db, create=True) as store:
            _wait_for(go)
            released = sizing.release(
                store, client_order_id=order_id, reason=reason, at=support.ts(DAY, "09:05")
            )
            _emit({"result": released})
    elif command == "hold-uncommitted":
        order_id, ready = argv[2], Path(argv[3])
        with Store.open(db, create=True) as store:
            with store.transaction():
                store.insert_reservation_event(
                    event="RESERVE", client_order_id=order_id, amount=1_000_000.0,
                    loss_budget=40_000.0, reason="BUY_PENDING", at=support.ts(DAY, "09:00"),
                )
                _hang(ready)
    elif command == "reserve-then-hang":
        order_id, ready = argv[2], Path(argv[3])
        with Store.open(db, create=True) as store:
            _reserve(store, order_id)
            _hang(ready)
    elif command == "halt-then-hang":
        ready = Path(argv[2])
        with Store.open(db, create=True) as store:
            risk_halt.raise_halt(
                store, kind=HaltKind.DRAWDOWN, reason="synthetic drawdown",
                policy_hash=support.risk_policy().policy_hash(), trading_day=DAY,
                at=support.ts(DAY, "10:00"),
            )
            _hang(ready)
    elif command == "release-daily":
        new_day, go = argv[2], Path(argv[3])
        with Store.open(db, create=True) as store:
            _wait_for(go)
            released_ids = risk_halt.release_daily(
                store, new_trading_day=new_day, procedure_ref="day-change",
                at=support.ts(new_day, "08:00"),
            )
            _emit({"result": released_ids})
    elif command == "budget":
        with Store.open(db) as store:
            use = sizing.budget_use(store, cash_as_of=CASH_AS_OF)
            _emit({"orders": sorted(sizing.orders(store)), "loss_in_use": use.loss_in_use})
    elif command == "gate":
        decision = gate.evaluate_from_environment(
            strategy_fingerprint=support.FINGERPRINT,
            operational_ready=support.OPERATIONAL_PASS,
            now=datetime.fromisoformat(argv[2]),
            env={gate.ENV_DIR: str(db)},
        )
        _emit(
            {
                "eligible_for_real": decision.eligible_for_real,
                "blockers": decision.blockers(),
                "risk_not_halted": decision.components["risk_not_halted"].as_dict(),
                "permits": {action.value: gate.permits(decision, action) for action in Action},
            }
        )
    else:
        raise SystemExit(f"unknown command: {command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
