# H5 일봉 52주 신고가 돌파 — 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** KRX 일별매매정보를 받아 두고, 사전등록된 규칙으로 52주 신고가 돌파 가설을 봉인된 절차로 딱 한 번 판정한다.

**Architecture:** 수집기(`krx_daily_fetch.py`)는 날짜×시장 원문을 gzip 캐시에 저장만 한다. 판정기는 세 개의 순수 함수 층 — 데이터(`breakout_panel.py`: 캐시 → 종목별 열 배열, 기준가 역산 수정가), 규칙(`breakout_rules.py`: 유니버스·신호·선정·진입·청산·대조군), 판정(`breakout_gates.py`: 블록 부트스트랩·G1~G5·기록 지표) — 과, 봉인과 보고만 하는 얇은 CLI(`breakout_sieve.py`)로 나눈다. 모든 테스트는 합성 픽스처로만 돈다.

**Tech Stack:** Python 3.12, httpx(수집기, 테스트는 `httpx.MockTransport`), 표준 라이브러리만(`array`, `bisect`, `random`, `statistics`, `gzip`, `hashlib`, `subprocess`). **numpy 없음** — 프로젝트 venv에 없다. pytest.

**Spec:** `docs/superpowers/specs/2026-09-30-h5-daily-breakout-hypothesis.md` (등록 `859fe70`, 수정 `cd3351d`). 이 계획은 스펙을 구현할 뿐 바꾸지 않는다 — 스펙 §2~§4의 규칙·상수는 닫혀 있다(§7, §11).

## Global Constraints

- 수집 기간 `20100104` ~ `20260929`. 신호일은 2011년 첫 거래일부터.
- 엔드포인트: `https://data-dbg.krx.co.kr/svc/apis/sto/` + `stk_bydd_trd` · `ksq_bydd_trd` · `stk_isu_base_info` · `ksq_isu_base_info`, 인자 `basDd=YYYYMMDD`, 헤더 `AUTH_KEY`, 응답 `{"OutBlock_1": [...]}`, 값은 전부 문자열.
- 키는 개발 트리 `.env`의 `KRX_AUTH_KEY`. **키 값을 출력·로그·커밋하지 않는다.**
- 캐시 `data/krx_daily/{stk,ksq}/YYYYMMDD.json.gz`, `data/krx_daily/base/{stk,ksq}/YYYYMMDD.json.gz`. 판정 기록 `data/h5/verdict_log.jsonl`(추가만).
- 일 호출 상한 **9,000**(KRX 일 한도 10,000). 401은 즉시 멈춘다.
- 조인 키: 일별 `ISU_CD`(6자리) = 기본정보 `ISU_SRT_CD`. 기본정보 `ISU_CD`는 ISIN이라 쓰지 않는다.
- 상수(스펙 그대로): 이력 250행, 보유 20거래일, 종가 ≥ 1,000원, 거래대금 ≥ 1,000,000,000원, 가격제한폭 `20150615` 이전 15%·이후 30%, 상한가 여유 × 0.995, 하한가 여유 × 1.005, 맞춤 대조군 상위 ⌈10%⌉, 왕복 비용 0.0035, 블록 20, 부트스트랩 10,000회, 시드 20260930, CI 하한 = 정렬 인덱스 250, 상위 ⌈1%⌉ 제거, 구간 2011–2015/2016–2020/2021–2026, 최소 50건, 결측 한도 1%, 자금 곡선 20칸.
- 등록 시 범주값(스펙 §5.3): `SECUGRP_NM` ∈ {주권, 부동산투자회사, 사회간접자본투융자회사, 투자회사, 외국주권, 주식예탁증권}, `KIND_STKCERT_TP_NM` ∈ {보통주, 구형우선주, 신형우선주, 종류주권}.
- 실데이터로 신호·수익을 계산하는 코드는 `breakout_sieve.py --verdict` 밖에서 돌리지 않는다. 테스트는 합성 데이터만.
- 어떤 파일도 전략 지문 대상(`src/release.py`의 `_STRATEGY_FILES`)이 아니다. `src/`, `main.py`를 건드리지 않는다.
- 파일은 LF로 저장한다(`.gitattributes` `* text=auto eol=lf`). 파이썬으로 파일을 쓸 때 `write_text` 대신 `write_bytes`, JSONL은 `open(..., newline="")` + `print(..., file=fh)`.
- 코드와 문자열 리터럴에 백슬래시 이스케이프를 쓰지 않는다(줄바꿈이 필요하면 `print` 또는 `splitlines`). 이 저장소 도구 체인에서 이스케이프가 두 번 해석되는 사고가 있었다.
- ruff(`line-length = 100`, E·F·W·I), mypy 베이스라인에 새 오류 0건. 커밋 메시지 끝에 `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- 명령은 개발 트리(`D:\Private\stock`) Git Bash 기준: `./.venv/Scripts/python.exe -m pytest ...`.

## 파일 구조

| 파일 | 책임 | 만드는 Task |
|---|---|---|
| `scripts/backup_data.py` (수정) | `data/krx_daily/`를 일일 백업에서 뺀다 | 1 |
| `scripts/krx_daily_fetch.py` | KRX 호출·원자적 저장·예산·멱등·재시도·CLI. 성과 계산 없음 | 2, 3 |
| `scripts/breakout_panel.py` | 캐시 → `Panel`(시장 달력·결측·종목별 열 배열·기준가 역산 수정가·직전 250행 최고치·기본정보 스냅샷·범주값) | 4 |
| `scripts/breakout_rules.py` | 유니버스 판정, 신호, 한 종목 모의 매매(`simulate`), 일별 선정 루프, 두 대조군, 전 신호 이벤트 연구 | 5 |
| `scripts/breakout_gates.py` | 블록 부트스트랩, 상위 1% 제거 평균, G1~G5, 기록 지표, 20칸 자금 곡선 최대 낙폭 | 6 |
| `scripts/breakout_sieve.py` | `--verdict` 봉인(깨끗한 트리·커밋 해시·결측·범주값·매니페스트·재실행 사유), 판정 기록, 보고 | 7 |
| `tests/test_backup_data.py` (수정), `tests/test_krx_daily_fetch.py`, `tests/test_breakout_panel.py`, `tests/test_breakout_rules.py`, `tests/test_breakout_gates.py`, `tests/test_breakout_sieve.py` | 합성 픽스처 테스트 | 각 Task |

스펙 §6.1은 판정기를 `breakout_sieve.py` 하나로 적었다. 순수 함수 층을 세 파일로 나눈 것은 파일 하나가 한 책임만 지게 하려는 구현상 분할이며, 실데이터를 읽는 진입점은 여전히 `breakout_sieve.py --verdict` 하나다.

---

### Task 1: 백업에서 KRX 캐시 제외

**Files:**
- Modify: `scripts/backup_data.py` (`run()`의 복사 루프)
- Test: `tests/test_backup_data.py`

**Interfaces:**
- Consumes: 없음
- Produces: `backup_data.EXCLUDED_TOP_DIRS: tuple[str, ...] = ("krx_daily",)`

- [ ] **Step 1: 실패하는 테스트 쓰기** — `tests/test_backup_data.py` 끝에 추가

```python
def test_backup_skips_the_krx_daily_cache_but_keeps_h5_log(tmp_path):
    """KRX 캐시는 최대 약 644MB에 다시 받을 수 있고, 약관상 한 곳에서 지울 수 있어야 한다.

    판정 기록(data/h5)은 다시 만들 수 없으므로 백업한다. 스펙 §6.1.
    """
    src = _make_source(tmp_path)
    (src / "krx_daily" / "stk").mkdir(parents=True)
    (src / "krx_daily" / "stk" / "20170223.json.gz").write_bytes(b"cache")
    (src / "h5").mkdir()
    (src / "h5" / "verdict_log.jsonl").write_bytes(b"{}")

    dest = backup_data.run(tmp_path / "backups", keep=10, source=src)

    assert not (dest / "krx_daily").exists()
    assert (dest / "h5" / "verdict_log.jsonl").exists()
    assert (dest / "f1_snapshots" / "20260909_090138.jsonl").exists()
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_backup_data.py::test_backup_skips_the_krx_daily_cache_but_keeps_h5_log -q`
Expected: FAIL — `assert not (dest / "krx_daily").exists()`

- [ ] **Step 3: 구현** — `scripts/backup_data.py`

`KEEP = 10` 줄 아래에 상수를 추가한다.

```python
# 일일 백업에서 빼는 data/ 최상위 디렉터리. krx_daily 는 H5 판정용 KRX 원문 캐시다 —
# 최대 약 644MB라 10벌이면 6.4GB이고, 다시 받을 수 있으며, KRX 약관상 계약이 끝나면
# 이 한 곳만 지우면 되게 모아 둔다(H5 스펙 §5.2, §6.1).
EXCLUDED_TOP_DIRS: tuple[str, ...] = ("krx_daily",)
```

`run()`의 루프에서 `rel = src.relative_to(source)` 바로 다음 줄에 넣는다.

```python
        if rel.parts and rel.parts[0] in EXCLUDED_TOP_DIRS:
            continue
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_backup_data.py -q`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add scripts/backup_data.py tests/test_backup_data.py
git commit -m "chore(backup): leave the H5 KRX cache out of the daily data backup"
```

---

### Task 2: 수집기 — 호출 한 번과 원자적 저장

**Files:**
- Create: `scripts/krx_daily_fetch.py`
- Test: `tests/test_krx_daily_fetch.py`

**Interfaces:**
- Consumes: 없음
- Produces (Task 3·4가 쓴다):
  - `BASE_URL: str`, `ENDPOINTS: dict[str, str]` — 키 `"stk"`, `"ksq"`, `"base/stk"`, `"base/ksq"`
  - `DAILY_KINDS = ("stk", "ksq")`, `BASE_KINDS = ("base/stk", "base/ksq")`
  - `START = "20100104"`, `END = "20260929"`, `DAILY_BUDGET = 9000`, `RETRIES = 2`
  - `DEFAULT_CACHE: Path` — `ROOT / "data" / "krx_daily"`
  - `class AuthError(RuntimeError)`, `class TransientError(RuntimeError)`
  - `weekdays(start: str, end: str) -> list[str]`
  - `cache_path(cache_dir: Path, kind: str, bas_dd: str) -> Path`
  - `save_atomic(path: Path, rows: list[dict]) -> None`
  - `read_rows(path: Path) -> list[dict]`
  - `fetch_one(client: httpx.Client, kind: str, bas_dd: str, key: str, sleep=time.sleep) -> list[dict]`

- [ ] **Step 1: 실패하는 테스트 쓰기** — `tests/test_krx_daily_fetch.py` 새로 만들기

```python
"""KRX 수집기 — HTTP는 가짜(httpx.MockTransport)만 쓴다. 실제 KRX를 부르지 않는다."""

import httpx
import pytest

from scripts.krx_daily_fetch import (
    AuthError,
    TransientError,
    cache_path,
    fetch_one,
    read_rows,
    save_atomic,
    weekdays,
)

KEY = "k" * 40


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _no_sleep(_seconds: float) -> None:
    return None


def test_weekdays_skips_weekends():
    # 2019-10-04 금, 10-05 토, 10-06 일, 10-07 월
    assert weekdays("20191004", "20191007") == ["20191004", "20191007"]


def test_cache_path_layout(tmp_path):
    assert cache_path(tmp_path, "stk", "20170223") == tmp_path / "stk" / "20170223.json.gz"
    assert cache_path(tmp_path, "base/ksq", "20170201") == (
        tmp_path / "base" / "ksq" / "20170201.json.gz"
    )


def test_save_atomic_round_trips_and_leaves_no_tmp(tmp_path):
    path = cache_path(tmp_path, "stk", "20170223")
    save_atomic(path, [{"ISU_CD": "117930", "ISU_NM": "한진해운"}])
    assert read_rows(path) == [{"ISU_CD": "117930", "ISU_NM": "한진해운"}]
    assert [p.name for p in path.parent.iterdir()] == ["20170223.json.gz"]


def test_fetch_one_sends_date_and_key_and_returns_rows():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["basDd"] = request.url.params.get("basDd")
        seen["key"] = request.headers.get("AUTH_KEY")
        return httpx.Response(200, json={"OutBlock_1": [{"ISU_CD": "117930"}]})

    rows = fetch_one(_client(handler), "stk", "20170223", KEY, sleep=_no_sleep)

    assert rows == [{"ISU_CD": "117930"}]
    assert seen == {"path": "/svc/apis/sto/stk_bydd_trd", "basDd": "20170223", "key": KEY}


def test_fetch_one_returns_empty_list_on_a_holiday():
    def handler(request):
        return httpx.Response(200, json={"OutBlock_1": []})

    assert fetch_one(_client(handler), "ksq", "20191003", KEY, sleep=_no_sleep) == []


@pytest.mark.parametrize("message", ["Unauthorized Key", "Unauthorized API Call"])
def test_fetch_one_stops_immediately_on_401(message):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401, json={"respMsg": message, "respCode": "401"})

    with pytest.raises(AuthError, match=message):
        fetch_one(_client(handler), "stk", "20170223", KEY, sleep=_no_sleep)
    assert len(calls) == 1


def test_fetch_one_retries_5xx_twice_then_raises():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, text="busy")

    with pytest.raises(TransientError, match="503"):
        fetch_one(_client(handler), "stk", "20170223", KEY, sleep=_no_sleep)
    assert len(calls) == 3


def test_fetch_one_recovers_after_one_5xx():
    responses = [httpx.Response(502, text="bad"), httpx.Response(200, json={"OutBlock_1": []})]

    def handler(request):
        return responses.pop(0)

    assert fetch_one(_client(handler), "stk", "20170223", KEY, sleep=_no_sleep) == []
    assert responses == []


def test_fetch_one_treats_a_body_without_outblock_as_transient():
    def handler(request):
        return httpx.Response(200, json={"respMsg": "?"})

    with pytest.raises(TransientError, match="OutBlock_1"):
        fetch_one(_client(handler), "stk", "20170223", KEY, sleep=_no_sleep)
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_krx_daily_fetch.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.krx_daily_fetch'`

