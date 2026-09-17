"""장전 호가 수급 추출 — 프로브 원문 덤프에서 후보 쌍별 잔량 비율을 뽑는다. 읽기 전용.

운영의 paper_fast_probe 가 08:59:45(PREOPEN)와 09:00:00(OPEN)에 받은 멀티시세 응답을
`data/paper_fast_probe/YYYYMMDD.jsonl` 에 원문 그대로 남긴다. 그 행의 총매수·총매도
호가잔량이 "장전 수급"이다. 운영 코드는 이 값을 버리지만(buy_sell_ratio 0 고정) 덤프에는
남아 있으므로 수집기 없이 소급된다.

이 스크립트는 손익과 조인하지 않는다. 커버리지와 라벨 분포만 낸다 — 규칙과 문턱은
분포를 본 뒤, 손익을 보기 전에 H3로 등록한다.

    python scripts/preopen_book.py --universes data/replay/universes.json \
        --probe-dir D:/Private/stock-prod/data/paper_fast_probe \
        --out data/catalyst/preopen_book.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.replay_universe import load_universe_pairs  # noqa: E402

PHASE_EVENTS = {
    "PAPER_FAST_PROBE_MULTI": "PREOPEN",
    "PAPER_FAST_PROBE_OPEN_MULTI": "OPEN",
}
BOOK_FIELDS = ("total_bidp_rsqn", "total_askp_rsqn")


def _to_int(value) -> int | None:
    """비숫자·빈 값은 0이 아니라 None — 0으로 만들면 비율이 0이 되어 ASK_DOMINANT로 새는다."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _ratio(num: int | None, den: int | None) -> float | None:
    if num is None or den is None or den <= 0:
        return None
    return num / den


def parse_probe_lines(lines) -> dict[str, dict[str, dict]]:
    """하루치 덤프 → {PREOPEN|OPEN: {ticker: 원문 행}}. 잔량 필드가 없는 행은 버린다."""
    parsed: dict[str, dict[str, dict]] = {"PREOPEN": {}, "OPEN": {}}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        phase = PHASE_EVENTS.get(event.get("event"))
        if phase is None:
            continue
        # 운영 파서(paper_fast_probe)와 같은 조건 — 이벤트 이름과 phase 필드가 함께 맞아야 한다.
        if event.get("phase") not in (None, phase):
            continue
        rows = (event.get("response") or {}).get("output") or []
        for row in rows:
            ticker = str(row.get("inter_shrn_iscd") or "")
            if not ticker or any(f not in row for f in BOOK_FIELDS):
                continue
            parsed[phase][ticker] = row
    return parsed


def book_features(row: dict) -> dict:
    """원문 행 → 잔량 비율. 매도 잔량 0이면 비율은 None(무한대로 적지 않는다)."""
    total_bid = _to_int(row.get("total_bidp_rsqn"))
    total_ask = _to_int(row.get("total_askp_rsqn"))
    top1_bid = _to_int(row.get("shnu_rsqn"))
    top1_ask = _to_int(row.get("seln_rsqn"))
    return {
        "total_bid": total_bid,
        "total_ask": total_ask,
        "bid_ask_ratio": _ratio(total_bid, total_ask),
        "top1_bid": top1_bid,
        "top1_ask": top1_ask,
        "top1_bid_ask_ratio": _ratio(top1_bid, top1_ask),
        "antc_vol": _to_int(row.get("intr_antc_vol")),
        "askp": _to_float(row.get("inter2_askp")),
        "bidp": _to_float(row.get("inter2_bidp")),
        "hour_cls_code": str(row.get("hour_cls_code") or ""),
    }


