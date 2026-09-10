# 트랙 B 재생 하네스 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 로그에 남은 52거래일의 종목 목록을 복원해, 트랙 B 진입 규칙 하나를 몇 분 만에 시험할 수 있게 한다.

**Architecture:** 조각 셋. 신규는 하나뿐이다 — 로그에서 (날짜, 순위, 종목)을 뽑는 복원기. 나머지 둘은 기존 스크립트에 입력 경로를 하나씩 여는 것이다. `track_b_backfill.py`는 복원된 유니버스 파일을 받게, `track_b_backtest.py`는 이미 매겨진 순위를 그대로 받게 한다. 산출물(유니버스 JSON, 분봉 캐시) 둘 다 재생성 가능하므로 새로운 재생 불가 자산을 만들지 않는다.

**Tech Stack:** Python 3.12, pytest (asyncio_mode=auto), ruff(line-length 100), mypy. 기존 `scripts/` 관례를 따른다.

**Spec:** `docs/superpowers/specs/2026-09-11-track-b-replay-harness-design.md`

## Global Constraints

- **전략 파일을 수정하지 않는다.** `src/release.py`의 `_STRATEGY_FILES` 20개 중 어느 것도 건드리지 않는다. 이 계획이 만지는 파일은 전부 `scripts/`와 `tests/`다. 지문은 `10a02f6a44b3`으로 유지된다.
- **운영 트리에 쓰지 않는다.** `D:\Private\stock-prod` 아래 어떤 경로에도 쓰지 않는다. 로그는 읽기만 한다.
- **운영 프로세스를 건드리지 않는다.** `main.pid`, `.stock-role`, `.env*`에 손대지 않는다.
- **테스트는 실제 KIS를 호출하지 않는다.** 응답은 목으로 넣는다.
- **테스트는 `tmp_path`만 쓴다.** 실제 `data/` 아래에 아무것도 쓰지 않는다.
- **줄 길이 100자(코드포인트 기준).** 한글 주석은 바이트가 아니라 글자 수로 센다.
- **PowerShell 5.1 제약은 이 계획에 해당 없음** — 새로 만드는 것은 Python뿐이다.
- 커밋 메시지 끝에 `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>` 를 넣는다.

## File Structure

| 파일 | 책임 |
|---|---|
| `scripts/replay_universe.py` (신규) | 로그 → 순위 보존 유니버스 JSON. 순수 파싱 + CLI. |
| `tests/test_replay_universe.py` (신규) | 위의 파싱 규칙 검증. |
| `scripts/track_b_backfill.py` (수정) | `--universes` 입력 경로 추가. 페이징·캐시는 그대로. |
| `tests/test_track_b_backfill.py` (수정) | 새 입력 경로 검증. |
| `scripts/track_b_backtest.py` (수정) | `ranked_tickers` 주입 경로 추가. |
| `tests/test_track_b_backtest.py` (수정) | 주입 경로 검증. |

---

### Task 1: 유니버스 복원기

**Files:**
- Create: `scripts/replay_universe.py`
- Test: `tests/test_replay_universe.py`

**Interfaces:**
- Consumes: 없음 (첫 태스크)
- Produces:
  - `restore_universes(log_dirs: list[Path]) -> dict[str, list[str]]`
    — 날짜(`YYYYMMDD`) → 순위 순서가 보존된 종목 코드 리스트
  - `build_document(days: dict[str, list[str]], log_dirs: list[Path]) -> dict`
    — 파일로 쓸 JSON 문서
  - `main(argv: list[str] | None = None) -> int` — CLI

**배경 (구현자가 알아야 할 것):**

운영 로그는 하루 한 개의 JSON Lines 파일이다(`data/logs/20260910.jsonl`). 각 줄이 이벤트 하나이고, 우리가 쓰는 것은 `event == "TARGET_LOCKED"` 한 종류다. 이 이벤트에는 `target_tickers` 필드가 F1 랭크 순서대로 최대 3개 들어 있다.

주의할 실제 데이터 특성 세 가지다.

1. **하루에 여러 번 난다.** F2가 재시도하면 종목이 바뀐다. 실제 진입을 시도한 조합은 마지막 것이다.
2. **`target_tickers`가 없는 날이 있다.** 필드가 도중에 추가되어 2026-06-29 로그에는 없다(`None`).
3. **`target_tickers`가 빈 리스트인 이벤트가 있다.** 전체 154건 중 8건.