- [ ] **Step 3: 구현** — `scripts/krx_daily_fetch.py` 새로 만들기

```python
"""KRX OPEN API 일별매매정보·종목기본정보 수집기 — 저장만 한다.

H5 스펙 §6.2 (docs/superpowers/specs/2026-09-30-h5-daily-breakout-hypothesis.md).
성과(신고가·수익률)는 계산하지 않는다. 호출 수와 행 수만 출력한다.
응답 원문을 날짜×시장마다 gzip JSON 으로 원자적으로 저장하고, 이미 있는 파일은
다시 부르지 않는다. 휴장일의 빈 목록도 그대로 저장해 다시 부르지 않는다.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE_URL = "https://data-dbg.krx.co.kr/svc/apis/sto"
ENDPOINTS = {
    "stk": "stk_bydd_trd",
    "ksq": "ksq_bydd_trd",
    "base/stk": "stk_isu_base_info",
    "base/ksq": "ksq_isu_base_info",
}
DAILY_KINDS = ("stk", "ksq")
BASE_KINDS = ("base/stk", "base/ksq")
START = "20100104"
END = "20260929"
DAILY_BUDGET = 9000
RETRIES = 2
DEFAULT_CACHE = ROOT / "data" / "krx_daily"


class AuthError(RuntimeError):
    """401. 'Unauthorized Key'(키 틀림)든 'Unauthorized API Call'(서비스 미승인)든 즉시 멈춘다."""


class TransientError(RuntimeError):
    """5xx·타임아웃·전송 오류·본문 이상. 재시도한 뒤에도 남으면 그 날짜를 건너뛴다."""


def weekdays(start: str, end: str) -> list[str]:
    day = datetime.strptime(start, "%Y%m%d").date()
    last = datetime.strptime(end, "%Y%m%d").date()
    out: list[str] = []
    while day <= last:
        if day.weekday() < 5:
            out.append(day.strftime("%Y%m%d"))
        day += timedelta(days=1)
    return out


def cache_path(cache_dir: Path, kind: str, bas_dd: str) -> Path:
    return cache_dir / kind / f"{bas_dd}.json.gz"


def save_atomic(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False)
    os.replace(tmp, path)


def read_rows(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        rows = json.load(fh)
    return rows if isinstance(rows, list) else []


def fetch_one(
    client: httpx.Client,
    kind: str,
    bas_dd: str,
    key: str,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict]:
    url = f"{BASE_URL}/{ENDPOINTS[kind]}"
    last_error = ""
    for attempt in range(RETRIES + 1):
        if attempt:
            sleep(2.0 * attempt)
        try:
            resp = client.get(url, params={"basDd": bas_dd}, headers={"AUTH_KEY": key})
        except httpx.TransportError as exc:
            last_error = f"전송 오류 {type(exc).__name__}"
            continue
        if resp.status_code == 401:
            try:
                message = str(resp.json().get("respMsg") or "")
            except ValueError:
                message = ""
            raise AuthError(message or "401")
        if resp.status_code >= 500:
            last_error = f"HTTP {resp.status_code}"
            continue
        if resp.status_code != 200:
            raise TransientError(f"HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            last_error = "JSON 아님"
            continue
        rows = body.get("OutBlock_1") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            last_error = "OutBlock_1 없음"
            continue
        return rows
    raise TransientError(last_error or "알 수 없는 실패")
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_krx_daily_fetch.py -q && ./.venv/Scripts/python.exe -m ruff check scripts/krx_daily_fetch.py tests/test_krx_daily_fetch.py`
Expected: 전부 PASS, `All checks passed!`

- [ ] **Step 5: 커밋**

```bash
git add scripts/krx_daily_fetch.py tests/test_krx_daily_fetch.py
git commit -m "feat(h5): fetch and atomically store one KRX daily response"
```

---

### Task 3: 수집기 — 실행 루프와 CLI

**Files:**
- Modify: `scripts/krx_daily_fetch.py` (Task 2 파일에 추가)
- Test: `tests/test_krx_daily_fetch.py` (추가)

**Interfaces:**
- Consumes: Task 2의 `fetch_one`, `save_atomic`, `read_rows`, `cache_path`, `weekdays`, 상수들
- Produces:
  - `@dataclass class RunStats: calls: int; saved: int; skipped_existing: int; failed: int; rows: int; stopped: str`
  - `month_first_trading_days(cache_dir: Path, days: list[str]) -> list[str]`
  - `record_failure(cache_dir: Path, kind: str, bas_dd: str, error: str) -> None` — `cache_dir/failures.jsonl`에 한 줄 추가
  - `run(cache_dir: Path, key: str, *, client: httpx.Client, start: str = START, end: str = END, budget: int = DAILY_BUDGET, sleep=time.sleep) -> RunStats`
  - `load_key() -> str`
  - `main(argv: list[str] | None = None) -> int` — 인증 실패·키 없음이면 2, 그 밖엔 0

순서 규칙: 평일마다 두 시장 일별매매정보를 먼저 다 받고, 그다음 **월 첫 거래일**의 두 시장 기본정보를 받는다. 월 첫 거래일 = 그 달에서 두 시장 중 한쪽이라도 행이 있는 첫 평일이며, 그 앞에 아직 파일이 없는 날이 있으면 그 달은 다음 실행으로 미룬다(실패로 빠진 날 때문에 둘째 거래일을 첫날로 잘못 잡지 않게). 예산은 이번 실행에서 보낸 **항목 수**로 센다. 재시도(`RETRIES = 2`)도 KRX 일 한도를 쓰지만 9,000과 10,000 사이의 여유 1,000이 이를 덮는다.

- [ ] **Step 1: 실패하는 테스트 쓰기** — `tests/test_krx_daily_fetch.py`

맨 위 import 블록을 고친다: `import json`을 추가하고, `from scripts.krx_daily_fetch import (...)` 목록에 `load_key`, `main`, `month_first_trading_days`, `run`을 알파벳 순서로 더한다. 그다음 파일 끝에 추가한다.

```python
def _router(answers: dict):
    """(엔드포인트, basDd) → 행 목록 또는 HTTP 상태 코드. 없으면 빈 목록(휴장)."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        bas_dd = request.url.params.get("basDd")
        calls.append((endpoint, bas_dd))
        answer = answers.get((endpoint, bas_dd), [])
        if answer == 401:
            return httpx.Response(401, json={"respMsg": "Unauthorized API Call", "respCode": "401"})
        if isinstance(answer, int):
            return httpx.Response(answer, text="err")
        return httpx.Response(200, json={"OutBlock_1": answer})

    return handler, calls


ROW = [{"ISU_CD": "005930"}]


def test_run_saves_both_markets_every_weekday_then_the_monthly_base(tmp_path):
    handler, calls = _router({("stk_bydd_trd", "20170201"): ROW})
    stats = run(tmp_path, KEY, client=_client(handler), start="20170201", end="20170202",
                sleep=_no_sleep)

    assert calls == [
        ("stk_bydd_trd", "20170201"), ("ksq_bydd_trd", "20170201"),
        ("stk_bydd_trd", "20170202"), ("ksq_bydd_trd", "20170202"),
        ("stk_isu_base_info", "20170201"), ("ksq_isu_base_info", "20170201"),
    ]
    assert (stats.calls, stats.saved, stats.failed, stats.stopped) == (6, 6, 0, "")
    assert read_rows(cache_path(tmp_path, "stk", "20170201")) == ROW
    assert cache_path(tmp_path, "base/ksq", "20170201").exists()


def test_run_is_idempotent(tmp_path):
    save_atomic(cache_path(tmp_path, "stk", "20170201"), ROW)
    handler, calls = _router({})
    stats = run(tmp_path, KEY, client=_client(handler), start="20170201", end="20170201",
                sleep=_no_sleep)

    assert ("stk_bydd_trd", "20170201") not in calls
    assert stats.skipped_existing == 1


def test_run_stops_at_the_budget(tmp_path):
    handler, calls = _router({})
    stats = run(tmp_path, KEY, client=_client(handler), start="20170201", end="20170202",
                budget=3, sleep=_no_sleep)

    assert (stats.calls, stats.stopped) == (3, "BUDGET")
    assert len(calls) == 3
    assert not cache_path(tmp_path, "ksq", "20170202").exists()


def test_run_stops_immediately_on_an_auth_error(tmp_path):
    handler, calls = _router({("stk_bydd_trd", "20170201"): 401})
    stats = run(tmp_path, KEY, client=_client(handler), start="20170201", end="20170202",
                sleep=_no_sleep)

    assert len(calls) == 1
    assert stats.stopped.startswith("AUTH")
    assert "Unauthorized API Call" in stats.stopped
    assert not any(tmp_path.rglob("*.json.gz"))


def test_run_records_a_transient_failure_continues_and_defers_that_month(tmp_path):
    handler, calls = _router({("stk_bydd_trd", "20170201"): 503,
                              ("stk_bydd_trd", "20170202"): ROW})
    stats = run(tmp_path, KEY, client=_client(handler), start="20170201", end="20170202",
                sleep=_no_sleep)

    assert stats.failed == 1
    assert not cache_path(tmp_path, "stk", "20170201").exists()
    assert cache_path(tmp_path, "stk", "20170202").exists()
    lines = (tmp_path / "failures.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["basDd"] == "20170201"
    # 2월 1일 파일이 없으니 2월의 첫 거래일을 아직 정할 수 없다 → 기본정보를 받지 않는다
    assert not any(endpoint.endswith("isu_base_info") for endpoint, _ in calls)


def test_month_first_trading_day_skips_an_all_empty_day(tmp_path):
    for kind in ("stk", "ksq"):
        save_atomic(cache_path(tmp_path, kind, "20191003"), [])      # 개천절
    save_atomic(cache_path(tmp_path, "stk", "20191004"), ROW)
    save_atomic(cache_path(tmp_path, "ksq", "20191004"), [])

    assert month_first_trading_days(tmp_path, ["20191003", "20191004"]) == ["20191004"]


def test_load_key_prefers_the_environment(monkeypatch):
    monkeypatch.setenv("KRX_AUTH_KEY", "e" * 40)
    assert load_key() == "e" * 40


def test_main_refuses_without_a_key(monkeypatch, tmp_path):
    # conftest 가 STOCK_SKIP_DOTENV=1 로 .env 를 막아 둔다
    monkeypatch.delenv("KRX_AUTH_KEY", raising=False)
    assert main(["--cache-dir", str(tmp_path)]) == 2
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_krx_daily_fetch.py -q`
Expected: FAIL — `ImportError: cannot import name 'load_key'`

- [ ] **Step 3: 구현** — `scripts/krx_daily_fetch.py`

import 블록에 추가한다: `import argparse`, `from dataclasses import dataclass`.

파일 끝에 추가한다.

```python
@dataclass
class RunStats:
    calls: int = 0
    saved: int = 0
    skipped_existing: int = 0
    failed: int = 0
    rows: int = 0
    stopped: str = ""


def month_first_trading_days(cache_dir: Path, days: list[str]) -> list[str]:
    """월마다 두 시장 중 한쪽이라도 행이 있는 첫 평일.

    그 달 앞쪽에 아직 파일이 없는 날이 있으면 그 달은 보류한다 — 실패로 빠진 날을
    건너뛰어 둘째 거래일을 첫 거래일로 잘못 잡지 않기 위해서다.
    """
    by_month: dict[str, list[str]] = {}
    for bas_dd in days:
        by_month.setdefault(bas_dd[:6], []).append(bas_dd)
    firsts: list[str] = []
    for month in sorted(by_month):
        for bas_dd in by_month[month]:
            paths = [cache_path(cache_dir, kind, bas_dd) for kind in DAILY_KINDS]
            if not all(p.exists() for p in paths):
                break
            if any(read_rows(p) for p in paths):
                firsts.append(bas_dd)
                break
    return firsts


def record_failure(cache_dir: Path, kind: str, bas_dd: str, error: str) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "basDd": bas_dd,
        "error": error,
    }
    with open(cache_dir / "failures.jsonl", "a", encoding="utf-8", newline="") as fh:
        print(json.dumps(entry, ensure_ascii=False), file=fh)


def run(
    cache_dir: Path,
    key: str,
    *,
    client: httpx.Client,
    start: str = START,
    end: str = END,
    budget: int = DAILY_BUDGET,
    sleep: Callable[[float], None] = time.sleep,
) -> RunStats:
    stats = RunStats()
    days = weekdays(start, end)

    def process(kind: str, bas_dd: str) -> bool:
        """False 면 이번 실행을 멈춘다."""
        path = cache_path(cache_dir, kind, bas_dd)
        if path.exists():
            stats.skipped_existing += 1
            return True
        if stats.calls >= budget:
            stats.stopped = "BUDGET"
            return False
        stats.calls += 1
        try:
            rows = fetch_one(client, kind, bas_dd, key, sleep=sleep)
        except AuthError as exc:
            stats.stopped = f"AUTH: {exc}"
            return False
        except TransientError as exc:
            stats.failed += 1
            record_failure(cache_dir, kind, bas_dd, str(exc))
            return True
        save_atomic(path, rows)
        stats.saved += 1
        stats.rows += len(rows)
        return True

    for bas_dd in days:
        for kind in DAILY_KINDS:
            if not process(kind, bas_dd):
                return stats
    for bas_dd in month_first_trading_days(cache_dir, days):
        for kind in BASE_KINDS:
            if not process(kind, bas_dd):
                return stats
    return stats


def load_key() -> str:
    value = os.getenv("KRX_AUTH_KEY", "").strip()
    if value:
        return value
    if os.getenv("STOCK_SKIP_DOTENV", "0") == "1":
        return ""
    from dotenv import dotenv_values

    return str(dotenv_values(ROOT / ".env").get("KRX_AUTH_KEY") or "").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KRX 일별매매정보·종목기본정보 수집 (H5, 저장만)")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--budget", type=int, default=DAILY_BUDGET, help="이번 실행의 최대 호출 수")
    args = parser.parse_args(argv)

    key = load_key()
    if not key:
        print("KRX_AUTH_KEY 가 비어 있습니다 (.env).", file=sys.stderr)
        return 2
    with httpx.Client(timeout=60) as client:
        stats = run(args.cache_dir, key, client=client, budget=args.budget)
    print(
        f"호출 {stats.calls} · 저장 {stats.saved} · 기존 {stats.skipped_existing} · "
        f"실패 {stats.failed} · 행 {stats.rows}"
    )
    if stats.stopped:
        print(f"멈춤: {stats.stopped}")
    return 2 if stats.stopped.startswith("AUTH") else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_krx_daily_fetch.py -q && ./.venv/Scripts/python.exe -m ruff check scripts/krx_daily_fetch.py tests/test_krx_daily_fetch.py`
