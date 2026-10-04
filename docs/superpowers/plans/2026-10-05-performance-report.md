# 공통 성과 리포트 (CAGR·MDD) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 표준 거래 목록에서 CAGR·MDD(전략 자본, 보수 비용 대표)를 전체·구간·연도별로 계산해 네 파일로 남기고, 첫 적용으로 운영 트랙 A 실거래 리포트를 만든다.

**Architecture:** `scripts/performance.py`는 순수 계산과 파일 쓰기만 맡는다. `scripts/performance_report.py`는 운영 DB를 읽기 전용으로 읽어 표준 거래로 바꾸는 트랙 A 어댑터이자 CLI다. `overnight_sieve.py`는 비용 상수를 `performance.py`에서 가져다 쓴다.

**Tech Stack:** Python 3.12 표준 라이브러리(`sqlite3`, `json`, `statistics`, `subprocess`), pytest.

**Spec:** `docs/superpowers/specs/2026-10-04-performance-report-design.md`

## Global Constraints

- `main.py`와 `src/` 아래 파일은 건드리지 않는다(전략 지문 무관).
- 운영 DB는 `file:<path>?mode=ro` URI로만 연다.
- 파일은 모두 `write_bytes`로 쓴다(UTF-8, LF). `write_text`는 Windows에서 CRLF가 된다.
- 비율 지표(`cagr`, `mdd`, `total_return`, `win_rate`)는 **분수**(−0.052 = −5.2%)로 저장한다. 거래·일 단위 손익은 `_pct` 접미사가 붙은 **퍼센트**다.
- 비용 상수: `BASE_ROUND_TRIP_COST_PCT = 0.18`, `HARD_STOP_SLIPPAGE_PCT = 0.30`, `TRAILING_SLIPPAGE_PCT = 0.15`, `TIMEOUT_SLIPPAGE_PCT = 0.20`.
- CAGR: `최종자산 ** (365.25 / 일수) − 1`, 일수 = (마지막 청산일 − 첫 진입일).days + 1, 일수 < 365면 `cagr_reference_only = True`.
- ruff `line-length = 100`. mypy 새 오류 0(`scripts/mypy_baseline.py`).
- 테스트 실행: `./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider <파일>` (Bash, 저장소 루트).

---

### Task 1: 비용과 단일 지표 (`performance.py` 기초)

**Files:**
- Create: `scripts/performance.py`
- Test: `tests/test_performance.py`

**Interfaces:**
- Produces:
  - 상수 `BASE_ROUND_TRIP_COST_PCT`, `HARD_STOP_SLIPPAGE_PCT`, `TRAILING_SLIPPAGE_PCT`, `TIMEOUT_SLIPPAGE_PCT`, `SLIPPAGE_BY_REASON: dict[str, float]`, `CAGR_MIN_DAYS = 365`, `DAYS_PER_YEAR = 365.25`
  - `class OverlapError(ValueError)`
  - `apply_costs(gross_pct: float, exit_reason: str) -> dict` → `{"net_cost_pct", "net_conservative_pct"}`
  - `ordered(trades: list[dict]) -> list[dict]` — 제외 안 된 거래를 `exit_at` 순으로, 겹치면 `OverlapError`
  - `compute_metrics(trades: list[dict], key: str) -> dict` → `n, cagr, cagr_reference_only, total_return, mdd, win_rate, mean_pct, median_pct`
  - `period_of(trades: list[dict]) -> dict | None` → `start, end, days, trading_days`
- 표준 거래 dict 키: `trade_id, ticker, track, entry_at, exit_at, gross_pct, exit_reason, excluded, meta` (+ 비용 적용 후 `net_cost_pct`, `net_conservative_pct`)

- [ ] **Step 1: Write the failing test**

