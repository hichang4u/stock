# C1 오버나이트 그림자 트랙 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 매 거래일 15:32에 종가 기준 후보를 기록하고, 다음날 분봉으로 A의 F4 규칙을 재생해 C1 가설의 판정 수치를 내는 그림자 파이프라인을 `scripts/`에만 만든다.

**Architecture:** 세 스크립트 + 기존 백필 한 곳 수정. `overnight_screen.py`(15:32, KIS 랭킹·일봉 → `data/overnight/candidates/<date>.jsonl`), `track_b_backfill.needed_pairs`(후보의 D+1 분봉을 기존 백필에 합류), `overnight_sieve.py`(분봉 F4 재생 + 비용 + 집계). 규칙·비용·판정식은 순수 함수로 두고 네트워크·파일은 얇은 껍데기로 분리해 테스트는 순수 함수만 본다. `main.py`·`src/modules`는 건드리지 않는다(전략 지문 무관).

**Tech Stack:** Python 3.12 (`.venv`), `src.api.kis_rest`(httpx AsyncClient, `CallBudget`), `scripts.track_b_rules.simulate_exit`, `scripts.track_b_backtest.bootstrap_ci`, pytest, ruff, mypy. 런처는 PowerShell 5.1.

**Spec:** `docs/superpowers/specs/2026-09-21-c1-overnight-close-hypothesis.md`

## Global Constraints

- 전략 지문 대상(`src/release.py`의 `_STRATEGY_FILES` 19개)과 `main.py`는 수정 금지. 이 계획의 모든 변경은 `scripts/`, `tests/`, `docs/` 안에서 끝난다.
- 선정 규칙 상수는 스펙 §2.2 값 그대로: 등락률 `3.0 ≤ r < 25.0`, 종가 위치 `≥ 0.8`, 거래대금 배수 `≥ 2.0`, 거래대금 `≥ 1,000,000,000`, 종가 `≥ 1,000`. 순위는 거래대금 배수 내림차순, 동률이면 등락률.
- 비용 상수는 개선 계획 §2 그대로: 왕복 `0.18`, 하드스탑 슬리피지 `0.30`, 트레일 `0.15`, 타임아웃 `0.20` (%p). 종가 진입 슬리피지 `0`.
- F4 재생 상수는 `scripts/track_b_rules.py`의 `HARD_STOP=0.020`, `STEP_SIZE=0.025`, `STEP_TRAIL=0.020`, `TIMEOUT_TIME="151500"`을 import해서 쓴다 — 복사하지 않는다.
- 판정 상수: `n ≥ 50`, bootstrap 10,000회 95%, 상위 2건 제거, 조기 중단 n=30에서 평균 < −1.0% 또는 최대 낙폭 < −15%p.
- 모든 파이썬 파일은 `ruff check`·`mypy` 통과(줄 100자). 커밋 메시지는 저장소 관례(`feat(scripts): ...`)와 `Co-Authored-By` 꼬리.
- 실행 명령은 항상 `.\.venv\Scripts\python.exe`(시스템 파이썬은 3.14라 의존성이 없다).
- 이 저장소는 PowerShell 5.1이다: `??`, `?.`, 삼항, `&&`/`||` 금지. 네이티브 명령 stderr는 `$ErrorActionPreference="Continue"` 구간에서만 `2>&1`로 받는다.

---

## 파일 구조

| 파일 | 책임 |
|---|---|
| `scripts/overnight_screen.py` (신규) | 순수: 일봉 파싱, 규칙 적용, 순위. 껍데기: KIS 호출, jsonl 기록, CLI |
| `scripts/run_overnight_screen.ps1` (신규) | Task Scheduler 런처. 로그 파일, stderr 보존, 종료 코드 전파 |
| `scripts/track_b_backfill.py` (수정) | `needed_pairs(..., overnight_dir=)`로 후보의 D+1 쌍 합류. `main_async`에서 기본 디렉터리 전달 |
| `scripts/overnight_sieve.py` (신규) | 순수: 갭 하드스탑 + F4 재생, 비용, 집계. 껍데기: 후보·분봉 읽기, 결과 기록, CLI |
| `tests/test_overnight_screen.py` (신규) | 규칙·순위·파싱·기록 형식 |
| `tests/test_overnight_sieve.py` (신규) | 갭 처리·비용·집계·결측 회계 |
| `tests/test_track_b_backfill.py` (수정) | `overnight_dir` 합류 테스트 2개 추가 |
| `docs/DEV_ENV.md` (수정) | §11-3 `StockBot_OvernightScreen` 등록 절차 |

후보 파일 한 줄의 형식(모든 태스크가 공유):

```json
{"date": "20260921", "ticker": "005930", "name": "삼성전자", "rank": 1,
 "close": 71000.0, "open": 69000.0, "high": 71200.0, "low": 68800.0,
 "change_pct": 4.41, "close_position": 0.92, "amount": 812000000000.0,
 "avg_amount_20d": 310000000000.0, "amount_multiple": 2.62,
 "rejected_reason": null, "raw": {"ranking": {...}, "daily": {...}}}
```

거부 행은 `rank: null`, `rejected_reason: "CHANGE_PCT" | "CLOSE_POSITION" | "AMOUNT_MULTIPLE" | "AMOUNT_FLOOR" | "PRICE_FLOOR" | "NAME_EXCLUDED" | "NO_HISTORY" | "DAILY_FAILED" | "BELOW_TOP"` (`BELOW_TOP` = 통과했지만 6위 이하). 마지막 줄은 요약 행 `{"summary": true, "date": "...", "universe": N, "candidates": K, "degraded": false}`.

결과 파일 `data/overnight/results/<date>.json` 한 건의 형식:

```json
{"date": "20260921", "next_date": "20260922", "ticker": "005930", "rank": 1,
 "entry_price": 71000.0, "open": 71500.0, "gap_pct": 0.70,
 "exit_reason": "TRAILING", "exit_time": "091200", "exit_price": 72775.0,
 "gross_pct": 2.5, "net_cost_pct": 2.32, "net_slip_pct": 2.17,
 "bars_complete": true, "ambiguous": false}
```

---

### Task 1: overnight_screen — 규칙·순위 순수 함수

**Files:**
- Create: `scripts/overnight_screen.py`
- Test: `tests/test_overnight_screen.py`

**Interfaces:**
- Produces:
  - `CHANGE_MIN=3.0, CHANGE_MAX=25.0, CLOSE_POSITION_MIN=0.8, AMOUNT_MULTIPLE_MIN=2.0, AMOUNT_FLOOR=1_000_000_000.0, PRICE_FLOOR=1_000.0, RECORD_TOP=5, HISTORY_DAYS=20`
  - `close_position(high: float, low: float, close: float) -> float | None` — 고가=저가면 None
  - `is_excluded_name(name: str) -> bool`
  - `evaluate(row: dict) -> str | None` — 거부 사유 문자열, 통과면 None. `row`는 후보 파일 한 줄과 같은 키(`change_pct, close, high, low, amount, avg_amount_20d, name`)
  - `rank_candidates(rows: list[dict]) -> list[dict]` — 통과 행만 `amount_multiple` 내림차순(동률 `change_pct`), `rank` 1..N 부여, `RECORD_TOP`개까지

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_overnight_screen.py
"""C1 종가 스크리닝 — 순수 함수만 검사한다. 네트워크는 부르지 않는다.

규칙 임계값은 스펙 §2.2에 고정돼 있다. 여기서 값을 바꾸면 스펙부터 바꿔야 한다.
"""

from scripts.overnight_screen import (
    AMOUNT_MULTIPLE_MIN,
    CHANGE_MAX,
    CHANGE_MIN,
    CLOSE_POSITION_MIN,
    close_position,
    evaluate,
    is_excluded_name,
    rank_candidates,
)


def _row(**over) -> dict:
    base = {
        "ticker": "000001", "name": "정상전자", "change_pct": 5.0,
        "open": 100.0, "high": 110.0, "low": 99.0, "close": 108.0,
        "amount": 5_000_000_000.0, "avg_amount_20d": 1_000_000_000.0,
    }
    base.update(over)
    base["close_position"] = close_position(base["high"], base["low"], base["close"])
    base["amount_multiple"] = (
        base["amount"] / base["avg_amount_20d"] if base["avg_amount_20d"] else None
    )
    return base


def test_thresholds_match_spec():
    assert (CHANGE_MIN, CHANGE_MAX, CLOSE_POSITION_MIN, AMOUNT_MULTIPLE_MIN) == (
        3.0, 25.0, 0.8, 2.0
    )


def test_close_position_is_fraction_of_range_and_none_when_flat():
    assert close_position(110.0, 100.0, 108.0) == 0.8
    assert close_position(100.0, 100.0, 100.0) is None


def test_evaluate_passes_a_textbook_close():
    assert evaluate(_row()) is None


def test_evaluate_rejects_each_rule_with_its_reason():
    assert evaluate(_row(change_pct=2.9)) == "CHANGE_PCT"
    assert evaluate(_row(change_pct=25.0)) == "CHANGE_PCT"
    assert evaluate(_row(close=107.0)) == "CLOSE_POSITION"       # (107-99)/11 = 0.727
    assert evaluate(_row(high=100.0, low=100.0, close=100.0)) == "CLOSE_POSITION"
    assert evaluate(_row(amount=1_900_000_000.0)) == "AMOUNT_MULTIPLE"
    assert evaluate(_row(amount=900_000_000.0, avg_amount_20d=100_000_000.0)) == "AMOUNT_FLOOR"
    assert evaluate(_row(close=999.0, high=1000.0, low=990.0)) == "PRICE_FLOOR"
    assert evaluate(_row(name="KODEX 200")) == "NAME_EXCLUDED"
    assert evaluate(_row(avg_amount_20d=None)) == "NO_HISTORY"


def test_excluded_names_cover_etf_etn_spac():
    assert is_excluded_name("TIGER 반도체")
    assert is_excluded_name("삼성 KRX 2X ETN")
    assert is_excluded_name("하나32호스팩")
    assert not is_excluded_name("성호전자")