Expected: 전부 PASS, `All checks passed!`

- [ ] **Step 5: 커밋**

```bash
git add scripts/krx_daily_fetch.py tests/test_krx_daily_fetch.py
git commit -m "feat(h5): collect KRX daily and monthly base snapshots within a daily budget"
```

---

### Task 4: 판정기 데이터 층 — `breakout_panel.py`

**Files:**
- Create: `scripts/breakout_panel.py`
- Test: `tests/test_breakout_panel.py`

**Interfaces:**
- Consumes: Task 2의 `BASE_KINDS`, `DAILY_KINDS`, `START`, `END`, `cache_path`, `read_rows`, `weekdays`
- Produces (Task 5·6·7이 쓴다):
  - `HISTORY_ROWS = 250`, `NO_CHANGE = -(2**62)`
  - `KNOWN_SECUGRP: frozenset[str]`, `KNOWN_KIND: frozenset[str]`
  - `BaseInfo = tuple[str, str, str, str]` — (증권그룹, 주식종류, 소속부, 약칭)
  - `to_int(text: object) -> int | None`
  - `prior_max(values: list[float], window: int) -> array` — `array("d")`, 이력 부족은 `nan`
  - `@dataclass class Series` — 필드 `ticker, market, dates, names, open, high, low, close, change, value, mult, prior_max, events, no_basis`; 메서드 `pos(d) -> int`(없으면 -1), `basis(i) -> int | None`, `adj_open(i) -> float`, `adj_close(i) -> float`
  - `@dataclass class Panel` — 필드 `calendar, series, by_date, missing, unknown_days, last_day, base_dates, base, categories, weekday_count, cal_index`; 메서드 `base_info(ticker, d) -> BaseInfo | None`, `missing_ratio() -> float`
  - `build_panel(days: list[str], get_daily: Callable[[str, str], list[dict] | None], base: Iterable[tuple[str, str, list[dict]]]) -> Panel`
  - `load_panel(cache_dir: Path, start: str = START, end: str = END) -> Panel`

설계 메모:
- 메모리: 약 900만 행을 행 객체로 들면 수 GB다. 종목마다 `array("q")` 열로 담고, 날짜 문자열·종목명은 `sys.intern`으로 공유한다. `build_panel`은 날짜 하나씩 `get_daily`로 받아 소화하므로 원문 전체를 한꺼번에 들지 않는다.
- 시장 달력(스펙 §2.7): 두 시장 모두 빈 목록 → 휴장(달력에서 뺌). 한쪽이라도 행이 있으면 거래일. 행이 있는 날 다른 시장이 빈 목록이거나 파일이 없으면 결측(달력에 넣음). 두 시장 모두 파일이 없거나 한쪽 없음·한쪽 빈 목록이면 거래일인지 알 수 없어 달력에서 빼고 `unknown_days`와 `missing`에 넣는다(스펙 §6.6은 한 시장만 빈 날을 다룬다 — 둘 다 모르는 날은 이 계획의 해석이며 결측 한도 1% 안에서만 생긴다).
- `last_day[시장]` = 그 시장에 행이 있던 마지막 달력일. Task 5가 "소멸"을 가를 때 쓴다(결측일에 행이 빠진 것을 소멸로 오판하지 않게).
- 수정가(스펙 §3.1): `f_t = 기준가_t / 종가_(t−1)`, `mult[s] = s 이후 모든 f 의 곱`. 전일대비가 비면 `f = 1`, `no_basis += 1`.

- [ ] **Step 1: 실패하는 테스트 쓰기** — `tests/test_breakout_panel.py` 새로 만들기

```python
"""H5 데이터 층 — 합성 행만 쓴다."""

import math

from scripts.breakout_panel import (
    KNOWN_KIND,
    KNOWN_SECUGRP,
    build_panel,
    load_panel,
    prior_max,
)
from scripts.krx_daily_fetch import cache_path, save_atomic


def rec(ticker, d, *, o, hi, lo, c, chg, val=2_000_000_000, name="종목"):
    return {
        "BAS_DD": d, "ISU_CD": ticker, "ISU_NM": name,
        "TDD_OPNPRC": str(o), "TDD_HGPRC": str(hi), "TDD_LWPRC": str(lo),
        "TDD_CLSPRC": str(c), "CMPPREVDD_PRC": str(chg), "ACC_TRDVAL": str(val),
    }


def base_rec(code, secugrp="주권", kind="보통주", sect="", abbrv="종목"):
    return {"ISU_CD": "KR7" + code + "000", "ISU_SRT_CD": code, "SECUGRP_NM": secugrp,
            "KIND_STKCERT_TP_NM": kind, "SECT_TP_NM": sect, "ISU_ABBRV": abbrv}


def panel_of(table, days=None, base=()):
    days = days or sorted({d for (_m, d) in table})
    return build_panel(days, lambda m, d: table.get((m, d)), list(base))


def test_prior_max_uses_only_the_previous_window():
    out = prior_max([1.0, 3.0, 2.0, 5.0, 4.0], 2)
    assert math.isnan(out[0]) and math.isnan(out[1])
    assert list(out)[2:] == [3.0, 3.0, 5.0]


def test_samsung_split_shape_gives_factor_one_fiftieth():
    table = {
        ("stk", "20180503"): [rec("005930", "20180503", o=0, hi=0, lo=0, c=2650000, chg=0)],
        ("stk", "20180504"): [rec("005930", "20180504", o=53000, hi=53900, lo=51300,
                                  c=51900, chg=-1100)],
        ("ksq", "20180503"): [rec("000250", "20180503", o=1, hi=1, lo=1, c=1, chg=0)],
        ("ksq", "20180504"): [rec("000250", "20180504", o=1, hi=1, lo=1, c=1, chg=0)],
    }
    s = panel_of(table).series["005930"]
    assert s.events == [("20180504", 0.02)]
    assert s.basis(1) == 53000
    assert math.isclose(s.adj_close(0), 53000.0)
    assert s.adj_close(1) == 51900.0


def test_liquidation_crash_is_a_loss_not_an_event():
    table = {
        ("stk", "20170222"): [rec("117930", "20170222", o=0, hi=0, lo=0, c=780, chg=0)],
        ("stk", "20170223"): [rec("117930", "20170223", o=420, hi=420, lo=300, c=310, chg=-470)],
        ("ksq", "20170222"): [rec("000250", "20170222", o=1, hi=1, lo=1, c=1, chg=0)],
        ("ksq", "20170223"): [rec("000250", "20170223", o=1, hi=1, lo=1, c=1, chg=0)],
    }
    s = panel_of(table).series["117930"]
    assert s.events == []
    assert list(s.mult) == [1.0, 1.0]


def _rising(ticker, days, *, merge_at=None):
    """종가가 날마다 1원씩 오르는 합성 종목. merge_at 행에서 10:1 병합(가격 10배)."""
    rows = []
    for i, d in enumerate(days):
        close = 1000 + i
        chg = 1
        if merge_at is not None and i >= merge_at:
            close = (1000 + i) * 10
            chg = 10 if i > merge_at else close - (1000 + i - 1) * 10
        rows.append(rec(ticker, d, o=close, hi=close, lo=close, c=close, chg=chg))
    return rows


def test_future_event_factors_do_not_change_earlier_signals():
    days = [f"D{i:04d}" for i in range(300)]
    plain = _rising("111110", days)
    merged = _rising("111110", days, merge_at=280)
    a = panel_of({("stk", d): [r] for d, r in zip(days, plain)} |
                 {("ksq", d): [] for d in days}).series["111110"]
    b = panel_of({("stk", d): [r] for d, r in zip(days, merged)} |
                 {("ksq", d): [] for d in days}).series["111110"]

    assert b.events and b.events[0][0] == "D0280"
    before = range(250, 280)
    assert [a.adj_close(i) > a.prior_max[i] for i in before] == [
        b.adj_close(i) > b.prior_max[i] for i in before
    ]
    assert b.adj_close(280) > b.prior_max[280]


def test_calendar_holiday_missing_and_unknown_days():
    row = [rec("005930", "x", o=1, hi=1, lo=1, c=1, chg=0)]
    table = {
        ("stk", "D1"): row, ("ksq", "D1"): row,        # 거래일
        ("stk", "D2"): [], ("ksq", "D2"): [],          # 휴장
        ("stk", "D3"): row,                            # 코스닥 파일 없음 → 결측
        # D4: 두 시장 모두 파일 없음 → 거래일인지 모름
        ("stk", "D5"): row, ("ksq", "D5"): [],         # 코스닥만 빈 목록 → 결측
    }
    p = panel_of(table, days=["D1", "D2", "D3", "D4", "D5"])

    assert p.calendar == ["D1", "D3", "D5"]
    assert p.missing == {"D3": frozenset({"ksq"}), "D4": frozenset({"stk", "ksq"}),
                         "D5": frozenset({"ksq"})}
    assert p.unknown_days == ["D4"]
    assert math.isclose(p.missing_ratio(), 3 / 5)
    assert p.last_day == {"stk": "D5", "ksq": "D1"}
    assert p.cal_index["D3"] == 1


def test_base_snapshot_is_the_latest_on_or_before_the_day():
    table = {("stk", "20170215"): [rec("005930", "20170215", o=1, hi=1, lo=1, c=1, chg=0)],
             ("ksq", "20170215"): []}
    base = [("stk", "20170201", [base_rec("005930")]),
            ("stk", "20170301", [base_rec("005930", sect="X"), base_rec("000660")])]
    p = panel_of(table, base=base)

    assert p.base_info("005930", "20170215") == ("주권", "보통주", "", "종목")
    assert p.base_info("005930", "20170131") is None
    assert p.base_info("000660", "20170215") is None
    assert p.base_info("005930", "20170302") == ("주권", "보통주", "X", "종목")
    assert p.categories["SECUGRP_NM"] == {"주권"}


def test_blank_change_counts_as_no_basis_and_factor_one():
    r0 = rec("005930", "D1", o=100, hi=100, lo=100, c=100, chg=0)
    r1 = rec("005930", "D2", o=200, hi=200, lo=200, c=200, chg=0)
    r1["CMPPREVDD_PRC"] = ""
    p = panel_of({("stk", "D1"): [r0], ("stk", "D2"): [r1], ("ksq", "D1"): [], ("ksq", "D2"): []})
    s = p.series["005930"]
    assert s.no_basis == 1 and s.events == [] and s.basis(1) is None


def test_pos_finds_dates_and_misses():
    table = {("stk", "D1"): [rec("005930", "D1", o=1, hi=1, lo=1, c=1, chg=0)], ("ksq", "D1"): []}
    s = panel_of(table).series["005930"]
    assert s.pos("D1") == 0 and s.pos("D0") == -1 and s.pos("D9") == -1


def test_load_panel_reads_the_cache_layout(tmp_path):
    save_atomic(cache_path(tmp_path, "stk", "20170201"),
                [rec("005930", "20170201", o=1, hi=1, lo=1, c=1, chg=0)])
    save_atomic(cache_path(tmp_path, "ksq", "20170201"), [])
    save_atomic(cache_path(tmp_path, "base/stk", "20170201"), [base_rec("005930")])

    p = load_panel(tmp_path, start="20170201", end="20170201")

    assert p.calendar == ["20170201"]
    assert p.base_info("005930", "20170201") == ("주권", "보통주", "", "종목")


def test_known_categories_match_the_spec():
    assert KNOWN_SECUGRP == {"주권", "부동산투자회사", "사회간접자본투융자회사", "투자회사",
                             "외국주권", "주식예탁증권"}
    assert KNOWN_KIND == {"보통주", "구형우선주", "신형우선주", "종류주권"}
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_breakout_panel.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.breakout_panel'`

- [ ] **Step 3: 구현** — `scripts/breakout_panel.py` 새로 만들기