따라서 규칙은 **"그날의 이벤트 중 `target_tickers`가 비어 있지 않은 마지막 것"** 이다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_replay_universe.py`를 만든다.

```python
import json
from pathlib import Path

from scripts.replay_universe import build_document, restore_universes


def _write_log(directory: Path, date: str, events: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e, ensure_ascii=False) for e in events]
    (directory / f"{date}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _locked(tickers) -> dict:
    return {"event": "TARGET_LOCKED", "target_tickers": tickers}


def test_takes_the_last_nonempty_lock_of_the_day(tmp_path):
    """F2가 재시도하면 종목이 바뀐다. 실제 진입을 시도한 조합은 마지막 것이다."""
    _write_log(tmp_path, "20260910", [
        _locked(["111111", "222222"]),
        {"event": "F1_DONE", "passed": 10},
        _locked(["333333", "444444", "555555"]),
    ])

    assert restore_universes([tmp_path]) == {"20260910": ["333333", "444444", "555555"]}


def test_skips_empty_and_missing_ticker_lists(tmp_path):
    """필드가 없던 초기 로그(2026-06-29)와 빈 목록 이벤트를 건너뛴다."""
    _write_log(tmp_path, "20260629", [{"event": "TARGET_LOCKED"}])
    _write_log(tmp_path, "20260630", [_locked([])])
    _write_log(tmp_path, "20260701", [_locked([]), _locked(["777777"])])

    assert restore_universes([tmp_path]) == {"20260701": ["777777"]}


def test_ignores_unparseable_lines(tmp_path):
    """로그가 잘린 채 끝날 수 있다. 그 줄만 버리고 나머지는 살린다."""
    directory = tmp_path / "logs"
    directory.mkdir()
    (directory / "20260910.jsonl").write_text(
        json.dumps(_locked(["111111"])) + "\n{ 잘린 줄\n", encoding="utf-8"
    )

    assert restore_universes([directory]) == {"20260910": ["111111"]}


def test_later_directory_wins_for_the_same_date(tmp_path):
    """운영과 개발 보관본에 같은 날짜가 있으면 나중에 준 디렉터리를 쓴다."""
    old, new = tmp_path / "old", tmp_path / "new"
    _write_log(old, "20260910", [_locked(["111111"])])
    _write_log(new, "20260910", [_locked(["999999"])])

    assert restore_universes([old, new]) == {"20260910": ["999999"]}


def test_document_records_provenance(tmp_path):
    """언제 어디서 만들었는지 남는다 — 재생성 가능한 산출물임을 드러낸다."""
    doc = build_document({"20260910": ["111111"]}, [tmp_path])

    assert doc["days"] == {"20260910": [{"rank": 1, "ticker": "111111"}]}
    assert doc["source_dirs"] == [str(tmp_path)]
    assert doc["day_count"] == 1
    assert doc["pair_count"] == 1
    assert doc["generated_at"].endswith("+09:00")
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv\Scripts\python.exe -m pytest tests/test_replay_universe.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.replay_universe'`

- [ ] **Step 3: 복원기를 구현한다**

`scripts/replay_universe.py`를 만든다.

```python
"""운영 로그에서 트랙 B 재생용 유니버스를 복원한다 — 읽기 전용.

data/f1_snapshots 는 09:00 실시간 예상체결 상태라 그 시각에만 존재하고,
2026-09-03에 `git worktree remove` 가 정션을 따라가 통째로 지웠다. 남은 것은
로그뿐이다. TARGET_LOCKED 이벤트가 F1 랭크 순서대로 최대 3종목을 남긴다.

종목 코드와 순서만 남고 갭·대금은 1위 것뿐이라, f1_selector.rank_candidates()
를 다시 돌릴 수는 없다. 이미 매겨진 순위를 그대로 재사용한다(스펙 §2.3).

이 산출물은 로그에서 언제든 재생성된다. 소실돼도 손실이 아니다.

    python scripts/replay_universe.py --out data/replay/universes.json \
        --log-dir data/logs.migrated --log-dir D:/Private/stock-prod/data/logs
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

KST = ZoneInfo("Asia/Seoul")
LOCK_EVENT = "TARGET_LOCKED"


def _last_nonempty_lock(path: Path) -> list[str] | None:
    """하루치 로그에서 종목이 담긴 마지막 TARGET_LOCKED 를 고른다.

    F2가 재시도하면 종목이 바뀐다. 실제 진입을 시도한 조합은 마지막 것이다.
    target_tickers 필드는 도중에 추가되어 초기 로그에는 없다(2026-06-29).
    """
    found: list[str] | None = None
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # 로그가 잘린 채 끝날 수 있다. 그 줄만 버린다.
                continue
            if not isinstance(event, dict) or event.get("event") != LOCK_EVENT:
                continue
            tickers = event.get("target_tickers")
            if isinstance(tickers, list) and tickers:
                found = [str(t) for t in tickers if t]
    return found


def restore_universes(log_dirs: list[Path]) -> dict[str, list[str]]:
    """날짜 → 순위 순서가 보존된 종목 코드. 같은 날짜는 나중 디렉터리가 이긴다."""
    days: dict[str, list[str]] = {}
    for directory in log_dirs:
        for path in sorted(Path(directory).glob("*.jsonl")):
            date = path.name[:8]
            if not (len(date) == 8 and date.isdigit()):
                continue
            tickers = _last_nonempty_lock(path)
            if tickers:
                days[date] = tickers
    return days


def build_document(days: dict[str, list[str]], log_dirs: list[Path]) -> dict:
    return {
        "generated_at": datetime.now(KST).isoformat(),
        "source_dirs": [str(d) for d in log_dirs],
        "day_count": len(days),
        "pair_count": sum(len(v) for v in days.values()),
        "days": {
            date: [{"rank": i, "ticker": t} for i, t in enumerate(tickers, start=1)]
            for date, tickers in sorted(days.items())
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="로그에서 트랙 B 재생 유니버스 복원")
    parser.add_argument(
        "--log-dir", action="append", default=None, type=Path,
        help="로그 디렉터리. 여러 번 줄 수 있고, 나중 것이 같은 날짜를 덮는다",
    )
    parser.add_argument(
        "--out", type=Path, default=ROOT / "data" / "replay" / "universes.json",
    )
    args = parser.parse_args(argv)

    log_dirs = args.log_dir or [ROOT / os.getenv("LOG_DIR", "data/logs")]
    days = restore_universes(log_dirs)
    document = build_document(days, log_dirs)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    dates = sorted(days)
    span = f"{dates[0]} ~ {dates[-1]}" if dates else "-"
    print(
        f"{document['day_count']}거래일 / {document['pair_count']}쌍  ({span})"
        f"  → {args.out}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv\Scripts\python.exe -m pytest tests/test_replay_universe.py -q`
Expected: PASS, 5 passed

- [ ] **Step 5: 실제 로그로 돌려 스펙의 숫자를 확인한다**

Run:
```bash
.venv/Scripts/python.exe scripts/replay_universe.py \
  --log-dir data/logs.migrated \
  --log-dir D:/Private/stock-prod/data/logs \
  --out data/replay/universes.json
```
Expected: `52거래일 / 149쌍  (20260701 ~ 20260910)` 형태의 출력.

일수가 52가 아니면 멈추고 보고한다. 스펙 §2.2가 이 숫자를 근거로 쓰였으므로, 어긋나면 스펙이 틀렸거나 로그가 바뀐 것이다.

- [ ] **Step 6: 린트와 타입 검사**

Run: `.venv\Scripts\python.exe -m ruff check scripts/replay_universe.py tests/test_replay_universe.py`
Expected: `All checks passed!`

Run: `.venv\Scripts\python.exe scripts/mypy_baseline.py`
Expected: `새 mypy 오류 없음`

- [ ] **Step 7: 커밋**

`data/replay/universes.json` 은 커밋하지 않는다 — `data/` 는 gitignore 대상이고, 이 파일은 로그에서 언제든 재생성된다.

```bash
git add scripts/replay_universe.py tests/test_replay_universe.py
git commit -F - <<'EOF'
feat(replay): restore Track B universes from the operational logs

data/f1_snapshots holds the 09:00 expected-fill state, which exists only
at that instant, and the 2026-09-03 worktree accident took every one of
them. The logs are what survived: TARGET_LOCKED carries the F1-ranked
tickers, and 52 trading days of them are still on disk.

Three properties of the real data drive the parsing rule. F2 retries, so
a day can hold several locks and only the last one names the tickers
actually attempted. The target_tickers field was added partway through,
so 2026-06-29 has none. And eight of the 154 events carry an empty list.
Hence: the last lock of the day whose ticker list is non-empty.

Only the codes and their order survive -- gap and amount are recorded for
the winner alone -- so F1 ranking cannot be recomputed from this. The
restored order is reused as-is, which the selection spec already permits
by fixing F1 as a control variable.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 2: 수급기가 복원된 유니버스를 받게 한다

**Files:**
- Modify: `scripts/track_b_backfill.py` (`needed_pairs`, `main_async`)
- Test: `tests/test_track_b_backfill.py`

**Interfaces:**
- Consumes: Task 1의 `build_document()` 출력 형식 — `{"days": {"YYYYMMDD": [{"rank": 1, "ticker": "005930"}, ...]}}`
- Produces: `needed_pairs(depth, snapshot_dir=None, warmup_days=0, universes_path=None) -> dict[str, set[str]]`

**배경:**

`needed_pairs`는 지금 `load_universes()`로 `data/f1_snapshots`를 읽고 `f1_selector.rank_candidates(rows)[:depth]`로 순위를 매긴다. 복원된 유니버스에는 순위를 다시 매길 속성이 없으므로(스펙 §2.3), 순위를 그대로 받는 두 번째 입력 경로를 연다.

기존 경로는 그대로 둔다. `f1_snapshots` 원본이 다시 쌓이고 있으므로 둘 다 필요하다.

워밍업 로직(`warmup_days`)은 입력 출처와 무관하게 그대로 적용된다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_track_b_backfill.py` 끝에 붙인다.

```python
def test_needed_pairs_reads_a_restored_universe_file(tmp_path):
    """복원된 유니버스에는 순위를 다시 매길 속성이 없다. 순서를 그대로 쓴다."""
    path = tmp_path / "universes.json"
    path.write_text(
        json.dumps({
            "days": {
                "20260910": [
                    {"rank": 1, "ticker": "111111"},
                    {"rank": 2, "ticker": "222222"},
                    {"rank": 3, "ticker": "333333"},
                ]
            }
        }),
        encoding="utf-8",
    )

    needed = needed_pairs(depth=2, universes_path=path)

    assert needed == {"20260910": {"111111", "222222"}}


def test_needed_pairs_restored_universe_still_adds_warmup_days(tmp_path):
    """워밍업은 입력 출처와 무관하게 적용된다."""
    path = tmp_path / "universes.json"
    path.write_text(
        json.dumps({
            "days": {
                "20260909": [{"rank": 1, "ticker": "999999"}],
                "20260910": [{"rank": 1, "ticker": "111111"}],
            }
        }),
        encoding="utf-8",
    )

    needed = needed_pairs(depth=5, universes_path=path, warmup_days=1)

    assert needed["20260910"] == {"111111"}
    assert needed["20260909"] == {"999999", "111111"}
```

`needed_pairs` 가 이미 import 되어 있는지 확인한다. 없으면 파일 상단의 `from scripts.track_b_backfill import (...)` 목록에 더한다.

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv\Scripts\python.exe -m pytest tests/test_track_b_backfill.py -k restored -q`
Expected: FAIL — `TypeError: needed_pairs() got an unexpected keyword argument 'universes_path'`

- [ ] **Step 3: 입력 경로를 연다**

`scripts/track_b_backfill.py`의 `needed_pairs`를 바꾼다. 기존 시그니처는

```python
def needed_pairs(
    depth: int = 5, snapshot_dir: Path | None = None, warmup_days: int = 0
) -> dict[str, set[str]]:
```

이것을 아래로 바꾼다. 워밍업 블록(`if warmup_days > 0:` 이하)은 손대지 않는다.

```python
def load_restored_universes(path: Path) -> dict[str, list[str]]:
    """replay_universe.py 산출물 → 날짜별 순위 순서 종목 목록.

    rank 필드를 신뢰하지 않고 명시적으로 정렬한다. 파일을 손으로 고쳤을 때
    순서가 조용히 뒤바뀌면 어느 종목을 본 것인지 알 수 없게 된다.
    """
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    days: dict[str, list[str]] = {}
    for date, rows in (document.get("days") or {}).items():
        ordered = sorted(rows, key=lambda r: int(r["rank"]))
        tickers = [str(r["ticker"]) for r in ordered if r.get("ticker")]
        if tickers:
            days[str(date)] = tickers
    return days


def needed_pairs(
    depth: int = 5,
    snapshot_dir: Path | None = None,
    warmup_days: int = 0,
    universes_path: Path | None = None,
) -> dict[str, set[str]]:
    """날짜별 F1 랭크 1~depth 종목.

    ``universes_path`` 가 있으면 복원된 유니버스를 쓴다. 그 파일에는 순위를
    다시 매길 속성이 없으므로(재생 스펙 §2.3) 기록된 순서를 그대로 자른다.
    없으면 f1_snapshots 를 읽어 운영 랭킹 함수를 돌린다.

    ``warmup_days``가 0보다 크면 각 종목의 전 거래일 쌍을 함께 대상에 넣는다.
    지표 워밍업이 그 봉을 필요로 하는데, 그 종목이 그날 F1 상위에 없었으면
    캐시에 없기 때문이다(스펙 §5.1).
    """
    needed: dict[str, set[str]] = {}
    if universes_path is not None:
        restored = load_restored_universes(universes_path)
        all_dates = sorted(restored)
        for date, tickers in restored.items():
            picked = set(tickers[:depth])
            if picked:
                needed[date] = picked
    else:
        universes = (
            load_universes(snapshot_dir) if snapshot_dir is not None else load_universes()
        )
        all_dates = sorted(universes)
        for date, rows in universes.items():
            ranked = f1_selector.rank_candidates(rows)[:depth]
            tickers_set = {str(r["ticker"]) for r in ranked if r.get("ticker")}
            if tickers_set:
                needed[date] = tickers_set

    if warmup_days > 0:
        for date in list(needed):
            cursor = date
            for _ in range(warmup_days):
                previous = previous_trading_date(all_dates, cursor)
                if previous is None:
                    break
                cursor = previous
                needed.setdefault(cursor, set()).update(needed[date])
    return needed
```

`json` 이 이미 import 되어 있는지 확인한다. 없으면 `import json` 을 표준 라이브러리 블록에 더한다.

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv\Scripts\python.exe -m pytest tests/test_track_b_backfill.py -q`
Expected: PASS — 새 2건 포함, 기존 테스트도 전부 통과해야 한다. 특히 `test_needed_pairs_uses_operational_ranking` 과 워밍업 테스트 2건이 깨지지 않았는지 본다.

- [ ] **Step 5: CLI에 인자를 더한다**

`main_async`의 argparse 블록에 더한다.

```python
    parser.add_argument(
        "--universes", type=Path, default=None,
        help="replay_universe.py 산출물. 주면 f1_snapshots 대신 이것을 쓴다",
    )
```

그리고 `needed_pairs` 호출을 바꾼다.

```python
    needed = needed_pairs(
        args.depth, warmup_days=args.warmup_days, universes_path=args.universes
    )
```

출력 줄도 출처를 드러내게 바꾼다.

```python
    source = str(args.universes) if args.universes else "data/f1_snapshots"
    pairs = sum(len(v) for v in needed.values())
    print(f"대상 {len(needed)}거래일 / {pairs}쌍 (랭크 1~{args.depth}, 출처 {source})")
```

- [ ] **Step 6: dry-run으로 계획을 확인한다**

Run:
```bash
.venv/Scripts/python.exe scripts/track_b_backfill.py \
  --universes data/replay/universes.json --warmup-days 0 --dry-run
```
Expected: `대상 52거래일 / 149쌍 (랭크 1~5, 출처 data/replay/universes.json)`

`--dry-run` 은 KIS를 호출하지 않는다. 15:40 이전이어도 돈다 — `assert_backfill_window` 는 dry-run 반환 뒤에 있다.

- [ ] **Step 7: 검증된 사실을 주석에 반영한다**

`src/api/kis_minute_bars.py:133`의 docstring이 아직 이렇게 돼 있다.

```python
    """일별 분봉 한 페이지. 과거 관측일 소급용이며 가용성은 미검증이다."""
```

2026-09-11에 실측했으므로 바꾼다.

```python
    """일별 분봉 한 페이지 — 과거 관측일 소급용.

    2026-09-11 실측(005930): 343일 전까지 정상 응답, 435일 전은 0봉. 한 종목
    하루는 커서를 앞으로 밀며 4호출이면 09:00~15:30 전 구간이 덮인다.
    """
```

`src/api/kis_minute_bars.py`는 `src/release.py`의 `_STRATEGY_FILES`에 없다. 주석만
바꾸는 것이지만, 만약 목록에 있었다면 지문이 바뀌므로 손대면 안 됐을 파일이다.
Step 8에서 지문이 그대로인지 확인한다.

- [ ] **Step 8: 린트와 타입 검사**

Run: `.venv\Scripts\python.exe -m ruff check scripts/track_b_backfill.py tests/test_track_b_backfill.py src/api/kis_minute_bars.py`
Expected: `All checks passed!`

Run: `.venv\Scripts\python.exe scripts/mypy_baseline.py`
Expected: `새 mypy 오류 없음`

Run:
```bash
.venv/Scripts/python.exe -c "from dotenv import load_dotenv; load_dotenv(); from src.release import strategy_fingerprint; print(strategy_fingerprint())"
```
Expected: `10a02f6a44b3`

- [ ] **Step 9: 커밋**

```bash
git add scripts/track_b_backfill.py tests/test_track_b_backfill.py src/api/kis_minute_bars.py
git commit -F - <<'EOF'
feat(replay): let the backfill read a restored universe

needed_pairs ranked its input with f1_selector.rank_candidates, which
needs each candidate's gap and amount. A universe restored from the logs
carries neither -- only the codes and the order they were locked in -- so
it gets a second entry path that slices the recorded order instead.

The f1_snapshots path stays exactly as it was. Snapshots are accumulating
again after the 2026-09-03 loss, so both sources will matter: 52 restored
days now, and full-attribute days from here on.

Warmup is unchanged and applies to either source. It walks back through
the dates the chosen source actually has, so a restored universe cannot
invent a trading day the logs never saw.

fetch_daily_minute_bars still carried "availability unverified" in its
docstring. It is verified now: 343 days back returns a full response, 435
returns nothing, and four paginated calls cover one ticker's session.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

### Task 3: 스윕이 이미 매겨진 순위를 받게 한다

**Files:**
- Modify: `scripts/track_b_backtest.py` (`simulate_day`, `run_axis`)
- Test: `tests/test_track_b_backtest.py`

**Interfaces:**
- Consumes: Task 2의 `load_restored_universes(path) -> dict[str, list[str]]`
- Produces: `simulate_day(..., ranked_tickers: list[str] | None = None)` — 주면 `rank_candidates`를 부르지 않는다

**배경:**

`simulate_day`는 `universe`를 오직 한 곳에서만 쓴다 — `f1_selector.rank_candidates(universe)[:depth]`. 그 결과인 `ranked_tickers` 이후로는 종목 목록과 분봉만 쓴다. 따라서 목록을 직접 주는 경로를 열면 `universe`는 빈 리스트여도 된다.

`run_axis`는 날짜별로 `simulate_day`를 부른다. 여기에도 같은 통로를 뚫어야 복원된 유니버스가 끝까지 흐른다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_track_b_backtest.py` 끝에 붙인다. 파일이 없으면 만들고, 아래 import를 넣는다.

```python
from unittest.mock import patch

from scripts.track_b_backtest import simulate_day


def _bars(n: int = 40) -> list[dict]:
    """09:00부터 1분 간격. 종가가 계속 올라 R1(고점 회복)이 반드시 걸린다."""
    rows = []
    for i in range(n):
        price = 10_000 + i * 10
        rows.append({
            "date": "20260910",
            "time": f"{9 + i // 60:02d}{i % 60:02d}00",
            "open": float(price), "high": float(price + 5),
            "low": float(price - 5), "close": float(price),
            "volume": 1_000,
        })
    return rows


def test_ranked_tickers_bypasses_the_f1_ranking():
    """복원된 유니버스에는 순위를 다시 매길 속성이 없다. 부르면 안 된다."""
    bars = {"111111": _bars()}

    with patch("scripts.track_b_backtest.f1_selector.rank_candidates") as ranker:
        simulate_day(
            "20260910", [], bars, "R1", {},
            ranked_tickers=["111111"],
        )

    ranker.assert_not_called()


def test_ranked_tickers_is_truncated_to_depth():
    """depth 를 넘는 종목은 보지 않는다 — 캐시에 없는 종목을 찾지 않게."""
    bars = {"111111": _bars()}

    simulate_day(
        "20260910", [], bars, "R1", {},
        ranked_tickers=["111111", "222222", "333333"], depth=1,
    )
    # 222222/333333 의 분봉이 없어도 KeyError 없이 끝나야 한다.
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv\Scripts\python.exe -m pytest tests/test_track_b_backtest.py -k ranked_tickers -q`
Expected: FAIL — `TypeError: simulate_day() got an unexpected keyword argument 'ranked_tickers'`

- [ ] **Step 3: 주입 경로를 연다**

`scripts/track_b_backtest.py`의 `simulate_day` 시그니처에 인자를 더하고, 순위 계산부를 바꾼다. 현재 코드는

```python
    ranked = f1_selector.rank_candidates(universe)[:depth]
    ranked_tickers = [str(r["ticker"]) for r in ranked if r.get("ticker")]
```

시그니처의 `warmup_days: int = 1,` 다음 줄에 더한다.

```python
    ranked_tickers: list[str] | None = None,
```

그리고 위 두 줄을 아래로 바꾼다.

```python
    # 복원된 유니버스(재생 스펙 §2.3)에는 순위를 다시 매길 속성이 없다.
    # 기록된 순서를 그대로 자른다.
    if ranked_tickers is None:
        ranked = f1_selector.rank_candidates(universe)[:depth]
        ranked_tickers = [str(r["ticker"]) for r in ranked if r.get("ticker")]
    else:
        ranked_tickers = [str(t) for t in ranked_tickers][:depth]
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv\Scripts\python.exe -m pytest tests/test_track_b_backtest.py -q`
Expected: PASS

- [ ] **Step 5: `run_axis`와 `sign_stability`에 통로를 뚫는다**

`run_axis`는 두 곳에서 불린다 — 축 평가(195행)와 `sign_stability`(관문 2의 부호 검사).
**둘 다 뚫어야 한다.** `sign_stability`를 빠뜨리면 관문 2만 조용히 옛 경로로 돌아
`f1_snapshots` 4일치로 부호를 판정한다.

`run_axis`의 시그니처 끝(`warmup_days: int = 1,` 다음)에 더한다.

```python
    ranked_by_date: dict[str, list[str]] | None = None,
```

그리고 그 안의 `simulate_day` 호출에 전달한다.

```python
        result = simulate_day(
            date, universes[date], bars.get(date, {}), rule_key, params,
            slippage=slippage, warmup_by_ticker=warm.get(date),
            warmup_days=warmup_days,
            ranked_tickers=(ranked_by_date or {}).get(date),
        )
```

`sign_stability`의 시그니처 끝(`warmup: ... = None,` 다음)에도 같은 인자를 더하고,
그 안의 `run_axis` 호출에 넘긴다.

```python
        rows = run_axis(universes, bars, rule_key, params,
                        slippage=slip, warmup=warmup,
                        ranked_by_date=ranked_by_date)
```

`universes[date]`가 `KeyError`를 내지 않도록, 복원 경로에서는 `universes`를
`{date: [] for date in ranked_by_date}` 형태로 넘긴다. 이 조립은 Step 6의 CLI가 한다.

- [ ] **Step 5b: 두 호출 경로가 같은 종목을 보는지 확인하는 테스트**

`tests/test_track_b_backtest.py` 에 더한다.

```python
def test_sign_stability_uses_the_restored_order_too():
    """관문 2가 조용히 옛 경로로 돌면 4일치로 부호를 판정하게 된다."""
    from scripts.track_b_backtest import sign_stability

    bars = {"20260910": {"111111": _bars()}}
    universes = {"20260910": []}

    with patch("scripts.track_b_backtest.f1_selector.rank_candidates") as ranker:
        sign_stability(
            universes, bars, "R1", {},
            ranked_by_date={"20260910": ["111111"]},
        )

    ranker.assert_not_called()
```

- [ ] **Step 6: CLI에 인자를 더한다**

`track_b_backtest.py`의 `main` argparse 블록에 더한다.

```python
    parser.add_argument(
        "--universes", type=Path, default=None,
        help="replay_universe.py 산출물. 주면 f1_snapshots 대신 이것을 쓴다",
    )
```

유니버스를 조립하는 부분에서 분기한다. 현재는 `_lu()`(= `strategy_backtest.load_universes`)를 부른다.

```python
    if args.universes is not None:
        from scripts.track_b_backfill import load_restored_universes

        ranked_by_date = load_restored_universes(args.universes)
        universes = {date: [] for date in ranked_by_date}
    else:
        ranked_by_date = None
        universes = _lu()
```

그리고 `run_axis` 호출(391행)과 `sign_stability` 호출 양쪽에
`ranked_by_date=ranked_by_date`를 넘긴다.

`Path`가 import 되어 있는지 확인한다. 없으면 `from pathlib import Path`를 더한다.

**관문 3은 복원 경로에서 나오지 않는다.** `a_daily_from_baseline()`이 트랙 A의 일별
손익을 내려면 종목별 갭·대금이 필요한데(`strategy_backtest.simulate_day`), 복원된
유니버스에는 없다. 그래서 `--universes` 로 돌리면 `corr_with_a` 와
`a_missing_day_coverage` 는 `f1_snapshots` 에 남은 4일치만 보고 계산되어 사실상
무의미하다. 선정 설계 §5.3이 관문 3을 **선택**으로 규정했으므로 이는 허용된
제약이다. 출력에서 오해하지 않도록, `--universes` 를 준 실행에서는 표 아래에 한 줄을
찍는다.

```python
    if args.universes is not None:
        print(
            "  주의: 복원 유니버스에는 종목 속성이 없어 관문 3(트랙 A 상관)은 "
            "의미 없는 값이다. 관문 1·2·4만 읽어라.",
            flush=True,
        )
```

- [ ] **Step 7: 전체 스위트와 검사**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: 전부 통과. 실행 시간이 60초를 넘으면 멈추고 보고한다 — 정상은 약 50초다.

Run: `.venv\Scripts\python.exe -m ruff check .`
Expected: `All checks passed!`

Run: `.venv\Scripts\python.exe scripts/mypy_baseline.py`
Expected: `새 mypy 오류 없음`

Run:
```bash
.venv/Scripts/python.exe -c "from dotenv import load_dotenv; load_dotenv(); from src.release import strategy_fingerprint; print(strategy_fingerprint())"
```
Expected: `10a02f6a44b3` — 전략 파일을 건드리지 않았으므로 변하면 안 된다.

- [ ] **Step 8: 커밋**

```bash
git add scripts/track_b_backtest.py tests/test_track_b_backtest.py
git commit -F - <<'EOF'
feat(replay): run the sweep against a restored universe

simulate_day touched its universe argument in exactly one place, to rank
it. Everything downstream needs only the ticker list and the bars, so
handing the list in directly lets a universe with no rankable attributes
drive the whole sweep -- which is what the log-restored days are.

run_axis gets the same passage so the restored order reaches every date,
and the CLI grows --universes to select it. The f1_snapshots path is
untouched and still the default.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

## 실행 후 — 사람이 하는 일

계획의 세 태스크가 끝나면 코드는 준비된다. 실제 데이터를 채우는 것은 **15:40 이후**에 사람이 돌린다. 스펙 §5.1의 이유로 장중에는 돌리지 않으며, 우회 스위치는 없다.

```bash
# 1. 유니버스 복원 (KIS 호출 없음, 아무 때나)
.venv/Scripts/python.exe scripts/replay_universe.py \
  --log-dir data/logs.migrated \
  --log-dir D:/Private/stock-prod/data/logs \
  --out data/replay/universes.json

# 2. 분봉 수급 (15:40 이후, 약 11분)
.venv/Scripts/python.exe scripts/track_b_backfill.py \
  --universes data/replay/universes.json --warmup-days 0

# 3. 규칙 스윕 (KIS 호출 없음)
.venv/Scripts/python.exe scripts/track_b_backtest.py \
  --universes data/replay/universes.json
```

SMA·MACD를 쓰는 규칙을 시험할 때는 2번을 `--warmup-days 1`로 다시 돌린다(약 22분). 캐시는 누적되므로 이미 받은 쌍은 건너뛴다.

**결과를 읽는 법은 스펙 §3에 있다.** 이 하네스는 선별기가 아니라 거름망이다. 통과는 "실전 관찰 대상으로 승격"을 뜻하지 "채택"을 뜻하지 않는다.