```python
"""공통 성과 계산 — docs/superpowers/specs/2026-10-04-performance-report-design.md."""

import pytest

from scripts.performance import (
    OverlapError,
    apply_costs,
    compute_metrics,
    ordered,
    period_of,
)


def _t(tid, entry, exit_, pct, reason="TRAILING", excluded=None, fp=None):
    """보수 손익이 정확히 pct가 되도록 net_* 를 직접 채운 표준 거래."""
    return {"trade_id": tid, "ticker": "000001", "track": "A",
            "entry_at": f"{entry}T09:01:00+09:00", "exit_at": f"{exit_}T09:30:00+09:00",
            "gross_pct": pct, "exit_reason": reason, "excluded": excluded,
            "meta": {"strategy_fingerprint": fp},
            "net_cost_pct": pct, "net_conservative_pct": pct}


def test_apply_costs_conservative_uses_reason_slippage():
    assert apply_costs(2.0, "TRAILING") == pytest.approx(
        {"net_cost_pct": 1.82, "net_conservative_pct": 1.67})
    assert apply_costs(-2.0, "HARD_STOP")["net_conservative_pct"] == pytest.approx(-2.48)
    assert apply_costs(-3.0, "GAP_HARD_STOP")["net_conservative_pct"] == pytest.approx(-3.48)


def test_apply_costs_unknown_reason_uses_timeout_slippage():
    assert apply_costs(1.0, "SOMETHING")["net_conservative_pct"] == pytest.approx(0.62)


def test_metrics_compound_and_drawdown_from_the_starting_peak():
    trades = [_t("1", "2026-07-01", "2026-07-01", -10.0),
              _t("2", "2026-07-02", "2026-07-02", 20.0),
              _t("3", "2026-07-03", "2026-07-03", -5.0)]
    m = compute_metrics(trades, "net_conservative_pct")
    assert m["n"] == 3
    assert m["total_return"] == pytest.approx(0.9 * 1.2 * 0.95 - 1)
    assert m["mdd"] == pytest.approx(-0.10)   # 시작 1.0이 첫 고점
    assert m["win_rate"] == pytest.approx(1 / 3)
    assert m["mean_pct"] == pytest.approx(5 / 3)
    assert m["median_pct"] == pytest.approx(-5.0)


def test_cagr_is_annualised_and_flagged_under_a_year():
    trades = [_t("1", "2026-01-01", "2026-01-01", 10.0),
              _t("2", "2026-03-31", "2026-03-31", 0.0)]
    m = compute_metrics(trades, "net_conservative_pct")
    days = 90
    assert m["cagr"] == pytest.approx(1.10 ** (365.25 / days) - 1)
    assert m["cagr_reference_only"] is True


def test_cagr_over_a_year_is_not_reference_only():
    trades = [_t("1", "2025-01-01", "2025-01-01", 10.0),
              _t("2", "2026-01-01", "2026-01-01", 0.0)]
    m = compute_metrics(trades, "net_conservative_pct")
    assert m["cagr_reference_only"] is False
    assert m["cagr"] == pytest.approx(1.10 ** (365.25 / 366) - 1)


def test_metrics_with_no_trades_are_none():
    m = compute_metrics([], "net_conservative_pct")
    assert m["n"] == 0 and m["cagr"] is None and m["mdd"] is None


def test_excluded_trades_are_left_out():
    trades = [_t("1", "2026-07-01", "2026-07-01", 5.0),
              _t("2", "2026-07-02", "2026-07-02", -50.0, excluded="MANUAL")]
    assert compute_metrics(trades, "net_conservative_pct")["n"] == 1


def test_overlapping_positions_raise():
    a = _t("1", "2026-07-01", "2026-07-03", 1.0)
    b = _t("2", "2026-07-02", "2026-07-02", 1.0)
    with pytest.raises(OverlapError, match="1"):
        ordered([a, b])


def test_period_counts_calendar_and_trading_days():
    trades = [_t("1", "2026-07-01", "2026-07-01", 1.0),
              _t("2", "2026-07-03", "2026-07-03", 1.0)]
    assert period_of(trades) == {"start": "2026-07-01", "end": "2026-07-03",
                                 "days": 3, "trading_days": 2}
    assert period_of([]) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_performance.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.performance'`

- [ ] **Step 3: Write minimal implementation**

```python
"""공통 성과 계산 — CAGR·MDD. 읽기만 하고 판단하지 않는다.

설계: docs/superpowers/specs/2026-10-04-performance-report-design.md

- 전략 자본 기준: 거래마다 투입금 전액으로 복리(시작 자산 1.0).
- 대표값은 보수 손익(수수료·세금 + 청산 사유별 슬리피지). 비용 차감만 한 값도 함께 낸다.
- 한 번에 한 포지션만 가정한다. 겹치면 계산하지 않는다.
- 비율 지표는 분수(−0.052 = −5.2%), 거래·일 손익은 `_pct`(퍼센트).
"""

from __future__ import annotations

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_performance.py`
Expected: 9 passed

- [ ] **Step 5: Lint and commit**

```bash
./.venv/Scripts/python.exe -m ruff check scripts/performance.py tests/test_performance.py
git add scripts/performance.py tests/test_performance.py
git commit -m "feat(perf): compound CAGR/MDD on strategy capital with conservative costs"
```

---

### Task 2: 구간·연도 요약과 복수 변경 경고

**Files:**
- Modify: `scripts/performance.py` (끝에 추가)
- Test: `tests/test_performance.py` (끝에 추가)

**Interfaces:**
- Consumes: Task 1의 `apply_costs`, `compute_metrics`, `period_of`, `ordered`, `SLIPPAGE_BY_REASON`, `_day`
- Produces:
  - 구간 정의 dict: `{"name": str, "start": "YYYYMMDD", "end": "YYYYMMDD" | None, "changed": list[str]}`
  - `segment_of(day: date, segments: list[dict]) -> str | None`
  - `annotate(trades: list[dict], segments: list[dict]) -> list[dict]` — 제외 안 된 거래에 `net_cost_pct`·`net_conservative_pct`, 모든 거래에 `segment`·`year`
  - `summarize(trades: list[dict], segments: list[dict], *, holding_overnight: bool = False, extra_warnings: tuple[str, ...] = ()) -> dict` — `annotate` 된 거래를 받는다

- [ ] **Step 1: Write the failing test**