```python
"""H5 판정기의 데이터 층 — 캐시를 시장 달력과 종목별 열 배열로 올린다.

스펙 §2.7(시장 달력·결측), §3.1(기준가 역산·수정가), §5(데이터 규약).
약 900만 행을 행 객체로 들면 수 GB라, 종목마다 array 열로 담고 날짜·종목명 문자열은
intern 해서 공유한다. 원문은 날짜 하나씩 받아 소화한다. 성과는 여기서 계산하지 않는다.
"""

from __future__ import annotations

import bisect
import math
import sys
from array import array
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from scripts.krx_daily_fetch import (
    BASE_KINDS,
    DAILY_KINDS,
    END,
    START,
    cache_path,
    read_rows,
    weekdays,
)

HISTORY_ROWS = 250
NO_CHANGE = -(2**62)  # 전일대비가 빈 행의 표지

# 스펙 §5.3 "등록 시 확인된 범주값". 봉인(Task 7)이 이 밖의 값을 보면 멈춘다.
KNOWN_SECUGRP = frozenset(
    {"주권", "부동산투자회사", "사회간접자본투융자회사", "투자회사", "외국주권", "주식예탁증권"}
)
KNOWN_KIND = frozenset({"보통주", "구형우선주", "신형우선주", "종류주권"})

BaseInfo = tuple[str, str, str, str]  # (증권그룹, 주식종류, 소속부, 약칭)


def to_int(text: object) -> int | None:
    s = str(text if text is not None else "").replace(",", "").strip()
    if s in ("", "-"):
        return None
    return int(float(s))


def prior_max(values: list[float], window: int) -> array:
    """values[i] 직전 window 개의 최댓값. 이력이 window 개 미만이면 nan. O(n) 단조 덱."""
    out = array("d", [math.nan]) * len(values)
    dq: deque[int] = deque()
    for i, v in enumerate(values):
        while dq and dq[0] < i - window:
            dq.popleft()
        if i >= window:
            out[i] = values[dq[0]]
        while dq and values[dq[-1]] <= v:
            dq.pop()
        dq.append(i)
    return out


@dataclass
class Series:
    ticker: str
    market: str
    dates: list[str]
    names: list[str]
    open: array
    high: array
    low: array
    close: array
    change: array
    value: array
    mult: array = field(default_factory=lambda: array("d"))
    prior_max: array = field(default_factory=lambda: array("d"))
    events: list[tuple[str, float]] = field(default_factory=list)
    no_basis: int = 0

    def pos(self, d: str) -> int:
        i = bisect.bisect_left(self.dates, d)
        return i if i < len(self.dates) and self.dates[i] == d else -1

    def basis(self, i: int) -> int | None:
        """기준가 = 종가 − 전일대비 (스펙 §3.1). 전일대비가 비면 None."""
        change = self.change[i]
        return None if change == NO_CHANGE else self.close[i] - change

    def adj_open(self, i: int) -> float:
        return self.open[i] * self.mult[i]

    def adj_close(self, i: int) -> float:
        return self.close[i] * self.mult[i]


class _Acc:
    __slots__ = ("dates", "names", "open", "high", "low", "close", "change", "value", "market")

    def __init__(self) -> None:
        self.dates: list[str] = []
        self.names: list[str] = []
        self.open = array("q")
        self.high = array("q")
        self.low = array("q")
        self.close = array("q")
        self.change = array("q")
        self.value = array("q")
        self.market = ""

    def add(self, rec: dict, d: str, market: str) -> None:
        if self.dates and self.dates[-1] == d:
            return  # 같은 날 두 시장에 겹쳐 나오면 먼저 본 것만 쓴다
        self.dates.append(d)
        self.names.append(sys.intern(str(rec.get("ISU_NM") or "")))
        self.open.append(to_int(rec.get("TDD_OPNPRC")) or 0)
        self.high.append(to_int(rec.get("TDD_HGPRC")) or 0)
        self.low.append(to_int(rec.get("TDD_LWPRC")) or 0)
        self.close.append(to_int(rec.get("TDD_CLSPRC")) or 0)
        change = to_int(rec.get("CMPPREVDD_PRC"))
        self.change.append(NO_CHANGE if change is None else change)
        self.value.append(to_int(rec.get("ACC_TRDVAL")) or 0)
        self.market = market


def build_series(ticker: str, acc: _Acc) -> Series:
    s = Series(ticker, acc.market, acc.dates, acc.names, acc.open, acc.high, acc.low,
               acc.close, acc.change, acc.value)
    n = len(s.dates)
    factors = [1.0] * n
    for i in range(1, n):
        basis = s.basis(i)
        if basis is None:
            s.no_basis += 1
            continue
        prev_close = s.close[i - 1]
        if prev_close > 0 and basis != prev_close:
            factors[i] = basis / prev_close
            s.events.append((s.dates[i], factors[i]))
    mult = array("d", [1.0]) * n
    running = 1.0
    for i in range(n - 1, -1, -1):
        mult[i] = running
        running *= factors[i]
    s.mult = mult
    s.prior_max = prior_max([s.close[i] * mult[i] for i in range(n)], HISTORY_ROWS)
    return s


@dataclass
class Panel:
    calendar: list[str]
    series: dict[str, Series]
    by_date: dict[str, list[str]]
    missing: dict[str, frozenset[str]]
    unknown_days: list[str]
    last_day: dict[str, str]
    base_dates: list[str]
    base: dict[str, dict[str, BaseInfo]]
    categories: dict[str, set[str]]
    weekday_count: int
    cal_index: dict[str, int] = field(init=False)

    def __post_init__(self) -> None:
        self.cal_index = {d: i for i, d in enumerate(self.calendar)}

    def base_info(self, ticker: str, d: str) -> BaseInfo | None:
        i = bisect.bisect_right(self.base_dates, d) - 1
        if i < 0:
            return None
        return self.base[self.base_dates[i]].get(ticker)

    def missing_ratio(self) -> float:
        return len(self.missing) / self.weekday_count if self.weekday_count else 1.0


def _base_snapshots(
    base: Iterable[tuple[str, str, list[dict]]],
) -> tuple[dict[str, dict[str, BaseInfo]], dict[str, set[str]]]:
    snapshots: dict[str, dict[str, BaseInfo]] = {}
    categories: dict[str, set[str]] = {
        "SECUGRP_NM": set(), "KIND_STKCERT_TP_NM": set(), "SECT_TP_NM": set(),
    }
    for _market, d, rows in base:
        snap = snapshots.setdefault(d, {})
        for rec in rows:
            code = str(rec.get("ISU_SRT_CD") or "")
            if not code:
                continue
            info: BaseInfo = (
                sys.intern(str(rec.get("SECUGRP_NM") or "")),
                sys.intern(str(rec.get("KIND_STKCERT_TP_NM") or "")),
                sys.intern(str(rec.get("SECT_TP_NM") or "")),
                sys.intern(str(rec.get("ISU_ABBRV") or "")),
            )
            snap[code] = info
            categories["SECUGRP_NM"].add(info[0])
            categories["KIND_STKCERT_TP_NM"].add(info[1])
            categories["SECT_TP_NM"].add(info[2])
    return snapshots, categories


def build_panel(
    days: list[str],
    get_daily: Callable[[str, str], list[dict] | None],
    base: Iterable[tuple[str, str, list[dict]]],
) -> Panel:
    """days: 평일 목록. get_daily(시장, 날짜) = 행 목록, 파일이 없으면 None.
    base: (시장, 스냅샷 날짜, 기본정보 행 목록)."""
    accs: dict[str, _Acc] = {}
    calendar: list[str] = []
    by_date: dict[str, list[str]] = {}
    missing: dict[str, frozenset[str]] = {}
    unknown: list[str] = []
    last_day: dict[str, str] = {}
    for raw_day in days:
        d = sys.intern(raw_day)
        got = {m: get_daily(m, d) for m in DAILY_KINDS}
        filled = [m for m in DAILY_KINDS if got[m]]
        if not filled:
            if all(got[m] is not None for m in DAILY_KINDS):
                continue  # 두 시장 모두 빈 목록 — 휴장
            missing[d] = frozenset(m for m in DAILY_KINDS if got[m] is None)
            unknown.append(d)  # 거래일인지 알 수 없다
            continue
        gaps = frozenset(m for m in DAILY_KINDS if not got[m])
        if gaps:
            missing[d] = gaps
        calendar.append(d)
        tickers: list[str] = []
        for m in filled:
            last_day[m] = d
            for rec in got[m] or []:
                ticker = sys.intern(str(rec.get("ISU_CD") or ""))
                if not ticker:
                    continue
                accs.setdefault(ticker, _Acc()).add(rec, d, m)
                tickers.append(ticker)
        by_date[d] = tickers
    series = {t: build_series(t, acc) for t, acc in accs.items()}
    snapshots, categories = _base_snapshots(base)
    return Panel(calendar, series, by_date, missing, unknown, last_day,
                 sorted(snapshots), snapshots, categories, len(days))


def load_panel(cache_dir: Path, start: str = START, end: str = END) -> Panel:
    def get_daily(market: str, d: str) -> list[dict] | None:
        path = cache_path(cache_dir, market, d)
        return read_rows(path) if path.exists() else None

    def iter_base() -> Iterator[tuple[str, str, list[dict]]]:
        for kind in BASE_KINDS:
            for path in sorted((cache_dir / kind).glob("*.json.gz")):
                yield kind.split("/")[1], path.name[:8], read_rows(path)

    return build_panel(weekdays(start, end), get_daily, iter_base())
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_breakout_panel.py -q && ./.venv/Scripts/python.exe -m ruff check scripts/breakout_panel.py tests/test_breakout_panel.py`
Expected: 전부 PASS, `All checks passed!`

- [ ] **Step 5: 커밋**

```bash
git add scripts/breakout_panel.py tests/test_breakout_panel.py
git commit -m "feat(h5): load the KRX cache into a market calendar with base-price adjusted series"
```

---

### Task 5: 판정기 규칙 층 — `breakout_rules.py`

**Files:**
- Create: `scripts/breakout_rules.py`
- Test: `tests/test_breakout_rules.py`

**Interfaces:**
- Consumes: Task 4의 `Panel`, `Series`, `HISTORY_ROWS`, `build_panel`; Task 2의 `weekdays`
- Produces (Task 6·7이 쓴다):
  - 상수 `HOLD_DAYS = 20`, `PRICE_FLOOR = 1_000`, `VALUE_FLOOR = 1_000_000_000`, `LIMIT_CHANGE_DATE = "20150615"`, `LIMIT_UP_SLACK = 0.995`, `LIMIT_DOWN_SLACK = 1.005`, `MATCHED_TOP_FRAC = 0.10`, `COST = 0.0035`, `SIGNAL_START = "20110101"`
  - 상태 문자열 `COMPLETE`, `INCOMPLETE`, `NO_NEXT_DAY`, `NO_ENTRY_ROW`, `NO_ENTRY_SUSPENDED`, `NO_ENTRY_LIMIT_UP`
  - `price_limit(d: str) -> float`
  - `universe_reason(panel: Panel, ticker: str, d: str) -> str | None`
  - `is_signal(s: Series, i: int) -> bool`
  - `@dataclass(frozen=True) class Outcome` — `status, entry_date, exit_date, entry_price, exit_price, exit_kind, limit_down_delay`; 속성 `complete: bool`, `gross: float`. `exit_kind` ∈ {`"SCHEDULED"`, `"DELAYED"`, `"VANISHED"`}
  - `simulate(panel: Panel, ticker: str, signal_date: str) -> Outcome`
  - `@dataclass(frozen=True) class Trade` — `ticker, signal_date, outcome, matched_mean, matched_n, equal_mean, equal_n`
  - `@dataclass class RuleRun` — `trades: list[Trade]`, `counts: Counter[str]`, `event_net: list[float]`, `event_excess: list[float]`, `universe_sizes: dict[str, list[int]]`
  - `run_rules(panel: Panel) -> RuleRun`

규칙 대응(스펙 조항 → 코드):
- §2.2 유니버스 → `universe_reason` (사유 순서 고정: NO_ROW, NO_BASE, SECUGRP, KIND, SPAC, NOT_TRADED, PRICE, VALUE, HISTORY)
- §2.3 신호 → `is_signal`. 부동소수 오차로 같은 값이 '초과'로 뒤집히지 않게 `pm × (1 + 1e-9)`보다 커야 한다
- §2.4 선정 → `run_rules`: 원 거래대금 내림차순·코드 오름차순, 청산일 ≥ D+1인 보유 종목 건너뛰기, 미완료 거래는 끝까지 보유
- §2.5 진입 → `simulate` 앞부분. 진입 불가면 다음 순위로 대체하지 않는다
- §2.6 청산 → `simulate` 뒷부분. 소멸 = 그 종목 마지막 행 날짜 < 그 시장에 행이 있던 마지막 달력일(`panel.last_day`). 결측일에 행이 빠진 것은 소멸이 아니다
- §3.3 대조군 → 주 대조군 = 유니버스 원 거래대금 상위 ⌈10%⌉ 중 신호가 아닌 종목, 기록용 = 거래 종목만 뺀 유니버스 전체. 완료된 것들의 총수익 동일가중 평균
- §4.4 전 신호 이벤트 연구 → 신호가 있는 날마다 모든 신호 종목의 `gross − COST`와 `gross − 주 대조군 평균`

- [ ] **Step 1: 실패하는 테스트 쓰기** — `tests/test_breakout_rules.py` 새로 만들기

