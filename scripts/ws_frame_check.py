"""WS 체결 프레임 점검 — 캡처 raw로 레코드 길이와 파서 손실을 잰다. 읽기 전용.

2026-09-14 KIS가 H0STCNT0 레코드 끝에 `MARKET_CLS_CODE`를 더했다(46 → 47). 파서
(`src/api/kis_ws._split_records`)는 46을 가정해 다건 프레임을 쪼개지 못하고 첫 체결만
썼다. 11거래일 동안 아무 경보가 없었다 — `docs/WS_47FIELD_PARSER_FOLLOWUP_20261001.md`.

틱 캡처의 ws 행은 프레임 전체 값을 `raw`에 담는다. 그 값으로 두 가지를 잰다.

- 레코드 길이: 모든 조각이 종목코드로 시작하고 6자리 시각이 줄지 않게 되는 가장 작은
  길이를 찾는다. 기대값(`EXPECTED_FIELDS`)과 다르면 KIS가 또 필드를 바꾼 것이다.
- 파서 손실: 파서가 낸 체결 수와 실제 레코드 수를 비교한다.
  - tick-schema-3(2026-10 파서 수정 이후) 행은 운영 파서가 낸 체결 하나씩이다. 그날
    운영의 실제 손실이다. 헤더 건수와 레코드 수의 불일치(`HEADER_MISMATCH`)도 본다.
  - 그 전 행은 프레임 하나다. **이 트리의 현재** 파서에 다시 넣어 센다. 파서를 고친 뒤에는
    옛 날짜도 100%가 된다. 그날 운영이 실제로 본 비율은 프레임 ÷ 체결이다.

장중 파일은 기록 중이라 잘려 있다 — `EOFError`를 삼키고 읽힌 데까지 쓴다.

    .\\.venv\\Scripts\\python.exe scripts\\ws_frame_check.py --root D:\\Private\\stock-prod

과거 날짜는 `--date YYYYMMDD`로 본다. 기본값은 오늘(KST)이다.

종료 코드: 0 이상 없음, 1 이상 발견, 2 그날 캡처 없음.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

KST = ZoneInfo("Asia/Seoul")
# 2026-09-14부터. KIS 공식 예제는 2026-09-28(b76b076)에 같은 컬럼을 반영했다.
EXPECTED_FIELDS = 47
# 단건 프레임은 길이 전체가 한 레코드라 상한이 없으면 무엇이든 "정렬"된다. 47 근처만 본다.
_MIN_FIELDS, _MAX_FIELDS = 30, 60
_HMS = re.compile(r"\d{6}")

Splitter = Callable[[str], list[list[str]]]


def infer_record_length(raw: list[str], ticker: str) -> int | None:
    """프레임 값 배열을 정렬시키는 가장 작은 레코드 길이. 없으면 None."""
    for length in range(_MIN_FIELDS, min(len(raw), _MAX_FIELDS) + 1):
        if len(raw) % length:
            continue
        chunks = [raw[i:i + length] for i in range(0, len(raw), length)]
        times = [c[1] for c in chunks]
        if all(c[0] == ticker and _HMS.fullmatch(c[1]) for c in chunks) and times == sorted(times):
            return length
    return None


def _read_frames(path: Path) -> tuple[list[dict], bool]:
    rows: list[dict] = []
    truncated = False
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except EOFError:
        truncated = True
    return rows, truncated


def _live_splitter() -> Splitter:
    from src.api.kis_ws import _split_records

    return _split_records


def check_day(root: Path, date: str, split: Splitter | None = None) -> dict:
    """그날 틱 캡처 전체를 종목별로 점검한다."""
    split = split or _live_splitter()
    tickers: dict[str, dict] = {}
    for path in sorted((root / "data" / "strategy_ticks" / date).glob("*.jsonl.gz")):
        ticker = path.name.split(".")[0]
        t = tickers.setdefault(ticker, {
            "frames": 0, "records": 0, "multi_frames": 0, "parser_records": 0,
            "unaligned_frames": 0, "header_mismatch_frames": 0,
            "record_lengths": Counter(), "truncated": False,
        })
        rows, truncated = _read_frames(path)
        t["truncated"] = t["truncated"] or truncated
        for row in rows:
            raw = row.get("raw")
            if row.get("source") != "ws" or not isinstance(raw, list) or not raw:
                continue
            # tick-schema-3: 행 하나가 파서가 낸 체결 하나이고 프레임 위치가 붙는다.
            # 그 전 행은 프레임 하나이므로 파서에 다시 넣어 낸 체결 수를 센다.
            per_record = row.get("frame_size") is not None
            if per_record:
                if row.get("frame_index") == 0:
                    t["frames"] += 1
                    t["multi_frames"] += row["frame_size"] > 1
                    header = row.get("frame_count")
                    mismatch = header is not None and header != row["frame_size"]
                    t["header_mismatch_frames"] += mismatch
            else:
                t["frames"] += 1
            length = infer_record_length([str(v) for v in raw], ticker)
            if length is None:
                t["unaligned_frames"] += 1
                continue
            count = len(raw) // length
            t["records"] += count
            t["record_lengths"][str(length)] += 1
            if per_record:
                t["parser_records"] += 1
            else:
                t["multi_frames"] += count > 1
                t["parser_records"] += len(split("^".join(str(v) for v in raw)))

    issues: set[str] = set()
    for t in tickers.values():
        t["record_lengths"] = dict(t["record_lengths"])
        records = t["records"]
        t["coverage_pct"] = round(100 * t["parser_records"] / records, 1) if records else None
        if any(k != str(EXPECTED_FIELDS) for k in t["record_lengths"]):
            issues.add("FIELD_COUNT_CHANGED")
        if t["unaligned_frames"]:
            issues.add("UNALIGNED")
        if t["parser_records"] < t["records"]:
            issues.add("PARSER_DROPS")
        if t["header_mismatch_frames"]:
            issues.add("HEADER_MISMATCH")
    return {
        "date": date, "expected_fields": EXPECTED_FIELDS,
        "tickers": tickers, "issues": sorted(issues),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WS 체결 프레임 점검 (읽기 전용)")
    parser.add_argument("--root", type=Path, default=ROOT, help="운영 또는 개발 트리 루트")
    parser.add_argument("--date", default=datetime.now(KST).strftime("%Y%m%d"))
    parser.add_argument("--json", type=Path, default=None, help="결과를 JSON으로도 저장")
    args = parser.parse_args(argv)

    out = check_day(args.root, args.date)
    if not out["tickers"]:
        print(f"{args.date}: 틱 캡처 없음")
        return 2
    for ticker, t in out["tickers"].items():
        print(
            f"{args.date} {ticker}: 프레임 {t['frames']:,} / 체결 {t['records']:,} "
            f"(다건 {t['multi_frames']:,}) | 파서가 낸 체결 {t['parser_records']:,} "
            f"= {t['coverage_pct']}% | 레코드 길이 {t['record_lengths']} "
            f"| 정렬 실패 {t['unaligned_frames']}" + (" | 잘린 파일" if t["truncated"] else "")
        )
    print("이상: " + (", ".join(out["issues"]) if out["issues"] else "없음"))
    if args.json:
        args.json.write_bytes(json.dumps(out, ensure_ascii=False, indent=2).encode("utf-8"))
    return 1 if out["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
