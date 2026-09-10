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

    if document["day_count"] == 0:
        searched = ", ".join(str(d) for d in log_dirs)
        print(
            f"오류: 복원된 거래일이 0일이다. 검색한 디렉터리: {searched} — "
            f"{args.out} 은(는) 덮어쓰지 않는다.",
            file=sys.stderr,
        )
        return 1

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