def test_rank_orders_by_amount_multiple_then_change_and_caps_at_five():
    rows = [
        _row(ticker="A", amount=3_000_000_000.0, change_pct=4.0),
        _row(ticker="B", amount=5_000_000_000.0, change_pct=4.0),
        _row(ticker="C", amount=5_000_000_000.0, change_pct=9.0),
        _row(ticker="D", change_pct=2.0),                      # 거부, 순위 밖
        _row(ticker="E", amount=2_500_000_000.0),
        _row(ticker="F", amount=2_400_000_000.0),
        _row(ticker="G", amount=2_300_000_000.0),
    ]
    ranked = rank_candidates(rows)
    assert [r["ticker"] for r in ranked] == ["C", "B", "A", "E", "F"]
    assert [r["rank"] for r in ranked] == [1, 2, 3, 4, 5]
```

- [ ] **Step 2: 실패 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_overnight_screen.py -q`
Expected: `ModuleNotFoundError: No module named 'scripts.overnight_screen'`

- [ ] **Step 3: 순수 함수 구현**

```python
# scripts/overnight_screen.py
"""C1 종가 스크리닝 — 15:32에 종가 기준 후보를 뽑아 data/overnight/candidates/<date>.jsonl 에 남긴다.

규칙·임계값·순위는 docs/superpowers/specs/2026-09-21-c1-overnight-close-hypothesis.md §2에
고정돼 있다. 이 파일은 그것을 구현할 뿐이고, 값을 바꾸려면 C2를 등록한다.

    .\\.venv\\Scripts\\python.exe scripts\\overnight_screen.py            # 기록
    .\\.venv\\Scripts\\python.exe scripts\\overnight_screen.py --dry-run  # 호출만, 기록 없음

유니버스는 KIS 등락률 랭킹(코스피·코스닥 각 30), 필터는 랭킹 응답 + 일봉 1콜(당일 OHLC와
직전 20거래일 거래대금). 마감 후에 돌므로 A·F5와 유량이 겹치지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if os.getenv("STOCK_SKIP_DOTENV", "0") != "1":
    load_dotenv(ROOT / ".env")

KST = ZoneInfo("Asia/Seoul")

# 스펙 §2.2 — 바꾸지 않는다.
CHANGE_MIN = 3.0
CHANGE_MAX = 25.0
CLOSE_POSITION_MIN = 0.8
AMOUNT_MULTIPLE_MIN = 2.0
AMOUNT_FLOOR = 1_000_000_000.0
PRICE_FLOOR = 1_000.0
RECORD_TOP = 5
HISTORY_DAYS = 20

# ETF/ETN/스팩은 이름으로 거른다. 랭킹 응답에 상품 구분 플래그가 없다.
_EXCLUDED_NAME_TOKENS = ("ETF", "ETN", "스팩", "KODEX", "TIGER", "KBSTAR", "ARIRANG",
                         "HANARO", "SOL ", "ACE ", "KOSEF", "TIMEFOLIO", "PLUS ")


def close_position(high: float, low: float, close: float) -> float | None:
    """종가가 당일 범위의 어디에 있는가. 0=저가, 1=고가. 고가=저가면 None."""
    if high <= low:
        return None
    return (close - low) / (high - low)


def is_excluded_name(name: str) -> bool:
    upper = (name or "").upper()
    return any(token in upper for token in _EXCLUDED_NAME_TOKENS)


def evaluate(row: dict) -> str | None:
    """스펙 §2.2 필터. 거부 사유를 돌려주고 통과면 None. 검사 순서도 고정이다."""
    if is_excluded_name(str(row.get("name") or "")):
        return "NAME_EXCLUDED"
    change = row.get("change_pct")
    if change is None or not (CHANGE_MIN <= float(change) < CHANGE_MAX):
        return "CHANGE_PCT"
    position = row.get("close_position")
    if position is None or float(position) < CLOSE_POSITION_MIN:
        return "CLOSE_POSITION"
    if float(row.get("close") or 0.0) < PRICE_FLOOR:
        return "PRICE_FLOOR"
    if float(row.get("amount") or 0.0) < AMOUNT_FLOOR:
        return "AMOUNT_FLOOR"
    if row.get("avg_amount_20d") is None or row.get("amount_multiple") is None:
        return "NO_HISTORY"
    if float(row["amount_multiple"]) < AMOUNT_MULTIPLE_MIN:
        return "AMOUNT_MULTIPLE"
    return None


def rank_candidates(rows: list[dict]) -> list[dict]:
    """통과 행을 거래대금 배수 내림차순(동률은 등락률)으로 세워 상위 RECORD_TOP개에 rank를 붙인다."""
    passed = [r for r in rows if evaluate(r) is None]
    passed.sort(
        key=lambda r: (float(r["amount_multiple"]), float(r["change_pct"])), reverse=True
    )
    ranked = []
    for rank, row in enumerate(passed[:RECORD_TOP], start=1):
        ranked.append({**row, "rank": rank, "rejected_reason": None})
    return ranked
```

- [ ] **Step 4: 통과 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_overnight_screen.py -q`
Expected: `7 passed`

- [ ] **Step 5: 린트·커밋**

```bash
.\.venv\Scripts\python.exe -m ruff check scripts/overnight_screen.py tests/test_overnight_screen.py
.\.venv\Scripts\python.exe -m mypy scripts/overnight_screen.py
git add scripts/overnight_screen.py tests/test_overnight_screen.py
git commit -m "feat(scripts): overnight_screen rules and ranking (C1 spec §2.2-2.3)"
```

---

### Task 2: overnight_screen — 일봉 파싱과 후보 행 조립

**Files:**
- Modify: `scripts/overnight_screen.py`
- Test: `tests/test_overnight_screen.py`

**Interfaces:**
- Produces:
  - `parse_daily(output2: list[dict], date: str) -> dict | None` — 일봉 output2(최신순 무관)에서 `date` 행의 OHLC·거래대금과 그 앞 `HISTORY_DAYS`일 평균 거래대금. `date` 행이 없으면 None. 앞 거래일이 20일 미만이면 `avg_amount_20d=None`.
  - `build_row(date: str, ranking_row: dict, daily: dict | None, daily_error: str | None) -> dict` — 후보 파일 한 줄(순위·거부 사유는 아직 없음). `daily=None`이면 `rejected_reason="DAILY_FAILED"`를 미리 넣는다.

- [ ] **Step 1: 실패하는 테스트 추가**

```python
# tests/test_overnight_screen.py 에 추가
from scripts.overnight_screen import build_row, parse_daily


def _daily_row(date: str, close: float, amount: float, high=None, low=None, open_=None) -> dict:
    return {
        "stck_bsop_date": date, "stck_clpr": str(close),
        "stck_oprc": str(open_ if open_ is not None else close),
        "stck_hgpr": str(high if high is not None else close),
        "stck_lwpr": str(low if low is not None else close),
        "acml_vol": "1000", "acml_tr_pbmn": str(amount),
    }


def test_parse_daily_reads_today_and_prior_20_day_mean():
    # 최신순으로 오지만 순서에 기대지 않는다. 20260921이 오늘, 앞 20일 평균은 1e9.
    rows = [_daily_row("20260921", 108.0, 5e9, high=110.0, low=99.0, open_=100.0)]
    rows += [_daily_row(f"2026{8 + i // 30:02d}{1 + i % 30:02d}", 100.0, 1e9) for i in range(25)]
    rows.reverse()
    parsed = parse_daily(rows, "20260921")
    assert parsed["close"] == 108.0 and parsed["high"] == 110.0 and parsed["low"] == 99.0
    assert parsed["amount"] == 5e9
    assert parsed["avg_amount_20d"] == 1e9
    assert parsed["history_days"] == 20


def test_parse_daily_marks_short_history_and_missing_today():
    rows = [_daily_row("20260921", 108.0, 5e9)] + [
        _daily_row(f"202609{d:02d}", 100.0, 1e9) for d in range(1, 6)
    ]
    parsed = parse_daily(rows, "20260921")
    assert parsed["avg_amount_20d"] is None and parsed["history_days"] == 5
    assert parse_daily(rows, "20260922") is None


def test_build_row_carries_raw_fields_and_daily_failure():
    ranking = {"mksc_shrn_iscd": "000001", "hts_kor_isnm": "정상전자", "prdy_ctrt": "5.00",
               "stck_prpr": "108", "acml_tr_pbmn": "5000000000"}
    daily = {"open": 100.0, "high": 110.0, "low": 99.0, "close": 108.0, "amount": 5e9,
             "avg_amount_20d": 1e9, "history_days": 20}
    row = build_row("20260921", ranking, daily, None)
    assert row["ticker"] == "000001" and row["date"] == "20260921"
    assert row["change_pct"] == 5.0 and row["close_position"] == close_position(110.0, 99.0, 108.0)
    assert row["amount_multiple"] == 5.0
    assert row["raw"]["ranking"] is ranking and row["raw"]["daily"] is daily
    assert row["rejected_reason"] is None

    failed = build_row("20260921", ranking, None, "KIS_ERROR:EGW00123")
    assert failed["rejected_reason"] == "DAILY_FAILED"
    assert failed["raw"]["daily_error"] == "KIS_ERROR:EGW00123"
    assert failed["close"] == 108.0  # 랭킹의 현재가로라도 채운다
```

- [ ] **Step 2: 실패 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_overnight_screen.py -q`
Expected: `ImportError: cannot import name 'build_row'`

- [ ] **Step 3: 구현**

