"""트레일 폭 1.5% 대 2.0% — 1.5% 선 터치 뒤 가격이 2.0% 선까지 어떻게 움직였는지 잰다.

`trailing_shadow_comparisons`의 discordant 거래(두 폭의 결과가 다른 거래)마다 틱 캡처를
읽어, 1.5% 선을 처음 맞은 순간부터 (a) 2.0% 선까지 뚫렸는지 (b) 다음 스텝에 닿아
두 선이 함께 올라갔는지를 판정하고, 그 사이 저가·반등·소요 시간을 남긴다. 이어서
2.0% 선을 뚫린 뒤 30분 안에 다음 스텝을 회복했는지도 본다 — 더 넓은 폭이 잡았을
움직임인지 알기 위해서다.

진단 전용이다. 판정은 `docs/PAPER_STRATEGY_IMPROVEMENT_PLAN.md`의 사전등록식
(discordant 20건)으로만 하며, 이 출력은 그때 "한 건이 얼마나 아슬아슬했나"를
읽는 보조 자료다. 2026-09-18 첫 실행 결과는 같은 문서가 아니라 대화 기록에만 있다.

    .\\.venv\\Scripts\\python.exe scripts\\trail_near_miss.py            # 운영 트리에서
    .\\.venv\\Scripts\\python.exe scripts\\trail_near_miss.py --json out.json

틱은 `data/strategy_ticks/<date>/<ticker>.<HH>.jsonl.gz`의 WS 행만 쓴다(REST 백업 틱은
`valid=False`라 제외). 잘린 청크는 읽힌 데까지만 쓴다.
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# f4_tracking.py의 상수와 같은 값. 그쪽은 지문 대상이라 import 하지 않고 여기 복사한다 —
# 값이 바뀌면 여기도 맞춘다.
STEP_SIZE = 0.025
STEP_TRAIL = 0.020
BASELINE_TRAIL = 0.015
POST_HIT_WINDOW_SEC = 1800


def stop_lines(entry_price: float, highest_step: float) -> tuple[float, float, float]:
    """(1.5% 선, 2.0% 선, 다음 스텝 가격). f4_tracking._trailing_shadow_stop_prices와 같은 식."""
    return (
        entry_price * (1 + highest_step - BASELINE_TRAIL),
        entry_price * (1 + highest_step - STEP_TRAIL),
        entry_price * (1 + highest_step + STEP_SIZE),
    )


def measure_episode(
    ticks: list[dict],
    *,
    entry_price: float,
    highest_step: float,
    touch_at: datetime,
) -> dict | None:
    """1.5% 선 터치(touch_at)부터 2.0% 선 이탈 또는 다음 스텝 도달까지.

    ticks는 source_ts 오름차순의 WS 틱. 반환 None은 터치 이후 틱이 없다는 뜻.
    outcome은 HIT(2.0% 선 이탈) / ESCAPE(다음 스텝 도달) / OPEN(둘 다 아님, 캡처 끝).
    """
    base, rec, escape = stop_lines(entry_price, highest_step)
    after = [t for t in ticks if datetime.fromisoformat(t["source_ts"]) >= touch_at]
    if not after:
        return None
    low = high = after[0]["price"]
    outcome = "OPEN"
    end_ts: str | None = None
    for tick in after:
        price = tick["price"]
        low = min(low, price)
        high = max(high, price)
        if price <= rec:
            outcome, end_ts = "HIT", tick["source_ts"]
            break
        if price >= escape:
            outcome, end_ts = "ESCAPE", tick["source_ts"]
            break
    elapsed = (
        (datetime.fromisoformat(end_ts) - touch_at).total_seconds() if end_ts else None
    )
    return {
        "baseline_line": base,
        "recommended_line": rec,
        "next_step_price": escape,
        "low_after_touch": low,
        "margin_to_recommended_pct": (low - rec) / rec * 100,
        "bounce_over_baseline_pct": (high - base) / base * 100,
        "outcome": outcome,
        "elapsed_sec": elapsed,
        "end_ts": end_ts,
    }


def measure_post_hit(
    ticks: list[dict],
    *,
    entry_price: float,
    highest_step: float,
    hit_ts: str,
    window_sec: int = POST_HIT_WINDOW_SEC,
) -> dict:
    """2.0% 선 이탈 뒤 window_sec 안의 고가·저가와 다음 스텝 회복 여부."""
    _, rec, escape = stop_lines(entry_price, highest_step)
    hit_at = datetime.fromisoformat(hit_ts)
    window = [
        t for t in ticks
        if 0 <= (datetime.fromisoformat(t["source_ts"]) - hit_at).total_seconds() <= window_sec
    ]
    high = max(t["price"] for t in window)
    low = min(t["price"] for t in window)
    return {
        "high_pct_vs_line": (high - rec) / rec * 100,
        "low_pct_vs_line": (low - rec) / rec * 100,
        "reached_next_step": high >= escape,
    }


def load_ws_ticks(root: Path, date: str, ticker: str) -> list[dict]:
    ticks: list[dict] = []
    pattern = root / "data" / "strategy_ticks" / date / f"{ticker}.*.jsonl.gz"
    for path in sorted(glob.glob(str(pattern))):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        ticks.append(json.loads(line))
        except EOFError:
            pass  # 잘린 청크: 읽힌 데까지만 쓴다
    ticks = [
        t for t in ticks
        if t.get("source") == "ws" and t.get("source_ts") and t.get("price")
    ]
    ticks.sort(key=lambda t: t["source_ts"])
    return ticks


def discordant_trades(db_path: Path) -> list[dict]:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """
        select c.trade_id, c.entry_price, c.baseline_highest_step, c.baseline_exit_at,
               c.pnl_delta_pct, t.date, t.ticker, t.name
          from trailing_shadow_comparisons c
          join trades t on t.id = c.trade_id
         where c.pnl_delta_pct != 0
         order by t.date
        """
    ).fetchall()
    return [dict(r) for r in rows]


def run(root: Path) -> list[dict]:
    results: list[dict] = []
    for trade in discordant_trades(root / "data" / "db" / "trading.db"):
        ticks = load_ws_ticks(root, trade["date"], trade["ticker"])
        row = {k: trade[k] for k in ("date", "ticker", "name", "trade_id", "pnl_delta_pct")}
        row["highest_step"] = trade["baseline_highest_step"]
        if not ticks:
            row["episode"] = None
            results.append(row)
            continue
        episode = measure_episode(
            ticks,
            entry_price=trade["entry_price"],
            highest_step=trade["baseline_highest_step"],
            touch_at=datetime.fromisoformat(trade["baseline_exit_at"]),
        )
        row["episode"] = episode
        if episode and episode["outcome"] == "HIT":
            row["post_hit"] = measure_post_hit(
                ticks,
                entry_price=trade["entry_price"],
                highest_step=trade["baseline_highest_step"],
                hit_ts=episode["end_ts"],
            )
        results.append(row)
    return results


def print_table(results: list[dict]) -> None:
    print(
        f"{'거래일':8} {'종목':6} {'이름':6} {'스텝':>5} {'1.5%선':>7} {'2.0%선':>7} "
        f"{'터치후저가':>7} {'여유%':>6} {'결과':6} {'반등%':>6} {'경과':>6}"
    )
    for r in results:
        ep = r["episode"]
        if ep is None:
            print(f"{r['date']:8} {r['ticker']:6} {r['name'][:4]:6} 틱 없음")
            continue
        elapsed = f"{ep['elapsed_sec']:.0f}s" if ep["elapsed_sec"] is not None else "-"
        print(
            f"{r['date']:8} {r['ticker']:6} {r['name'][:4]:6} {r['highest_step']*100:4.1f}% "
            f"{ep['baseline_line']:7.0f} {ep['recommended_line']:7.0f} "
            f"{ep['low_after_touch']:7.0f} {ep['margin_to_recommended_pct']:6.2f} "
            f"{ep['outcome']:6} {ep['bounce_over_baseline_pct']:6.2f} {elapsed:>6}"
        )
    print()
    print(
        f"--- 2.0% 선 이탈 뒤 {POST_HIT_WINDOW_SEC // 60}분: "
        "고가·저가(선 대비) / 다음 스텝 회복 ---"
    )
    for r in results:
        post = r.get("post_hit")
        if not post:
            continue
        print(
            f"{r['date']} {r['name'][:5]:6} 고가 {post['high_pct_vs_line']:+.2f}%  "
            f"저가 {post['low_pct_vs_line']:+.2f}%  "
            f"다음스텝 {'도달' if post['reached_next_step'] else '미달'}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="트레일 1.5% 대 2.0% near-miss 진단")
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="data/ 가 있는 트리 (기본: 이 트리)"
    )
    parser.add_argument("--json", type=Path, default=None, help="결과를 JSON으로도 저장")
    args = parser.parse_args(argv)

    results = run(args.root)
    print_table(results)
    if args.json:
        args.json.write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n저장: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
