"""빠른 경로 날의 F1 유니버스를 프로브 덤프에서 복원한다 — 읽기 전용.

2026-09-11 fast path 승격 뒤 `data/f1_snapshots/<date>_090000.jsonl` 은 통과 후보
(1~3행)만 담는다. `strategy_backtest.load_universes` 는 30행 미만 스냅샷을 버리므로
트랙 B 백필·재생이 그날들을 통째로 놓쳤다(09/10 이후 정지). 그런데 운영은 같은 날
`data/paper_fast_probe/<date>.jsonl` 에 개장 멀티시세 30행(`PAPER_FAST_PROBE_OPEN_MULTI`)을
원문으로 남긴다. 여기서 그 30행을 운영과 같은 변환(`paper_fast_probe._candidate_from_multi`)
으로 되살린다. 결과 행은 스냅샷 행과 같은 키(ticker, gap_pct, expected_amount,
avg_amount_5d, gap_allowed …)를 가지므로 `f1_selector.rank_candidates` 를 그대로 돌릴 수 있다.

`paper_fast_probe.load_persisted_open_candidates` 와 같은 재생 규칙이지만, 그쪽은
승인된 후보만 돌려주고 여기는 **유니버스 전체**를 돌려준다. 운영 모듈은 import 만
한다(지문 대상 파일은 건드리지 않는다).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.modules import f1_selector, paper_fast_probe  # noqa: E402

PROBE_DIR = ROOT / "data" / "paper_fast_probe"


def load_probe_universe(path: Path) -> list[dict]:
    """프로브 파일의 마지막 완주 사이클(PREOPEN_START … OPEN_DONE)에서 개장 유니버스 전체.

    완주하지 못한 사이클(OPEN_DONE 없음)은 무시하고 직전 완주 결과를 남긴다.
    파일이 없거나 완주 사이클이 없으면 빈 리스트.
    """
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    ranking_by_ticker: dict[str, dict] = {}
    market_by_ticker: dict[str, str] = {}
    prepared_by_ticker: dict[str, dict] = {}
    pending_open_rows: list[dict] = []
    universe: list[dict] = []

    for line in lines:
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        event = record.get("event")
        if event == "PAPER_FAST_PROBE_PREOPEN_START":
            # 새 사이클 — 진행 중 상태만 비운다. universe 는 OPEN_DONE 에서만 갱신되므로
            # 완주하지 못한 사이클이 직전 결과를 지우지 않는다.
            ranking_by_ticker = {}
            market_by_ticker = {}
            prepared_by_ticker = {}
            pending_open_rows = []
            continue

        rows = paper_fast_probe._rows(record.get("response") or {})
        if event == "PAPER_FAST_PROBE_RANKING":
            market = str(record.get("market") or "J")
            for row in rows:
                ticker = str(row.get("stck_shrn_iscd") or row.get("mksc_shrn_iscd") or "")
                if ticker:
                    ranking_by_ticker[ticker] = row
                    market_by_ticker[ticker] = market
            continue
        if event == "PAPER_FAST_PROBE_MULTI" and record.get("phase") == "PREOPEN":
            market = str(record.get("market") or "J")
            for row in rows:
                candidate = paper_fast_probe._candidate_from_multi(row, market, ranking_by_ticker)
                if candidate is not None:
                    prepared_by_ticker[str(candidate["ticker"])] = candidate
            continue
        if event == "PAPER_FAST_PROBE_OPEN_MULTI":
            pending_open_rows = rows
            continue
        if event != "PAPER_FAST_PROBE_OPEN_DONE" or not pending_open_rows:
            continue

        parsed: list[dict] = []
        for row in pending_open_rows:
            ticker = str(row.get("inter_shrn_iscd") or "")
            prepared = prepared_by_ticker.get(ticker, {})
            candidate = paper_fast_probe._candidate_from_multi(
                row,
                str(prepared.get("market") or market_by_ticker.get(ticker) or "J"),
                ranking_by_ticker,
            )
            if candidate is None:
                continue
            if prepared:
                candidate["avg_amount_5d"] = prepared.get("avg_amount_5d", 0.0)
            candidate["gap_allowed"] = f1_selector.gap_allowed(candidate)
            candidate["gap_source"] = f"fast.{candidate.get('gap_source', 'multi')}"
            parsed.append(candidate)
        universe = parsed
        pending_open_rows = []

    return universe


def probe_path_for(date: str, probe_dir: Path = PROBE_DIR) -> Path:
    return probe_dir / f"{date}.jsonl"
