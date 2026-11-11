"""실전투자 준비도 계산과 REAL 시작 게이트.

점수(`percent`)는 운영 안전성만 잰다. REAL 신규 진입 자격(`eligible_for_real`)은
docs/REAL_VALIDATION_DESIGN.md §2.1의 AND 게이트이고, 점수 100은 그 여섯 조건 중
하나(`operational_ready`)일 뿐이다.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from src import db
from src.modules import f3_entry
from src.modules.exit_recovery import EXIT_RECOVERY_VERSION
from src.real_validation import gate as real_gate
from src.real_validation.core import Finding, Verdict
from src.release import strategy_fingerprint


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip() == "1"


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _check(
    key: str,
    label: str,
    passed: bool,
    weight: float,
    *,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "passed": bool(passed),
        "weight": weight,
        "earned": weight if passed else 0.0,
        "detail": detail,
    }


async def _clean_paper_trade_count(fingerprint: str) -> int:
    """트랙 A의 무결한 PAPER 청산 건수. REAL 전환 게이트의 근거다.

    `track='A'`는 파라미터가 아니라 리터럴이다. 이 게이트는 트랙 A 실행 경로의
    실전 자격만을 판정하므로, 트랙 B가 쌓은 PAPER 실적이 A의 실탄 자격으로
    흘러들어가서는 안 된다.
    """
    conn = db.get()
    async with conn.execute(
        """SELECT COUNT(*) AS cnt
             FROM trades t
            WHERE t.status='CLOSED'
              AND t.track='A'
              AND t.execution_mode='PAPER'
              AND t.strategy_fingerprint=?
              AND t.entry_qty > 0
              AND t.exit_qty = t.entry_qty
              AND NOT EXISTS (
                  SELECT 1
                    FROM orders o
                   WHERE o.trade_id=t.id
                     AND o.order_type='SELL'
                     AND o.status IN ('PENDING','PARTIAL_FILL','FAILED')
              )""",
        (fingerprint,),
    ) as cur:
        row = await cur.fetchone()
    # COUNT는 늘 한 행을 낸다. 방어적으로만 좁힌다.
    return int(row["cnt"] or 0) if row is not None else 0


async def calculate() -> dict[str, Any]:
    fingerprint = strategy_fingerprint()
    # 운영자가 임계값을 낮춰 PAPER 증거를 우회할 수 없게 20회를 하한으로 둔다.
    target_trades = max(20, int(os.getenv("REAL_READINESS_MIN_PAPER_TRADES", "20")))
    clean_trades = await _clean_paper_trade_count(fingerprint)

    code_checks = [
        _check(
            "exit_recovery_bundle",
            "청산 복구 안전성 번들(의도 선저장·응답 유실 대사·재시작 복구)",
            EXIT_RECOVERY_VERSION >= 1,
            30,
            detail=f"EXIT_RECOVERY_VERSION={EXIT_RECOVERY_VERSION}",
        ),
    ]

    paper_earned = round(min(1.0, clean_trades / target_trades) * 30, 2)
    paper_checks = [{
        "key": "stable_paper_trades",
        "label": f"현재 코드 지문 PAPER 정상 청산 {target_trades}회",
        "passed": clean_trades >= target_trades,
        "weight": 30,
        "earned": paper_earned,
        "detail": f"{clean_trades}/{target_trades}, fingerprint={fingerprint}",
    }]

    quote_raw = os.getenv("F3_FINAL_QUOTE_MAX_AGE_MS")
    quote_safe = quote_raw in (None, "") or 0 < _float(
        "F3_FINAL_QUOTE_MAX_AGE_MS", 500
    ) <= 500
    config_checks = [
        _check(
            "initial_allocation",
            "초기 실전 투입비율 20% 이하",
            0 < _float("F3_ALLOC_RATIO", 0.95) <= 0.20,
            5,
            detail=f"F3_ALLOC_RATIO={_float('F3_ALLOC_RATIO', 0.95):g}",
        ),
        _check(
            "pyramiding_disabled",
            "피라미딩 비활성",
            abs(float(f3_entry.FIRST_RATIO) - 1.0) < 1e-9,
            4,
            detail=f"runtime FIRST_RATIO={float(f3_entry.FIRST_RATIO):g}",
        ),
        _check(
            "paper_experiments_off",
            "REAL 비호환 PAPER 실험 기능 비활성",
            not _flag("PAPER_FAST_PROBE")
            and not _flag("PAPER_FAST_HYBRID"),
            4,
        ),
        _check(
            "fresh_final_quote",
            "REAL 최종 호가 신선도 500ms 이하",
            quote_safe,
            3,
            detail=f"F3_FINAL_QUOTE_MAX_AGE_MS={quote_raw or 'REAL default 500'}",
        ),
        _check(
            "stop_monitor_backup",
            "F4 REST 백업 및 WS stale 전환 활성",
            _flag("F4_REST_BACKUP_ENABLED", "1")
            and _flag("F4_REST_ONLY_WHEN_WS_STALE", "1"),
            3,
        ),
        _check(
            "runtime_order_flags",
            "DRY_RUN 해제·강제 catch-up 비활성",
            not _flag("DRY_RUN") and not _flag("FORCE_CATCHUP"),
            2,
        ),
        _check(
            "real_config_verified",
            "실전 키·계좌·REST/WS URL 별도 검증",
            _flag("REAL_CONFIG_VERIFIED"),
            4,
            detail="검증 후에만 REAL_CONFIG_VERIFIED=1",
        ),
    ]

    approval_checks = [
        _check(
            "real_smoke_test",
            "실전 서버 최소수량 주문·취소·체결·잔고 대사",
            _flag("REAL_SMOKE_TEST_APPROVED"),
            5,
        ),
        _check(
            "critical_alert_test",
            "Telegram CRIT 알림 실수신",
            _flag("REAL_ALERT_TEST_APPROVED"),
            5,
        ),
        _check(
            "operator_drill",
            "MTS 수동 청산·장애 대응 훈련",
            _flag("REAL_OPERATOR_DRILL_APPROVED"),
            5,
        ),
    ]

    groups: list[dict[str, Any]] = [
        {
            "key": "code_safety",
            "label": "주문 안전성",
            "weight": 30,
            "checks": code_checks,
        },
        {
            "key": "paper_evidence",
            "label": "동일 버전 PAPER 실적",
            "weight": 30,
            "checks": paper_checks,
        },
        {
            "key": "safe_config",
            "label": "REAL 안전 설정",
            "weight": 25,
            "checks": config_checks,
        },
        {
            "key": "operations",
            "label": "실전 운영 검증",
            "weight": 15,
            "checks": approval_checks,
        },
    ]
    for group in groups:
        group["earned"] = round(sum(c["earned"] for c in group["checks"]), 2)

    earned = round(sum(group["earned"] for group in groups), 2)
    percent = round(earned, 2)
    operational_blockers = [
        check["label"]
        for group in groups
        for check in group["checks"]
        if not check["passed"]
    ]
    operational_ready = percent >= 100.0 and not operational_blockers
    decision = _real_gate(fingerprint, operational_ready, operational_blockers)
    # 운영 점수가 100이어도 수익성·경로·위험 증거가 없으면 차단 사유가 비지 않게 한다.
    # main의 REAL_START_BLOCKED 로그와 대시보드가 이 목록을 그대로 보여준다.
    gate_blockers = [
        blocker
        for blocker in decision.blockers()
        if not blocker.startswith("operational_ready=")
    ]
    return {
        "percent": percent,
        "operational_ready": operational_ready,
        "eligible_for_real": decision.eligible_for_real,
        "strategy_fingerprint": fingerprint,
        "clean_paper_trades": clean_trades,
        "required_paper_trades": target_trades,
        "groups": groups,
        "blockers": operational_blockers + gate_blockers,
        "real_gate": decision.as_dict(),
        "gate": (
            "REAL 주문은 운영 안전성 100%와 수익성·실행 경로·위험 정책·"
            "현재 증거·무중단을 모두 통과해야 시작 가능"
        ),
    }


def _now() -> datetime:
    """증거 만료 판단의 기준 시각. 테스트가 고정할 수 있게 따로 둔다."""
    return datetime.now(timezone.utc)


def _real_gate(
    fingerprint: str, operational_ready: bool, operational_blockers: list[str]
) -> real_gate.GateDecision:
    """운영 안전성 판정을 AND 게이트의 한 항목으로 넣는다. 읽기만 한다."""
    operational = Finding(
        Verdict.PASS if operational_ready else Verdict.FAIL,
        tuple(operational_blockers),
    )
    return real_gate.evaluate_from_environment(
        strategy_fingerprint=fingerprint,
        operational_ready=operational,
        now=_now(),
        env=os.environ,
    )