```python
# scripts/overnight_screen.py 에 추가 (rank_candidates 아래)

def _f(value: object) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_daily(output2: list[dict], date: str) -> dict | None:
    """일봉 output2에서 `date` 행과 그 앞 HISTORY_DAYS일 평균 거래대금.

    응답은 보통 최신순이지만 순서에 기대지 않고 날짜로 정렬한다.
    """
    rows = [r for r in output2 if r.get("stck_bsop_date")]
    rows.sort(key=lambda r: str(r["stck_bsop_date"]), reverse=True)
    today = next((r for r in rows if str(r["stck_bsop_date"]) == date), None)
    if today is None:
        return None
    prior = [r for r in rows if str(r["stck_bsop_date"]) < date][:HISTORY_DAYS]
    amounts = [a for a in (_f(r.get("acml_tr_pbmn")) for r in prior) if a is not None]
    avg = sum(amounts) / len(amounts) if len(amounts) >= HISTORY_DAYS else None
    return {
        "open": _f(today.get("stck_oprc")),
        "high": _f(today.get("stck_hgpr")),
        "low": _f(today.get("stck_lwpr")),
        "close": _f(today.get("stck_clpr")),
        "amount": _f(today.get("acml_tr_pbmn")),
        "avg_amount_20d": avg,
        "history_days": len(amounts),
    }


def build_row(
    date: str, ranking_row: dict, daily: dict | None, daily_error: str | None
) -> dict:
    """후보 파일 한 줄. 순위·거부 사유는 rank_candidates/evaluate가 뒤에 붙인다."""
    ticker = str(ranking_row.get("mksc_shrn_iscd") or ranking_row.get("stck_shrn_iscd") or "")
    close = (daily or {}).get("close") if daily else None
    if close is None:
        close = _f(ranking_row.get("stck_prpr"))
    high = (daily or {}).get("high")
    low = (daily or {}).get("low")
    amount = (daily or {}).get("amount")
    if amount is None:
        amount = _f(ranking_row.get("acml_tr_pbmn"))
    avg = (daily or {}).get("avg_amount_20d")
    multiple = (amount / avg) if (amount is not None and avg) else None
    position = (
        close_position(high, low, close)
        if high is not None and low is not None and close is not None
        else None
    )
    row = {
        "date": date,
        "ticker": ticker,
        "name": str(ranking_row.get("hts_kor_isnm") or ""),
        "rank": None,
        "close": close,
        "open": (daily or {}).get("open"),
        "high": high,
        "low": low,
        "change_pct": _f(ranking_row.get("prdy_ctrt")),
        "close_position": position,
        "amount": amount,
        "avg_amount_20d": avg,
        "amount_multiple": multiple,
        "rejected_reason": None if daily is not None else "DAILY_FAILED",
        "raw": {"ranking": ranking_row, "daily": daily, "daily_error": daily_error},
    }
    return row
```

- [ ] **Step 4: 통과 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_overnight_screen.py -q`
Expected: `10 passed`

- [ ] **Step 5: 린트·커밋**

```bash
.\.venv\Scripts\python.exe -m ruff check scripts/overnight_screen.py tests/test_overnight_screen.py
.\.venv\Scripts\python.exe -m mypy scripts/overnight_screen.py
git add scripts/overnight_screen.py tests/test_overnight_screen.py
git commit -m "feat(scripts): overnight_screen daily-bar parsing and candidate rows"
```

---

### Task 3: overnight_screen — KIS 호출, 기록, CLI

**Files:**
- Modify: `scripts/overnight_screen.py`
- Test: `tests/test_overnight_screen.py`

**Interfaces:**
- Consumes: `src.api.kis_rest.get(path, params=, tr_id=, budget=, request_priority=, stop_on_rate_limit=)`, `src.api.kis_rest.CallBudget`, `src.api.kis_rest.REQUEST_PRIORITY_BACKGROUND`, `src.api.auth.load_or_refresh() -> str`, `scripts.fast_path_counterfactual.PocStop`, `Throttle`, `scripts.track_b_backfill.assert_paper_mode`, `src.modules.paper_fast_probe.RANKING_PATH`, `RANKING_TR_ID`, `_ranking_params(ranking_input)`, `scripts.catalyst_label.DAILY_PATH`, `DAILY_TR`
- Produces:
  - `ranking_params(ranking_input: str) -> dict` — `paper_fast_probe._ranking_params`에 등락률 범위만 `CHANGE_MIN/CHANGE_MAX`로 덮은 것
  - `async screen(date: str, *, fetch_ranking, fetch_daily) -> tuple[list[dict], dict]` — (모든 행: 순위 행 + 거부 행, 요약 행). 네트워크 함수를 주입받아 테스트한다.
  - `write_candidates(path: Path, rows: list[dict], summary: dict) -> None`
  - `CANDIDATES_DIR = ROOT / "data" / "overnight" / "candidates"`

- [ ] **Step 1: 실패하는 테스트 추가**

```python
# tests/test_overnight_screen.py 에 추가
import asyncio
import json

from scripts.overnight_screen import (
    CHANGE_MAX, CHANGE_MIN, ranking_params, screen, write_candidates,
)


def test_ranking_params_only_overrides_the_change_range():
    from src.modules.paper_fast_probe import _ranking_params
    base = _ranking_params("0001")
    ours = ranking_params("0001")
    assert ours["fid_rsfl_rate1"] == f"{CHANGE_MIN:.1f}"
    assert ours["fid_rsfl_rate2"] == f"{CHANGE_MAX:.1f}"
    assert {k: v for k, v in ours.items() if not k.startswith("fid_rsfl")} == {
        k: v for k, v in base.items() if not k.startswith("fid_rsfl")
    }


def _ranking_output(*tickers: str) -> list[dict]:
    return [
        {"mksc_shrn_iscd": t, "hts_kor_isnm": f"종목{t}", "prdy_ctrt": "5.00",
         "stck_prpr": "108", "acml_tr_pbmn": "5000000000"}
        for t in tickers
    ]


def test_screen_joins_ranking_with_daily_and_records_rejections_and_summary():
    async def fetch_ranking(market: str) -> list[dict]:
        return _ranking_output("000001", "000002") if market == "0001" else _ranking_output("000003")

    async def fetch_daily(ticker: str) -> tuple[list[dict], str | None]:
        if ticker == "000003":
            return [], "KIS_ERROR:EGW00123"
        rows = [_daily_row("20260921", 108.0, 5e9, high=110.0, low=99.0, open_=100.0)]
        rows += [_daily_row(f"202608{d:02d}", 100.0, 1e9) for d in range(1, 26)]
        if ticker == "000002":
            rows[0] = _daily_row("20260921", 104.0, 5e9, high=110.0, low=99.0)  # 종가 위치 0.45
        return rows, None

    rows, summary = asyncio.run(
        screen("20260921", fetch_ranking=fetch_ranking, fetch_daily=fetch_daily)
    )
    by_ticker = {r["ticker"]: r for r in rows}
    assert by_ticker["000001"]["rank"] == 1 and by_ticker["000001"]["rejected_reason"] is None
    assert by_ticker["000002"]["rank"] is None
    assert by_ticker["000002"]["rejected_reason"] == "CLOSE_POSITION"
    assert by_ticker["000003"]["rejected_reason"] == "DAILY_FAILED"
    assert summary == {
        "summary": True, "date": "20260921", "universe": 3, "candidates": 1, "degraded": True,
    }


def test_screen_dedupes_tickers_across_markets_and_marks_zero_candidate_days():
    async def fetch_ranking(market: str) -> list[dict]:
        return _ranking_output("000001")

    async def fetch_daily(ticker: str) -> tuple[list[dict], str | None]:
        return [_daily_row("20260921", 108.0, 5e9, high=110.0, low=99.0)] + [
            _daily_row(f"202608{d:02d}", 100.0, 4e9) for d in range(1, 26)   # 배수 1.25
        ], None

    rows, summary = asyncio.run(
        screen("20260921", fetch_ranking=fetch_ranking, fetch_daily=fetch_daily)
    )
    assert len(rows) == 1 and rows[0]["rejected_reason"] == "AMOUNT_MULTIPLE"
    assert summary["universe"] == 1 and summary["candidates"] == 0 and not summary["degraded"]


def test_write_candidates_puts_summary_last(tmp_path):
    path = tmp_path / "20260921.jsonl"
    write_candidates(path, [{"ticker": "000001", "rank": 1}], {"summary": True, "candidates": 1})
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["ticker"] == "000001"
    assert lines[-1]["summary"] is True
```

- [ ] **Step 2: 실패 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_overnight_screen.py -q`
Expected: `ImportError: cannot import name 'ranking_params'`

- [ ] **Step 3: 구현**