def label_pairs(pairs: list[dict], probe_by_day: dict[str, dict]) -> list[dict]:
    rows: list[dict] = []
    for pair in pairs:
        row = dict(pair)
        day = probe_by_day.get(pair["date"])
        if day is None:
            row.update(preopen=None, open=None, book_source="NO_PROBE_FILE")
            rows.append(row)
            continue
        pre = day["PREOPEN"].get(pair["ticker"])
        opn = day["OPEN"].get(pair["ticker"])
        row["preopen"] = book_features(pre) if pre else None
        row["open"] = book_features(opn) if opn else None
        row["book_source"] = (
            "BOTH" if pre and opn else "PREOPEN_ONLY" if pre else "OPEN_ONLY" if opn else "NONE"
        )
        rows.append(row)
    return rows


H3_THRESHOLD = 1.0  # 문서 §2.2 — 자연 경계, 판정까지 고정


def h3_label(row: dict) -> dict:
    """문서 §2.2. PREOPEN 총잔량 비율 ≥ 1.0 → BID_DOMINANT, < 1.0 → ASK_DOMINANT, 없음 → NONE."""
    pre = row.get("preopen")
    ratio = pre.get("bid_ask_ratio") if pre else None
    if ratio is None:
        label = "NONE"
    elif ratio >= H3_THRESHOLD:
        label = "BID_DOMINANT"
    else:
        label = "ASK_DOMINANT"
    return {"label": label, "preopen_bid_ask_ratio": ratio}


def quantiles(values: list[float | None]) -> dict:
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return {"n": 0, "min": None, "p25": None, "median": None, "p75": None, "max": None}

    def q(p: float) -> float:
        pos = (len(xs) - 1) * p
        lo, hi = int(pos), min(int(pos) + 1, len(xs) - 1)
        return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)

    return {"n": len(xs), "min": xs[0], "p25": q(0.25), "median": q(0.5), "p75": q(0.75), "max": xs[-1]}


def load_probe_dir(probe_dir: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(probe_dir.glob("*.jsonl")):
        if not path.stem[:8].isdigit():
            continue
        with path.open(encoding="utf-8") as handle:
            out[path.stem[:8]] = parse_probe_lines(handle)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="장전 호가 수급 추출 (손익 조인 없음)")
    parser.add_argument("--universes", type=Path, default=ROOT / "data/replay/universes.json")
    parser.add_argument("--probe-dir", type=Path, default=ROOT / "data/paper_fast_probe")
    parser.add_argument("--out", type=Path, default=ROOT / "data/catalyst/preopen_book.jsonl")
    parser.add_argument(
        "--h3-labels", type=Path, default=None,
        help="문서 §2.2 라벨(BID_DOMINANT/ASK_DOMINANT/NONE)을 쌍 단위로 이 파일에 쓴다",
    )
    args = parser.parse_args(argv)

    _dates, pairs = load_universe_pairs(args.universes)
    probe = load_probe_dir(args.probe_dir)
    rows = label_pairs(pairs, probe)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    sources: dict[str, int] = {}
    for row in rows:
        sources[row["book_source"]] = sources.get(row["book_source"], 0) + 1
    print(f"유니버스 {len(pairs)}쌍, 프로브 {len(probe)}일 → 커버 {sources}")
    for phase in ("preopen", "open"):
        ratios = [row[phase]["bid_ask_ratio"] for row in rows if row[phase]]
        top1 = [row[phase]["top1_bid_ask_ratio"] for row in rows if row[phase]]
        print(f"  {phase:8} 총잔량 매수/매도 {quantiles(ratios)}")
        print(f"  {phase:8} 1호가  매수/매도 {quantiles(top1)}")
    print(f"→ {args.out}")

    if args.h3_labels:
        counts: dict[str, int] = {}
        with args.h3_labels.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                labelled = {k: row[k] for k in ("date", "ticker", "rank", "book_source")}
                labelled.update(h3_label(row))
                counts[labelled["label"]] = counts.get(labelled["label"], 0) + 1
                handle.write(json.dumps(labelled, ensure_ascii=False) + "\n")
        print(f"H3 라벨 {counts} → {args.h3_labels}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