```python
from datetime import date

from scripts.performance import annotate, segment_of, summarize

SEGMENTS = [
    {"name": "S1", "start": "20260701", "end": "20260731", "changed": ["시작"]},
    {"name": "S2", "start": "20260801", "end": None, "changed": ["A 변경", "B 변경"]},
]


def _raw(tid, day, gross, reason="TRAILING", excluded=None, fp=None):
    return {"trade_id": tid, "ticker": "000001", "track": "A",
            "entry_at": f"{day}T09:01:00+09:00", "exit_at": f"{day}T09:30:00+09:00",
            "gross_pct": gross, "exit_reason": reason, "excluded": excluded,
            "meta": {"strategy_fingerprint": fp}}


def test_segment_of_uses_inclusive_bounds_and_open_end():
    assert segment_of(date(2026, 7, 31), SEGMENTS) == "S1"
    assert segment_of(date(2026, 8, 1), SEGMENTS) == "S2"
    assert segment_of(date(2026, 6, 30), SEGMENTS) is None


def test_annotate_adds_costs_segment_and_year_but_no_costs_for_excluded():
    rows = annotate([_raw("1", "2026-07-02", 2.0),
                     _raw("2", "2026-07-03", None, reason="MANUAL", excluded="MANUAL")],
                    SEGMENTS)
    assert rows[0]["net_conservative_pct"] == pytest.approx(1.67)
    assert (rows[0]["segment"], rows[0]["year"]) == ("S1", 2026)
    assert "net_conservative_pct" not in rows[1] and rows[1]["segment"] == "S1"


def test_summarize_restarts_equity_per_segment_and_year():
    rows = annotate([_raw("1", "2026-07-02", 10.18 + 0.15, fp="f1"),   # 보수 +10%
                     _raw("2", "2026-08-03", -10.0 + 0.18 + 0.15, fp="f2")],  # 보수 -10%
                    SEGMENTS)
    s = summarize(rows, SEGMENTS)
    s1, s2 = s["segments"]
    assert s1["conservative"]["total_return"] == pytest.approx(0.10)
    assert s2["conservative"]["total_return"] == pytest.approx(-0.10)
    assert s2["conservative"]["mdd"] == pytest.approx(-0.10)   # 구간 시작 1.0에서 다시
    assert s["overall"]["conservative"]["total_return"] == pytest.approx(1.1 * 0.9 - 1)
    assert s["years"][0]["year"] == 2026 and s["years"][0]["conservative"]["n"] == 2
    assert s["overall"]["period"]["start"] == "2026-07-02"


def test_multiple_fingerprints_or_changes_flag_the_segment():
    rows = annotate([_raw("1", "2026-07-02", 1.0, fp="f1"),
                     _raw("2", "2026-07-03", 1.0, fp="f2"),
                     _raw("3", "2026-08-03", 1.0, fp="f3")], SEGMENTS)
    s1, s2 = summarize(rows, SEGMENTS)["segments"]
    assert s1["multiple_changes"] is True and s1["fingerprints"] == ["f1", "f2"]
    assert s2["multiple_changes"] is True and s2["fingerprints"] == ["f3"]   # changed 2개


def test_missing_fingerprints_are_not_counted():
    rows = annotate([_raw("1", "2026-07-02", 1.0), _raw("2", "2026-07-03", 1.0)],
                    [{"name": "S1", "start": "20260701", "end": None, "changed": ["x"]}])
    seg = summarize(rows, [{"name": "S1", "start": "20260701", "end": None,
                            "changed": ["x"]}])["segments"][0]
    assert seg["fingerprints"] == [] and seg["multiple_changes"] is False


def test_summarize_counts_exclusions_unknown_reasons_and_warns():
    rows = annotate([_raw("1", "2026-07-02", 1.0, reason="WEIRD"),
                     _raw("2", "2026-07-03", None, reason="MANUAL", excluded="MANUAL")],
                    SEGMENTS)
    s = summarize(rows, SEGMENTS, holding_overnight=True, extra_warnings=("추가 경고",))
    assert s["basis"] == "strategy_capital" and s["headline"] == "conservative"
    assert s["excluded"] == {"MANUAL": 1}
    assert s["unknown_reasons"] == {"WEIRD": 1}
    text = " ".join(s["warnings"])
    assert "1년 미만" in text and "보유 중 평가손" in text and "추가 경고" in text
    assert "복수 변경" in text   # S2의 changed 2개
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_performance.py`
Expected: FAIL — `ImportError: cannot import name 'annotate'`

- [ ] **Step 3: Write minimal implementation** (append to `scripts/performance.py`)

```python
from collections import Counter  # 파일 맨 위 import 블록으로 옮긴다


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
```

Move `from collections import Counter` into the top import block (ruff I001 otherwise).

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_performance.py`
Expected: 15 passed

- [ ] **Step 5: Lint and commit**

```bash
./.venv/Scripts/python.exe -m ruff check scripts/performance.py tests/test_performance.py
git add scripts/performance.py tests/test_performance.py
git commit -m "feat(perf): segment and year breakdowns with a multiple-change warning"
```

---

### Task 3: 네 파일 쓰기 (`write_run`)

**Files:**
- Modify: `scripts/performance.py` (끝에 추가)
- Test: `tests/test_performance.py` (끝에 추가)

**Interfaces:**
- Consumes: Task 1·2 전부
- Produces:
  - `daily_rows(trades: list[dict]) -> list[dict]` — `annotate` 된 거래 → 청산일별 `date, trades, day_return_pct, equity, equity_cost, peak, drawdown_pct, segment`
  - `git_info(root: Path) -> dict` → `{"git_commit": str | None, "git_dirty": bool | None}`
  - `write_run(out_root: Path, name: str, trades: list[dict], segments: list[dict], *, now: datetime, manifest_extra: dict | None = None, extra_summary: dict | None = None, holding_overnight: bool = False, extra_warnings: tuple[str, ...] = ()) -> tuple[Path, dict]` — 실행 폴더와 요약을 돌려준다

- [ ] **Step 1: Write the failing test**

```python
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.performance import daily_rows, write_run