```python
# scripts/overnight_screen.py 에 추가 (build_row 아래)
from typing import Awaitable, Callable  # 파일 상단 import 블록으로 옮긴다

CANDIDATES_DIR = ROOT / "data" / "overnight" / "candidates"
MARKETS = ("0001", "1001")  # 코스피, 코스닥 — paper_fast_probe와 같은 코드
CALL_BUDGET = 60
REQUEST_INTERVAL_SEC = 1.2

FetchRanking = Callable[[str], Awaitable[list[dict]]]
FetchDaily = Callable[[str], Awaitable[tuple[list[dict], str | None]]]


def ranking_params(ranking_input: str) -> dict:
    """paper_fast_probe의 랭킹 파라미터에 등락률 범위만 §2.2 값으로 덮는다."""
    from src.modules.paper_fast_probe import _ranking_params

    params = dict(_ranking_params(ranking_input))
    params["fid_rsfl_rate1"] = f"{CHANGE_MIN:.1f}"
    params["fid_rsfl_rate2"] = f"{CHANGE_MAX:.1f}"
    return params


async def screen(
    date: str, *, fetch_ranking: FetchRanking, fetch_daily: FetchDaily
) -> tuple[list[dict], dict]:
    """유니버스 → 일봉 조인 → 규칙 → 순위. (모든 행, 요약 행)을 돌려준다.

    거부 행도 원시 필드와 함께 남긴다(스펙 §2.3). 일봉이 하나라도 실패하면
    degraded=True — 랭크 1이 그 종목이었을 가능성을 알 수 없어서다(§3.4).
    """
    ranking_rows: dict[str, dict] = {}
    for market in MARKETS:
        for row in await fetch_ranking(market):
            ticker = str(row.get("mksc_shrn_iscd") or row.get("stck_shrn_iscd") or "")
            if len(ticker) == 6 and ticker.isdigit() and ticker not in ranking_rows:
                ranking_rows[ticker] = row

    rows: list[dict] = []
    degraded = False
    for ticker, ranking_row in ranking_rows.items():
        # 랭킹만으로 떨어지는 조건은 일봉을 부르지 않는다 — 호출 예산(§2.4).
        pre = build_row(date, ranking_row, None, None)
        change = pre["change_pct"]
        if is_excluded_name(pre["name"]):
            pre["rejected_reason"] = "NAME_EXCLUDED"
        elif change is None or not (CHANGE_MIN <= change < CHANGE_MAX):
            pre["rejected_reason"] = "CHANGE_PCT"
        else:
            pre["rejected_reason"] = None
        if pre["rejected_reason"] is not None:
            rows.append(pre)
            continue
        output2, error = await fetch_daily(ticker)
        daily = parse_daily(output2, date) if error is None else None
        if error is None and daily is None:
            error = "NO_TODAY_BAR"
        row = build_row(date, ranking_row, daily, error)
        if row["rejected_reason"] == "DAILY_FAILED":
            degraded = True
        else:
            row["rejected_reason"] = evaluate(row)
        rows.append(row)

    ranked = rank_candidates([r for r in rows if r["rejected_reason"] is None])
    rank_of = {r["ticker"]: r["rank"] for r in ranked}
    for row in rows:
        row["rank"] = rank_of.get(row["ticker"])
        if row["rejected_reason"] is None and row["rank"] is None:
            row["rejected_reason"] = "BELOW_TOP"   # 통과했지만 6위 이하
    rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, r["ticker"]))
    summary = {
        "summary": True, "date": date, "universe": len(ranking_rows),
        "candidates": len(ranked), "degraded": degraded,
    }
    return rows, summary


def write_candidates(path: Path, rows: list[dict], summary: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.write(json.dumps(summary, ensure_ascii=False) + "\n")


def _kis_fetchers(budget: "kis_rest.CallBudget", throttle: "Throttle") -> tuple[FetchRanking, FetchDaily]:
    from scripts.catalyst_label import DAILY_PATH, DAILY_TR
    from scripts.fast_path_counterfactual import _assert_success
    from src.api import kis_rest
    from src.modules.paper_fast_probe import RANKING_PATH, RANKING_TR_ID

    async def _pace() -> None:
        # Throttle은 순수 페이서다: wait_seconds(now)로 남은 시간을 받아 자고 mark(now)한다.
        await asyncio.sleep(throttle.wait_seconds(monotonic()))
        throttle.mark(monotonic())

    async def fetch_ranking(market: str) -> list[dict]:
        await _pace()
        resp = await kis_rest.get(
            RANKING_PATH, params=ranking_params(market), tr_id=RANKING_TR_ID,
            stop_on_rate_limit=True, request_priority=kis_rest.REQUEST_PRIORITY_BACKGROUND,
            budget=budget,
        )
        _assert_success(resp)
        return list(resp.get("output") or [])[:30]

    async def fetch_daily(ticker: str) -> tuple[list[dict], str | None]:
        await _pace()
        today = datetime.now(KST).strftime("%Y%m%d")
        resp = await kis_rest.get(
            DAILY_PATH, tr_id=DAILY_TR,
            params={
                "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker,
                "FID_INPUT_DATE_1": "20250101", "FID_INPUT_DATE_2": today,
                "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
            },
            stop_on_rate_limit=True, request_priority=kis_rest.REQUEST_PRIORITY_BACKGROUND,
            budget=budget,
        )
        if str(resp.get("rt_cd") or "") != "0":
            return [], f"KIS_ERROR:{resp.get('msg_cd') or 'UNKNOWN'}"
        return list(resp.get("output2") or []), None

    return fetch_ranking, fetch_daily


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C1 종가 스크리닝 (15:32)")
    parser.add_argument("--dry-run", action="store_true", help="호출은 하되 기록하지 않는다")
    parser.add_argument("--root", type=Path, default=ROOT, help="data/ 가 있는 트리")
    parser.add_argument("--date", default=None, help="기록 파일 이름(기본: 오늘 KST). 테스트용")
    args = parser.parse_args(argv)

    from scripts.fast_path_counterfactual import PocStop, Throttle
    from scripts.track_b_backfill import assert_paper_mode
    from src.api import auth, kis_rest

    assert_paper_mode()
    if not await auth.load_or_refresh():
        raise PocStop("TOKEN_UNAVAILABLE")
    date = args.date or datetime.now(KST).strftime("%Y%m%d")
    budget = kis_rest.CallBudget(CALL_BUDGET)
    fetch_ranking, fetch_daily = _kis_fetchers(budget, Throttle(REQUEST_INTERVAL_SEC))
    rows, summary = await screen(date, fetch_ranking=fetch_ranking, fetch_daily=fetch_daily)
    print(f"유니버스 {summary['universe']} / 후보 {summary['candidates']} / "
          f"호출 {budget.used} / degraded={summary['degraded']}")
    for row in rows:
        if row["rank"]:
            print(f"  {row['rank']} {row['ticker']} {row['name']} 종가 {row['close']:.0f} "
                  f"등락 {row['change_pct']:+.2f}% 배수 {row['amount_multiple']:.2f}")
    if args.dry_run:
        print("(dry-run: 기록하지 않음)")
        return 0
    path = args.root / "data" / "overnight" / "candidates" / f"{date}.jsonl"
    write_candidates(path, rows, summary)
    print(f"기록: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    from scripts.fast_path_counterfactual import PocStop

    try:
        return asyncio.run(main_async(argv))
    except PocStop as exc:
        print(f"중단: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

`Throttle`(`scripts/fast_path_counterfactual.py:274`)은 `wait_seconds(now)`·`mark(now)`만 있는 순수 페이서라 위 `_pace()`로 감싼다. `from time import monotonic`을 상단 import에 추가한다.

- [ ] **Step 4: 통과 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_overnight_screen.py -q`
Expected: `14 passed`

- [ ] **Step 5: 개발 트리에서 dry-run (장 마감 후, 15:40 이후에 실행)**

Run: `.\.venv\Scripts\python.exe scripts\overnight_screen.py --dry-run`
Expected: `유니버스 N / 후보 K / 호출 M / degraded=False` 뒤에 후보 줄, `(dry-run: 기록하지 않음)`. 호출 수 M ≤ 60. `.env`가 PAPER가 아니면 `중단: NOT_PAPER_MODE`로 끝나는 것이 정상.

- [ ] **Step 6: 린트·커밋**

```bash
.\.venv\Scripts\python.exe -m ruff check scripts/overnight_screen.py tests/test_overnight_screen.py
.\.venv\Scripts\python.exe -m mypy scripts/overnight_screen.py
git add scripts/overnight_screen.py tests/test_overnight_screen.py
git commit -m "feat(scripts): overnight_screen KIS fetch, candidate file, CLI"
```

---

### Task 4: run_overnight_screen.ps1 런처와 DEV_ENV 등록 절차

**Files:**
- Create: `scripts/run_overnight_screen.ps1`
- Modify: `docs/DEV_ENV.md` (§11-2 뒤에 §11-3 추가)

**Interfaces:**
- Consumes: `scripts/overnight_screen.py` CLI (`--dry-run`)
- Produces: 로그 `data/logs/overnight_screen_<yyyyMMdd_HHmmss>.log`, 종료 코드 = 파이썬 종료 코드

- [ ] **Step 1: 런처 작성**

```powershell
# scripts/run_overnight_screen.ps1
<#
.SYNOPSIS
    C1 종가 스크리닝 런처. Task Scheduler(StockBot_OvernightScreen, 15:32)가 부른다.
.DESCRIPTION
    scripts\overnight_screen.py 를 운영 트리에서 실행해 data\overnight\candidates\<date>.jsonl
    을 남긴다. run_backfill.ps1 과 같은 이유로 콘솔 인코딩을 UTF-8로 두고 로그를 UTF-8로
    쓰며, 파이썬 호출 구간만 $ErrorActionPreference="Continue" 로 낮춰 stderr(트레이스백)가
    로그에 남게 한다(PS 5.1 + Stop + 2>&1 은 첫 stderr 줄에서 스크립트를 끝낸다).

    이 저장소는 PowerShell 5.1에서 돈다. `??`, `?.`, 삼항, `&&`/`||`는 쓰지 않는다.
#>
[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$script = Join-Path $repoRoot "scripts\overnight_screen.py"
$logDir = Join-Path $repoRoot "data\logs"

function Write-Ok([string]$Text)   { Write-Host "  [OK] $Text" -ForegroundColor Green }
function Write-Fail([string]$Text) { Write-Host "  [실패] $Text" -ForegroundColor Red }

if (-not (Test-Path $venvPython)) { Write-Fail "가상환경이 없습니다: $venvPython"; exit 1 }
if (-not (Test-Path $script)) { Write-Fail "스크립트가 없습니다: $script"; exit 1 }
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logPath = Join-Path $logDir "overnight_screen_$stamp.log"
Write-Host "  로그: $logPath"

$env:PYTHONIOENCODING = "utf-8"
$pyArgs = @("-u", $script)
if ($DryRun) { $pyArgs += "--dry-run" }

$previousOutputEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$writer = [System.IO.StreamWriter]::new($logPath, $false, [System.Text.UTF8Encoding]::new($false))
$writer.AutoFlush = $true

$code = 0
Push-Location -LiteralPath $repoRoot
$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & $venvPython $pyArgs 2>&1 | ForEach-Object {
        $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { "$_" }
        Write-Host $line
        $writer.WriteLine($line)
    }
    $code = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previousErrorAction
    Pop-Location
    $writer.Dispose()
    [Console]::OutputEncoding = $previousOutputEncoding
}

if ($code -ne 0) {
    Write-Fail "스크리닝이 종료 코드 $code 로 끝났습니다. 로그를 확인하세요."
    exit $code
}
Write-Ok "완료"
exit 0
```

- [ ] **Step 2: 런처를 가짜 트리에서 검증 (stderr·종료 코드)**

