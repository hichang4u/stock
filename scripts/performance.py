"""공통 성과 계산 — CAGR·MDD. 읽기만 하고 판단하지 않는다.

설계: docs/superpowers/specs/2026-10-04-performance-report-design.md

- 전략 자본 기준: 거래마다 투입금 전액으로 복리(시작 자산 1.0).
- 대표값은 보수 손익(수수료·세금 + 청산 사유별 슬리피지). 비용 차감만 한 값도 함께 낸다.
- 한 번에 한 포지션만 가정한다. 겹치면 계산하지 않는다.
- 비율 지표는 분수(−0.052 = −5.2%), 거래·일 손익은 `_pct`(퍼센트).
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from statistics import mean, median

# 개선 계획 §2 초기 PAPER 비용·체결 가정. 연구 상수이며 요율의 단정이 아니다.
BASE_ROUND_TRIP_COST_PCT = 0.18
HARD_STOP_SLIPPAGE_PCT = 0.30
TRAILING_SLIPPAGE_PCT = 0.15
TIMEOUT_SLIPPAGE_PCT = 0.20
SLIPPAGE_BY_REASON = {
    "GAP_HARD_STOP": HARD_STOP_SLIPPAGE_PCT,
    "HARD_STOP": HARD_STOP_SLIPPAGE_PCT,
    "TRAILING": TRAILING_SLIPPAGE_PCT,
    "TIMEOUT": TIMEOUT_SLIPPAGE_PCT,
    "DATA_END": TIMEOUT_SLIPPAGE_PCT,
}

CAGR_MIN_DAYS = 365        # 이보다 짧으면 CAGR은 참고 표시
DAYS_PER_YEAR = 365.25


class OverlapError(ValueError):
    """한 번에 한 포지션 가정이 깨졌다 — 동시 보유 전략은 이 모듈을 쓰지 않는다."""


def apply_costs(gross_pct: float, exit_reason: str) -> dict:
    net_cost = gross_pct - BASE_ROUND_TRIP_COST_PCT
    slip = SLIPPAGE_BY_REASON.get(exit_reason, TIMEOUT_SLIPPAGE_PCT)
    return {"net_cost_pct": net_cost, "net_conservative_pct": net_cost - slip}


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _day(value: str) -> date:
    return _ts(value).date()


def ordered(trades: list[dict]) -> list[dict]:
    """제외되지 않은 거래를 청산 순서로. 앞 거래 청산 전에 다음 진입이 있으면 OverlapError."""
    xs = sorted((t for t in trades if not t.get("excluded")), key=lambda t: _ts(t["exit_at"]))
    for prev, cur in zip(xs, xs[1:]):
        if _ts(cur["entry_at"]) < _ts(prev["exit_at"]):
            raise OverlapError(
                f"포지션 겹침: {prev['trade_id']} 청산 {prev['exit_at']} > "
                f"{cur['trade_id']} 진입 {cur['entry_at']}"
            )
    return xs


def period_of(trades: list[dict]) -> dict | None:
    xs = ordered(trades)
    if not xs:
        return None
    start = min(_day(t["entry_at"]) for t in xs)
    end = max(_day(t["exit_at"]) for t in xs)
    return {"start": start.isoformat(), "end": end.isoformat(),
            "days": (end - start).days + 1,
            "trading_days": len({_day(t["exit_at"]) for t in xs})}


def compute_metrics(trades: list[dict], key: str) -> dict:
    """복리 자산곡선 위의 CAGR·MDD와 보조 지표. ``key``는 손익 필드(퍼센트)."""
    xs = ordered(trades)
    if not xs:
        return {"n": 0, "cagr": None, "cagr_reference_only": None, "total_return": None,
                "mdd": None, "win_rate": None, "mean_pct": None, "median_pct": None}
    values = [float(t[key]) for t in xs]
    equity, peak, mdd = 1.0, 1.0, 0.0
    for v in values:
        equity *= 1 + v / 100
        peak = max(peak, equity)
        mdd = min(mdd, equity / peak - 1)
    period = period_of(xs)
    assert period is not None
    days = period["days"]
    return {
        "n": len(values),
        "cagr": equity ** (DAYS_PER_YEAR / days) - 1,
        "cagr_reference_only": days < CAGR_MIN_DAYS,
        "total_return": equity - 1,
        "mdd": mdd,
        "win_rate": sum(1 for v in values if v > 0) / len(values),
        "mean_pct": mean(values),
        "median_pct": median(values),
    }


def segment_of(day: date, segments: list[dict]) -> str | None:
    key = day.strftime("%Y%m%d")
    for seg in segments:
        if seg["start"] <= key and (seg.get("end") is None or key <= seg["end"]):
            return str(seg["name"])
    return None


def annotate(trades: list[dict], segments: list[dict]) -> list[dict]:
    """비용 두 벌과 소속(구간·연도)을 붙인 사본. 소속은 청산일 기준이다."""
    out = []
    for trade in trades:
        row = dict(trade)
        if not row.get("excluded"):
            row.update(apply_costs(float(row["gross_pct"]), str(row["exit_reason"])))
        day = _day(row["exit_at"])
        row["segment"] = segment_of(day, segments)
        row["year"] = day.year
        out.append(row)
    return out


def _block(trades: list[dict]) -> dict:
    return {"period": period_of(trades),
            "conservative": compute_metrics(trades, "net_conservative_pct"),
            "cost_only": compute_metrics(trades, "net_cost_pct")}


def summarize(
    trades: list[dict],
    segments: list[dict],
    *,
    holding_overnight: bool = False,
    extra_warnings: tuple[str, ...] = (),
) -> dict:
    """``annotate`` 된 거래 → 전체·구간·연도 요약. 구간과 연도는 자산을 1.0에서 다시 시작한다."""
    included = [t for t in trades if not t.get("excluded")]
    warnings: list[str] = ["MDD는 청산 직후 자산으로 쟀다(보유 중 평가손 미포함)."]
    if holding_overnight:
        warnings.append("하루 넘게 보유하는 전략 — 보유 중 평가손이 빠져 MDD가 작게 나올 수 있다.")

    seg_out = []
    for seg in segments:
        xs = [t for t in included if t.get("segment") == seg["name"]]
        fps = sorted({
            str((t.get("meta") or {}).get("strategy_fingerprint"))
            for t in xs if (t.get("meta") or {}).get("strategy_fingerprint")
        })
        changed = list(seg.get("changed") or [])
        multiple = len(fps) > 1 or len(changed) > 1
        if multiple:
            warnings.append(
                f"구간 '{seg['name']}': 복수 변경 — 성과 변화를 한 조건에 귀속할 수 없다 "
                f"(지문 {len(fps)}개, 바뀐 조건 {len(changed)}개)."
            )
        seg_out.append({"name": seg["name"], "start": seg["start"], "end": seg.get("end"),
                        "changed": changed, "fingerprints": fps,
                        "multiple_changes": multiple, **_block(xs)})

    years = sorted({int(t["year"]) for t in included})
    year_out = [{"year": y, **_block([t for t in included if t["year"] == y])} for y in years]

    overall = _block(included)
    if overall["conservative"]["cagr_reference_only"]:
        warnings.append("1년 미만 CAGR은 참고 — 연환산이 크게 흔들린다. 총수익률을 함께 본다.")
    warnings.extend(extra_warnings)

    return {
        "basis": "strategy_capital",
        "headline": "conservative",
        "overall": overall,
        "segments": seg_out,
        "years": year_out,
        "excluded": dict(Counter(str(t["excluded"]) for t in trades if t.get("excluded"))),
        "unknown_reasons": dict(Counter(
            str(t["exit_reason"]) for t in included
            if t["exit_reason"] not in SLIPPAGE_BY_REASON
        )),
        "warnings": warnings,
    }