```python
"""H5 규칙 층 — 합성 시장만 쓴다. 2010년은 52주 준비, 2011년 상반기에 신호를 낸다."""

import math

from scripts.breakout_panel import build_panel
from scripts.breakout_rules import (
    COMPLETE,
    INCOMPLETE,
    NO_ENTRY_LIMIT_UP,
    NO_ENTRY_SUSPENDED,
    is_signal,
    price_limit,
    run_rules,
    simulate,
    universe_reason,
)
from scripts.krx_daily_fetch import weekdays

DAYS = weekdays("20100104", "20110630")
S = next(i for i, d in enumerate(DAYS) if d >= "20110101") + 5  # 신호일 인덱스
BIG = 2_000_000_000


def rec(ticker, d, *, o, c, chg, val, name):
    return {
        "BAS_DD": d, "ISU_CD": ticker, "ISU_NM": name,
        "TDD_OPNPRC": str(o), "TDD_HGPRC": str(max(o, c) if o else 0),
        "TDD_LWPRC": str(min(o, c) if o else 0), "TDD_CLSPRC": str(c),
        "CMPPREVDD_PRC": str(chg), "ACC_TRDVAL": str(val),
    }


def path(at=None):
    """기본 시가·종가 1,000원. at = {인덱스: (시가, 종가)}."""
    out = [(1000, 1000)] * len(DAYS)
    for i, oc in (at or {}).items():
        out[i] = oc
    return out


class Market:
    def __init__(self):
        self.rows: dict = {}
        self.base: list = []
        self.missing: set = set()

    def add(self, ticker, prices, *, market="stk", value=BIG, name="종목", secugrp="주권",
            kind="보통주", sect="", abbrv="종목", start=0, base=True):
        prev = None
        for d, (o, c) in zip(DAYS[start:], prices):
            chg = 0 if prev is None else c - prev
            self.rows.setdefault((market, d), []).append(
                rec(ticker, d, o=o, c=c, chg=chg, val=value, name=name))
            prev = c
        if base:
            self.base.append({"ISU_SRT_CD": ticker, "SECUGRP_NM": secugrp,
                              "KIND_STKCERT_TP_NM": kind, "SECT_TP_NM": sect,
                              "ISU_ABBRV": abbrv})
        return self

    def panel(self):
        # 두 시장 모두 매일 행이 있게 유니버스 밖의 채움 종목을 넣는다(외국주권)
        self.add("900001", path(), market="stk", secugrp="외국주권")
        self.add("900002", path(), market="ksq", secugrp="외국주권")
        table = {key: rows for key, rows in self.rows.items() if key not in self.missing}
        return build_panel(DAYS, lambda m, d: table.get((m, d)), [("stk", DAYS[0], self.base)])


def test_price_limit_switches_on_20150615():
    assert price_limit("20150612") == 0.15
    assert price_limit("20150615") == 0.30


def test_universe_reason_branches():
    m = Market()
    m.add("000010", path())
    m.add("000020", path(), secugrp="부동산투자회사")
    m.add("000030", path(), kind="구형우선주")
    m.add("000040", path(), sect="SPAC(소속부없음)")
    m.add("000050", path(), sect="관리종목(소속부없음)", abbrv="하나31호스팩")
    m.add("000060", path({S: (0, 1000)}))
    m.add("000070", path({S: (999, 999)}))
    m.add("000080", path(), value=999_999_999)
    m.add("000090", path()[S - 100:], start=S - 100)
    m.add("000100", path(), name="메리츠금융지주", abbrv="메리츠금융지주")
    m.add("000110", path(), base=False)
    p = m.panel()
    day = DAYS[S]

    got = {t: universe_reason(p, t, day) for t in
           ("000010", "000020", "000030", "000040", "000050", "000060", "000070",
            "000080", "000090", "000100", "000110", "999999")}
    assert got == {
        "000010": None, "000020": "SECUGRP", "000030": "KIND", "000040": "SPAC",
        "000050": "SPAC", "000060": "NOT_TRADED", "000070": "PRICE", "000080": "VALUE",
        "000090": "HISTORY", "000100": None, "000110": "NO_BASE", "999999": "NO_ROW",
    }


def test_signal_needs_a_strictly_higher_adjusted_close():
    p = Market().add("000010", path({S: (1000, 1100)})).add("000020", path()).panel()
    a, b = p.series["000010"], p.series["000020"]
    assert is_signal(a, S) is True
    assert is_signal(a, S + 1) is False
    assert is_signal(b, S) is False


def test_simulate_exits_at_the_twentieth_day_open():
    p = Market().add("000010", path({S: (1000, 1100), S + 21: (1210, 1210)})).panel()
    o = simulate(p, "000010", DAYS[S])
    assert (o.status, o.entry_date, o.exit_date, o.exit_kind) == (
        COMPLETE, DAYS[S + 1], DAYS[S + 21], "SCHEDULED")
    assert math.isclose(o.gross, 0.21)


def test_simulate_refuses_a_suspended_entry():
    p = Market().add("000010", path({S: (1000, 1100), S + 1: (0, 1100)})).panel()
    assert simulate(p, "000010", DAYS[S]).status == NO_ENTRY_SUSPENDED


def test_simulate_refuses_a_limit_up_open_with_the_pre_2015_limit():
    m = Market()
    m.add("000010", path({S: (1000, 1100), S + 1: (1265, 1265)}))   # 1100 × 1.15
    m.add("000020", path({S: (1000, 1100), S + 1: (1250, 1250)}))   # 기준 1258.6 미만
    p = m.panel()
    assert simulate(p, "000010", DAYS[S]).status == NO_ENTRY_LIMIT_UP
    assert simulate(p, "000020", DAYS[S]).status == COMPLETE


def test_simulate_delays_the_exit_on_suspension_and_on_a_limit_down_open():
    m = Market()
    m.add("000010", path({S: (1000, 1100), S + 21: (0, 1000)}))
    m.add("000020", path({S: (1000, 1100), S + 21: (850, 850)}))    # 1000 × 0.85 × 1.005 이하
    p = m.panel()
    a, b = simulate(p, "000010", DAYS[S]), simulate(p, "000020", DAYS[S])
    assert (a.exit_date, a.exit_kind, a.limit_down_delay) == (DAYS[S + 22], "DELAYED", False)
    assert (b.exit_date, b.exit_kind, b.limit_down_delay) == (DAYS[S + 22], "DELAYED", True)


def test_simulate_exits_a_vanished_stock_at_its_last_close():
    m = Market()
    m.add("000010", path({S: (1000, 1100), S + 10: (500, 500)})[: S + 11])
    m.add("000020", path())
    o = simulate(m.panel(), "000010", DAYS[S])
    assert (o.status, o.exit_date, o.exit_kind) == (COMPLETE, DAYS[S + 10], "VANISHED")
    assert math.isclose(o.gross, -0.5)


def test_simulate_is_incomplete_past_the_end_of_data():
    p = Market().add("000010", path()).panel()
    assert simulate(p, "000010", DAYS[-5]).status == INCOMPLETE


def test_a_missing_market_file_is_not_a_vanishing():
    m = Market()
    m.add("000010", path({S: (1000, 1100)}), market="ksq")
    m.missing.add(("ksq", DAYS[S + 21]))
    o = simulate(m.panel(), "000010", DAYS[S])
    assert (o.exit_date, o.exit_kind) == (DAYS[S + 22], "DELAYED")


def test_run_rules_picks_the_top_value_signal_and_builds_both_controls():
    m = Market()
    m.add("000010", path({S: (1000, 1100)}), value=5_000_000_000)
    m.add("000020", path({S: (1000, 1100)}), value=3_000_000_000)
    # 청산일 시가만 움직이고 종가는 1,000원에 둔다 — 종가를 올리면 그날 새 신고가가 된다
    m.add("000030", path({S + 21: (1100, 1000)}), value=9_000_000_000)
    m.add("000040", path({S + 21: (900, 1000)}), value=2_000_000_000)
    run = run_rules(m.panel())

    assert [(t.ticker, t.signal_date) for t in run.trades] == [("000010", DAYS[S])]
    trade = run.trades[0]
    # 유니버스 4종목 → 상위 ⌈0.4⌉ = 1종목(000030), 신호가 아니다
    assert trade.matched_n == 1 and math.isclose(trade.matched_mean, 0.1)
    # 거래 종목만 뺀 3종목: 0, +0.1, −0.1
    assert trade.equal_n == 3 and math.isclose(trade.equal_mean, 0.0, abs_tol=1e-12)
    assert len(run.event_excess) == 2
    assert all(math.isclose(x, -0.1) for x in run.event_excess)


def test_run_rules_skips_a_held_stock_for_the_next_rank():
    m = Market()
    m.add("000010", path({S: (1000, 1100), S + 5: (1000, 1200)}), value=5_000_000_000)
    m.add("000020", path({S + 5: (1000, 1100)}), value=3_000_000_000)
    run = run_rules(m.panel())
    assert [(t.ticker, t.signal_date) for t in run.trades] == [
        ("000010", DAYS[S]), ("000020", DAYS[S + 5])]


def test_run_rules_breaks_value_ties_by_code():
    m = Market()
    m.add("000020", path({S: (1000, 1100)}))
    m.add("000010", path({S: (1000, 1100)}))
    assert run_rules(m.panel()).trades[0].ticker == "000010"


def test_run_rules_does_not_substitute_when_the_top_signal_cannot_be_entered():
    m = Market()
    m.add("000010", path({S: (1000, 1100), S + 1: (0, 1100)}), value=5_000_000_000)
    m.add("000020", path({S: (1000, 1100)}), value=3_000_000_000)
    run = run_rules(m.panel())
    assert run.trades == []
    assert run.counts[NO_ENTRY_SUSPENDED] == 1


def test_run_rules_keeps_a_trade_whose_matched_control_is_empty():
    m = Market()
    m.add("000010", path({S: (1000, 1100)}), value=5_000_000_000)
    run = run_rules(m.panel())
    assert run.trades[0].matched_mean is None and run.trades[0].matched_n == 0
    assert run.event_excess == []
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_breakout_rules.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.breakout_rules'`

- [ ] **Step 3: 구현** — `scripts/breakout_rules.py` 새로 만들기

```python
"""H5 판정기의 규칙 층 — 유니버스·신호·선정·진입·청산·대조군.

스펙 §2(선정·매매 규칙), §3.3~3.4(대조군·수익 정의), §4.4(전 신호 이벤트 연구).
순수 함수다. 캐시를 직접 읽지 않고 Panel 을 받는다.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from scripts.breakout_panel import HISTORY_ROWS, Panel, Series

HOLD_DAYS = 20
PRICE_FLOOR = 1_000
VALUE_FLOOR = 1_000_000_000
LIMIT_CHANGE_DATE = "20150615"
LIMIT_UP_SLACK = 0.995
LIMIT_DOWN_SLACK = 1.005
MATCHED_TOP_FRAC = 0.10
COST = 0.0035
SIGNAL_START = "20110101"
SIGNAL_TOLERANCE = 1e-9  # 수정가 곱셈의 부동소수 오차로 같은 값이 '초과'로 뒤집히지 않게

COMPLETE = "COMPLETE"
INCOMPLETE = "INCOMPLETE"
NO_NEXT_DAY = "NO_NEXT_DAY"
NO_ENTRY_ROW = "NO_ENTRY_ROW"
NO_ENTRY_SUSPENDED = "NO_ENTRY_SUSPENDED"
NO_ENTRY_LIMIT_UP = "NO_ENTRY_LIMIT_UP"


def price_limit(d: str) -> float:
    """가격제한폭 — 2015-06-15 에 ±15% → ±30% (스펙 §2.5)."""
    return 0.15 if d < LIMIT_CHANGE_DATE else 0.30


def universe_reason(panel: Panel, ticker: str, d: str) -> str | None:
    """스펙 §2.2. 통과면 None, 아니면 첫 탈락 사유."""
    s = panel.series.get(ticker)
    i = s.pos(d) if s is not None else -1
    if s is None or i < 0:
        return "NO_ROW"
    info = panel.base_info(ticker, d)
    if info is None:
        return "NO_BASE"
    secugrp, kind, sect, abbrv = info
    if secugrp != "주권":
        return "SECUGRP"
    if kind != "보통주":
        return "KIND"
    if "SPAC" in sect or "스팩" in abbrv or "스팩" in s.names[i]:
        return "SPAC"
    if s.open[i] <= 0:
        return "NOT_TRADED"
    if s.close[i] < PRICE_FLOOR:
        return "PRICE"
    if s.value[i] < VALUE_FLOOR:
        return "VALUE"
    if i < HISTORY_ROWS:
        return "HISTORY"
    return None


def is_signal(s: Series, i: int) -> bool:
    """스펙 §2.3 — 수정 종가 > 직전 250행 수정 종가 최고치 (엄격 초과)."""
    pm = s.prior_max[i]
    return not math.isnan(pm) and s.adj_close(i) > pm * (1 + SIGNAL_TOLERANCE)


@dataclass(frozen=True)
class Outcome:
    status: str
    entry_date: str = ""
    exit_date: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    exit_kind: str = ""
    limit_down_delay: bool = False

    @property
    def complete(self) -> bool:
        return self.status == COMPLETE

    @property
    def gross(self) -> float:
        return self.exit_price / self.entry_price - 1.0


def simulate(panel: Panel, ticker: str, signal_date: str) -> Outcome:
    """신호일 D 에 산다고 할 때의 결과 — 스펙 §2.5 진입, §2.6 청산."""
    s = panel.series[ticker]
    ci = panel.cal_index[signal_date]
    if ci + 1 >= len(panel.calendar):
        return Outcome(NO_NEXT_DAY)
    entry_day = panel.calendar[ci + 1]
    ei = s.pos(entry_day)
    if ei < 0:
        return Outcome(NO_ENTRY_ROW)
    if s.open[ei] <= 0:
        return Outcome(NO_ENTRY_SUSPENDED)
    basis = s.basis(ei)
    if basis is not None and s.open[ei] >= basis * (1 + price_limit(entry_day)) * LIMIT_UP_SLACK:
        return Outcome(NO_ENTRY_LIMIT_UP)
    entry_price = s.adj_open(ei)

    last = len(s.dates) - 1
    vanished = s.dates[last] < panel.last_day.get(s.market, s.dates[last])
    limit_down_seen = False
    target = ci + 1 + HOLD_DAYS
    for k in range(target, len(panel.calendar)):
        day = panel.calendar[k]
        if vanished and day > s.dates[last]:
            break
        xi = s.pos(day)
        if xi < 0 or s.open[xi] <= 0:
            continue
        b = s.basis(xi)
        if b is not None and s.open[xi] <= b * (1 - price_limit(day)) * LIMIT_DOWN_SLACK:
            limit_down_seen = True
            continue
        kind = "SCHEDULED" if k == target else "DELAYED"
        return Outcome(COMPLETE, entry_day, day, entry_price, s.adj_open(xi), kind,
                       limit_down_seen)
    if vanished:
        return Outcome(COMPLETE, entry_day, s.dates[last], entry_price, s.adj_close(last),
                       "VANISHED", limit_down_seen)
    return Outcome(INCOMPLETE, entry_date=entry_day, entry_price=entry_price)


@dataclass(frozen=True)
class Trade:
    ticker: str
    signal_date: str
    outcome: Outcome
    matched_mean: float | None
    matched_n: int
    equal_mean: float | None
    equal_n: int


@dataclass
class RuleRun:
    trades: list[Trade] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)
    event_net: list[float] = field(default_factory=list)
    event_excess: list[float] = field(default_factory=list)
    universe_sizes: dict[str, list[int]] = field(default_factory=dict)


def _cached(panel: Panel, cache: dict[str, Outcome], ticker: str, d: str) -> Outcome:
    outcome = cache.get(ticker)
    if outcome is None:
        outcome = simulate(panel, ticker, d)
        cache[ticker] = outcome
    return outcome


def _complete_mean(outcomes: Iterable[Outcome]) -> tuple[float | None, int]:
    grosses = [o.gross for o in outcomes if o.complete]
    if not grosses:
        return None, 0
    return sum(grosses) / len(grosses), len(grosses)


def run_rules(panel: Panel) -> RuleRun:
    run = RuleRun()
    held_until: dict[str, str | None] = {}  # 종목 → 청산일. 미완료면 None(끝까지 보유)
    for ci, d in enumerate(panel.calendar):
        if d < SIGNAL_START:
            continue
        members: list[str] = []
        values: dict[str, int] = {}
        for t in panel.by_date.get(d, []):
            reason = universe_reason(panel, t, d)
            if reason is None:
                s = panel.series[t]
                members.append(t)
                values[t] = s.value[s.pos(d)]
            elif reason == "NO_BASE":
                run.counts["universe_no_base"] += 1
        run.universe_sizes.setdefault(d[:4], []).append(len(members))

        signals = [t for t in members if is_signal(panel.series[t], panel.series[t].pos(d))]
        if not signals:
            run.counts["no_signal_day"] += 1
            continue

        cache: dict[str, Outcome] = {}
        ranked = sorted(members, key=lambda t: (-values[t], t))
        top = ranked[: math.ceil(len(members) * MATCHED_TOP_FRAC)]
        signal_set = set(signals)
        matched_mean, matched_n = _complete_mean(
            _cached(panel, cache, t, d) for t in top if t not in signal_set)
        if matched_mean is not None:
            for t in signals:
                o = _cached(panel, cache, t, d)
                if o.complete:
                    run.event_net.append(o.gross - COST)
                    run.event_excess.append(o.gross - matched_mean)

        entry_day = panel.calendar[ci + 1] if ci + 1 < len(panel.calendar) else ""
        chosen: str | None = None
        for t in sorted(signals, key=lambda t: (-values[t], t)):
            if t in held_until:
                until = held_until[t]
                if until is None or until >= entry_day:
                    continue
            chosen = t
            break
        if chosen is None:
            run.counts["all_signals_held"] += 1
            continue

        o = _cached(panel, cache, chosen, d)
        if not (o.complete or o.status == INCOMPLETE):
            run.counts[o.status] += 1
            continue
        equal_mean, equal_n = _complete_mean(
            _cached(panel, cache, t, d) for t in members if t != chosen)
        run.trades.append(Trade(chosen, d, o, matched_mean, matched_n, equal_mean, equal_n))
        held_until[chosen] = o.exit_date if o.complete else None
        if o.status == INCOMPLETE:
            run.counts["incomplete"] += 1
        elif o.exit_kind == "VANISHED":
            run.counts["exit_vanished"] += 1
        if o.limit_down_delay:
            run.counts["exit_limit_down_delay"] += 1
    return run
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_breakout_rules.py -q && ./.venv/Scripts/python.exe -m ruff check scripts/breakout_rules.py tests/test_breakout_rules.py`
Expected: 전부 PASS, `All checks passed!`