Run (PowerShell):
```powershell
$tmp = Join-Path $env:TEMP "os_test"
New-Item -ItemType Directory -Force "$tmp\scripts","$tmp\.venv\Scripts" | Out-Null
Copy-Item D:\Private\stock\scripts\run_overnight_screen.ps1 "$tmp\scripts\"
Copy-Item (Get-Command python).Source "$tmp\.venv\Scripts\python.exe"
Set-Content "$tmp\scripts\overnight_screen.py" -Encoding utf8 -Value "print('유니버스 3')`nraise RuntimeError('simulated')"
powershell -NoProfile -ExecutionPolicy Bypass -File "$tmp\scripts\run_overnight_screen.ps1"; "exit=$LASTEXITCODE"
Get-Content (Get-ChildItem "$tmp\data\logs\*.log" | Sort-Object LastWriteTime | Select-Object -Last 1).FullName
Remove-Item -Recurse -Force $tmp
```
Expected: 화면과 로그 양쪽에 `Traceback ... RuntimeError: simulated`, `exit=1`.

- [ ] **Step 3: 개발 트리에서 dry-run**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_overnight_screen.ps1 -DryRun; "exit=$LASTEXITCODE"`
Expected: Task 3 Step 5와 같은 출력, `[OK] 완료`, `exit=0`. (개발 `.env`가 PAPER가 아니면 `중단: NOT_PAPER_MODE`, exit=2 — 런처가 코드를 전파하는지 확인하는 용도로 그것도 유효하다.)

- [ ] **Step 4: DEV_ENV.md §11-3 추가** — §11-2 블록 끝(`로그 쓰기 실패는 무시한다 …` 줄) 바로 뒤, `12. 실행 방법` 구분선 앞에 삽입:

```text
──────────────────────────────────────────────────
11-3. C1 종가 스크리닝 등록 (StockBot_OvernightScreen, 15:32)
──────────────────────────────────────────────────

  스펙: docs/superpowers/specs/2026-09-21-c1-overnight-close-hypothesis.md §3.3
  운영 트리(D:\Private\stock-prod)에서 등록한다. 마감 후라 A·F5와 유량이 겹치지 않는다.

  $Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"D:\Private\stock-prod\scripts\run_overnight_screen.ps1`""

  $Trigger = New-ScheduledTaskTrigger -Daily -At 15:32

  $Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 0

  Register-ScheduledTask `
    -TaskName "StockBot_OvernightScreen" `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -RunLevel Highest

  확인: 등록 다음 거래일 15:33에 data\overnight\candidates\<날짜>.jsonl 이 있고 마지막
  줄이 {"summary": true, ...} 인지. 없으면 data\logs\overnight_screen_*.log 를 본다.
  주말·휴장일에도 돌지만 랭킹이 전일 값이라 후보 파일에 그날 날짜가 남는다 —
  overnight_sieve 가 D+1 분봉이 없는 날은 표본에서 뺀다(스펙 §4.3).
```

- [ ] **Step 5: 커밋**

```bash
git add scripts/run_overnight_screen.ps1 docs/DEV_ENV.md
git commit -m "feat(scripts): run_overnight_screen.ps1 launcher and Task Scheduler registration doc"
```

---

### Task 5: track_b_backfill — 후보의 D+1 분봉을 백필 대상에 합류

**Files:**
- Modify: `scripts/track_b_backfill.py:153-199` (`needed_pairs`), `main_async` (약 262~300행)
- Test: `tests/test_track_b_backfill.py`

**Interfaces:**
- Consumes: `data/overnight/candidates/<date>.jsonl` (Task 3 형식: `rank`가 정수인 행이 후보)
- Produces:
  - `overnight_pairs(candidates_dir: Path, all_dates: list[str]) -> dict[str, set[str]]` — `{D+1: {tickers}}`. D+1은 `all_dates`(정렬된 거래일 목록)에서 D-0 다음 날짜. 다음 날짜가 없으면(오늘이 D-0) 제외.
  - `needed_pairs(..., overnight_dir: Path | None = None)` — 주어지면 `overnight_pairs`의 쌍을 합친다. `all_dates`는 F1 스냅샷 날짜 ∪ 후보 파일 날짜 ∪ 오늘(KST).
  - `OVERNIGHT_CANDIDATES_DIR = ROOT / "data" / "overnight" / "candidates"`

- [ ] **Step 1: 실패하는 테스트 추가**

```python
# tests/test_track_b_backfill.py 에 추가
from scripts.track_b_backfill import overnight_pairs


def _candidates(tmp_path, date: str, ranked: list[str], rejected: list[str] = ()) -> None:
    d = tmp_path / "overnight"
    d.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"date": date, "ticker": t, "rank": i + 1}) for i, t in enumerate(ranked)]
    lines += [json.dumps({"date": date, "ticker": t, "rank": None, "rejected_reason": "X"})
              for t in rejected]
    lines.append(json.dumps({"summary": True, "date": date, "candidates": len(ranked)}))
    (d / f"{date}.jsonl").write_text("\n".join(lines), encoding="utf-8")


def test_overnight_pairs_maps_candidates_to_the_next_trading_date(tmp_path):
    _candidates(tmp_path, "20260918", ["000001", "000002"], rejected=["000009"])
    _candidates(tmp_path, "20260921", ["000003"])          # 다음 거래일 없음 → 제외
    pairs = overnight_pairs(tmp_path / "overnight", ["20260917", "20260918", "20260921"])
    assert pairs == {"20260921": {"000001", "000002"}}


def test_needed_pairs_merges_overnight_candidates_into_f1_pairs(tmp_path):
    for date in ("20260918", "20260921"):
        _snapshot(tmp_path, date, ["005930", "000660"])
    _candidates(tmp_path, "20260918", ["000001"])
    without = track_b_backfill.needed_pairs(depth=5, snapshot_dir=tmp_path, warmup_days=0)
    with_c = track_b_backfill.needed_pairs(depth=5, snapshot_dir=tmp_path, warmup_days=0,
                                           overnight_dir=tmp_path / "overnight")
    assert with_c["20260921"] == without["20260921"] | {"000001"}
    assert with_c["20260918"] == without["20260918"]
```

`_snapshot`은 이 테스트 파일에 이미 있는 헬퍼다(`test_needed_pairs_adds_the_previous_session_for_warmup`이 쓴다).

- [ ] **Step 2: 실패 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_track_b_backfill.py -q -k overnight`
Expected: `ImportError: cannot import name 'overnight_pairs'`

- [ ] **Step 3: 구현**

`scripts/track_b_backfill.py`에서 `needed_pairs` 위에 추가하고, `needed_pairs` 시그니처·본문 끝을 바꾼다:

```python
OVERNIGHT_CANDIDATES_DIR = ROOT / "data" / "overnight" / "candidates"


def overnight_pairs(candidates_dir: Path, all_dates: list[str]) -> dict[str, set[str]]:
    """C1 후보(rank가 정수인 행)의 (D+1, ticker). D+1은 all_dates에서 D-0 다음 거래일.

    다음 거래일이 아직 없으면(오늘이 D-0) 빠진다 — 내일 백필이 잡는다. 스펙 §3.
    """
    ordered = sorted(set(all_dates))
    pairs: dict[str, set[str]] = {}
    if not candidates_dir.exists():
        return pairs
    for path in sorted(candidates_dir.glob("*.jsonl")):
        date = path.stem
        later = [d for d in ordered if d > date]
        if not later:
            continue
        next_date = later[0]
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("summary") or not isinstance(row.get("rank"), int):
                continue
            pairs.setdefault(next_date, set()).add(str(row["ticker"]))
    return pairs
```

`needed_pairs`:

```python
def needed_pairs(
    depth: int = 5,
    snapshot_dir: Path | None = None,
    warmup_days: int = 0,
    universes_path: Path | None = None,
    overnight_dir: Path | None = None,
) -> dict[str, set[str]]:
    ...  # 기존 본문 그대로, `if warmup_days > 0:` 블록 앞에 아래를 넣는다
    if overnight_dir is not None:
        today = datetime.now(KST).strftime("%Y%m%d")
        calendar = sorted(
            set(all_dates) | {p.stem for p in overnight_dir.glob("*.jsonl")} | {today}
        )
        for date, tickers in overnight_pairs(overnight_dir, calendar).items():
            needed.setdefault(date, set()).update(tickers)
            if date not in all_dates:
                all_dates = sorted(set(all_dates) | {date})
```

`main_async`의 `needed = needed_pairs(...)` 호출에 `overnight_dir=OVERNIGHT_CANDIDATES_DIR`를 추가한다. docstring에 한 줄: "``overnight_dir``가 있으면 C1 후보의 D+1 쌍도 넣는다(스펙 §3)."