KST = ZoneInfo("Asia/Seoul")


def test_daily_rows_compound_both_cost_bases_and_track_drawdown():
    rows = annotate([_raw("1", "2026-07-02", 10.18 + 0.15),     # 보수 +10%
                     _raw("2", "2026-07-03", -10.0 + 0.18 + 0.15)],    # 보수 -10%
                    SEGMENTS)
    d1, d2 = daily_rows(rows)
    assert d1["date"] == "2026-07-02" and d1["trades"] == 1
    assert d1["equity"] == pytest.approx(1.10) and d1["day_return_pct"] == pytest.approx(10.0)
    assert d2["equity"] == pytest.approx(0.99) and d2["peak"] == pytest.approx(1.10)
    assert d2["drawdown_pct"] == pytest.approx(-10.0)
    assert d2["equity_cost"] == pytest.approx((1 + 0.1015) * (1 - 0.0985))


def test_write_run_writes_four_lf_files_with_equity_after_and_manifest(tmp_path):
    trades = [_raw("1", "2026-07-02", 2.0, fp="f1"),
              _raw("2", "2026-07-03", None, reason="MANUAL", excluded="MANUAL")]
    now = datetime(2026, 10, 5, 16, 0, 0, tzinfo=KST)
    run_dir, summary = write_run(
        tmp_path, "track_a", trades, SEGMENTS, now=now,
        manifest_extra={"source": {"rows_read": 2}},
        extra_summary={"account_reference": {"total_pnl_krw": 1.0}},
    )
    assert run_dir == tmp_path / "track_a" / "20261005_160000"
    for name in ("trades.jsonl", "daily.jsonl", "summary.json", "manifest.json"):
        data = (run_dir / name).read_bytes()
        assert b"\r\n" not in data and data.endswith(b"\n")
    lines = [json.loads(x) for x in (run_dir / "trades.jsonl").read_text("utf-8").splitlines()]
    assert lines[0]["equity_after"] == pytest.approx(1.0167)
    assert lines[1]["equity_after"] is None and lines[1]["excluded"] == "MANUAL"
    saved = json.loads((run_dir / "summary.json").read_text("utf-8"))
    assert saved["account_reference"] == {"total_pnl_krw": 1.0}
    assert summary["overall"]["conservative"]["n"] == 1
    manifest = json.loads((run_dir / "manifest.json").read_text("utf-8"))
    assert manifest["adapter"] == "track_a" and manifest["run_id"] == "20261005_160000"
    assert manifest["segments"] == SEGMENTS and manifest["source"] == {"rows_read": 2}
    assert manifest["costs"]["BASE_ROUND_TRIP_COST_PCT"] == 0.18
    assert "git_commit" in manifest and "git_dirty" in manifest


def test_write_run_with_only_excluded_trades_still_writes_files(tmp_path):
    trades = [_raw("1", "2026-07-02", None, reason="MANUAL", excluded="MANUAL")]
    now = datetime(2026, 10, 5, 16, 0, 1, tzinfo=KST)
    run_dir, summary = write_run(tmp_path, "track_a", trades, SEGMENTS, now=now)
    assert summary["overall"]["conservative"]["n"] == 0
    assert (run_dir / "daily.jsonl").read_bytes() == b""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_performance.py`
Expected: FAIL — `ImportError: cannot import name 'daily_rows'`

- [ ] **Step 3: Write minimal implementation** (append; move new imports `json`, `subprocess`, `Path` to the top block and put `ROOT` right after the constants)

```python
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def daily_rows(trades: list[dict]) -> list[dict]:
    """청산이 있었던 날 1줄. 매매 없는 날은 자산이 그대로라 만들지 않는다."""
    equity, equity_cost, peak = 1.0, 1.0, 1.0
    rows: dict[str, dict] = {}
    for trade in ordered(trades):
        day = _day(trade["exit_at"]).isoformat()
        row = rows.setdefault(day, {"date": day, "trades": 0, "_start": equity,
                                    "segment": trade.get("segment")})
        equity *= 1 + float(trade["net_conservative_pct"]) / 100
        equity_cost *= 1 + float(trade["net_cost_pct"]) / 100
        peak = max(peak, equity)
        row.update(trades=row["trades"] + 1, equity=equity, equity_cost=equity_cost,
                   peak=peak, drawdown_pct=(equity / peak - 1) * 100)
    out = []
    for row in rows.values():
        row["day_return_pct"] = (row["equity"] / row.pop("_start") - 1) * 100
        out.append(row)
    return out


def git_info(root: Path = ROOT) -> dict:
    def _git(*args: str) -> str | None:
        try:
            done = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                  text=True, timeout=10, check=True)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip()

    commit = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    return {"git_commit": commit, "git_dirty": None if status is None else bool(status)}