- [ ] **Step 5: 커밋**

```bash
git add scripts/breakout_rules.py tests/test_breakout_rules.py
git commit -m "feat(h5): select, enter and exit 52-week-high breakouts with matched controls"
```

---

### Task 6: 판정 층 — `breakout_gates.py`

**Files:**
- Create: `scripts/breakout_gates.py`
- Test: `tests/test_breakout_gates.py`

**Interfaces:**
- Consumes: Task 5의 `COST`, `Outcome`, `Trade`
- Produces (Task 7이 쓴다):
  - 상수 `BLOCK = 20`, `REPS = 10_000`, `SEED = 20260930`, `TRIM_FRAC = 0.01`, `PERIODS = (("2011", "2015"), ("2016", "2020"), ("2021", "2026"))`, `MIN_N = 50`, `SLOTS = 20`
  - `@dataclass(frozen=True) class Sample` — `ticker, entry_date, exit_date, entry_price, exit_price, net, excess, excess_eq`
  - `samples_from_trades(trades: Iterable[Trade]) -> tuple[list[Sample], Counter[str]]` — 진입일 순 정렬, 빠진 사유 `incomplete`·`no_matched_control`
  - `block_bootstrap_lower(values: list[float], *, block=BLOCK, reps=REPS, seed=SEED) -> float` — 표본이 한 블록보다 적으면 `-inf`
  - `trimmed_mean(values: list[float], frac=TRIM_FRAC) -> float`
  - `period_means(samples: list[Sample]) -> dict[str, float | None]` — 키 `"2011-2015"` 등
  - `evaluate(samples: list[Sample]) -> dict` — 키 `n, mean_net, mean_excess, lower_excess, trimmed_net, trimmed_excess, period_excess, gates, passed`
  - `recorded_metrics(samples: list[Sample]) -> dict` — 키 `win_rate, median_net, lower_net, mean_excess_eq, lower_excess_eq, control_gap, yearly`
  - `equity_curve_mdd(samples: list[Sample], close_on: Callable[[str, str], float | None], calendar: list[str], slots: int = SLOTS) -> dict` — 키 `mdd, excluded_trades, over_capacity_days`

규칙 대응: §4.2 G1 = `mean_net > 0`, G2 = X 블록 부트스트랩 CI 하한 > 0, G3 = A·X 각각 상위 ⌈1%⌉ 뺀 평균 > 0, G4 = 세 구간 X 평균 모두 > 0(빈 구간은 실패), G5 = n ≥ 50. §4.3 CI 하한 = 정렬한 평균의 인덱스 `int(reps × 0.025)`(10,000회면 250). §4.4 자금 곡선: 진입일에 이미 20칸이 차 있으면(진입일 ≤ 그날 < 청산일인 거래가 20개) 그 거래를 곡선에서 빼고 건수·날 수를 센다. 보유 종목은 매일 수정 종가로 평가하고 행이 없는 날은 직전 평가가를 쓴다.

- [ ] **Step 1: 실패하는 테스트 쓰기** — `tests/test_breakout_gates.py` 새로 만들기

```python
"""H5 판정 층 — 합성 표본만 쓴다."""

import math
import statistics

from scripts.breakout_gates import (
    Sample,
    block_bootstrap_lower,
    equity_curve_mdd,
    evaluate,
    recorded_metrics,
    samples_from_trades,
    trimmed_mean,
)
from scripts.breakout_rules import COMPLETE, INCOMPLETE, Outcome, Trade


def sample(entry, net=0.02, excess=0.01, excess_eq=0.0, ticker="000010", exit_="29991231"):
    return Sample(ticker, entry, exit_, 1000.0, 1000.0, net, excess, excess_eq)


def test_block_bootstrap_is_deterministic_and_needs_one_block():
    values = [0.01 * ((i * 7919) % 13 - 6) for i in range(400)]
    first = block_bootstrap_lower(values)
    assert first == block_bootstrap_lower(values)
    assert first < statistics.fmean(values)
    assert block_bootstrap_lower([1.0] * 100) == 1.0
    assert block_bootstrap_lower([1.0] * 19) == -math.inf


def test_trimmed_mean_drops_the_top_ceil_one_percent():
    assert trimmed_mean([float(v) for v in range(1, 101)]) == 50.0
    assert trimmed_mean([1.0, 2.0, 3.0]) == 1.5


def test_samples_from_trades_drops_incomplete_and_uncontrolled_and_sorts():
    done_late = Outcome(COMPLETE, "20120110", "20120207", 1000.0, 1100.0, "SCHEDULED")
    done_early = Outcome(COMPLETE, "20120105", "20120202", 1000.0, 900.0, "SCHEDULED")
    trades = [
        Trade("000010", "20120109", done_late, 0.05, 3, 0.02, 10),
        Trade("000020", "20120104", done_early, 0.00, 3, None, 0),
        Trade("000030", "20120104", Outcome(INCOMPLETE, "20120105"), 0.0, 3, 0.0, 10),
        Trade("000040", "20120104", done_early, None, 0, 0.0, 10),
    ]
    samples, dropped = samples_from_trades(trades)

    assert [s.ticker for s in samples] == ["000020", "000010"]
    late = samples[1]
    assert math.isclose(late.net, 0.1 - 0.0035)
    assert math.isclose(late.excess, 0.1 - 0.05)
    assert math.isclose(late.excess_eq, 0.1 - 0.02)
    assert samples[0].excess_eq is None
    assert dropped == {"incomplete": 1, "no_matched_control": 1}


def _even(n_per_period=20, **kw):
    out = []
    for year in ("2012", "2017", "2022"):
        out += [sample(f"{year}01{i + 1:02d}", **kw) for i in range(n_per_period)]
    return out


def test_evaluate_passes_when_every_gate_holds():
    result = evaluate(_even())
    assert result["gates"] == {"G1": True, "G2": True, "G3": True, "G4": True, "G5": True}
    assert result["passed"] is True
    assert result["n"] == 60
    assert math.isclose(result["lower_excess"], 0.01)


def test_evaluate_fails_g4_when_one_period_is_negative():
    samples = _even()
    samples = [s if not s.entry_date.startswith("2017") else sample(s.entry_date, excess=-0.01)
               for s in samples]
    result = evaluate(samples)
    assert result["gates"]["G4"] is False and result["passed"] is False


def test_evaluate_fails_g4_when_a_period_is_empty():
    samples = [s for s in _even() if not s.entry_date.startswith("2022")]
    assert evaluate(samples)["period_excess"]["2021-2026"] is None
    assert evaluate(samples)["gates"]["G4"] is False


def test_evaluate_fails_g5_below_fifty():
    assert evaluate(_even(n_per_period=16))["gates"]["G5"] is False


def test_evaluate_fails_g1_on_a_negative_mean_net():
    assert evaluate(_even(net=-0.001))["gates"]["G1"] is False


def test_evaluate_fails_g3_when_one_outlier_carries_the_mean():
    samples = [sample(f"2012{m:02d}{d:02d}", excess=-0.001)
               for m in range(1, 11) for d in range(1, 11)][:99]
    samples.append(sample("20121231", excess=0.5))
    assert evaluate(samples)["gates"]["G3"] is False


def test_recorded_metrics():
    samples = [sample("20120101", net=0.02, excess=0.01, excess_eq=0.03),
               sample("20130101", net=-0.01, excess=-0.02, excess_eq=-0.01)]
    m = recorded_metrics(samples)
    assert m["win_rate"] == 0.5
    assert math.isclose(m["median_net"], 0.005)
    assert math.isclose(m["control_gap"], 0.015)
    assert sorted(m["yearly"]) == ["2012", "2013"]
    assert m["yearly"]["2012"]["n"] == 1


def test_equity_curve_flat_holding_has_no_drawdown():
    calendar = ["D1", "D2", "D3"]
    s = Sample("000010", "D1", "D3", 1000.0, 1000.0, -0.0035, 0.0, 0.0)
    got = equity_curve_mdd([s], lambda t, d: 1000.0, calendar)
    assert got == {"mdd": 0.0, "excluded_trades": 0, "over_capacity_days": 0}


def test_equity_curve_marks_open_positions_to_market_with_one_slot_weight():
    calendar = ["D1", "D2", "D3"]
    s = Sample("000010", "D1", "D3", 1000.0, 1000.0, -0.0035, 0.0, 0.0)
    closes = {"D1": 1000.0, "D2": 500.0}
    got = equity_curve_mdd([s], lambda t, d: closes.get(d), calendar)
    assert math.isclose(got["mdd"], 0.5 / 20)


def test_equity_curve_excludes_trades_beyond_twenty_slots():
    calendar = ["D1", "D2", "D3"]
    samples = [Sample(f"{i:06d}", "D1", "D3", 1000.0, 1000.0, 0.0, 0.0, 0.0) for i in range(21)]
    got = equity_curve_mdd(samples, lambda t, d: 1000.0, calendar)
    assert (got["excluded_trades"], got["over_capacity_days"]) == (1, 1)
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_breakout_gates.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.breakout_gates'`

- [ ] **Step 3: 구현** — `scripts/breakout_gates.py` 새로 만들기