- [ ] **Step 4: 통과 확인 (기존 백필 테스트 전부 포함)**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_track_b_backfill.py -q`
Expected: 기존 개수 + 2 passed, 실패 0

- [ ] **Step 5: 백필 dry-run으로 합류 확인**

Run: `.\.venv\Scripts\python.exe scripts\track_b_backfill.py --dry-run`
Expected: `대상 N거래일 / M쌍 …` — 아직 후보 파일이 없으면 이전과 같은 수. (Task 3 dry-run은 기록하지 않으므로 실제 합류는 운영 첫 실행 다음날 확인.)

- [ ] **Step 6: 린트·커밋**

```bash
.\.venv\Scripts\python.exe -m ruff check scripts/track_b_backfill.py tests/test_track_b_backfill.py
.\.venv\Scripts\python.exe -m mypy scripts/track_b_backfill.py
git add scripts/track_b_backfill.py tests/test_track_b_backfill.py
git commit -m "feat(scripts): backfill next-day minute bars for C1 overnight candidates"
```

---

### Task 6: overnight_sieve — 갭 하드스탑 + F4 재생 + 비용 (순수 함수)

**Files:**
- Create: `scripts/overnight_sieve.py`
- Test: `tests/test_overnight_sieve.py`

**Interfaces:**
- Consumes: `scripts.track_b_rules.simulate_exit(bars, entry_idx, entry_price, order=)`, `HARD_STOP`, `TIMEOUT_TIME`; `src.warmup.covers_session(bars) -> bool`
- Produces:
  - `BASE_ROUND_TRIP_COST_PCT=0.18, HARD_STOP_SLIPPAGE_PCT=0.30, TRAILING_SLIPPAGE_PCT=0.15, TIMEOUT_SLIPPAGE_PCT=0.20`
  - `simulate_overnight(bars: list[dict], entry_price: float) -> dict` — 키: `open, gap_pct, exit_reason ("GAP_HARD_STOP"|"HARD_STOP"|"TRAILING"|"TIMEOUT"|"DATA_END"), exit_time, exit_price, gross_pct, bars_complete`
  - `apply_costs(gross_pct: float, exit_reason: str) -> dict` — `net_cost_pct, net_slip_pct`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_overnight_sieve.py
"""C1 오버나이트 체 — 순수 함수만 검사한다. 파일·DB는 읽지 않는다."""

from scripts.overnight_sieve import (
    BASE_ROUND_TRIP_COST_PCT,
    HARD_STOP_SLIPPAGE_PCT,
    TRAILING_SLIPPAGE_PCT,
    apply_costs,
    simulate_overnight,
)


def _bar(time_: str, o: float, h: float, lo: float, c: float) -> dict:
    return {"date": "20260922", "time": time_, "open": o, "high": h, "low": lo, "close": c,
            "volume": 100.0}


def _session(first: dict, *rest: dict) -> list[dict]:
    """첫 봉 + 나머지 + 15:15 타임아웃 봉까지 채워 covers_session을 만족시킨다."""
    bars = [first, *rest]
    last_min = int(bars[-1]["time"][:2]) * 60 + int(bars[-1]["time"][2:4])
    close = bars[-1]["close"]
    for m in range(last_min + 1, 15 * 60 + 31):
        bars.append(_bar(f"{m // 60:02d}{m % 60:02d}00", close, close, close, close))
    return bars


def test_gap_down_beyond_hard_stop_exits_at_the_open():
    bars = _session(_bar("090000", 97.0, 99.0, 96.0, 98.0))
    r = simulate_overnight(bars, entry_price=100.0)
    assert r["exit_reason"] == "GAP_HARD_STOP" and r["exit_time"] == "090000"
    assert r["exit_price"] == 97.0 and r["gross_pct"] == -3.0
    assert r["gap_pct"] == -3.0 and r["bars_complete"] is True


def test_gap_up_then_trailing_uses_track_b_rule_low_first():
    # 시가 +1%, 09:01 고가 103(스텝 2.5%), 09:02 저가 100.4(스탑 100.5 이탈)
    bars = _session(
        _bar("090000", 101.0, 101.5, 100.8, 101.2),
        _bar("090100", 101.2, 103.0, 101.0, 102.8),
        _bar("090200", 102.8, 103.0, 100.4, 100.6),
    )
    r = simulate_overnight(bars, entry_price=100.0)
    assert r["gap_pct"] == 1.0
    assert r["exit_reason"] == "TRAILING" and r["exit_time"] == "090200"
    assert round(r["exit_price"], 2) == 100.5 and round(r["gross_pct"], 2) == 0.5


def test_hard_stop_inside_the_day_and_timeout():
    stopped = simulate_overnight(
        _session(_bar("090000", 100.5, 100.8, 97.9, 98.0)), entry_price=100.0
    )
    assert stopped["exit_reason"] == "HARD_STOP" and stopped["gross_pct"] == -2.0

    flat = simulate_overnight(_session(_bar("090000", 100.5, 100.8, 100.2, 100.5)),
                              entry_price=100.0)
    assert flat["exit_reason"] == "TIMEOUT" and flat["exit_time"] == "151500"


def test_incomplete_session_is_flagged_and_ends_with_data_end():
    bars = [_bar("090000", 100.5, 100.8, 100.2, 100.5), _bar("090100", 100.5, 100.6, 100.4, 100.5)]
    r = simulate_overnight(bars, entry_price=100.0)
    assert r["bars_complete"] is False and r["exit_reason"] == "DATA_END"


def test_costs_follow_the_improvement_plan_constants():
    assert (BASE_ROUND_TRIP_COST_PCT, HARD_STOP_SLIPPAGE_PCT, TRAILING_SLIPPAGE_PCT) == (
        0.18, 0.30, 0.15
    )
    c = apply_costs(2.5, "TRAILING")
    assert round(c["net_cost_pct"], 2) == 2.32 and round(c["net_slip_pct"], 2) == 2.17
    g = apply_costs(-3.0, "GAP_HARD_STOP")
    assert round(g["net_slip_pct"], 2) == -3.48   # 시가 하드스탑도 하드스탑 슬리피지
    t = apply_costs(0.0, "TIMEOUT")
    assert round(t["net_slip_pct"], 2) == -0.38
```

