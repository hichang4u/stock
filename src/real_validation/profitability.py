"""잠금 미래 검증의 절대 수익성 판정 (설계 §2.2).

순수 계산이다. 입력은 호출자가 넘긴 달력·거래 결과뿐이고 DB·시세를 읽지 않는다.

지키는 것:
- 사전등록값(`ProfitabilitySpec`)이 하나라도 비면 판정하지 않는다. 기본값으로 채우지 않는다.
- 판정일 전에는 신뢰구간을 **계산하지 않는다**(`COLLECTING`). 매일 들여다보다 유리한
  날에 멈추는 것을 막는다. 판정일 이후에는 판정일까지의 자료만 쓰므로 언제 돌려도 같다.
- 실패·수동 청산·경로 결측 거래도 계좌 집계에서 빼지 않는다.
- 입력 손익은 실제 체결 순손익(`ACTUAL_FILL_NET`)만 받는다. 슬리피지를 한 번 더 뺀
  conservative 수치를 넣으면 거부한다. 불리 시나리오는 여기서 조정폭을 한 번만 얹는다.

규칙 버전 2의 고정 절차(근거는 docs/REAL_VALIDATION_PREPARATION.md 4절):
- 블록 bootstrap은 거래일(무거래일 포함) 달력 위의 원형 블록 재표집이다. 통계량은
  재표집된 날들의 거래 손익률 합 ÷ 거래 수(비율 추정량)다.
- 신뢰구간 하한은 재표집 평균의 nearest-rank 분위수(percentile 방식)다.
- 거래가 하나도 없는 재표집은 평균이 정의되지 않는다. 값을 지어내지 않고 **모든 값보다
  아래**에 둔다 — 그 재표집이 무엇이었든 성립하는 하한이다. 분위수 자체가 그 구간에
  떨어지면 하한을 낼 수 없으므로 `INSUFFICIENT`다.
- 불리 시나리오에서도 평균과 MDD·CVaR·연속손실 한도를 모두 요구한다.
- 한도는 '이내'가 통과다(값 == 한도는 통과). 양수 조건은 0 초과다(0은 실패).
- 입력 결함(달력 누락, 손익률·금액 불일치)과 미해결 포지션은 최종 판정이 아니라 판정
  보류(`NOT_EVALUATED`)다. 통계를 내지 않으므로 고친 뒤 다시 판정해도 엿보기가 아니다.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.real_validation import evidence
from src.real_validation.core import (
    Finding,
    Verdict,
    canonical_hash,
    finite_number,
    is_yyyymmdd,
    not_evaluated,
    parse_aware,
)
from src.real_validation.store import Store

KST = ZoneInfo("Asia/Seoul")

REGISTRATION_KIND = "PROFITABILITY"
PNL_BASIS = "ACTUAL_FILL_NET"
# 설계가 고정한 값. 사전등록으로도 낮출 수 없다.
MIN_TRADE_DAYS_FLOOR = 60
CONFIDENCE_LEVEL = 0.95

CI_SIDES = ("TWO_SIDED", "ONE_SIDED")
MULTIPLE_TESTING_METHODS = ("NONE", "BONFERRONI")
OUTCOMES = ("NORMAL", "MANUAL_CLOSE", "FAILED")


@dataclass(frozen=True)
class ProfitabilitySpec:
    """수집 전에 잠그는 판정식. 전부 필수이며 None은 '미정'이다."""

    experiment_id: str | None = None
    strategy_fingerprint: str | None = None
    execution_profile_id: str | None = None
    cost_policy_hash: str | None = None
    risk_policy_hash: str | None = None
    start_date: str | None = None
    judgment_date: str | None = None
    min_trade_days: int | None = None
    block_length_days: int | None = None
    n_resamples: int | None = None
    seed: int | None = None
    ci_sides: str | None = None
    multiple_testing_method: str | None = None
    multiple_testing_n: int | None = None
    adverse_extra_pct: float | None = None
    account_capital: float | None = None
    max_account_mdd_pct: float | None = None
    cvar_alpha: float | None = None
    max_cvar_loss_pct: float | None = None
    max_consecutive_losses: int | None = None
    regime_min_trades: dict[str, int] | None = None
    evidence_valid_days: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def spec_hash(self) -> str:
        return canonical_hash(self.as_dict())

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProfitabilitySpec:
        unknown = set(raw) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown profitability spec keys: {sorted(unknown)}")
        return cls(**raw)

    def tail_alpha(self) -> float:
        """하한에 쓰는 한쪽 꼬리 확률. 복수 시험 보정을 반영한다."""
        alpha = 1 - CONFIDENCE_LEVEL
        if self.ci_sides == "TWO_SIDED":
            alpha /= 2
        if self.multiple_testing_method == "BONFERRONI":
            alpha /= int(self.multiple_testing_n or 1)
        return alpha

    def problems(self) -> list[str]:
        """미정·무효 항목. 비어 있어야 잠글 수 있고 판정할 수 있다."""
        found: list[str] = []
        for name in (
            "experiment_id",
            "strategy_fingerprint",
            "execution_profile_id",
            "cost_policy_hash",
            "risk_policy_hash",
        ):
            if not getattr(self, name):
                found.append(f"{name.upper()}_UNDETERMINED")

        for name in ("start_date", "judgment_date"):
            if not is_yyyymmdd(getattr(self, name)):
                found.append(f"{name.upper()}_UNDETERMINED")
        if (
            is_yyyymmdd(self.start_date)
            and is_yyyymmdd(self.judgment_date)
            and str(self.judgment_date) <= str(self.start_date)
        ):
            found.append("JUDGMENT_DATE_NOT_AFTER_START")

        for name in (
            "min_trade_days",
            "block_length_days",
            "n_resamples",
            "max_consecutive_losses",
            "evidence_valid_days",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                found.append(f"{name.upper()}_UNDETERMINED")
            elif value <= 0:
                found.append(f"{name.upper()}_NOT_POSITIVE")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            found.append("SEED_UNDETERMINED")
        if isinstance(self.min_trade_days, int) and 0 < self.min_trade_days < MIN_TRADE_DAYS_FLOOR:
            found.append("MIN_TRADE_DAYS_BELOW_FLOOR")

        if self.ci_sides not in CI_SIDES:
            found.append("CI_SIDES_UNDETERMINED")
        if self.multiple_testing_method not in MULTIPLE_TESTING_METHODS:
            found.append("MULTIPLE_TESTING_METHOD_UNDETERMINED")
        elif self.multiple_testing_method == "BONFERRONI":
            count = self.multiple_testing_n
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                found.append("MULTIPLE_TESTING_N_UNDETERMINED")
        elif self.multiple_testing_n not in (None, 1):
            found.append("MULTIPLE_TESTING_N_WITHOUT_METHOD")

        for name in ("account_capital", "max_account_mdd_pct", "max_cvar_loss_pct"):
            number = finite_number(getattr(self, name))
            if number is None:
                found.append(f"{name.upper()}_UNDETERMINED")
            elif number <= 0:
                found.append(f"{name.upper()}_NOT_POSITIVE")
        adverse = finite_number(self.adverse_extra_pct)
        if adverse is None:
            found.append("ADVERSE_EXTRA_PCT_UNDETERMINED")
        elif adverse <= 0:
            # 조정폭 0은 불리 시나리오가 없다는 뜻이다.
            found.append("ADVERSE_EXTRA_PCT_NOT_POSITIVE")
        alpha = finite_number(self.cvar_alpha)
        if alpha is None:
            found.append("CVAR_ALPHA_UNDETERMINED")
        elif not 0 < alpha < 1:
            found.append("CVAR_ALPHA_OUT_OF_RANGE")

        if self.regime_min_trades is None:
            found.append("REGIME_MIN_TRADES_UNDETERMINED")
        else:
            for regime, minimum in self.regime_min_trades.items():
                if not regime or isinstance(minimum, bool) or not isinstance(minimum, int):
                    found.append("REGIME_MIN_TRADES_INVALID")
                elif minimum <= 0:
                    found.append("REGIME_MIN_TRADES_INVALID")

        if not found and int(self.n_resamples or 0) * self.tail_alpha() < 1:
            # 재표집이 너무 적으면 하한 분위수가 최솟값으로 퇴화한다.
            found.append("N_RESAMPLES_TOO_FEW_FOR_TAIL")
        return sorted(set(found))


@dataclass(frozen=True)
class DayRecord:
    """창 안의 거래일 하루. 거래가 없던 날은 사유를 남긴다."""

    date: str
    no_trade_reason: str | None = None


@dataclass(frozen=True)
class TradeOutcome:
    """실제로 일어난 거래 한 건의 결과.

    `net_pnl_*`는 수수료·세금을 뺀 실제 체결 순손익이다(`costs.realized_pnl`).
    `resolved=False`는 아직 끝나지 않은 포지션이다 — 손익을 알 수 없으므로 판정을 막는다.
    """

    trade_date: str
    invested_amount: float
    net_pnl_amount: float | None
    net_pnl_pct: float | None
    pnl_basis: str = PNL_BASIS
    outcome: str = "NORMAL"
    cause: str | None = None
    regime: str | None = None
    path_complete: bool = True
    resolved: bool = True


def _lock_date(locked_at: str) -> str | None:
    parsed = parse_aware(locked_at)
    return None if parsed is None else parsed.astimezone(KST).strftime("%Y%m%d")


def register(store: Store, spec: ProfitabilitySpec, *, locked_at: str) -> str:
    """판정식을 잠근다. 같은 실험에 다른 값을 다시 등록할 수 없다.

    미정 항목이 있거나 수집 시작일 당일·이후에 잠그려 하면 거부한다.
    """
    problems = spec.problems()
    if problems:
        raise ValueError(f"spec has undetermined or invalid values: {problems}")
    lock_date = _lock_date(locked_at)
    if lock_date is None:
        raise ValueError("locked_at must be an offset-aware ISO timestamp")
    if lock_date >= str(spec.start_date):
        raise ValueError("spec must be locked before the collection start date")

    spec_hash = spec.spec_hash()
    experiment_id = str(spec.experiment_id)
    with store.transaction():
        existing = store.registration(experiment_id)
        if existing is None:
            store.insert_registration(
                experiment_id, REGISTRATION_KIND, spec.as_dict(), spec_hash, locked_at
            )
        elif existing["spec_hash"] != spec_hash:
            raise ValueError(
                f"{experiment_id} is already locked with a different spec — "
                "register an extension or retest as a new experiment"
            )
    return spec_hash


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _block_bootstrap_lower(
    per_day: Sequence[Sequence[float]], *, block: int, resamples: int, seed: int, alpha: float
) -> tuple[float | None, int]:
    """(거래일 원형 블록 bootstrap의 평균 하한, 거래가 없던 재표집 수).

    난수는 `random.Random(seed).random()`만 쓴다 — 파이썬이 시드 재현을 보장하는
    유일한 메서드라 버전이 바뀌어도 같은 재표집이 나온다.

    거래가 없는 재표집은 평균이 없다. 가장 낮은 순위를 차지한 것으로 세고, 하한 순위가
    그 안에 들면 하한은 None이다.
    """
    n_days = len(per_day)
    rng = random.Random(seed)
    n_blocks = math.ceil(n_days / block)
    means: list[float] = []
    empty = 0
    for _ in range(resamples):
        total = 0.0
        count = 0
        taken = 0
        for _block in range(n_blocks):
            start = int(rng.random() * n_days)
            for offset in range(block):
                if taken >= n_days:
                    break
                day = per_day[(start + offset) % n_days]
                total += sum(day)
                count += len(day)
                taken += 1
        if count:
            means.append(total / count)
        else:
            empty += 1
    means.sort()
    rank = max(1, math.ceil(alpha * resamples))
    if rank <= empty:
        return None, empty
    return means[rank - empty - 1], empty


def _drawdown_pct(capital: float, daily_pnl: Sequence[float]) -> float:
    equity = capital
    peak = capital
    worst = 0.0
    for pnl in daily_pnl:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak * 100)
    return worst


def _cvar_loss_pct(capital: float, daily_pnl: Sequence[float], alpha: float) -> float:
    returns = sorted(pnl / capital * 100 for pnl in daily_pnl)
    worst = returns[: max(1, math.ceil(alpha * len(returns)))]
    return max(0.0, -_mean(worst))


def _max_consecutive_losses(amounts: Sequence[float]) -> int:
    longest = run = 0
    for amount in amounts:
        run = run + 1 if amount < 0 else 0
        longest = max(longest, run)
    return longest


def _scenario(
    spec: ProfitabilitySpec,
    days: Sequence[str],
    trades: Sequence[TradeOutcome],
    *,
    extra_pct: float,
) -> dict[str, Any]:
    """한 비용 시나리오의 수익·위험 수치. `extra_pct`는 추가 불리 조정(%p)이다."""
    capital = float(spec.account_capital or 0.0)
    amounts = [
        float(t.net_pnl_amount or 0.0) - t.invested_amount * extra_pct / 100 for t in trades
    ]
    pcts = [float(t.net_pnl_pct or 0.0) - extra_pct for t in trades if t.invested_amount > 0]
    by_day = {day: 0.0 for day in days}
    for trade, amount in zip(trades, amounts, strict=True):
        by_day[trade.trade_date] += amount
    daily = [by_day[day] for day in days]
    invested = sum(t.invested_amount for t in trades)
    net = sum(amounts)
    return {
        "additional_adverse_adjustment_pct": extra_pct,
        "trades_in_mean": len(pcts),
        "mean_net_pct": _mean(pcts),
        "account_net_amount": net,
        "strategy_return_pct": net / invested * 100 if invested > 0 else 0.0,
        "account_return_pct": net / capital * 100,
        "account_mdd_pct": _drawdown_pct(capital, daily),
        "cvar_loss_pct": _cvar_loss_pct(capital, daily, float(spec.cvar_alpha or 0.0)),
        "max_consecutive_losses": _max_consecutive_losses(amounts),
    }


def evaluate(
    spec: ProfitabilitySpec,
    days: Sequence[DayRecord],
    trades: Sequence[TradeOutcome],
    *,
    as_of: str,
    locked_at: str,
) -> tuple[Finding, str | None]:
    """(판정, 판정에 쓴 자료의 해시). 판정하지 못했으면 해시는 None이다."""
    problems = spec.problems()
    if problems:
        return not_evaluated(*problems), None
    if not is_yyyymmdd(as_of):
        raise ValueError("as_of must be YYYYMMDD")
    start = str(spec.start_date)
    judgment = str(spec.judgment_date)

    lock_date = _lock_date(locked_at)
    if lock_date is None:
        return not_evaluated("LOCKED_AT_UNDETERMINED"), None
    if lock_date >= start:
        return not_evaluated("LOOKAHEAD_SPEC_LOCKED_AFTER_COLLECTION_START"), None

    # 탐색 구간 자료가 섞였으면 무엇을 검증한 것인지 알 수 없다. 조용히 걸러내지 않는다.
    if any(day.date < start for day in days) or any(t.trade_date < start for t in trades):
        return not_evaluated("LOOKAHEAD_DATA_BEFORE_COLLECTION_START"), None
    if any(t.pnl_basis != PNL_BASIS for t in trades):
        return not_evaluated("PNL_BASIS_NOT_ACTUAL_FILL_NET"), None
    if any(t.outcome not in OUTCOMES for t in trades):
        return not_evaluated("TRADE_OUTCOME_UNKNOWN"), None

    window_days = sorted((d for d in days if d.date <= judgment), key=lambda d: d.date)
    window_trades = sorted(
        (t for t in trades if t.trade_date <= judgment), key=lambda t: t.trade_date
    )
    ignored_after = (len(days) - len(window_days)) + (len(trades) - len(window_trades))
    trade_days = sorted({t.trade_date for t in window_trades if t.invested_amount > 0})
    progress = {
        "experiment_id": spec.experiment_id,
        "spec_hash": spec.spec_hash(),
        "start_date": start,
        "judgment_date": judgment,
        "as_of": as_of,
        "calendar_days": len(window_days),
        "trade_days": len(trade_days),
        "required_trade_days": spec.min_trade_days,
        "trades": len(window_trades),
    }

    if as_of < judgment:
        # 판정일 전에는 수익 통계를 내지 않는다.
        return Finding(Verdict.COLLECTING, ("BEFORE_JUDGMENT_DATE",), progress), None

    data_hash = canonical_hash(window_inputs(spec, days, trades))
    progress["ignored_after_judgment"] = ignored_after

    dates = [d.date for d in window_days]
    calendar_problems: list[str] = []
    if len(set(dates)) != len(dates):
        calendar_problems.append("CALENDAR_DUPLICATE_DAY")
    known = set(dates)
    traded = {t.trade_date for t in window_trades}
    if not traded <= known:
        calendar_problems.append("TRADE_DATE_MISSING_FROM_CALENDAR")
    for day in window_days:
        if day.date not in traded and not day.no_trade_reason:
            calendar_problems.append("NO_TRADE_DAY_WITHOUT_REASON")
        if day.date in traded and day.no_trade_reason:
            calendar_problems.append("TRADE_DAY_MARKED_NO_TRADE")
    if calendar_problems:
        # 입력을 고치면 되는 문제다. 실험을 태우는 최종 판정으로 남기지 않는다.
        return not_evaluated(*sorted(set(calendar_problems)), **progress), None

    unresolved = [
        t for t in window_trades
        if not t.resolved
        or finite_number(t.net_pnl_amount) is None
        or (t.invested_amount > 0 and finite_number(t.net_pnl_pct) is None)
    ]
    if unresolved:
        # 손실이 확정되지 않은 포지션을 빼고 판정하지도, 실험을 끝내 버리지도 않는다.
        # 확정된 뒤 판정일 기준 자료로 판정한다.
        progress["unresolved_trades"] = len(unresolved)
        return not_evaluated("UNRESOLVED_POSITION_OR_PNL", **progress), None
    inconsistent = [t for t in window_trades if not _pct_matches_amount(t)]
    if inconsistent:
        # 손익률과 금액의 기준이 다르면(예: 비용 차감 전 %와 차감 후 금액) 어느 쪽이
        # 맞는지 알 수 없다.
        progress["inconsistent_trades"] = len(inconsistent)
        return not_evaluated("PNL_PCT_AND_AMOUNT_INCONSISTENT", **progress), None
    if len(trade_days) < int(spec.min_trade_days or 0):
        return Finding(Verdict.INSUFFICIENT, ("TRADE_DAYS_BELOW_MINIMUM",), progress), data_hash

    base = _scenario(spec, dates, window_trades, extra_pct=0.0)
    stress = _scenario(spec, dates, window_trades, extra_pct=float(spec.adverse_extra_pct or 0.0))

    per_day: dict[str, list[float]] = {day: [] for day in dates}
    for trade in window_trades:
        if trade.invested_amount > 0:
            per_day[trade.trade_date].append(float(trade.net_pnl_pct or 0.0))
    ci_lower, empty_resamples = _block_bootstrap_lower(
        [per_day[day] for day in dates],
        block=min(int(spec.block_length_days or 1), len(dates)),
        resamples=int(spec.n_resamples or 0),
        seed=int(spec.seed or 0),
        alpha=spec.tail_alpha(),
    )
    progress["empty_resamples"] = empty_resamples
    if ci_lower is None:
        # 거래가 너무 드물어 이 블록 설계로는 하한이 정해지지 않는다.
        return (
            Finding(Verdict.INSUFFICIENT, ("BOOTSTRAP_LOWER_BOUND_UNDEFINED",), progress),
            data_hash,
        )
    ranked = sorted(
        (float(t.net_pnl_pct or 0.0) for t in window_trades if t.invested_amount > 0),
        reverse=True,
    )
    top1_removed = _mean(ranked[1:])
    top2_removed = _mean(ranked[2:])

    checks = [
        ("CI_LOWER_NOT_POSITIVE", ci_lower > 0),
        ("ACCOUNT_NET_NOT_POSITIVE", base["account_net_amount"] > 0),
        ("TOP1_REMOVED_MEAN_NOT_POSITIVE", len(ranked) > 1 and top1_removed > 0),
        ("TOP2_REMOVED_MEAN_NOT_POSITIVE", len(ranked) > 2 and top2_removed > 0),
        ("BASE_MEAN_NOT_POSITIVE", base["mean_net_pct"] > 0),
        ("STRESS_MEAN_NOT_POSITIVE", stress["mean_net_pct"] > 0),
    ]
    for label, scenario in (("BASE", base), ("STRESS", stress)):
        checks.extend(
            [
                (
                    f"{label}_MDD_OVER_LIMIT",
                    scenario["account_mdd_pct"] <= float(spec.max_account_mdd_pct or 0.0),
                ),
                (
                    f"{label}_CVAR_OVER_LIMIT",
                    scenario["cvar_loss_pct"] <= float(spec.max_cvar_loss_pct or 0.0),
                ),
                (
                    f"{label}_CONSECUTIVE_LOSSES_OVER_LIMIT",
                    scenario["max_consecutive_losses"] <= int(spec.max_consecutive_losses or 0),
                ),
            ]
        )
    failed = tuple(name for name, ok in checks if not ok)

    detail = {
        **progress,
        "data_hash": data_hash,
        "tail_alpha": spec.tail_alpha(),
        "ci_lower_mean_net_pct": ci_lower,
        "top1_removed_mean_net_pct": top1_removed,
        "top2_removed_mean_net_pct": top2_removed,
        "base": base,
        "stress": stress,
        "by_outcome": _by_outcome(window_trades),
        "path_incomplete_trades": sum(1 for t in window_trades if not t.path_complete),
        "regimes": _regimes(spec, window_trades),
        "no_trade_days": {
            d.date: d.no_trade_reason for d in window_days if d.no_trade_reason
        },
    }
    return Finding(Verdict.FAIL if failed else Verdict.PASS, failed, detail), data_hash


def _pct_matches_amount(trade: TradeOutcome) -> bool:
    """손익률(%)과 손익 금액이 같은 기준인지. 투입금이 없는 건은 비교할 것이 없다."""
    if trade.invested_amount <= 0:
        return True
    implied = trade.invested_amount * float(trade.net_pnl_pct or 0.0) / 100
    tolerance = max(0.5, trade.invested_amount * 1e-9)
    return abs(float(trade.net_pnl_amount or 0.0) - implied) <= tolerance


def window_inputs(
    spec: ProfitabilitySpec, days: Sequence[DayRecord], trades: Sequence[TradeOutcome]
) -> dict[str, Any]:
    """판정에 쓰이는 자료(판정일까지). 이 값의 해시가 `data_hash`이고 증거에 원본으로 남는다."""
    judgment = str(spec.judgment_date)
    return {
        "days": [
            asdict(d) for d in sorted(days, key=lambda d: d.date) if d.date <= judgment
        ],
        "trades": [
            asdict(t)
            for t in sorted(trades, key=lambda t: t.trade_date)
            if t.trade_date <= judgment
        ],
    }


def _by_outcome(trades: Sequence[TradeOutcome]) -> dict[str, dict[str, Any]]:
    """정상·수동 청산·실패를 원인별로 공개한다. 전부 계좌 집계에 들어 있다."""
    summary: dict[str, dict[str, Any]] = {}
    for trade in trades:
        key = trade.outcome if trade.outcome == "NORMAL" else f"{trade.outcome}:{trade.cause}"
        bucket = summary.setdefault(key, {"trades": 0, "net_amount": 0.0})
        bucket["trades"] += 1
        bucket["net_amount"] += float(trade.net_pnl_amount or 0.0)
    return summary


def _regimes(spec: ProfitabilitySpec, trades: Sequence[TradeOutcome]) -> dict[str, Any]:
    """시장 구간별 표본 수와 결과. 표본 요건 미달 구간에는 운용 자격을 주지 않는다."""
    grouped: dict[str, list[TradeOutcome]] = {}
    for trade in trades:
        grouped.setdefault(trade.regime or "UNLABELED", []).append(trade)
    report: dict[str, Any] = {}
    required = spec.regime_min_trades or {}
    for regime in sorted(set(grouped) | set(required)):
        members = grouped.get(regime, [])
        pcts = [float(t.net_pnl_pct or 0.0) for t in members if t.invested_amount > 0]
        minimum = required.get(regime)
        report[regime] = {
            "trades": len(members),
            "mean_net_pct": _mean(pcts),
            "net_amount": sum(float(t.net_pnl_amount or 0.0) for t in members),
            "required_trades": minimum,
            "sample_requirement_met": minimum is not None and len(members) >= minimum,
        }
    return report


_SPEC_BOUND_REFS = (
    "experiment_id",
    "strategy_fingerprint",
    "execution_profile_id",
    "cost_policy_hash",
    "risk_policy_hash",
)


def judge(
    store: Store,
    experiment_id: str,
    days: Sequence[DayRecord],
    trades: Sequence[TradeOutcome],
    *,
    as_of: str,
    evaluated_at: str,
    refs: evidence.EvidenceRefs,
    rerun_reason: str | None = None,
) -> Finding:
    """잠긴 판정식으로 판정하고, 최종 판정이면 입력 원본과 함께 증거로 남긴다.

    증거가 가리키는 버전이 잠긴 판정식·현재 판정 코드와 다르면 판정하지 않는다 — 다른
    전략·경로·정책의 표본을 이 실험의 증거로 남기지 않는다. 진행 중(`COLLECTING`)·판정
    보류는 기록하지 않는다.

    이미 최종 판정이 있으면 통계를 다시 내 보지도 않는다. 판정 코드가 바뀐 뒤의 재판정만
    `rerun_reason`과 함께 허용되고, 그때도 입력이 같아야 기록된다(`evidence.record`).
    """
    registered = store.registration(experiment_id)
    if registered is None or registered["kind"] != REGISTRATION_KIND:
        return not_evaluated("EXPERIMENT_NOT_REGISTERED")
    spec = ProfitabilitySpec.from_dict(registered["spec"])
    current_rules = evidence.rules_code_hash(evidence.KIND_PROFITABILITY)
    mismatched = [name for name in _SPEC_BOUND_REFS if getattr(spec, name) != getattr(refs, name)]
    if refs.spec_hash != registered["spec_hash"] or spec.spec_hash() != registered["spec_hash"]:
        mismatched.append("spec_hash")
    if refs.rules_code_hash != current_rules:
        mismatched.append("rules_code_hash")
    if refs.evaluation_rule_version != evidence.EVALUATION_RULE_VERSION:
        mismatched.append("evaluation_rule_version")
    if mismatched:
        return not_evaluated(*(f"REFS_DIFFER_FROM_LOCKED_SPEC_{n.upper()}" for n in mismatched))

    prior = evidence.last_final(store, evidence.KIND_PROFITABILITY, experiment_id)
    if prior is not None:
        same_rules = prior["refs"].get("rules_code_hash") == current_rules
        if same_rules or not (rerun_reason or "").strip():
            raise evidence.DuplicateVerdictError(
                f"{experiment_id} already judged: {prior['verdict']} (id={prior['id']})"
            )

    finding, data_hash = evaluate(
        spec, days, trades, as_of=as_of, locked_at=registered["locked_at"]
    )
    if finding.verdict in (Verdict.PASS, Verdict.FAIL, Verdict.INSUFFICIENT):
        evaluated = parse_aware(evaluated_at)
        if evaluated is None:
            raise ValueError("evaluated_at must be an offset-aware ISO timestamp")
        if evaluated.astimezone(KST).strftime("%Y%m%d") < str(spec.judgment_date):
            raise ValueError("a final verdict cannot be dated before the judgment date")
        expires = evaluated + timedelta(days=int(spec.evidence_valid_days or 0))
        evidence.record(
            store,
            kind=evidence.KIND_PROFITABILITY,
            finding=finding,
            refs=refs,
            data_hash=data_hash,
            evaluated_at=evaluated_at,
            expires_at=expires.isoformat(),
            inputs=window_inputs(spec, days, trades),
            rerun_reason=rerun_reason,
        )
    return finding


def audit(store: Store, evidence_id: int) -> Finding:
    """기록된 판정을 저장된 입력 원본으로 다시 계산해 대조한다(오프라인 감사용).

    재표집을 통째로 다시 돌리므로 게이트가 매번 부르지 않는다. PASS는 '지금 코드가 같은
    입력에서 같은 판정을 낸다'는 뜻이다. 판정 코드가 그 뒤 바뀌었으면 대조할 수 없다.
    """
    row = next(
        (r for r in store.evidence(evidence.KIND_PROFITABILITY) if r["id"] == evidence_id), None
    )
    if row is None:
        return not_evaluated("EVIDENCE_NOT_FOUND")
    experiment_id = str(row["refs"].get("experiment_id") or "")
    registered = store.registration(experiment_id)
    binding = evidence.check_binding(row, registered, not_before=None)
    if not binding.passed or registered is None:
        return binding
    if row["refs"].get("rules_code_hash") != evidence.rules_code_hash(
        evidence.KIND_PROFITABILITY
    ):
        return not_evaluated("RULES_CODE_CHANGED_SINCE_VERDICT")
    spec = ProfitabilitySpec.from_dict(registered["spec"])
    inputs = row["inputs"]
    finding, data_hash = evaluate(
        spec,
        [DayRecord(**d) for d in inputs["days"]],
        [TradeOutcome(**t) for t in inputs["trades"]],
        as_of=str(spec.judgment_date),
        locked_at=registered["locked_at"],
    )
    reasons: list[str] = []
    if finding.verdict.value != row["verdict"]:
        reasons.append("RECOMPUTED_VERDICT_DIFFERS")
    if list(finding.reasons) != list(row["reasons"]):
        reasons.append("RECOMPUTED_REASONS_DIFFER")
    if data_hash != row["data_hash"]:
        reasons.append("RECOMPUTED_DATA_HASH_DIFFERS")
    detail = {"recorded": row["verdict"], "recomputed": finding.verdict.value}
    return Finding(Verdict.FAIL if reasons else Verdict.PASS, tuple(reasons), detail)
