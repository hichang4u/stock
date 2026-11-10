"""빠른 경로 진입 타이밍 정리 — 첫 체결과 주문의 앞뒤, 주문별 갈래, 시가 대비 진입가. 읽기 전용.

빠른 경로는 09:00:05 무렵 1차 주문을 낸다. 모의 서버의 첫 체결(시가)은 개장 후 7 ~ 24초에 와서,
2초 창이 첫 체결 전에 닫히면 취소·재시도로 가고 진입가가 시가보다 비싸지거나 진입이 무산된다
(`docs/ENTRY_FILL_TIMEOUT_FOLLOWUP_20260903.md` 8절). 그 표를 매일 손으로 뽑던 것을 한 번에 낸다.

- 첫 체결: `strategy_ticks/<날짜>/<종목>.09.jsonl.gz`에서 체결시각(`raw[1]`)이 09:00:00 이후인 첫 ws
  레코드다. 시각은 수신 시각이다. 틱이 그 종목이 아니면(관측 대상이 바뀐 날) 모른다고 적는다.
  빠른 경로(`FAST_MULTI`) 날만 본다. 레거시 경로 날 캡처는 종목 고정(09:01 이후)부터라 첫 행이
  첫 체결이 아니다.
- 주문별 갈래: `ENTRY_ORDER_SENT` 뒤의 이벤트로 가른다.
  - `IN_WINDOW`: 2초 창 안에 체결 확인
  - `LATE_CONFIRM`: 창이 닫힌 뒤 체결돼 취소가 거부됨
  - `CANCELLED`: 취소 성공
  - `REJECTED`: 서버가 주문을 거부함(`rt_cd`가 0이 아님). 체결 대기 실패가 아니므로 요약 줄의
    무산에서 따로 센다(`docs/ORDER_REJECTED_PAPER_UNTRADABLE_FOLLOWUP_20261106.md`)
- 주문 API 통신 시간은 `LATENCY_HIGH`(`order-cash`)에서 읽는다. 임계 미만이면 기록이 없다(—).
- 시가 대비 금액은 **추정**이다. 첫 체결 전에 걸린 주문은 시가에 체결된다는 가정이다(8.9절).

    .\\.venv\\Scripts\\python.exe scripts\\entry_timing_report.py --root D:\\Private\\stock-prod

`--date`가 없으면 오늘(KST) 하루다. `--since`·`--until`로 범위를 준다. 장중 파일은 잘려 있다 —
읽힌 데까지 쓴다. 전원 차단으로 끊긴 멤버 뒤에 이어 쓴 멤버도 읽는다(`src/utils/jsonl_gz.py`).
누적은 `--since 20261013`으로 본다.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.jsonl_gz import read_jsonl_gz  # noqa: E402

KST = ZoneInfo("Asia/Seoul")

_OUTCOME_KO = {
    "IN_WINDOW": "창 안 체결",
    "LATE_CONFIRM": "늦은 확인",
    "CANCELLED": "취소",
    "REJECTED": "주문 거부",
    "UNKNOWN": "갈래 모름",
}
_ORDINAL = "1차", "2차", "3차", "4차"


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_bytes().decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def _read_gz(path: Path) -> list[dict]:
    try:
        got = read_jsonl_gz(path)
    except OSError:
        return []
    if got.damaged_at:
        print(
            f"[틱 파일] {path.name}: 끊긴 멤버를 건너뛰고 읽음 (바이트 {got.damaged_at})",
            file=sys.stderr,
        )
    return got.rows


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _hms(value: str) -> str:
    return value[11:23]


def _fields(raw) -> list[str] | None:
    if isinstance(raw, str):
        return raw.split("^")
    return raw if isinstance(raw, list) else None


def first_trade(root: Path, date: str, ticker: str) -> dict | None:
    """그 종목의 09:00 이후 첫 ws 체결. 틱 파일이 없거나 비면 None."""
    path = root / "data" / "strategy_ticks" / date / f"{ticker}.09.jsonl.gz"
    if not path.exists():
        return None
    ws = [r for r in _read_gz(path) if r.get("source") == "ws" and r.get("received_at")]
    ws.sort(key=lambda r: r["received_at"])
    for row in ws:
        raw = _fields(row.get("raw"))
        if not raw or len(raw) < 14 or raw[0] != ticker or raw[1] < "090000":
            continue
        try:
            return {
                "received_at": row["received_at"],
                "at": _hms(row["received_at"]),
                "hms": raw[1],
                "price": int(float(raw[2])),
                "qty": int(raw[12]),
            }
        except (TypeError, ValueError):
            continue
    return None


def _is_rejected(sent: dict) -> bool:
    """주문 응답이 거부인가. `rt_cd`가 없는 기록은 거부로 치지 않는다."""
    rt_cd = sent.get("rt_cd")
    return rt_cd not in (None, "") and str(rt_cd) != "0"


def _order_outcome(events: list[dict], order_id: str) -> str:
    mine = [e for e in events if e.get("order_id") == order_id]
    names = {e["event"] for e in mine}
    if "ENTRY_CANCEL_REJECTED_FILLED" in names:
        return "LATE_CONFIRM"
    if any(e["event"] == "ENTRY_CANCEL_SENT" and str(e.get("rt_cd")) == "0" for e in mine):
        return "CANCELLED"
    if "ENTRY_EXECUTED" in names:
        return "LATE_CONFIRM" if "ENTRY_FILL_POLL_TIMEOUT" in names else "IN_WINDOW"
    return "UNKNOWN"


def _order_api(events: list[dict], sent: dict) -> tuple[int | None, int | None]:
    """그 주문 직전(5초 안)의 order-cash 지연 기록. 임계 미만이면 기록이 없다."""
    sent_at = _ts(sent["ts"])
    best = None
    for e in events:
        if e.get("event") != "LATENCY_HIGH":
            continue
        if not str(e.get("api_endpoint", "")).endswith("order-cash"):
            continue
        at = _ts(e["ts"])
        if at <= sent_at and (sent_at - at).total_seconds() <= 5.0:
            best = e
    if best is None:
        return None, None
    return best.get("latency_ms"), best.get("rate_wait_ms")


def analyze_day(root: Path, date: str) -> dict | None:
    """그날 첫 진입 시도의 타이밍. 로그가 없으면 None, 주문이 없으면 orders=[]."""
    log_path = root / "data" / "logs" / f"{date}.jsonl"
    if not log_path.exists():
        return None
    events = [e for e in _read_jsonl(log_path) if isinstance(e, dict) and e.get("ts")]
    timing = next((e for e in events if e.get("event") == "ENTRY_PIPELINE_TIMING"), None)
    sents = [e for e in events if e.get("event") == "ENTRY_ORDER_SENT"]
    executed = next((e for e in events if e.get("event") == "ENTRY_EXECUTED"), None)
    failed = next((e for e in events if e.get("event") == "ENTRY_FAIL"), None)
    if executed is not None:
        # 진입 뒤 같은 날 다른 시도는 없다. 그래도 진입 주문까지만 본다.
        sents = [e for e in sents if e["ts"] <= executed["ts"]]

    # 그날의 종목은 체결된 종목이다. 앞 후보가 거부되고 다음 후보가 체결된 날, 거부된 종목의
    # 첫 체결가로 진입가를 재면 안 된다. 진입이 없으면 첫 주문의 종목이다.
    ticker = next((str(e["ticker"]) for e in [executed or {}] + sents if e.get("ticker")), None)
    source = timing.get("selection_source") if timing else None
    # 레거시 경로 날 캡처는 종목 고정(09:01 이후)부터라 첫 행이 그날 첫 체결이 아니다.
    firsts: dict[str, dict | None] = {}

    def first_of(tk: str | None) -> dict | None:
        if not tk or source != "FAST_MULTI":
            return None
        if tk not in firsts:
            firsts[tk] = first_trade(root, date, tk)
        return firsts[tk]

    first = first_of(ticker)

    orders = []
    for sent in sents:
        api_ms, wait_ms = _order_api(events, sent)
        # 주문마다 자기 종목의 첫 체결과 견준다.
        own_ticker = str(sent["ticker"]) if sent.get("ticker") else ticker
        own_first = first_of(own_ticker)
        lead = None
        if own_first is not None:
            lead = round((_ts(own_first["received_at"]) - _ts(sent["ts"])).total_seconds(), 3)
        orders.append({
            "order_id": sent.get("order_id"),
            "ticker": own_ticker,
            "attempt": sent.get("entry_attempt"),
            "sent_at": _hms(sent["ts"]),
            "api_ms": api_ms,
            "rate_wait_ms": wait_ms,
            "lead_sec": lead,
            # 거부된 주문은 주문번호가 빈 값이라 번호로 뒤 이벤트를 찾으면 안 된다.
            "outcome": (
                "REJECTED" if _is_rejected(sent)
                else _order_outcome(events, str(sent.get("order_id")))
            ),
        })

    result = "ENTERED" if executed is not None else ("FAILED" if failed is not None else "NO_ORDER")
    fill_price = executed.get("fill_price") if executed else None
    fill_qty = executed.get("fill_qty") if executed else None
    per_share = amount = None
    if first is not None and fill_price is not None:
        per_share = int(round(float(fill_price) - first["price"]))
        if fill_qty is not None:
            amount = per_share * int(fill_qty)
    return {
        "date": date,
        "source": source,
        "ticker": ticker,
        "first_trade": first,
        "orders": orders,
        "result": result,
        "fill_price": fill_price,
        "fill_qty": fill_qty,
        "entry_ms": timing.get("total_ms") if timing and result == "ENTERED" else None,
        "vs_open_per_share": per_share,
        "vs_open_amount": amount,
    }


def branch_label(day: dict) -> str:
    """주문별 '첫 체결 전/뒤 · 갈래'를 잇는다. 진입 무산이면 끝에 '무산'."""
    if day.get("source") and day["source"] != "FAST_MULTI":
        return "레거시 경로"
    if not day["orders"]:
        return "주문 없음"
    parts = []
    for i, o in enumerate(day["orders"]):
        nth = _ORDINAL[i] if i < len(_ORDINAL) else f"{i + 1}차"
        lead = o["lead_sec"]
        when = "첫 체결 모름" if lead is None else ("첫 체결 전" if lead > 0 else "첫 체결 뒤")
        parts.append(f"{nth} {when} · {_OUTCOME_KO[o['outcome']]}")
    if day["result"] == "FAILED":
        parts.append("무산")
    return " → ".join(parts)


def _fmt_int(v) -> str:
    return "—" if v is None else f"{int(v):,}"


def _fmt_signed(v) -> str:
    return "—" if v is None else f"{int(v):+,}"


def format_table(days: list[dict]) -> str:
    lines = [
        "| 날짜 | 종목 | 첫 체결 | 시가 | 수량 "
        "| 주문 (접수 · 주문 API ms · 한도 대기 ms · 첫 체결까지 초) "
        "| 갈래 | 결과 | 시가 대비 원/주 | 금액 (추정) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for d in days:
        ft = d["first_trade"] or {}
        orders = "<br>".join(
            f"{o['sent_at']} · {_fmt_int(o['api_ms'])} · {_fmt_int(o['rate_wait_ms'])} · "
            f"{'—' if o['lead_sec'] is None else format(o['lead_sec'], '+.2f')}"
            for o in d["orders"]
        ) or "—"
        entered = "진입" if d["entry_ms"] is None else f"진입 {d['entry_ms'] / 1000:.2f}초"
        result = {
            "ENTERED": entered,
            "FAILED": "**무산**",
            "NO_ORDER": "주문 없음",
        }[d["result"]]
        lines.append(
            f"| {d['date'][4:]} | {d['ticker'] or '—'} | {ft.get('at', '—')} "
            f"| {_fmt_int(ft.get('price'))} | {_fmt_int(ft.get('qty'))} "
            f"| {orders} | {branch_label(d)} | {result} "
            f"| {_fmt_signed(d['vs_open_per_share'])} | {_fmt_signed(d['vs_open_amount'])} |"
        )
    return "\n".join(lines)


def _summary(days: list[dict]) -> str:
    fast = [d for d in days if d["source"] == "FAST_MULTI" and d["orders"]]
    counts = {k: 0 for k in _OUTCOME_KO}
    after_first = 0
    for d in fast:
        for o in d["orders"]:
            counts[o["outcome"]] += 1
        first = d["orders"][0]["lead_sec"]
        if first is not None and first <= 0:
            after_first += 1
    at_open = sum(1 for d in fast if d["vs_open_per_share"] == 0)
    all_failed = [d for d in fast if d["result"] == "FAILED"]
    rejected = [d["date"][4:] for d in all_failed if d["orders"][-1]["outcome"] == "REJECTED"]
    failed = [d["date"][4:] for d in all_failed if d["orders"][-1]["outcome"] != "REJECTED"]
    known = [d["vs_open_amount"] for d in fast if d["vs_open_amount"] is not None]
    return (
        f"빠른 경로 {len(fast)}일 · 주문 {sum(counts.values())}건: "
        + ", ".join(f"{_OUTCOME_KO[k]} {v}" for k, v in counts.items() if v)
        + f" · 1차 주문이 첫 체결 뒤 {after_first}일 · 시가 진입 {at_open}일"
        + f" · 무산 {len(failed)}일{' (' + ', '.join(failed) + ')' if failed else ''}"
        + (f" · 주문 거부로 무산 {len(rejected)}일 ({', '.join(rejected)})" if rejected else "")
        + f" · 시가 대비 합계 {_fmt_signed(sum(known)) if known else '—'}원 (추정, {len(known)}일)"
    )


def _dates(root: Path, since: str, until: str) -> list[str]:
    logs = root / "data" / "logs"
    out = []
    for p in sorted(logs.glob("*.jsonl")) if logs.exists() else []:
        if len(p.stem) == 8 and p.stem.isdigit() and since <= p.stem <= until:
            out.append(p.stem)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="빠른 경로 진입 타이밍 정리 (읽기 전용)")
    parser.add_argument("--root", type=Path, default=ROOT, help="운영 또는 개발 트리 루트")
    parser.add_argument("--date", default=None, help="하루만 (기본 오늘)")
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    parser.add_argument("--json", type=Path, default=None, help="결과를 JSON으로도 저장")
    args = parser.parse_args(argv)

    today = datetime.now(KST).strftime("%Y%m%d")
    if args.since or args.until:
        dates = _dates(args.root, args.since or "00000000", args.until or today)
    else:
        dates = [args.date or today]
    days = [d for d in (analyze_day(args.root, date) for date in dates) if d is not None]
    days = [d for d in days if d["orders"] or d["result"] != "NO_ORDER"]
    if not days:
        print("진입 시도가 있는 날이 없다.")
        return 2
    print(format_table(days))
    print()
    print(_summary(days))
    if args.json:
        args.json.write_bytes(json.dumps(days, ensure_ascii=False, indent=2).encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
