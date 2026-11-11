"""실전 검증 게이트 테스트용 합성 자료.

여기의 숫자는 **테스트 입력**이다. 승인된 정책값이나 사전등록값이 아니며, 실제 자본·
허용 손실·검정 세부값은 사용자 결정으로 남아 있다.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.real_validation import evidence, execution_profile, gate, profitability
from src.real_validation.core import Finding, Verdict, canonical_hash
from src.real_validation.costs import research_scenario_policy
from src.real_validation.execution_profile import ExecutionProfile, ParityContract
from src.real_validation.profitability import DayRecord, ProfitabilitySpec, TradeOutcome
from src.real_validation.risk_policy import RiskPolicy
from src.real_validation.store import Store

KST = ZoneInfo("Asia/Seoul")
FINGERPRINT = "fp-synthetic-1"
START = "20270104"
LOCKED_AT = "2026-12-30T16:00:00+09:00"
FILL_MODEL = {"fill_model_version": "synthetic-fill-1"}


def ts(day: str, hhmm: str = "16:00") -> str:
    parsed = datetime.strptime(f"{day} {hhmm}", "%Y%m%d %H:%M").replace(tzinfo=KST)
    return parsed.isoformat()


def weekdays(start: str, count: int) -> list[str]:
    """`start`부터 평일 `count`개(합성 달력 — 실제 휴장일은 반영하지 않는다)."""
    current = datetime.strptime(start, "%Y%m%d").date()
    days: list[str] = []
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return days


def next_weekday(day: str) -> str:
    current: date = datetime.strptime(day, "%Y%m%d").date() + timedelta(days=1)
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current.strftime("%Y%m%d")


def risk_policy(**overrides: Any) -> RiskPolicy:
    values: dict[str, Any] = {
        "policy_version": "synthetic-risk-1",
        "approved_account_capital": 10_000_000.0,
        "per_trade_loss_budget_won": 50_000.0,
        "daily_loss_limit_won": 100_000.0,
        "max_drawdown_ratio": 0.05,
        "cvar_alpha": 0.10,
        "cvar_window_days": 20,
        "cvar_min_samples": 10,
        "cvar_limit_ratio": 0.02,
        "cvar_insufficient_action": "BLOCK",
        "max_consecutive_losses": 4,
        "absolute_order_cap": 2_000_000.0,
        "alloc_ratio": 0.20,
        "usable_depth_ratio": 0.5,
        "max_participation_ratio": 0.01,
        "max_quote_age_ms": 500.0,
        "max_entry_delay_ms": 3_000.0,
        "stress_stop_loss_ratio": 0.02,
        "stress_delay_gap_ratio": 0.01,
        "stress_adverse_fill_ratio": 0.005,
        "stress_cost_ratio": 0.0025,
        "account_data_max_age_sec": 30.0,
    }
    values.update(overrides)
    return RiskPolicy(**values)


def profile(route: str | None = "LEGACY", **section_overrides: Any) -> ExecutionProfile:
    raw: dict[str, Any] = {"route": route}
    for section, names in execution_profile.REQUIRED_FIELDS.items():
        raw[section] = {name: f"synthetic-{name}" for name in names}
    raw["calls"]["per_mode_call_limits"] = {"PAPER": "1/s", "REAL": "18/s"}
    for section, values in section_overrides.items():
        raw[section] = {**raw[section], **values}
    return ExecutionProfile.from_dict(raw)


def spec(judgment_days: int = 60, **overrides: Any) -> ProfitabilitySpec:
    current_profile = profile()
    values: dict[str, Any] = {
        "experiment_id": "real-future-synthetic-1",
        "strategy_fingerprint": FINGERPRINT,
        "execution_profile_id": current_profile.profile_id(),
        "cost_policy_hash": research_scenario_policy().policy_hash(),
        "risk_policy_hash": risk_policy().policy_hash(),
        "start_date": START,
        "judgment_date": weekdays(START, judgment_days)[-1],
        "min_trade_days": 60,
        "block_length_days": 5,
        "n_resamples": 400,
        "seed": 20270104,
        "ci_sides": "TWO_SIDED",
        "multiple_testing_method": "NONE",
        "multiple_testing_n": None,
        "adverse_extra_pct": 0.10,
        "account_capital": 10_000_000.0,
        "max_account_mdd_pct": 5.0,
        "cvar_alpha": 0.10,
        "max_cvar_loss_pct": 1.0,
        "max_consecutive_losses": 5,
        "regime_min_trades": {},
        "evidence_valid_days": 30,
    }
    values.update(overrides)
    return ProfitabilitySpec(**values)


def trade(day: str, pct: float, *, invested: float = 1_000_000.0, **overrides: Any) -> TradeOutcome:
    values: dict[str, Any] = {
        "trade_date": day,
        "invested_amount": invested,
        "net_pnl_amount": invested * pct / 100,
        "net_pnl_pct": pct,
    }
    values.update(overrides)
    return TradeOutcome(**values)


def sample(pcts: list[float], *, start: str = START) -> tuple[list[DayRecord], list[TradeOutcome]]:
    """하루 한 건씩 `pcts` 순서대로 거래한 합성 표본."""
    days = weekdays(start, len(pcts))
    return [DayRecord(day) for day in days], [trade(day, pct) for day, pct in zip(days, pcts)]


# 평균이 양수이고 손실이 흩어져 있는 60일 표본(하루 한 건).
GOOD_PCTS = [1.2, 0.8, -0.3, 0.9, 1.1, -0.4] * 10


def contract(current_profile: ExecutionProfile | None = None, **overrides: Any) -> ParityContract:
    values: dict[str, Any] = {
        "execution_profile_id": (current_profile or profile()).profile_id(),
        "registered_at": LOCKED_AT,
        "min_replay_inputs": 3,
        "min_latency_samples": 5,
        "max_p95_excess_ms": {stage: 200.0 for stage in execution_profile.STAGES},
        "revalidation_days": 30,
    }
    values.update(overrides)
    return ParityContract(**values)


def refs(
    experiment_id: str,
    *,
    fingerprint: str = FINGERPRINT,
    current_profile: ExecutionProfile | None = None,
    policy: RiskPolicy | None = None,
) -> evidence.EvidenceRefs:
    current_profile = current_profile or profile()
    return evidence.EvidenceRefs(
        strategy_fingerprint=fingerprint,
        experiment_id=experiment_id,
        execution_profile_id=current_profile.profile_id(),
        execution_profile_hash=current_profile.profile_hash(),
        cost_policy_hash=research_scenario_policy().policy_hash(),
        fill_model_hash=canonical_hash(FILL_MODEL),
        risk_policy_hash=(policy or risk_policy()).policy_hash(),
        evaluation_rule_version=evidence.EVALUATION_RULE_VERSION,
    )


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def write_config(
    directory: Path, *, route: str = "LEGACY", policy: RiskPolicy | None = None
) -> None:
    """정책·프로필·비용·체결 모델 파일만 쓴다(저장소는 건드리지 않는다)."""
    research = research_scenario_policy()
    write_json(directory / gate.RISK_POLICY_FILE, (policy or risk_policy()).as_dict())
    write_json(directory / gate.EXECUTION_PROFILE_FILE, profile(route).as_dict())
    write_json(
        directory / gate.COST_POLICY_FILE,
        {"policy_id": research.policy_id, "versions": [v.as_dict() for v in research.versions]},
    )
    write_json(directory / gate.FILL_MODEL_FILE, FILL_MODEL)


def passing_directory(directory: Path, *, evaluated_day: str | None = None) -> str:
    """모든 증거가 통과한 상태의 합성 디렉터리를 만든다. 평가 시각(ISO)을 돌려준다."""
    directory.mkdir(parents=True, exist_ok=True)
    write_config(directory)
    current_spec = spec()
    evaluated_at = ts(evaluated_day or str(current_spec.judgment_date))
    days, trades = sample(GOOD_PCTS)
    with Store.open(directory / gate.STORE_FILE, create=True) as store:
        profitability.register(store, current_spec, locked_at=LOCKED_AT)
        finding = profitability.judge(
            store,
            str(current_spec.experiment_id),
            days,
            trades,
            as_of=str(current_spec.judgment_date),
            evaluated_at=evaluated_at,
            refs=refs(str(current_spec.experiment_id)),
        )
        assert finding.verdict is Verdict.PASS, finding.reasons
        parity_id = execution_profile.register_contract(store, contract())
        evidence.record(
            store,
            kind=evidence.KIND_EXECUTION_PARITY,
            finding=Finding(Verdict.PASS),
            refs=refs(parity_id),
            data_hash="synthetic-parity-data",
            evaluated_at=evaluated_at,
            expires_at=(datetime.fromisoformat(evaluated_at) + timedelta(days=30)).isoformat(),
        )
    return evaluated_at


OPERATIONAL_PASS = Finding(Verdict.PASS)
