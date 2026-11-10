"""캡처된 틱을 다시 흘려 트랙 B 1분 봉을 만든다 (오프라인 백필).

봉 집계는 장중에 실시간으로 돌지만, 그 계층이 붙기 전에 수집된 날은
`data/bars/`에 아무것도 없다. 이 스크립트는 `data/strategy_ticks/<날짜>/`의
캡처 파일을 `src.bars`의 같은 경로(_consume → _flush)로 재생해서 그 날의
봉 파일을 만든다. 실시간 경로와 코드를 공유하므로 결과는 그날 장중에
집계됐을 봉과 같다.

실행 중인 이벤트 루프가 없으므로 `ensure_worker()`는 조용히 무시된다 —
분봉 API 정정 호출은 한 건도 나가지 않는다. 그래서 만들어진 봉은 전부
`confirmed=False`다(틱 집계만으로 만든 값이라는 정직한 표시).

사용:
    python -m scripts.replay_ticks_to_bars 20260827
    python -m scripts.replay_ticks_to_bars 20260827 006340
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import bars  # noqa: E402
from src.utils.jsonl_gz import read_jsonl_gz  # noqa: E402

CAPTURE_DIR = Path("data/strategy_ticks")


def capture_files(date: str, ticker: str = "") -> list[Path]:
    """<종목>.<시>.jsonl.gz 를 종목별·시각순으로 정렬해 돌려준다."""
    day = CAPTURE_DIR / date
    if not day.is_dir():
        return []
    found = []
    for path in day.glob("*.jsonl.gz"):
        parts = path.name.split(".")
        if len(parts) < 3:
            continue
        if ticker and parts[0] != ticker:
            continue
        found.append((parts[0], parts[1], path))
    return [path for _, _, path in sorted(found)]


def tick_from_record(record: dict, ticker: str) -> dict | None:
    """캡처 레코드 → 실시간 경로가 받던 tick dict.

    캡처 파일은 종목을 파일명에 담으므로 레코드 안에 `ticker`가 없다.
    원시 필드의 0번(종목코드)을 우선 쓰고, 없으면 파일명에서 받는다.
    """
    if not isinstance(record, dict):
        return None
    raw = record.get("raw")
    code = raw[0] if isinstance(raw, list) and raw else ticker
    tick = dict(record)
    tick["ticker"] = str(code)
    return tick


def replay(date: str, ticker: str = "") -> dict[tuple[str, str], int]:
    """캡처를 재생하고 (날짜, 종목) → 봉 개수를 돌려준다."""
    files = capture_files(date, ticker)
    if not files:
        return {}

    fed = 0
    for path in files:
        code = path.name.split(".")[0]
        # 잘린 파일은 읽힌 데까지, 끊긴 멤버 뒤의 멤버도 읽는다(src/utils/jsonl_gz.py).
        for record in read_jsonl_gz(path).rows:
            tick = tick_from_record(record, code)
            if tick is not None:
                bars.on_tick(tick)
                fed += 1
        # 파일 단위로 비운다 — 하루치를 전부 메모리에 쌓지 않는다.
        bars.drain()

    print(f"틱 {fed}건 재생 ({len(files)}개 파일)")
    return {key: len(minutes) for key, minutes in bars._series.items()}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    date = sys.argv[1]
    ticker = sys.argv[2] if len(sys.argv) > 2 else ""

    counts = replay(date, ticker)
    if not counts:
        print(f"{date}: 재생할 캡처가 없다 ({CAPTURE_DIR / date})")
        return 1

    for (day, code), count in sorted(counts.items()):
        print(f"{day} {code}: {count}봉 → {bars.bars_path(day, code)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