def _write_json(path: Path, obj: object) -> None:
    path.write_bytes((json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    path.write_bytes(body.encode("utf-8"))


def write_run(
    out_root: Path,
    name: str,
    trades: list[dict],
    segments: list[dict],
    *,
    now: datetime,
    manifest_extra: dict | None = None,
    extra_summary: dict | None = None,
    holding_overnight: bool = False,
    extra_warnings: tuple[str, ...] = (),
) -> tuple[Path, dict]:
    """표준 거래 → ``out_root/name/<YYYYMMDD_HHMMSS>/`` 네 파일. 기존 실행은 덮어쓰지 않는다."""
    rows = annotate(trades, segments)
    equity, after = 1.0, {}
    for trade in ordered(rows):
        equity *= 1 + float(trade["net_conservative_pct"]) / 100
        after[trade["trade_id"]] = equity
    for row in rows:
        row["equity_after"] = after.get(row["trade_id"])

    summary = summarize(rows, segments, holding_overnight=holding_overnight,
                        extra_warnings=extra_warnings)
    summary.update(extra_summary or {})

    run_id = now.strftime("%Y%m%d_%H%M%S")
    run_dir = out_root / name / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    _write_jsonl(run_dir / "trades.jsonl", rows)
    _write_jsonl(run_dir / "daily.jsonl", daily_rows(rows))
    _write_json(run_dir / "summary.json", summary)
    _write_json(run_dir / "manifest.json", {
        "adapter": name, "run_id": run_id, "created_at": now.isoformat(), **git_info(),
        "costs": {"BASE_ROUND_TRIP_COST_PCT": BASE_ROUND_TRIP_COST_PCT,
                  "SLIPPAGE_BY_REASON": SLIPPAGE_BY_REASON,
                  "UNKNOWN_REASON_SLIPPAGE_PCT": TIMEOUT_SLIPPAGE_PCT},
        "segments": segments, **(manifest_extra or {}),
    })
    return run_dir, summary
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_performance.py`
Expected: 18 passed

- [ ] **Step 5: Lint, type-check and commit**

```bash
./.venv/Scripts/python.exe -m ruff check scripts/performance.py tests/test_performance.py
./.venv/Scripts/python.exe scripts/mypy_baseline.py
git add scripts/performance.py tests/test_performance.py
git commit -m "feat(perf): write trades/daily/summary/manifest per run"
```

---

### Task 4: 트랙 A 어댑터와 CLI

**Files:**
- Create: `scripts/performance_report.py`
- Test: `tests/test_performance_report.py`

**Interfaces:**
- Consumes: `scripts.performance.write_run(...)`, `summary` 구조(Task 2)
- Produces:
  - `TRACK_A_SEGMENTS: list[dict]`, `ACCOUNT_CAPITAL_KRW = 500_000_000`
  - `load_track_a(root: Path, until: str | None = None) -> tuple[list[dict], dict]` — 표준 거래와 출처 정보
  - `account_reference(trades: list[dict], capital: float = ACCOUNT_CAPITAL_KRW) -> dict`
  - `main(argv: list[str] | None = None) -> int` — 0 정상, 2 계산 대상 0건

- [ ] **Step 1: Write the failing test**

```python
"""트랙 A 성과 리포트 어댑터 — 운영 DB를 읽기 전용으로 읽어 표준 거래로 바꾼다."""

import json
import sqlite3

import pytest

from scripts.performance_report import (
    ACCOUNT_CAPITAL_KRW,
    TRACK_A_SEGMENTS,
    account_reference,
    load_track_a,
    main,
)

COLS = ("id, date, track, ticker, name, entry_price, entry_qty, entry_at, exit_price, "
        "exit_qty, exit_at, close_reason, pnl_pct, pnl_amount, status, execution_mode, "
        "strategy_fingerprint, experiment_id")


def _db(root, rows):
    d = root / "data" / "db"
    d.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(d / "trading.db")
    con.execute(f"create table trades ({COLS})")
    con.executemany(f"insert into trades ({COLS}) values ({','.join('?' * 18)})", rows)
    con.commit()
    con.close()


def _row(i, day, pnl, reason="TRAILING", amount=1000.0, status="CLOSED", track="A",
         fp="f1", entry=100.0, exit_=101.0):
    iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    return (i, day, track, "000001", "x", entry, 10, f"{iso}T09:01:00+09:00", exit_, 10,
            f"{iso}T09:30:00+09:00", reason, pnl, amount, status, "PAPER", fp, "e")


def test_load_track_a_maps_rows_and_excludes_manual_open_and_other_tracks(tmp_path):
    _db(tmp_path, [_row(1, "20260702", 2.0),
                   _row(2, "20260708", -1.7, reason="MANUAL"),
                   _row(3, "20260709", 5.0, status="HOLDING"),
                   _row(4, "20260710", 3.0, track="B"),
                   _row(5, "20260711", None, entry=100.0, exit_=103.0)])
    trades, source = load_track_a(tmp_path)
    assert [t["trade_id"] for t in trades] == ["1", "2", "5"]
    assert trades[0]["gross_pct"] == 2.0 and trades[0]["excluded"] is None
    assert trades[0]["meta"]["strategy_fingerprint"] == "f1"
    assert trades[1]["excluded"] == "MANUAL"
    assert trades[2]["gross_pct"] == pytest.approx(3.0)   # pnl_pct 없음 → 가격으로
    assert source["rows_read"] == 3 and source["rows_used"] == 2


def test_load_track_a_marks_no_pnl_and_respects_until(tmp_path):
    _db(tmp_path, [_row(1, "20260702", None, entry=None, exit_=None),
                   _row(2, "20260801", 1.0)])
    trades, _ = load_track_a(tmp_path, until="20260731")
    assert [t["excluded"] for t in trades] == ["NO_PNL"]


def test_load_track_a_without_db_fails_clearly(tmp_path):
    with pytest.raises(FileNotFoundError, match="trading.db"):
        load_track_a(tmp_path)


def test_account_reference_uses_krw_and_capital():
    trades = [{"exit_at": "2026-07-02T09:30:00+09:00", "excluded": None,
               "meta": {"pnl_amount": 1_000_000.0}},
              {"exit_at": "2026-07-03T09:30:00+09:00", "excluded": None,
               "meta": {"pnl_amount": -3_000_000.0}},
              {"exit_at": "2026-07-04T09:30:00+09:00", "excluded": "MANUAL",
               "meta": {"pnl_amount": -9_000_000.0}}]
    ref = account_reference(trades)
    assert ref["total_pnl_krw"] == -2_000_000.0
    assert ref["mdd_krw"] == -3_000_000.0
    assert ref["mdd"] == pytest.approx(-3_000_000 / ACCOUNT_CAPITAL_KRW)
    assert ref["capital_krw"] == ACCOUNT_CAPITAL_KRW


def test_segments_cover_from_the_first_trade_with_an_open_end():
    assert TRACK_A_SEGMENTS[0]["start"] == "20260702"
    assert TRACK_A_SEGMENTS[-1]["end"] is None
    for a, b in zip(TRACK_A_SEGMENTS, TRACK_A_SEGMENTS[1:]):
        assert a["end"] < b["start"]


def test_main_writes_a_run_and_returns_zero(tmp_path, capsys):
    _db(tmp_path, [_row(1, "20260702", 2.0), _row(2, "20261002", -1.0, reason="HARD_STOP")])
    out = tmp_path / "out"
    assert main(["--track", "A", "--root", str(tmp_path), "--out-dir", str(out)]) == 0
    runs = list((out / "track_a").iterdir())
    assert len(runs) == 1
    summary = json.loads((runs[0] / "summary.json").read_text("utf-8"))
    assert summary["account_reference"]["capital_krw"] == ACCOUNT_CAPITAL_KRW
    assert any(s["name"] == "파서 수정 후" and s["multiple_changes"] for s in summary["segments"])
    printed = capsys.readouterr().out
    assert "CAGR" in printed and "MDD" in printed and "(참고)" in printed


def test_main_returns_two_when_nothing_is_countable(tmp_path):
    _db(tmp_path, [_row(1, "20260708", -1.7, reason="MANUAL")])
    assert main(["--track", "A", "--root", str(tmp_path),
                 "--out-dir", str(tmp_path / "out")]) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_performance_report.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.performance_report'`

- [ ] **Step 3: Write minimal implementation**

```python
r"""트랙 A 성과 리포트 — 운영 DB 실거래의 CAGR·MDD. 읽기 전용.

설계: docs/superpowers/specs/2026-10-04-performance-report-design.md

    .\.venv\Scripts\python.exe scripts\performance_report.py --track A --root D:\Private\stock-prod

결과는 이 트리의 data/performance/track_a/<실행ID>/ 에 네 파일로 남는다. 대표값은 전략 자본
기준 보수 손익이고, 1년 미만 CAGR은 참고로 표시한다.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.performance import write_run  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
ACCOUNT_CAPITAL_KRW = 500_000_000

# 구간 경계와 그때 바뀐 조건(설명). 실제 복수 변경은 구간 안 지문 개수로 자동 검사한다.
TRACK_A_SEGMENTS: list[dict] = [
    {"name": "초기 운영", "start": "20260702", "end": "20260806",
     "changed": ["(PAPER 표시 전, 지문 기록 없음)"]},
    {"name": "PAPER 레거시", "start": "20260807", "end": "20260910",
     "changed": ["실행 모드·지문 기록 시작, 진입 안전장치 강화 묶음"]},
    {"name": "빠른 경로", "start": "20260911", "end": "20261001",
     "changed": ["빠른 경로 하이브리드 활성(PAPER_FAST_HYBRID=1). 0914~1001은 WS 파서 결함 기간"]},
    {"name": "파서 수정 후", "start": "20261002", "end": None,
     "changed": ["WS 파서 헤더 분할 수정", "상한가 잠김 예외"]},
]


def load_track_a(root: Path, until: str | None = None) -> tuple[list[dict], dict]:
    path = root / "data" / "db" / "trading.db"
    if not path.exists():
        raise FileNotFoundError(f"운영 DB가 없다: {path} (trading.db)")
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "select * from trades where track='A' and status='CLOSED' order by date, id"
        ).fetchall()
    finally:
        con.close()

    trades: list[dict] = []
    for r in rows:
        if until and str(r["date"]) > until:
            continue
        gross = r["pnl_pct"]
        if gross is None and r["entry_price"] and r["exit_price"]:
            gross = (float(r["exit_price"]) / float(r["entry_price"]) - 1) * 100
        excluded = None
        if r["close_reason"] == "MANUAL":
            excluded = "MANUAL"
        elif gross is None:
            excluded = "NO_PNL"
        trades.append({
            "trade_id": str(r["id"]), "ticker": str(r["ticker"]), "track": "A",
            "entry_at": str(r["entry_at"]), "exit_at": str(r["exit_at"]),
            "gross_pct": None if gross is None else float(gross),
            "exit_reason": str(r["close_reason"] or "UNKNOWN"), "excluded": excluded,
            "meta": {"date": r["date"], "name": r["name"],
                     "strategy_fingerprint": r["strategy_fingerprint"],
                     "experiment_id": r["experiment_id"],
                     "execution_mode": r["execution_mode"], "pnl_amount": r["pnl_amount"]},
        })
    used = [t for t in trades if not t["excluded"]]
    source = {"db_path": str(path), "rows_read": len(trades), "rows_used": len(used),
              "first_date": trades[0]["meta"]["date"] if trades else None,
              "last_date": trades[-1]["meta"]["date"] if trades else None, "until": until}
    return trades, source


def account_reference(trades: list[dict], capital: float = ACCOUNT_CAPITAL_KRW) -> dict:
    """계좌 기준 참고 지표 — 원 단위 누적 손익과 낙폭. 대표값이 아니다."""
    xs = sorted((t for t in trades if not t.get("excluded")), key=lambda t: t["exit_at"])
    cum, peak, mdd = 0.0, 0.0, 0.0
    for trade in xs:
        cum += float((trade.get("meta") or {}).get("pnl_amount") or 0.0)
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return {"capital_krw": capital, "total_pnl_krw": cum, "total_return": cum / capital,
            "mdd_krw": mdd, "mdd": mdd / capital,
            "note": "모의 계좌 분기 교체와 초기 소량 거래 때문에 참고용"}


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:+.2f}%"


def _line(label: str, block: dict) -> str:
    m = block["conservative"]
    ref = " (참고)" if m["cagr_reference_only"] else ""
    win = "—" if m["win_rate"] is None else f"{m['win_rate'] * 100:.0f}%"
    return (f"{label:<14} n={m['n']:>3}  CAGR {_pct(m['cagr'])}{ref}  MDD {_pct(m['mdd'])}  "
            f"총수익 {_pct(m['total_return'])}  승률 {win}")


def print_report(summary: dict, run_dir: Path) -> None:
    print("[트랙 A 성과 — 전략 자본, 보수 비용 대표]")
    print(_line("전체", summary["overall"]))
    for seg in summary["segments"]:
        flag = "  ⚠ 복수 변경" if seg["multiple_changes"] else ""
        print(_line(seg["name"], seg) + flag)
    for year in summary["years"]:
        print(_line(str(year["year"]), year))
    acc = summary.get("account_reference") or {}
    if acc:
        print(f"계좌 기준(참고): 누적 {acc['total_pnl_krw']:+,.0f}원  "
              f"MDD {acc['mdd_krw']:+,.0f}원")
    print(f"제외: {summary['excluded']}  모르는 청산 사유: {summary['unknown_reasons']}")
    for warning in summary["warnings"]:
        print(f"- {warning}")
    print(f"저장: {run_dir}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="트랙 성과 리포트 (CAGR·MDD)")
    parser.add_argument("--track", choices=["A"], required=True)
    parser.add_argument("--root", type=Path, default=ROOT, help="운영 DB가 있는 트리")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "data" / "performance")
    parser.add_argument("--until", default=None, help="YYYYMMDD까지만")
    args = parser.parse_args(argv)

    trades, source = load_track_a(args.root, until=args.until)
    run_dir, summary = write_run(
        args.out_dir, "track_a", trades, TRACK_A_SEGMENTS, now=datetime.now(KST),
        manifest_extra={"source": source, "account_capital_krw": ACCOUNT_CAPITAL_KRW},
        extra_summary={"account_reference": account_reference(trades)},
        extra_warnings=("계좌 기준 지표는 참고용 — 모의 계좌 분기 교체, 초기 소량 거래.",),
    )
    print_report(summary, run_dir)
    return 0 if summary["overall"]["conservative"]["n"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_performance_report.py tests/test_performance.py`
Expected: all passed (25)

- [ ] **Step 5: Lint, type-check and commit**

```bash
./.venv/Scripts/python.exe -m ruff check scripts/performance_report.py tests/test_performance_report.py
./.venv/Scripts/python.exe scripts/mypy_baseline.py
git add scripts/performance_report.py tests/test_performance_report.py
git commit -m "feat(perf): track A adapter and CLI over the prod DB, read-only"
```

---

### Task 5: 비용 상수 공유 · daily-log 단계 · 실데이터 확인

**Files:**
- Modify: `scripts/overnight_sieve.py:39-51` (비용 상수 정의 → import)
- Modify: `.claude/skills/daily-log/SKILL.md` (6절 앞에 "7. 누적 성과" 추가 — 번호는 기존 "6. 보고할 때" 다음)
- Modify: `docs/superpowers/specs/2026-10-04-performance-report-design.md` (상태 줄)
- Test: 기존 `tests/test_overnight_sieve.py`, `tests/test_overnight_secondary.py`

**Interfaces:**
- Consumes: `scripts.performance` 상수
- Produces: 없음(동작 무변경)

- [ ] **Step 1: Replace the constants in `scripts/overnight_sieve.py`**

현재:
```python
# 개선 계획 §2 초기 PAPER 비용·체결 가정. 연구 상수이며 요율의 단정이 아니다.
BASE_ROUND_TRIP_COST_PCT = 0.18
HARD_STOP_SLIPPAGE_PCT = 0.30
TRAILING_SLIPPAGE_PCT = 0.15
TIMEOUT_SLIPPAGE_PCT = 0.20
ENTRY_SLIPPAGE_PCT = 0.0  # 마감 동시호가 단일가 체결 가정 (스펙 §3.2)

_SLIPPAGE_BY_REASON = {
    "GAP_HARD_STOP": HARD_STOP_SLIPPAGE_PCT,
    "HARD_STOP": HARD_STOP_SLIPPAGE_PCT,
    "TRAILING": TRAILING_SLIPPAGE_PCT,
    "TIMEOUT": TIMEOUT_SLIPPAGE_PCT,
    "DATA_END": TIMEOUT_SLIPPAGE_PCT,
}
```
바꿀 내용 (import 블록의 `from scripts.overnight_calendar ...` 줄 아래에 import를 추가하고, 위 블록은 아래로 대체):
```python
from scripts.performance import (  # noqa: E402, F401 — 재수출
    BASE_ROUND_TRIP_COST_PCT,
    HARD_STOP_SLIPPAGE_PCT,
    SLIPPAGE_BY_REASON,
    TIMEOUT_SLIPPAGE_PCT,
    TRAILING_SLIPPAGE_PCT,
)
```
```python
# 비용 상수는 scripts/performance.py 한 곳에 둔다(개선 계획 §2). 테스트가 이 모듈에서
# HARD_STOP_SLIPPAGE_PCT·TRAILING_SLIPPAGE_PCT를 가져가므로 import 줄에 noqa: F401을 단다.
ENTRY_SLIPPAGE_PCT = 0.0  # 마감 동시호가 단일가 체결 가정 (스펙 §3.2)
_SLIPPAGE_BY_REASON = SLIPPAGE_BY_REASON
```

- [ ] **Step 2: Run the existing overnight tests — behaviour must not change**

Run: `./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_overnight_sieve.py tests/test_overnight_secondary.py`
Expected: all passed (38)

- [ ] **Step 3: Add the daily-log step**

In `.claude/skills/daily-log/SKILL.md`, insert before `## 6. 보고할 때`:
~~~markdown
## 5.5 누적 성과 (CAGR·MDD)

```bash
cd /d/Private/stock && PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/performance_report.py --track A --root D:/Private/stock-prod
```

보고 맨 끝에 전체 기간과 현재 구간("파서 수정 후")의 CAGR·MDD를 한 줄씩 붙인다. 대표값은
전략 자본 기준 보수 손익이다. 1년 미만 CAGR에는 "(참고)"가 붙는다 — 총수익률을 함께 적는다.
`⚠ 복수 변경`이 붙은 구간은 성과 변화를 한 조건에 귀속하지 않는다. 결과 폴더
`data/performance/track_a/<실행ID>/`에 네 파일이 남는다.
~~~

- [ ] **Step 4: Run on real data**

```bash
cd /d/Private/stock && PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe scripts/performance_report.py --track A --root D:/Private/stock-prod
```
Expected: 종료 코드 0. 전체 n = 49(50건 중 MANUAL 1건 제외), 구간 4개, 연도 2026 한 줄, `(참고)` 표시, "PAPER 레거시"·"빠른 경로"·"파서 수정 후"에 복수 변경 경고. 출력 수치는 확인만 하고 이 단계에서 해석하지 않는다.

- [ ] **Step 5: Update the spec status line, run the full suite, commit**

`docs/superpowers/specs/2026-10-04-performance-report-design.md`에서 두 곳을 고친다.
- `상태: 설계 승인 대기` → `상태: 구현 완료 (2026-10-05)`
- §5.6의 소속 문장("트랙 A는 trades.date, 표준 거래에서는 exit_at의 날짜")을 "소속은 청산일(exit_at의 날짜) 기준이다. 트랙 A는 당일 청산이라 거래일과 같다."로 바꾼다.

```bash
./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
./.venv/Scripts/python.exe -m ruff check scripts tests
./.venv/Scripts/python.exe scripts/mypy_baseline.py
git add scripts/overnight_sieve.py .claude/skills/daily-log/SKILL.md docs/superpowers/specs/2026-10-04-performance-report-design.md
git commit -m "refactor(perf): share cost constants; run the report in daily-log"
```

---

## Self-Review

- **Spec coverage:** §3 구성 → Task 1–5. §4 표준 거래 → Task 1 helper·Task 4 `load_track_a`. §5.1 비용 → Task 1. §5.2 자산곡선·겹침 → Task 1. §5.3 MDD·보유 경고 → Task 1·2. §5.4 CAGR·참고 → Task 1·2. §5.5 보조 지표 → Task 1. §5.6 구간·연도·복수 변경 → Task 2. §6 네 파일 → Task 3. §7 어댑터·구간 상수·계좌 기준·CLI → Task 4. §8 daily-log → Task 5. §9 오류 처리 → Task 1(겹침)·4(NO_PNL, DB 없음, 종료 코드 2)·2(모르는 사유). §10 테스트 → 각 Task. §11 범위 밖 → 다루지 않음.
- **Spec 차이 하나:** §5.6은 소속을 "트랙 A는 `trades.date`"라고 했다. 이 계획은 모든 거래의 소속을 `exit_at` 날짜로 정한다. 트랙 A는 당일 청산이라 `trades.date`(진입일)와 같다. Task 5에서 스펙 문장을 "청산일 기준(트랙 A는 당일 청산이라 거래일과 같다)"으로 맞춘다.
- **Placeholder scan:** 없음.
- **Type consistency:** `write_run(out_root, name, trades, segments, *, now, manifest_extra, extra_summary, holding_overnight, extra_warnings)` — Task 3 정의와 Task 4 호출 일치. `summary["overall"]["conservative"]["n"]` 경로가 Task 2·3·4에서 같다.