- [ ] **Step 2: 실패 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_overnight_sieve.py -q`
Expected: `ModuleNotFoundError: No module named 'scripts.overnight_sieve'`

- [ ] **Step 3: 구현**

```python
# scripts/overnight_sieve.py
"""C1 오버나이트 체 — 후보를 D-0 종가에 산 것으로 두고 D+1 분봉에 A의 F4 규칙을 재생한다.

판정 기준은 docs/superpowers/specs/2026-09-21-c1-overnight-close-hypothesis.md §4에 고정돼
있다. 이 스크립트는 그 수치를 계산해 보여줄 뿐 판정하지 않는다 — n≥50 전에는 어떤 값도
결론이 아니다.

    .\\.venv\\Scripts\\python.exe scripts\\overnight_sieve.py --root D:\\Private\\stock-prod
    .\\.venv\\Scripts\\python.exe scripts\\overnight_sieve.py --root D:\\Private\\stock-prod --json out.json

분봉은 track_b_backfill이 채운 data/backtest_bars/<D+1>_<ticker>.json 이다. 봉 안 순서는
"저가 먼저"로 고정한다(트랙 B와 같다).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.track_b_rules import HARD_STOP, simulate_exit  # noqa: E402
from src import warmup  # noqa: E402

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


def simulate_overnight(bars: list[dict], entry_price: float) -> dict:
    """전날 종가 진입. 첫 봉 시가가 하드스탑 아래면 시가에서 끝, 아니면 트랙 B F4 재생."""
    first = bars[0]
    open_price = float(first["open"])
    gap_pct = (open_price / entry_price - 1) * 100
    complete = warmup.covers_session(bars)
    if open_price <= entry_price * (1 - HARD_STOP):
        return {
            "open": open_price, "gap_pct": gap_pct, "exit_reason": "GAP_HARD_STOP",
            "exit_time": first["time"], "exit_price": open_price, "gross_pct": gap_pct,
            "bars_complete": complete,
        }
    result = simulate_exit(bars, 0, entry_price, order="low_first")
    return {
        "open": open_price, "gap_pct": gap_pct, "exit_reason": result["reason"],
        "exit_time": result["exit_time"], "exit_price": result["exit_price"],
        "gross_pct": result["pct"], "bars_complete": complete,
    }


def apply_costs(gross_pct: float, exit_reason: str) -> dict:
    net_cost = gross_pct - BASE_ROUND_TRIP_COST_PCT
    slip = _SLIPPAGE_BY_REASON.get(exit_reason, TIMEOUT_SLIPPAGE_PCT) + ENTRY_SLIPPAGE_PCT
    return {"net_cost_pct": net_cost, "net_slip_pct": net_cost - slip}
```

- [ ] **Step 4: 통과 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_overnight_sieve.py -q`
Expected: `5 passed`. `test_hard_stop_inside_the_day_and_timeout`의 TIMEOUT 케이스에서 `simulate_exit`가 `TIMEOUT_TIME="151500"`인 봉에서 `TIMEOUT`을 돌려주는지 확인 — `_session`이 15:15 봉을 만든다.

- [ ] **Step 5: 린트·커밋**

```bash
.\.venv\Scripts\python.exe -m ruff check scripts/overnight_sieve.py tests/test_overnight_sieve.py
.\.venv\Scripts\python.exe -m mypy scripts/overnight_sieve.py
git add scripts/overnight_sieve.py tests/test_overnight_sieve.py
git commit -m "feat(scripts): overnight_sieve gap hard stop, F4 replay and cost model"
```

---

### Task 7: overnight_sieve — 집계·결측 회계·판정 수치

**Files:**
- Modify: `scripts/overnight_sieve.py`
- Test: `tests/test_overnight_sieve.py`

**Interfaces:**
- Consumes: `scripts.track_b_backtest.bootstrap_ci(values, *, seed=, resamples=, alpha=) -> (lo, hi)`
- Produces:
  - `summarize(results: list[dict], *, missing: dict) -> dict` — `results`는 결과 파일 형식(§파일 구조)의 행. 키: `n, mean, ci_low, ci_high, mean_top2_removed, max_drawdown_pct, pass_conditions {mean_positive, ci_low_positive, robust_top2}, early_stop {evaluable, triggered}, reasons {…: count}, gap {mean, median, share_positive}, rank_means {1..5: mean}, missing`
  - 판정 상수 `MIN_N=50, EARLY_STOP_N=30, EARLY_STOP_MEAN=-1.0, EARLY_STOP_DRAWDOWN=-15.0, TOP_REMOVED=2`

- [ ] **Step 1: 실패하는 테스트 추가**

```python
# tests/test_overnight_sieve.py 에 추가
from scripts.overnight_sieve import EARLY_STOP_N, MIN_N, summarize


def _result(date: str, pct: float, rank: int = 1, reason: str = "TRAILING", gap: float = 0.5):
    return {"date": date, "rank": rank, "net_slip_pct": pct, "exit_reason": reason,
            "gap_pct": gap, "bars_complete": True}


def test_summarize_uses_rank1_only_for_the_primary_metric_and_counts_reasons():
    results = [_result("20260901", 1.0), _result("20260901", 9.0, rank=2),
               _result("20260902", -2.0, reason="HARD_STOP", gap=-1.0),
               _result("20260903", 3.0)]
    s = summarize(results, missing={"screen_failed": 1, "degraded": 0, "bars_incomplete": 0,
                                    "no_candidate": 2})
    assert s["n"] == 3
    assert round(s["mean"], 4) == round((1.0 - 2.0 + 3.0) / 3, 4)
    assert s["reasons"] == {"TRAILING": 2, "HARD_STOP": 1}
    assert s["rank_means"][2] == 9.0
    assert s["gap"]["share_positive"] == 2 / 3
    assert s["missing"]["screen_failed"] == 1 and s["missing"]["no_candidate"] == 2
    assert s["pass_conditions"]["evaluable"] is False  # n < 50


def test_summarize_top2_removed_and_drawdown():
    pcts = [5.0, 4.0] + [-0.5] * 10
    results = [_result(f"202609{i + 1:02d}", p) for i, p in enumerate(pcts)]
    s = summarize(results, missing={})
    assert round(s["mean_top2_removed"], 2) == -0.5
    assert s["max_drawdown_pct"] == -5.0          # 9.0 정점 뒤 -0.5×10
    assert s["pass_conditions"]["robust_top2"] is False


def test_early_stop_evaluates_only_at_thirty_and_triggers_on_mean():
    results = [_result(f"202609{i + 1:02d}", -1.5) for i in range(EARLY_STOP_N)]
    s = summarize(results, missing={})
    assert s["early_stop"] == {"evaluable": True, "triggered": True, "reason": "MEAN"}
    fewer = summarize(results[:-1], missing={})
    assert fewer["early_stop"]["evaluable"] is False


def test_pass_requires_all_three_conditions_at_min_n():
    results = [_result(f"2026{9 + i // 28:02d}{1 + i % 28:02d}", 1.0 + (i % 3) * 0.1)
               for i in range(MIN_N)]
    s = summarize(results, missing={})
    assert s["pass_conditions"]["evaluable"] is True
    assert s["pass_conditions"]["mean_positive"] and s["pass_conditions"]["ci_low_positive"]
    assert s["pass_conditions"]["robust_top2"] and s["pass_conditions"]["all"]
```

- [ ] **Step 2: 실패 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_overnight_sieve.py -q`
Expected: `ImportError: cannot import name 'summarize'`

- [ ] **Step 3: 구현**

```python
# scripts/overnight_sieve.py 에 추가 (apply_costs 아래)
from collections import Counter  # 상단 import 블록으로
from statistics import mean, median  # 상단 import 블록으로

from scripts.track_b_backtest import bootstrap_ci  # noqa: E402  (상단 import 블록으로)

# 스펙 §4.1 — 바꾸지 않는다.
MIN_N = 50
EARLY_STOP_N = 30
EARLY_STOP_MEAN = -1.0
EARLY_STOP_DRAWDOWN = -15.0
TOP_REMOVED = 2


def _max_drawdown(values: list[float]) -> float:
    peak = 0.0
    cum = 0.0
    worst = 0.0
    for v in values:
        cum += v
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return worst


def summarize(results: list[dict], *, missing: dict) -> dict:
    """§4.1 주 지표·판정 조건과 §4.2 부 지표. 판정은 하지 않고 조건 충족 여부만 낸다."""
    rank1 = sorted((r for r in results if r.get("rank") == 1 and r.get("bars_complete")),
                   key=lambda r: r["date"])
    values = [float(r["net_slip_pct"]) for r in rank1]
    n = len(values)
    avg = mean(values) if values else None
    lo, hi = bootstrap_ci(values) if n >= 2 else (None, None)
    trimmed = sorted(values)[:-TOP_REMOVED] if n > TOP_REMOVED else []
    trimmed_mean = mean(trimmed) if trimmed else None
    drawdown = _max_drawdown(values)

    evaluable = n >= MIN_N
    conditions = {
        "evaluable": evaluable,
        "mean_positive": bool(avg is not None and avg > 0),
        "ci_low_positive": bool(lo is not None and lo > 0),
        "robust_top2": bool(trimmed_mean is not None and trimmed_mean > 0),
    }
    conditions["all"] = evaluable and all(
        conditions[k] for k in ("mean_positive", "ci_low_positive", "robust_top2")
    )

    early: dict = {"evaluable": n >= EARLY_STOP_N, "triggered": False, "reason": None}
    if early["evaluable"] and avg is not None:
        if avg < EARLY_STOP_MEAN:
            early.update(triggered=True, reason="MEAN")
        elif drawdown < EARLY_STOP_DRAWDOWN:
            early.update(triggered=True, reason="DRAWDOWN")

    gaps = [float(r["gap_pct"]) for r in rank1]
    by_rank: dict[int, list[float]] = {}
    for r in results:
        if isinstance(r.get("rank"), int) and r.get("bars_complete"):
            by_rank.setdefault(r["rank"], []).append(float(r["net_slip_pct"]))
    return {
        "n": n,
        "mean": avg,
        "ci_low": lo,
        "ci_high": hi,
        "mean_top2_removed": trimmed_mean,
        "max_drawdown_pct": drawdown,
        "pass_conditions": conditions,
        "early_stop": early,
        "reasons": dict(Counter(r["exit_reason"] for r in rank1)),
        "gap": {
            "mean": mean(gaps) if gaps else None,
            "median": median(gaps) if gaps else None,
            "share_positive": (sum(1 for g in gaps if g > 0) / n) if n else None,
        },
        "rank_means": {k: mean(v) for k, v in sorted(by_rank.items())},
        "missing": dict(missing),
    }
```

- [ ] **Step 4: 통과 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_overnight_sieve.py -q`
Expected: `9 passed`

- [ ] **Step 5: 린트·커밋**

```bash
.\.venv\Scripts\python.exe -m ruff check scripts/overnight_sieve.py tests/test_overnight_sieve.py
.\.venv\Scripts\python.exe -m mypy scripts/overnight_sieve.py
git add scripts/overnight_sieve.py tests/test_overnight_sieve.py
git commit -m "feat(scripts): overnight_sieve summary with pre-registered pass and early-stop conditions"
```

---

### Task 8: overnight_sieve — 파일 읽기·결과 기록·CLI

**Files:**
- Modify: `scripts/overnight_sieve.py`
- Test: `tests/test_overnight_sieve.py`

**Interfaces:**
- Consumes: `scripts.strategy_backtest.read_cached_bars(date, ticker, cache_dir=) -> list[dict] | None`, `scripts.track_b_backfill.overnight_pairs`(D+1 계산 규칙과 같게 하려고 그 달력 논리를 재사용: `all_dates` = 후보 파일 날짜 ∪ `backtest_bars` 파일 날짜)
- Produces:
  - `load_candidates(candidates_dir: Path) -> dict[str, list[dict]]` — `{date: [후보 행(rank 정수)]}`; 요약 행이 없는 파일은 `missing["screen_died"]`로 셈
  - `run(root: Path) -> tuple[list[dict], dict]` — (결과 행, missing)
  - 결과 행은 §파일 구조의 결과 형식, `data/overnight/results/<date>.json`에 날짜별 리스트로 저장

- [ ] **Step 1: 실패하는 테스트 추가**

```python
# tests/test_overnight_sieve.py 에 추가
import json

from scripts.overnight_sieve import load_candidates, run


def _write_candidates(root, date, rows, summary=True):
    d = root / "data" / "overnight" / "candidates"
    d.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in rows]
    if summary:
        lines.append(json.dumps({"summary": True, "date": date, "candidates": len(rows)}))
    (d / f"{date}.jsonl").write_text("\n".join(lines), encoding="utf-8")


def _write_bars(root, date, ticker, bars):
    d = root / "data" / "backtest_bars"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{date}_{ticker}.json").write_text(json.dumps(bars), encoding="utf-8")


def test_load_candidates_keeps_ranked_rows_and_flags_dead_files(tmp_path):
    _write_candidates(tmp_path, "20260921", [
        {"date": "20260921", "ticker": "000001", "rank": 1, "close": 100.0},
        {"date": "20260921", "ticker": "000009", "rank": None, "rejected_reason": "X"},
    ])
    _write_candidates(tmp_path, "20260922", [{"date": "20260922", "ticker": "000002",
                                              "rank": 1, "close": 50.0}], summary=False)
    loaded, missing = load_candidates(tmp_path / "data" / "overnight" / "candidates")
    assert [r["ticker"] for r in loaded["20260921"]] == ["000001"]
    assert "20260922" not in loaded and missing["screen_died"] == 1


def test_run_replays_next_day_bars_and_accounts_for_missing(tmp_path):
    _write_candidates(tmp_path, "20260921", [
        {"date": "20260921", "ticker": "000001", "rank": 1, "close": 100.0},
        {"date": "20260921", "ticker": "000002", "rank": 2, "close": 100.0},   # 분봉 없음
    ])
    _write_candidates(tmp_path, "20260922", [])                                 # 후보 0
    _write_bars(tmp_path, "20260922", "000001",
                _session(_bar("090000", 97.0, 99.0, 96.0, 98.0)))
    results, missing = run(tmp_path)
    assert len(results) == 1
    r = results[0]
    assert (r["date"], r["next_date"], r["ticker"], r["rank"]) == ("20260921", "20260922",
                                                                   "000001", 1)
    assert r["exit_reason"] == "GAP_HARD_STOP" and round(r["net_slip_pct"], 2) == -3.48
    assert missing["bars_missing"] == 1 and missing["no_candidate"] == 1
    saved = json.loads((tmp_path / "data" / "overnight" / "results" / "20260921.json")
                       .read_text(encoding="utf-8"))
    assert saved[0]["ticker"] == "000001"
```

- [ ] **Step 2: 실패 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_overnight_sieve.py -q`
Expected: `ImportError: cannot import name 'load_candidates'`

- [ ] **Step 3: 구현**

```python
# scripts/overnight_sieve.py 에 추가 (summarize 아래)
from collections import Counter  # 이미 있음

from scripts.strategy_backtest import read_cached_bars  # noqa: E402  (상단 import 블록으로)


def load_candidates(candidates_dir: Path) -> tuple[dict[str, list[dict]], dict]:
    """{D-0: [rank가 정수인 행]}. 요약 행이 없는 파일은 중간에 죽은 것 — 표본에서 빼고 센다."""
    loaded: dict[str, list[dict]] = {}
    missing = {"screen_died": 0, "degraded": 0, "no_candidate": 0}
    if not candidates_dir.exists():
        return loaded, missing
    for path in sorted(candidates_dir.glob("*.jsonl")):
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not rows or not rows[-1].get("summary"):
            missing["screen_died"] += 1
            continue
        summary = rows[-1]
        if summary.get("degraded"):
            missing["degraded"] += 1
        ranked = [r for r in rows[:-1] if isinstance(r.get("rank"), int)]
        if not ranked:
            missing["no_candidate"] += 1
        loaded[path.stem] = ranked
    return loaded, missing


def _next_dates(candidate_dates: list[str], bars_dir: Path) -> dict[str, str | None]:
    bar_dates = {p.name[:8] for p in bars_dir.glob("*_*.json")} if bars_dir.exists() else set()
    calendar = sorted(set(candidate_dates) | bar_dates)
    out: dict[str, str | None] = {}
    for date in candidate_dates:
        later = [d for d in calendar if d > date]
        out[date] = later[0] if later else None
    return out


def run(root: Path) -> tuple[list[dict], dict]:
    candidates_dir = root / "data" / "overnight" / "candidates"
    results_dir = root / "data" / "overnight" / "results"
    bars_dir = root / "data" / "backtest_bars"
    loaded, missing = load_candidates(candidates_dir)
    missing.update({"bars_missing": 0, "bars_incomplete": 0, "awaiting_next_day": 0})
    next_of = _next_dates(sorted(loaded), bars_dir)
    results: list[dict] = []
    for date, rows in sorted(loaded.items()):
        next_date = next_of[date]
        if next_date is None:
            missing["awaiting_next_day"] += 1
            continue
        day_results: list[dict] = []
        for row in rows:
            bars = read_cached_bars(next_date, str(row["ticker"]), cache_dir=bars_dir)
            if not bars:
                missing["bars_missing"] += 1
                continue
            sim = simulate_overnight(bars, float(row["close"]))
            if not sim["bars_complete"]:
                missing["bars_incomplete"] += 1
            costs = apply_costs(sim["gross_pct"], sim["exit_reason"])
            day_results.append({
                "date": date, "next_date": next_date, "ticker": str(row["ticker"]),
                "rank": int(row["rank"]), "entry_price": float(row["close"]),
                **sim, **costs, "ambiguous": False,
            })
        if day_results:
            results_dir.mkdir(parents=True, exist_ok=True)
            (results_dir / f"{date}.json").write_text(
                json.dumps(day_results, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        results.extend(day_results)
    return results, missing


def print_report(results: list[dict], summary: dict) -> None:
    print(f"{'D-0':8} {'D+1':8} {'종목':6} {'순위':>2} {'진입':>8} {'갭%':>6} {'사유':13} "
          f"{'시각':6} {'총%':>7} {'비용후%':>7}")
    for r in results:
        print(f"{r['date']:8} {r['next_date']:8} {r['ticker']:6} {r['rank']:>2} "
              f"{r['entry_price']:8.0f} {r['gap_pct']:+6.2f} {r['exit_reason']:13} "
              f"{r['exit_time']:6} {r['gross_pct']:+7.2f} {r['net_slip_pct']:+7.2f}")
    s = summary
    print(f"\n랭크1 n={s['n']}  평균 {s['mean']}  CI [{s['ci_low']}, {s['ci_high']}]  "
          f"상위2제외 {s['mean_top2_removed']}  최대낙폭 {s['max_drawdown_pct']:+.2f}%p")
    print(f"판정 가능 n≥{MIN_N}: {s['pass_conditions']['evaluable']}  조건: {s['pass_conditions']}")
    print(f"조기 중단(n={EARLY_STOP_N}): {s['early_stop']}")
    print(f"청산 사유: {s['reasons']}   갭: {s['gap']}   랭크별 평균: {s['rank_means']}")
    print(f"결측: {s['missing']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C1 오버나이트 체 — 후보의 D+1 F4 재생")
    parser.add_argument("--root", type=Path, default=ROOT, help="data/ 가 있는 트리")
    parser.add_argument("--json", type=Path, default=None, help="집계를 JSON으로도 저장")
    args = parser.parse_args(argv)
    results, missing = run(args.root)
    summary = summarize(results, missing=missing)
    print_report(results, summary)
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "results": results},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n저장: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 통과 확인**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_overnight_sieve.py -q`
Expected: `11 passed`

- [ ] **Step 5: 빈 트리에서 CLI 동작 확인**

Run: `.\.venv\Scripts\python.exe scripts\overnight_sieve.py --root D:\Private\stock-prod`
Expected: 후보 파일이 아직 없으므로 표는 비고 `랭크1 n=0 … 판정 가능 n≥50: False … 결측: {...}`. 예외 없이 종료 코드 0.

- [ ] **Step 6: 린트·전체 테스트·커밋**

```bash
.\.venv\Scripts\python.exe -m ruff check scripts tests
.\.venv\Scripts\python.exe -m mypy scripts/overnight_sieve.py scripts/overnight_screen.py scripts/track_b_backfill.py
.\.venv\Scripts\python.exe -m pytest tests -q
git add scripts/overnight_sieve.py tests/test_overnight_sieve.py
git commit -m "feat(scripts): overnight_sieve file I/O, results, and CLI report"
```

---

### Task 9: 릴리스·운영 등록

**Files:**
- 없음 (절차만). `docs/superpowers/specs/2026-09-21-c1-overnight-close-hypothesis.md` §7에 등록 실행일 한 줄.

- [ ] **Step 1: preflight 통과 확인**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\preflight.ps1`
Expected: `사전 검사 통과`. 지문은 개발 트리 값(참고용)이고, 이 계획은 전략 파일을 건드리지 않았으므로 운영 승격 시 `지문 무변경 — 1단 승격`이어야 한다.

- [ ] **Step 2: 태그·푸시**

```bash
git tag release/$(date +%Y%m%d)-1     # 같은 날 두 번째면 -2
git push origin main --tags
```

- [ ] **Step 3: 운영 승격 (운영 트리, 포지션 CLOSED/IDLE 상태에서)**

Run: `cd D:\Private\stock-prod; powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\promote.ps1 -Tag release/<tag>`
Expected: `승격 전 지문 == 승격 후 지문`, `지문 무변경 — 1단 승격`, 재시작 완료. (스크립트만 바뀌었으나 promote.ps1은 항상 main.py를 재시작한다 — 장 마감 후에 한다.)

- [ ] **Step 4: Task Scheduler 등록** — DEV_ENV §11-3 블록을 관리자 PowerShell에서 실행하고 확인:

Run: `Get-ScheduledTask StockBot_OvernightScreen | Get-ScheduledTaskInfo`
Expected: `NextRunTime`이 다음 거래일 15:32.

- [ ] **Step 5: 첫 실행 확인 (다음 거래일 15:33 이후)**

Run: `Get-Content D:\Private\stock-prod\data\overnight\candidates\<날짜>.jsonl -Tail 1`
Expected: `{"summary": true, "date": "<날짜>", "universe": N, "candidates": K, "degraded": false}`. 실패면 `data\logs\overnight_screen_*.log`의 트레이스백.

- [ ] **Step 6: 둘째 거래일 15:46 이후 — 백필 합류와 체 첫 출력 확인**

Run:
```
Get-Content D:\Private\stock-prod\data\logs\backfill_<둘째날>_154501.log
.\.venv\Scripts\python.exe scripts\overnight_sieve.py --root D:\Private\stock-prod
```
Expected: 백필 로그의 `대상 … 쌍` 수가 후보 수만큼 늘고 `filled` > 0; 체 출력에 첫날 후보의 D+1 행이 `GAP_HARD_STOP|HARD_STOP|TRAILING|TIMEOUT` 중 하나로 찍힌다.

- [ ] **Step 7: 스펙 §7에 기록·커밋**

`docs/superpowers/specs/2026-09-21-c1-overnight-close-hypothesis.md` §7 "(등록일 기준 비어 있음)" 아래에:

```
- <실행일>: 그림자 수집 시작 (release/<tag>, StockBot_OvernightScreen 등록). 첫 후보 파일 <날짜>, 첫 체 출력 <둘째날>.
```

```bash
git add docs/superpowers/specs/2026-09-21-c1-overnight-close-hypothesis.md
git commit -m "docs(c1): record shadow collection start"
git push origin main
```

---

## 셀프 리뷰

**스펙 커버리지**
- §2.1 유니버스 (랭킹 2시장 30개, 등락률 서버 필터) → Task 3 `ranking_params`, `screen`
- §2.2 필터 5종 + 제외 → Task 1 `evaluate`, `is_excluded_name`
- §2.3 순위·상위 5·원시 필드·거부 행·요약 행 → Task 1 `rank_candidates`, Task 2 `build_row`, Task 3 `screen`/`write_candidates`
- §2.4 호출 예산 60 → Task 3 `CALL_BUDGET`, 랭킹만으로 떨어지는 종목은 일봉을 안 부름
- §3.1 세 스크립트 + 백필 수정 → Tasks 3, 4, 5, 8
- §3.2 저가 먼저, 시가 갭 하드스탑, 비용, 세 손익값 → Task 6. **틱 재생(A 종목과 겹친 날) 비교는 이 계획에 없다** — 겹친 날이 생긴 뒤 `trail_near_miss.load_ws_ticks`와 `vwap_exit_sieve.simulate_exit`로 별도 태스크. 스펙 §4.2의 부 지표라 판정과 무관.
- §3.3 스케줄링 → Task 4, Task 9
- §3.4 실패 처리 (파일 없음 / 요약 없음 / DAILY_FAILED·degraded) → Task 3, Task 8 `load_candidates`
- §4.1 판정 조건·조기 중단 → Task 7 `summarize`
- §4.2 부 지표 (갭·랭크별·사유) → Task 7. **A와 같은 날 상관·비용 민감도**는 미구현 — 표본이 있어야 의미가 있고 판정 무관. 첫 재평가(10/31) 전에 추가.
- §4.3 결측 회계 → Task 8 `missing`

**타입 일관성**: `screen`이 만드는 행의 `rank`(int|None)·`close`(float)를 Task 5 `overnight_pairs`(`isinstance(rank, int)`)와 Task 8 `run`(`float(row["close"])`)이 같은 이름으로 읽는다. `simulate_overnight`의 키(`open, gap_pct, exit_reason, exit_time, exit_price, gross_pct, bars_complete`)를 Task 8이 `**sim`으로 펼쳐 결과 형식과 일치한다. `apply_costs`의 `net_slip_pct`를 Task 7 `summarize`가 읽는다.

**확인한 항목**: `Throttle`은 `wait_seconds/mark`(Task 3에 반영). `warmup.covers_session`은 `time` 키만 보며 `WARMUP_MIN_BARS` 이상 + 개장 무렵 첫 봉 + 마감 무렵 마지막 봉 + 연속매매 구간(≤15:20) 공백 없음을 요구한다 — Task 6의 `_session` 헬퍼는 09:00부터 15:30까지 1분 봉을 빠짐없이 채우므로 통과한다.