```python
"""H5 판정 층 — 블록 부트스트랩, G1~G5, 기록 지표, 20칸 자금 곡선.

스펙 §4.2(통과 조건), §4.3(부트스트랩), §4.4(기록만 하는 지표). 순수 파이썬 —
프로젝트 venv 에 numpy 가 없다.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from scripts.breakout_rules import COST, Trade

BLOCK = 20
REPS = 10_000
SEED = 20260930
TRIM_FRAC = 0.01
PERIODS = (("2011", "2015"), ("2016", "2020"), ("2021", "2026"))
MIN_N = 50
SLOTS = 20


@dataclass(frozen=True)
class Sample:
    ticker: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    net: float  # A = 총수익 − 왕복 비용
    excess: float  # X = 총수익 − 주 대조군(거래대금 맞춤) 평균
    excess_eq: float | None  # X_eq = 총수익 − 기록용 동일가중 대조군 평균


def samples_from_trades(trades: Iterable[Trade]) -> tuple[list[Sample], Counter[str]]:
    """완료되고 주 대조군이 있는 거래만 판정 표본이 된다 (스펙 §2.6, §3.3)."""
    samples: list[Sample] = []
    dropped: Counter[str] = Counter()
    for t in trades:
        o = t.outcome
        if not o.complete:
            dropped["incomplete"] += 1
            continue
        if t.matched_mean is None:
            dropped["no_matched_control"] += 1
            continue
        gross = o.gross
        excess_eq = None if t.equal_mean is None else gross - t.equal_mean
        samples.append(Sample(t.ticker, o.entry_date, o.exit_date, o.entry_price, o.exit_price,
                              gross - COST, gross - t.matched_mean, excess_eq))
    samples.sort(key=lambda s: s.entry_date)
    return samples, dropped


def block_bootstrap_lower(
    values: list[float], *, block: int = BLOCK, reps: int = REPS, seed: int = SEED
) -> float:
    """이동 블록 부트스트랩 평균의 95% CI 하한 (스펙 §4.3). 한 블록도 안 되면 -inf."""
    n = len(values)
    k = n // block
    if k < 1:
        return -math.inf
    prefix = [0.0]
    for v in values:
        prefix.append(prefix[-1] + v)
    sums = [prefix[i + block] - prefix[i] for i in range(n - block + 1)]
    rng = random.Random(seed)
    m = len(sums)
    means = sorted(
        sum(sums[rng.randrange(m)] for _ in range(k)) / (k * block) for _ in range(reps)
    )
    return means[int(reps * 0.025)]


def trimmed_mean(values: list[float], frac: float = TRIM_FRAC) -> float:
    """큰 쪽 상위 ⌈frac⌉ 를 뺀 평균 (스펙 §4.2 G3)."""
    if not values:
        return math.nan
    drop = math.ceil(len(values) * frac)
    kept = sorted(values)[: len(values) - drop]
    return statistics.fmean(kept) if kept else math.nan


def period_means(samples: list[Sample]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for lo, hi in PERIODS:
        xs = [s.excess for s in samples if lo <= s.entry_date[:4] <= hi]
        out[f"{lo}-{hi}"] = statistics.fmean(xs) if xs else None
    return out


def evaluate(samples: list[Sample]) -> dict:
    """G1~G5 (스펙 §4.2). 다섯 개 모두여야 통과, 부분 통과 없음."""
    net = [s.net for s in samples]
    excess = [s.excess for s in samples]
    n = len(samples)
    mean_net = statistics.fmean(net) if net else math.nan
    mean_excess = statistics.fmean(excess) if excess else math.nan
    lower_excess = block_bootstrap_lower(excess)
    trimmed_net, trimmed_excess = trimmed_mean(net), trimmed_mean(excess)
    periods = period_means(samples)
    gates = {
        "G1": mean_net > 0,
        "G2": lower_excess > 0,
        "G3": trimmed_net > 0 and trimmed_excess > 0,
        "G4": all(m is not None and m > 0 for m in periods.values()),
        "G5": n >= MIN_N,
    }
    return {
        "n": n,
        "mean_net": mean_net,
        "mean_excess": mean_excess,
        "lower_excess": lower_excess,
        "trimmed_net": trimmed_net,
        "trimmed_excess": trimmed_excess,
        "period_excess": periods,
        "gates": gates,
        "passed": all(gates.values()),
    }


def recorded_metrics(samples: list[Sample]) -> dict:
    """판정에 쓰지 않는 기록 지표 (스펙 §4.4)."""
    net = [s.net for s in samples]
    with_eq = [s for s in samples if s.excess_eq is not None]
    eq = [s.excess_eq for s in with_eq if s.excess_eq is not None]
    by_year: dict[str, list[Sample]] = {}
    for s in samples:
        by_year.setdefault(s.entry_date[:4], []).append(s)
    return {
        "win_rate": sum(v > 0 for v in net) / len(net) if net else math.nan,
        "median_net": statistics.median(net) if net else math.nan,
        "lower_net": block_bootstrap_lower(net),
        "mean_excess_eq": statistics.fmean(eq) if eq else math.nan,
        "lower_excess_eq": block_bootstrap_lower(eq),
        # 주 대조군 평균 − 동일가중 평균 = X_eq − X. 거래대금이 몰린 종목 집단의 효과 추정치.
        "control_gap": (statistics.fmean([s.excess_eq - s.excess for s in with_eq
                                          if s.excess_eq is not None])
                        if with_eq else math.nan),
        "yearly": {
            year: {
                "n": len(group),
                "mean_net": statistics.fmean(s.net for s in group),
                "mean_excess": statistics.fmean(s.excess for s in group),
            }
            for year, group in sorted(by_year.items())
        },
    }


def equity_curve_mdd(
    samples: list[Sample],
    close_on: Callable[[str, str], float | None],
    calendar: list[str],
    slots: int = SLOTS,
) -> dict:
    """자본을 slots 칸으로 나눠 거래마다 한 칸, 보유 종목은 매일 수정 종가로 평가 (스펙 §4.4)."""
    admitted: list[Sample] = []
    excluded = 0
    over_days: set[str] = set()
    for s in sorted(samples, key=lambda s: (s.entry_date, s.ticker)):
        live = sum(1 for q in admitted if q.entry_date <= s.entry_date < q.exit_date)
        if live >= slots:
            excluded += 1
            over_days.add(s.entry_date)
            continue
        admitted.append(s)
    result = {"mdd": 0.0, "excluded_trades": excluded, "over_capacity_days": len(over_days)}
    if not admitted:
        return result

    weight = 1.0 / slots
    start = admitted[0].entry_date
    end = max(s.exit_date for s in admitted)
    realized = 0.0
    open_idx: list[int] = []
    last_px: dict[int, float] = {}
    nxt = 0
    peak = 1.0
    mdd = 0.0
    for day in calendar:
        if day < start:
            continue
        if day > end:
            break
        while nxt < len(admitted) and admitted[nxt].entry_date <= day:
            open_idx.append(nxt)
            nxt += 1
        still: list[int] = []
        unrealized = 0.0
        for idx in open_idx:
            s = admitted[idx]
            if s.exit_date <= day:
                realized += weight * (s.exit_price / s.entry_price - 1.0)
                continue
            px = close_on(s.ticker, day)
            if px is not None:
                last_px[idx] = px
            unrealized += weight * (last_px.get(idx, s.entry_price) / s.entry_price - 1.0)
            still.append(idx)
        open_idx = still
        equity = 1.0 + realized + unrealized
        peak = max(peak, equity)
        mdd = max(mdd, 1.0 - equity / peak)
    result["mdd"] = mdd
    return result
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_breakout_gates.py -q && ./.venv/Scripts/python.exe -m ruff check scripts/breakout_gates.py tests/test_breakout_gates.py`
Expected: 전부 PASS, `All checks passed!`

- [ ] **Step 5: 커밋**

```bash
git add scripts/breakout_gates.py tests/test_breakout_gates.py
git commit -m "feat(h5): judge the five gates with a block bootstrap and record the equity drawdown"
```

---

### Task 7: 봉인된 판정 CLI — `breakout_sieve.py`

**Files:**
- Create: `scripts/breakout_sieve.py`
- Test: `tests/test_breakout_sieve.py`

**Interfaces:**
- Consumes: Task 4 `Panel`, `load_panel`, `KNOWN_SECUGRP`, `KNOWN_KIND`, `build_panel`(테스트); Task 5 `run_rules`; Task 6 `samples_from_trades`, `evaluate`, `recorded_metrics`, `equity_curve_mdd`; Task 2 `DEFAULT_CACHE`, `weekdays`(테스트)
- Produces:
  - `SPEC`, `CODE_FILES`, `DEFAULT_LOG = ROOT / "data" / "h5" / "verdict_log.jsonl"`, `MAX_MISSING_RATIO = 0.01`, `VERDICT = "VERDICT"`, `INCOMPLETE_DATA = "INCOMPLETE_DATA"`
  - `class SealError(RuntimeError)`
  - `check_clean(root: Path) -> None`, `commit_ids(root: Path) -> dict[str, str]`, `manifest_sha256(cache_dir: Path) -> tuple[str, int]`, `unknown_categories(panel: Panel) -> dict[str, list[str]]`, `verdict_runs(log_path: Path) -> int`, `append_log(log_path: Path, record: dict) -> None`
  - `run_verdict(root: Path, cache_dir: Path, log_path: Path, rerun_reason: str = "", *, load=load_panel) -> dict`
  - `main(argv: list[str] | None = None) -> int` — `--verdict` 없으면 2, 봉인 거부 3, 판정 완료 0

봉인 순서(스펙 §6.4): ① 워킹트리 깨끗 ② 이전 판정이 있으면 `--rerun-reason` 필수 ③ 커밋 해시(HEAD·스펙·판정기 코드) 기록 ④ 결측 ≤ 1% — 넘으면 `INCOMPLETE_DATA`를 로그에 남기고 거부 ⑤ `SECUGRP_NM`·`KIND_STKCERT_TP_NM`에 등록 때 못 본 값이 있으면 거부(`SECT_TP_NM`은 값 목록만 기록) ⑥ 캐시 매니페스트 SHA-256 ⑦ 계산 ⑧ 로그에 **추가만**. ①·②는 데이터를 올리기 전에 막는다.

- [ ] **Step 1: 실패하는 테스트 쓰기** — `tests/test_breakout_sieve.py` 새로 만들기

```python
"""H5 봉인 판정 — 임시 git 저장소와 합성 Panel 만 쓴다. 실제 캐시·로그를 건드리지 않는다."""

import json
import subprocess

import pytest

from scripts.breakout_panel import build_panel
from scripts.breakout_sieve import (
    INCOMPLETE_DATA,
    VERDICT,
    SealError,
    main,
    manifest_sha256,
    run_verdict,
    verdict_runs,
)
from scripts.krx_daily_fetch import cache_path, save_atomic, weekdays

DAYS = weekdays("20100104", "20110331")


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "README").write_bytes(b"x")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def fake_panel(*, missing_every=0, secugrp="주권"):
    """종가가 날마다 1원씩 오르는 두 종목 — 2011년에는 매일 신고가."""

    def row(ticker, d, i):
        c = 1000 + i
        return {"BAS_DD": d, "ISU_CD": ticker, "ISU_NM": "종목", "TDD_OPNPRC": str(c),
                "TDD_HGPRC": str(c), "TDD_LWPRC": str(c), "TDD_CLSPRC": str(c),
                "CMPPREVDD_PRC": "1" if i else "0", "ACC_TRDVAL": "2000000000"}

    def get_daily(market, d):
        i = DAYS.index(d)
        if missing_every and market == "ksq" and i % missing_every == 0:
            return None
        return [row("000010" if market == "stk" else "000020", d, i)]

    def info(code, group):
        return {"ISU_SRT_CD": code, "SECUGRP_NM": group, "KIND_STKCERT_TP_NM": "보통주",
                "SECT_TP_NM": "", "ISU_ABBRV": "종목"}

    base = [("stk", DAYS[0], [info("000010", secugrp), info("000020", "주권")])]
    return build_panel(DAYS, get_daily, base)


def test_main_refuses_without_the_verdict_flag():
    assert main([]) == 2


def test_dirty_tree_is_refused_before_loading(repo, tmp_path):
    (repo / "untracked.txt").write_bytes(b"x")

    def must_not_load(_cache_dir):
        raise AssertionError("데이터를 올리기 전에 막아야 한다")

    with pytest.raises(SealError, match="깨끗"):
        run_verdict(repo, tmp_path / "cache", tmp_path / "log.jsonl", load=must_not_load)


def test_first_run_logs_a_verdict_and_a_second_needs_a_reason(repo, tmp_path):
    log = tmp_path / "h5" / "verdict_log.jsonl"
    record = run_verdict(repo, tmp_path / "cache", log, load=lambda _d: fake_panel())

    assert record["status"] == VERDICT
    assert set(record["verdict"]["gates"]) == {"G1", "G2", "G3", "G4", "G5"}
    assert "equity" in record["recorded"] and "counts" in record["recorded"]
    assert record["commits"]["head"]
    assert verdict_runs(log) == 1

    with pytest.raises(SealError, match="rerun-reason"):
        run_verdict(repo, tmp_path / "cache", log, load=lambda _d: fake_panel())

    again = run_verdict(repo, tmp_path / "cache", log, "판정기 버그 수정 뒤 재실행",
                        load=lambda _d: fake_panel())
    assert again["rerun_reason"] == "판정기 버그 수정 뒤 재실행"
    assert again["prior_verdicts"] == 1
    assert verdict_runs(log) == 2


def test_too_many_missing_days_are_logged_and_refused(repo, tmp_path):
    log = tmp_path / "log.jsonl"
    with pytest.raises(SealError, match="결측"):
        run_verdict(repo, tmp_path / "cache", log, load=lambda _d: fake_panel(missing_every=50))

    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert [x["status"] for x in lines] == [INCOMPLETE_DATA]
    assert verdict_runs(log) == 0


def test_unseen_category_values_are_refused(repo, tmp_path):
    with pytest.raises(SealError, match="범주값"):
        run_verdict(repo, tmp_path / "cache", tmp_path / "log.jsonl",
                    load=lambda _d: fake_panel(secugrp="신종증권"))


def test_manifest_is_deterministic_and_tracks_file_sizes(tmp_path):
    save_atomic(cache_path(tmp_path, "stk", "20170223"), [{"ISU_CD": "117930"}])
    first = manifest_sha256(tmp_path)
    assert first == manifest_sha256(tmp_path)
    assert first[1] == 1
    save_atomic(cache_path(tmp_path, "stk", "20170223"), [{"ISU_CD": "117930", "X": "y" * 50}])
    assert manifest_sha256(tmp_path)[0] != first[0]
```

- [ ] **Step 2: 실패 확인**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_breakout_sieve.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.breakout_sieve'`

- [ ] **Step 3: 구현** — `scripts/breakout_sieve.py` 새로 만들기

```python
"""H5 판정기 CLI — 봉인된 판정을 한 번 실행한다 (스펙 §6.3~§6.4).

실데이터를 읽는 경로는 --verdict 하나뿐이다. 개발과 테스트는 합성 픽스처만 쓴다.
결과는 data/h5/verdict_log.jsonl 에 추가만 하고, 사람이 스펙 §9 표로 옮긴다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.breakout_gates import (  # noqa: E402
    equity_curve_mdd,
    evaluate,
    recorded_metrics,
    samples_from_trades,
)
from scripts.breakout_panel import KNOWN_KIND, KNOWN_SECUGRP, Panel, load_panel  # noqa: E402
from scripts.breakout_rules import run_rules  # noqa: E402
from scripts.krx_daily_fetch import DEFAULT_CACHE  # noqa: E402

SPEC = "docs/superpowers/specs/2026-09-30-h5-daily-breakout-hypothesis.md"
CODE_FILES = (
    "scripts/breakout_sieve.py",
    "scripts/breakout_panel.py",
    "scripts/breakout_rules.py",
    "scripts/breakout_gates.py",
    "scripts/krx_daily_fetch.py",
)
DEFAULT_LOG = ROOT / "data" / "h5" / "verdict_log.jsonl"
MAX_MISSING_RATIO = 0.01
VERDICT = "VERDICT"
INCOMPLETE_DATA = "INCOMPLETE_DATA"


class SealError(RuntimeError):
    """봉인 조건을 어겼다 — 판정을 계산하지 않는다."""


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def check_clean(root: Path) -> None:
    if _git(root, "status", "--porcelain"):
        raise SealError("워킹트리가 깨끗하지 않다 — 커밋되지 않은 변경으로 판정하지 않는다")


def commit_ids(root: Path) -> dict[str, str]:
    return {
        "head": _git(root, "rev-parse", "HEAD"),
        "spec": _git(root, "log", "-1", "--format=%H", "--", SPEC),
        "code": _git(root, "log", "-1", "--format=%H", "--", *CODE_FILES),
    }


def manifest_sha256(cache_dir: Path) -> tuple[str, int]:
    """캐시 파일 목록(상대 경로·크기)의 SHA-256. 판정이 어떤 데이터를 봤는지 고정한다."""
    digest = hashlib.sha256()
    count = 0
    for path in sorted(cache_dir.rglob("*.json.gz")):
        rel = path.relative_to(cache_dir).as_posix()
        digest.update(f"{rel}|{path.stat().st_size};".encode())
        count += 1
    return digest.hexdigest(), count


def unknown_categories(panel: Panel) -> dict[str, list[str]]:
    unknown = {
        "SECUGRP_NM": sorted(panel.categories.get("SECUGRP_NM", set()) - KNOWN_SECUGRP),
        "KIND_STKCERT_TP_NM": sorted(
            panel.categories.get("KIND_STKCERT_TP_NM", set()) - KNOWN_KIND),
    }
    return {field: values for field, values in unknown.items() if values}


def verdict_runs(log_path: Path) -> int:
    if not log_path.exists():
        return 0
    return sum(
        1 for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("status") == VERDICT
    )


def append_log(log_path: Path, record: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8", newline="") as fh:
        print(json.dumps(record, ensure_ascii=False), file=fh)


def close_on(panel: Panel) -> Callable[[str, str], float | None]:
    def lookup(ticker: str, day: str) -> float | None:
        s = panel.series.get(ticker)
        if s is None:
            return None
        i = s.pos(day)
        return s.adj_close(i) if i >= 0 else None

    return lookup


def top_events(panel: Panel, limit: int = 10) -> list[dict]:
    events = [
        (abs(math.log(f)), t, d, f)
        for t, s in panel.series.items() for d, f in s.events if f > 0
    ]
    events.sort(reverse=True)
    return [{"ticker": t, "date": d, "factor": f} for _, t, d, f in events[:limit]]


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else math.nan


def run_verdict(
    root: Path,
    cache_dir: Path,
    log_path: Path,
    rerun_reason: str = "",
    *,
    load: Callable[[Path], Panel] = load_panel,
) -> dict:
    check_clean(root)
    prior = verdict_runs(log_path)
    if prior and not rerun_reason.strip():
        raise SealError(
            f"판정 기록이 이미 {prior}건 있다 — 다시 돌리려면 --rerun-reason 이 필요하다")
    base_record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "commits": commit_ids(root),
        "rerun_reason": rerun_reason.strip(),
        "prior_verdicts": prior,
    }

    panel = load(cache_dir)
    ratio = panel.missing_ratio()
    if ratio > MAX_MISSING_RATIO:
        append_log(log_path, {**base_record, "status": INCOMPLETE_DATA, "missing_ratio": ratio,
                              "missing_days": len(panel.missing)})
        raise SealError(f"결측 {ratio:.2%} — 한도 1% 초과. 다시 받은 뒤 실행한다")
    unknown = unknown_categories(panel)
    if unknown:
        raise SealError(f"처음 보는 범주값 {unknown} — 스펙에 처리를 기록한 뒤 다시 실행한다")
    digest, files = manifest_sha256(cache_dir)

    run = run_rules(panel)
    samples, dropped = samples_from_trades(run.trades)
    matched_sizes = [t.matched_n for t in run.trades if t.matched_n > 0]
    record = {
        **base_record,
        "status": VERDICT,
        "manifest_sha256": digest,
        "manifest_files": files,
        "missing_ratio": ratio,
        "unknown_days": len(panel.unknown_days),
        "verdict": evaluate(samples),
        "recorded": {
            **recorded_metrics(samples),
            "trades_entered": len(run.trades),
            "dropped": dict(dropped),
            "counts": dict(run.counts),
            "event_study": {
                "n": len(run.event_net),
                "mean_net": _mean(run.event_net),
                "mean_excess": _mean(run.event_excess),
            },
            "universe_size_by_year": {
                year: {"min": min(v), "median": statistics.median(v), "max": max(v)}
                for year, v in sorted(run.universe_sizes.items())
            },
            "matched_control_size": {
                "min": min(matched_sizes) if matched_sizes else 0,
                "median": statistics.median(matched_sizes) if matched_sizes else 0,
            },
            "corporate_events": sum(len(s.events) for s in panel.series.values()),
            "top_events": top_events(panel),
            "no_basis_rows": sum(s.no_basis for s in panel.series.values()),
            "equity": equity_curve_mdd(samples, close_on(panel), panel.calendar),
            "sect_values": sorted(panel.categories.get("SECT_TP_NM", set())),
        },
    }
    append_log(log_path, record)
    return record


def print_report(record: dict) -> None:
    v = record["verdict"]
    print(f"판정: {'통과' if v['passed'] else '실패'} — 완료 거래 {v['n']}건")
    for gate, ok in v["gates"].items():
        print(f"  {gate}: {'통과' if ok else '실패'}")
    print(f"  A 평균 {v['mean_net']:+.4%} · X 평균 {v['mean_excess']:+.4%} · "
          f"X CI 하한 {v['lower_excess']:+.4%}")
    print(f"  상위 1% 제거 — A {v['trimmed_net']:+.4%} · X {v['trimmed_excess']:+.4%}")
    for period, mean in v["period_excess"].items():
        print(f"  {period} X 평균 " + ("없음" if mean is None else f"{mean:+.4%}"))
    print(f"  HEAD {record['commits']['head'][:7]} · 매니페스트 {record['manifest_sha256'][:12]}")
    print("  스펙 §9 표로 옮긴다.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="H5 봉인 판정 (스펙 §6.4)")
    parser.add_argument("--verdict", action="store_true",
                        help="봉인된 판정을 실행한다 — 실데이터를 읽는다")
    parser.add_argument("--rerun-reason", default="", help="두 번째 이후 실행의 사유")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)

    if not args.verdict:
        print("실데이터 판정은 --verdict 로만 한다 (스펙 §6.4). 개발·테스트는 합성 픽스처를 쓴다.")
        return 2
    try:
        record = run_verdict(args.root, args.cache_dir, args.log, args.rerun_reason)
    except SealError as exc:
        print(f"봉인 거부: {exc}", file=sys.stderr)
        return 3
    print_report(record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 통과 확인**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_breakout_sieve.py -q && ./.venv/Scripts/python.exe -m ruff check scripts/breakout_sieve.py tests/test_breakout_sieve.py`
Expected: 전부 PASS, `All checks passed!`

- [ ] **Step 5: 커밋**

```bash
git add scripts/breakout_sieve.py tests/test_breakout_sieve.py
git commit -m "feat(h5): seal the verdict run behind a clean tree, completeness and category checks"
```

---

### Task 8: 전체 검증과 푸시

**Files:** 없음(검증만). 문제가 나오면 해당 Task 파일을 고친다.

- [ ] **Step 1: preflight**

Run (PowerShell): `Set-Location D:\Private\stock; .\scripts\preflight.ps1`
Expected: ruff 0건, mypy 새 오류 없음, pytest 전부 통과, `사전 검사 통과`. 전략 지문은 **직전 값 그대로**여야 한다 — 이 계획의 어떤 파일도 `_STRATEGY_FILES`가 아니다. 지문이 바뀌었다면 멈추고 원인을 찾는다.

- [ ] **Step 2: 실데이터를 읽는 코드가 봉인 밖에 없는지 확인**

Run: `grep -n "load_panel" scripts/*.py`
Expected: `breakout_panel.py`(정의)와 `breakout_sieve.py`(`run_verdict` 기본값) 두 곳뿐.

- [ ] **Step 3: 푸시**

```bash
git push origin main
```

운영 승격은 필요 없다. 수집과 판정은 개발 트리에서만 돌고, 운영 매매 코드와 지문은 바뀌지 않는다.

---

### Task 9: 수집 실행 — 실제 KRX 호출 (이틀)

**Files:** 없음. 캐시는 `data/krx_daily/`(gitignore, 백업 제외)에 쌓인다.

KRX 호출은 KIS 유량과 무관하지만, 같은 PC에서 운영 봇이 돌므로 **장 마감 뒤(15:45 이후)**에 돌린다. 이 Task는 저장만 한다 — **캐시를 열어 신고가나 수익률을 보지 않는다**(스펙 §7).

- [ ] **Step 1: 사용자에게 수집 시작을 알린다** — 약 9,136건, 이틀, 1일차 약 5시간.

- [ ] **Step 2: 1일차 실행**

Run: `./.venv/Scripts/python.exe scripts/krx_daily_fetch.py`
Expected: 마지막 줄 부근에 `호출 9000 · 저장 … · 실패 …` 와 `멈춤: BUDGET`. `멈춤: AUTH: …`면 키나 서비스 승인 문제다 — 문구로 가른다(`Unauthorized Key` = 키, `Unauthorized API Call` = 서비스 미승인).

- [ ] **Step 3: 2일차 실행** (다음 날, 같은 명령)

Expected: 호출 수백 건, `멈춤` 없음. 실패로 빠진 날은 파일이 없으므로 이 실행이 다시 받는다.

- [ ] **Step 4: 파일 수만으로 완전성 확인** — 성과는 보지 않는다

```bash
./.venv/Scripts/python.exe - <<'PY'
from scripts.krx_daily_fetch import DAILY_KINDS, DEFAULT_CACHE, END, START, cache_path, weekdays

days = weekdays(START, END)
absent = [(k, d) for d in days for k in DAILY_KINDS if not cache_path(DEFAULT_CACHE, k, d).exists()]
base = sorted(p.name for p in (DEFAULT_CACHE / "base" / "stk").glob("*.json.gz"))
print("평일", len(days), "· 없는 일별 파일", len(absent), "· 월 기본정보(유가증권)", len(base))
print("없는 파일 처음 10개", absent[:10])
failures = DEFAULT_CACHE / "failures.jsonl"
print("실패 기록 줄 수", len(failures.read_text(encoding="utf-8").splitlines()) if failures.exists() else 0)
PY
```

Expected: 없는 일별 파일 0(또는 극소수), 월 기본정보 201개. 없는 파일이 남으면 Step 3 명령을 한 번 더 돌린다. 계속 실패하는 날은 그대로 두고 Task 10의 결측 한도(평일의 1%)가 판단한다.

---

### Task 10: 봉인된 판정 실행 — 딱 한 번

**Files:**
- Modify: `docs/superpowers/specs/2026-09-30-h5-daily-breakout-hypothesis.md` (§9 판정 기록, 실패면 §10)

- [ ] **Step 1: 실행 전 사용자 확인** — 판정은 한 번뿐이다. 반드시 사용자에게 "지금 판정을 돌린다"고 확인받는다. 장 마감 뒤에 돌린다(메모리 약 1GB, 수~수십 분).

- [ ] **Step 2: 워킹트리가 깨끗한지 확인**

Run: `git status --short`
Expected: 출력 없음. 있으면 커밋하거나 치운 뒤 진행한다(봉인이 거부한다).

- [ ] **Step 3: 판정**

Run: `./.venv/Scripts/python.exe scripts/breakout_sieve.py --verdict`
Expected: `판정: 통과` 또는 `판정: 실패`와 G1~G5, A·X 평균, X CI 하한, 구간별 X 평균. 종료 코드 0.

봉인 거부(종료 코드 3)면 메시지대로 처리한다.
- `결측 …% — 한도 1% 초과`: Task 9 Step 3을 다시 돌린 뒤 이 Step을 다시 한다(판정은 계산되지 않았으므로 재실행 사유가 필요 없다).
- `처음 보는 범주값 …`: 그 값이 무엇인지 KRX 기본정보에서 확인하고, 스펙 §5.3 "등록 시 확인된 범주값"에 처리를 날짜와 함께 기록·커밋한 뒤, `scripts/breakout_panel.py`의 `KNOWN_SECUGRP`/`KNOWN_KIND`에 반영하고 다시 실행한다. 허용 목록("주권"·"보통주")은 바꾸지 않는다.

- [ ] **Step 4: 결과를 스펙 §9 표에 옮긴다**

`data/h5/verdict_log.jsonl`의 마지막 `"status": "VERDICT"` 줄에서 날짜, `commits.head`(7자리), `verdict.n`, `mean_net`·`mean_excess`·`lower_excess`, G1~G5, 통과/실패를 §9 표의 `(판정 전)` 행 자리에 적는다. 표 아래에 기록 지표 요약(승률, 중앙값, X_eq 평균, 두 대조군 차이, 전 신호 이벤트 연구 평균, 자금 곡선 최대 낙폭, 진입 불가·소멸·미완료 건수)을 짧게 붙인다.

실패면 §10의 인용 문구를 그대로 "닫힌 축" 기록으로 옮기고(검출력 한계 포함), H1 스펙 §6 목록에도 한 줄로 추가한다.

- [ ] **Step 5: 커밋과 푸시**

```bash
git add docs/superpowers/specs/2026-09-30-h5-daily-breakout-hypothesis.md docs/superpowers/specs/2026-09-11-h1-gap-selection-hypothesis.md
git commit -m "docs(h5): record the sealed verdict"
git push origin main
```

(통과면 H1 스펙은 바뀌지 않으므로 `git add`에서 뺀다.)

- [ ] **Step 6: 다음 단계 안내** — 통과면 스펙 §8대로 PAPER 실매매 트랙 스펙을 새로 쓴다(진입 신호만 검증됐다는 점, 약관의 "비상업" 해석을 KRX에 확인할 것). 실패면 이 축은 닫혔다.
