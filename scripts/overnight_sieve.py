r"""C1 오버나이트 체 — 후보를 D-0 종가에 산 것으로 두고 D+1 분봉에 A의 F4 규칙을 재생한다.

판정 기준은 docs/superpowers/specs/2026-09-21-c1-overnight-close-hypothesis.md §4에 고정돼
있다. 이 스크립트는 그 수치를 계산해 보여줄 뿐 판정하지 않는다 — n≥50 전에는 어떤 값도
결론이 아니다.

    .\.venv\Scripts\python.exe scripts\overnight_sieve.py --root D:\Private\stock-prod
    .\.venv\Scripts\python.exe scripts\overnight_sieve.py \
        --root D:\Private\stock-prod --json out.json

분봉은 track_b_backfill이 채운 data/backtest_bars/<D+1>_<ticker>.json 이다. 봉 안 순서는
"저가 먼저"로 고정한다(트랙 B와 같다).
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import StatisticsError, correlation, mean, median
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.overnight_calendar import load_calendar, next_trading_date  # noqa: E402
from scripts.strategy_backtest import read_cached_bars  # noqa: E402
from scripts.track_b_backtest import bootstrap_ci  # noqa: E402
from scripts.track_b_rules import HARD_STOP, simulate_exit  # noqa: E402
from scripts.ws_frame_check import infer_record_length  # noqa: E402
from src import warmup  # noqa: E402

KST = ZoneInfo("Asia/Seoul")

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


# ±30%를 넘는 갭은 기준가 변경(액면분할·감자 등) 없이는 불가능하다 — 정정(2026-09-21,
# 첫 리뷰 후): 정확히 −30%는 KRX 하한가(상하한 ±30%)라 표본에 남긴다. 그래서 경계는
# 등호 없는 초과(>)이고, 판정은 절대값이다(상한가 쪽 기준가 변경도 같은 이유로 막는다).
SUSPECT_GAP_ABS_PCT = 30.0


def simulate_overnight(bars: list[dict], entry_price: float) -> dict:
    """전날 종가 진입. 첫 봉 시가가 하드스탑 아래면 시가에서 끝, 아니면 트랙 B F4 재생.

    시가 갭의 절대값이 SUSPECT_GAP_ABS_PCT(30%)를 넘으면 하드스탑보다 먼저
    SUSPECT_CORPORATE_ACTION으로 끝낸다 — 액면분할·감자 같은 기업 행위가 일반 갭하락
    손익 분포에 섞이는 것을 막는다. 정확히 −30%(하한가)는 넘지 않으므로 의심하지 않는다.
    """
    first = bars[0]
    open_price = float(first["open"])
    gap_pct = round((open_price / entry_price - 1) * 100, 10)
    complete = warmup.covers_session(bars)
    if abs(gap_pct) > SUSPECT_GAP_ABS_PCT:
        return {
            "open": open_price, "gap_pct": gap_pct, "exit_reason": "SUSPECT_CORPORATE_ACTION",
            "exit_time": first["time"], "exit_price": open_price, "gross_pct": gap_pct,
            "bars_complete": complete,
        }
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


def simulate_overnight_ticks(
    bars: list[dict], ticks: list[tuple[str, float]], entry_price: float
) -> dict:
    """§4.2 틱 재생. 갭 판정은 분봉 재생과 같고, 틱이 있는 구간만 틱으로 바꿔 F4를 돈다.

    틱 캡처는 A의 진입 뒤에 시작해 15:20에 끝난다. 그래서 첫 틱의 분 이전과 마지막 틱의 분
    이후는 분봉으로 잇는다. 틱 하나는 시·고·저·종이 같은 봉 하나로 넘긴다(시각은 HHMMSS).
    """
    base = simulate_overnight(bars, entry_price)
    if base["exit_reason"] in ("SUSPECT_CORPORATE_ACTION", "GAP_HARD_STOP") or not ticks:
        return base
    first_min, last_min = ticks[0][0][:4], ticks[-1][0][:4]
    prefix = [b for b in bars if str(b["time"])[:4] < first_min]
    suffix = [b for b in bars if str(b["time"])[:4] > last_min]
    tick_bars = [{"time": hms, "open": p, "high": p, "low": p, "close": p} for hms, p in ticks]
    result = simulate_exit(prefix + tick_bars + suffix, 0, entry_price, order="low_first")
    return {
        "open": base["open"], "gap_pct": base["gap_pct"], "exit_reason": result["reason"],
        "exit_time": result["exit_time"], "exit_price": result["exit_price"],
        "gross_pct": result["pct"], "bars_complete": base["bars_complete"],
    }


def _hms(received_at: str) -> str:
    return str(received_at)[11:19].replace(":", "")


def load_tick_prices(root: Path, date: str, ticker: str) -> list[tuple[str, float]] | None:
    """그날 그 종목의 틱 캡처 → [(체결시각 HHMMSS, 가격)]. 캡처가 없으면 None.

    2026-09-14 ~ 10-01 캡처는 ws 행 하나가 다건 프레임이라 `raw`를 레코드 길이로 쪼갠다
    (docs/WS_47FIELD_PARSER_FOLLOWUP_20261001.md). tick-schema-3 행(`frame_size` 있음)은 체결
    하나다. REST 백업 행은 수신 시각과 가격만 쓴다. 장중 잘린 파일은 읽힌 데까지 쓴다.
    """
    files = sorted((root / "data" / "strategy_ticks" / date).glob(f"{ticker}.*.jsonl.gz"))
    if not files:
        return None
    rows: list[dict] = []
    for path in files:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except EOFError:
            pass
    rows.sort(key=lambda r: (str(r.get("received_at", "")), r.get("frame_index") or 0))
    out: list[tuple[str, float]] = []
    for row in rows:
        raw = row.get("raw")
        if row.get("source") == "ws" and isinstance(raw, list) and raw:
            values = [str(v) for v in raw]
            length = (
                len(values) if row.get("frame_size") is not None
                else infer_record_length(values, ticker)
            )
            if length is None:
                if row.get("price"):
                    out.append((_hms(row.get("received_at", "")), float(row["price"])))
                continue
            for i in range(0, len(values), length):
                out.append((values[i + 1], float(values[i + 2])))
        elif row.get("price"):
            out.append((_hms(row.get("received_at", "")), float(row["price"])))
    return out


def apply_costs(gross_pct: float, exit_reason: str) -> dict:
    net_cost = gross_pct - BASE_ROUND_TRIP_COST_PCT
    slip = _SLIPPAGE_BY_REASON.get(exit_reason, TIMEOUT_SLIPPAGE_PCT) + ENTRY_SLIPPAGE_PCT
    return {"net_cost_pct": net_cost, "net_slip_pct": net_cost - slip}


# §4.2 비용 민감도 격자 — 비용 0.10/0.18/0.25%p, 슬리피지 0.5/1.0/1.5배.
COST_GRID_PCT = (0.10, 0.18, 0.25)
SLIP_MULTIPLIERS = (0.5, 1.0, 1.5)


def _primary_rank1(results: list[dict]) -> list[dict]:
    """§4.1 주 표본: 랭크 1, 분봉 완결, 기업 행위 의심 제외. 날짜순."""
    return sorted(
        (r for r in results if r.get("rank") == 1 and r.get("bars_complete")
         and r.get("exit_reason") != "SUSPECT_CORPORATE_ACTION"),
        key=lambda r: r["date"],
    )


def cost_sensitivity(results: list[dict]) -> dict[str, float]:
    """주 표본 평균을 비용·슬리피지 격자로 다시 낸다. 기본값(0.18, 1.0)은 주 지표와 같다."""
    rank1 = [r for r in _primary_rank1(results) if r.get("gross_pct") is not None]
    if not rank1:
        return {}
    grid: dict[str, float] = {}
    for cost in COST_GRID_PCT:
        for mult in SLIP_MULTIPLIERS:
            values = [
                float(r["gross_pct"]) - cost
                - (_SLIPPAGE_BY_REASON.get(r["exit_reason"], TIMEOUT_SLIPPAGE_PCT)
                   + ENTRY_SLIPPAGE_PCT) * mult
                for r in rank1
            ]
            grid[f"cost_{cost:.2f}_slip_{mult:.1f}"] = mean(values)
    return grid


def load_a_pnl(root: Path) -> dict[str, float]:
    """트랙 A 실거래 손익(%)을 날짜별로. 운영 DB를 읽기 전용으로 연다."""
    path = root / "data" / "db" / "trading.db"
    if not path.exists():
        return {}
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        rows = db.execute(
            "select date, pnl_pct from trades where track='A' and pnl_pct is not null"
        ).fetchall()
    finally:
        db.close()
    return {str(d): float(p) for d, p in rows}


def a_correlation(results: list[dict], a_pnl: dict[str, float]) -> dict:
    """§4.2 A와 같은 날 상관 — C 랭크 1의 청산일(D+1) 손익과 그날 A 손익. 3쌍 미만이면 None."""
    pairs = [
        (float(r["net_slip_pct"]), a_pnl[r["next_date"]])
        for r in _primary_rank1(results) if r.get("next_date") in a_pnl
    ]
    if len(pairs) < 3:
        return {"n": len(pairs), "pearson": None}
    xs, ys = zip(*pairs)
    try:
        pearson: float | None = correlation(xs, ys)
    except StatisticsError:
        pearson = None   # 한쪽이 상수면 정의되지 않는다
    return {"n": len(pairs), "pearson": pearson}


def tick_vs_bar(results: list[dict]) -> dict:
    """§4.2 틱 vs 분봉 재생 차이 — 틱 캡처가 있는 행(= A가 그 종목을 거래한 날)만."""
    rows = [
        {"date": r["date"], "next_date": r["next_date"], "ticker": r["ticker"],
         "rank": r["rank"], "bar_reason": r["exit_reason"],
         "tick_reason": r["tick"]["exit_reason"], "bar_pct": r["gross_pct"],
         "tick_pct": r["tick"]["gross_pct"]}
        for r in results if isinstance(r.get("tick"), dict)
    ]
    diffs = [float(x["tick_pct"]) - float(x["bar_pct"]) for x in rows]
    return {
        "n": len(rows),
        "mean_diff_pct": mean(diffs) if diffs else None,
        "reason_agree": sum(1 for x in rows if x["bar_reason"] == x["tick_reason"]),
        "rows": rows,
    }


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
    return round(worst, 10)


def summarize(results: list[dict], *, missing: dict) -> dict:
    """§4.1 주 지표·판정 조건과 §4.2 부 지표. 판정은 하지 않고 조건 충족 여부만 낸다."""
    suspect_rank1 = sum(
        1 for r in results
        if r.get("rank") == 1 and r.get("exit_reason") == "SUSPECT_CORPORATE_ACTION"
    )
    # SUSPECT_CORPORATE_ACTION(|시가 갭| > 30%)은 급락이 아니라 액면분할·감자 등 기업
    # 행위로 의심되는 값이라 주 표본에서 뺀다 — 정상 갭하락 분포에 섞이면 안 된다.
    rank1 = _primary_rank1(results)
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

    # 스펙 §5는 "n=30 도달 시 한 번만 본다". 조기 중단 판정은 사전 등록된 그대로 항상
    # "첫 30개 표본"(values[:EARLY_STOP_N], 날짜순 정렬돼 있다)으로만 계산한다 — n이
    # 30을 넘어 표본이 계속 쌓여도 이 값은 바뀌지 않아 재실행마다 같은 답을 준다.
    # 그 값을 판정에 한 번만 쓰는 것은 여전히 운영자의 몫이다.
    early_values = values[:EARLY_STOP_N]
    early_mean = mean(early_values) if early_values else None
    early_drawdown = _max_drawdown(early_values)
    early: dict = {"evaluable": n >= EARLY_STOP_N, "triggered": False, "reason": None}
    if early["evaluable"] and early_mean is not None:
        if early_mean < EARLY_STOP_MEAN:
            early.update(triggered=True, reason="MEAN")
        elif early_drawdown < EARLY_STOP_DRAWDOWN:
            early.update(triggered=True, reason="DRAWDOWN")

    gaps = [float(r["gap_pct"]) for r in rank1]
    by_rank: dict[int, list[float]] = {}
    for r in results:
        if isinstance(r.get("rank"), int) and r.get("bars_complete"):
            by_rank.setdefault(r["rank"], []).append(float(r["net_slip_pct"]))
    missing_out = dict(missing)
    missing_out["suspect_corporate_action"] = suspect_rank1
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
        "cost_sensitivity": cost_sensitivity(results),
        "tick_vs_bar": tick_vs_bar(results),
        "missing": missing_out,
    }


def load_candidates(candidates_dir: Path) -> tuple[dict[str, list[dict]], dict]:
    """{D-0: [rank가 정수인 행]}. 요약 행이 없는 파일은 중간에 죽은 것 — 표본에서 빼고 센다."""
    loaded: dict[str, list[dict]] = {}
    missing = {"screen_died": 0, "degraded": 0, "no_candidate": 0}
    if not candidates_dir.exists():
        return loaded, missing
    for path in sorted(candidates_dir.glob("*.jsonl")):
        rows: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            # 잘린 줄 하나가 그날 보고 전체를 멈추면 안 된다 — overnight_pairs와 같은 규칙.
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            rows.append(row)
        if not rows or not rows[-1].get("summary"):
            missing["screen_died"] += 1
            continue
        summary = rows[-1]
        if summary.get("degraded"):
            missing["degraded"] += 1
        ranked = [r for r in rows[:-1] if _is_valid_candidate_row(r)]
        if not ranked:
            missing["no_candidate"] += 1
        loaded[path.stem] = ranked
    return loaded, missing


def _is_valid_candidate_row(row: dict) -> bool:
    if not isinstance(row.get("rank"), int):
        return False
    ticker = str(row.get("ticker") or "")
    if len(ticker) != 6 or not ticker.isdigit():
        return False
    try:
        return float(row.get("close")) > 0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _next_dates(
    candidate_dates: list[str], bars_dir: Path, calendar: list[str] | None = None
) -> dict[str, str | None]:
    """D-0 → D+1. ``calendar``가 있으면 실제 거래일 달력에서 D+1을 정한다(§3.4 정정) —
    D+1에 후보 파일도 분봉도 없는 날(기계/API가 오후 내내 죽은 날)에 D+2를 D+1로 오추정해
    이틀 보유가 표본에 섞이는 것을 막는다. 없으면 후보·분봉 파일 존재로 추정하는 기존
    휴리스틱을 쓴다."""
    if calendar is not None:
        return {date: next_trading_date(calendar, date) for date in candidate_dates}
    bar_dates = {p.name[:8] for p in bars_dir.glob("*_*.json")} if bars_dir.exists() else set()
    heuristic_calendar = sorted(set(candidate_dates) | bar_dates)
    out: dict[str, str | None] = {}
    for date in candidate_dates:
        later = [d for d in heuristic_calendar if d > date]
        out[date] = later[0] if later else None
    return out


def count_screen_failed(calendar: list[str], file_dates: set[str], today: str) -> int:
    """후보 파일이 아예 없는 거래일 수 — 요약 없는 파일(screen_died)과는 다르다.

    달력의 거래일 중 [가장 이른 후보 파일 날짜, today) 구간에서 후보 파일이 하나도
    없는 날을 센다. 그 구간 밖(수집기를 아직 돌리기 전 과거·오늘)은 세지 않는다.
    """
    if not calendar or not file_dates:
        return 0
    earliest = min(file_dates)
    return sum(1 for d in calendar if earliest <= d < today and d not in file_dates)


def run(root: Path) -> tuple[list[dict], dict]:
    candidates_dir = root / "data" / "overnight" / "candidates"
    results_dir = root / "data" / "overnight" / "results"
    bars_dir = root / "data" / "backtest_bars"
    loaded, missing = load_candidates(candidates_dir)
    missing.update({"bars_missing": 0, "bars_incomplete": 0, "awaiting_next_day": 0})

    calendar = load_calendar(root)
    file_dates = (
        {p.stem for p in candidates_dir.glob("*.jsonl")} if candidates_dir.exists() else set()
    )
    today = datetime.now(KST).strftime("%Y%m%d")
    if calendar is not None:
        missing["screen_failed"] = count_screen_failed(calendar, file_dates, today)
    else:
        missing["screen_failed"] = 0
        missing["screen_failed_unknown"] = True

    next_of = _next_dates(sorted(loaded), bars_dir, calendar=calendar)
    results: list[dict] = []
    for date, rows in sorted(loaded.items()):
        next_date = next_of[date]
        if next_date is None:
            missing["awaiting_next_day"] += 1
            continue
        day_results: list[dict] = []
        for row in rows:
            # load_candidates가 이미 ticker/rank/close를 보장한다 — 여기서 다시 지키지 않는다.
            bars = read_cached_bars(next_date, str(row["ticker"]), cache_dir=bars_dir)
            if not isinstance(bars, list) or not bars or not all(
                isinstance(b, dict) for b in bars
            ):
                missing["bars_missing"] += 1
                continue
            # 캐시 파일의 봉 순서를 신뢰하지 않는다 — merge_bars가 시각순 정렬을
            # 보장하지만, 다른 경로로 쓰인 파일까지 대비해 여기서도 다시 정렬한다.
            bars = sorted(bars, key=lambda b: str(b.get("time", "")))
            sim = simulate_overnight(bars, float(row["close"]))
            if not sim["bars_complete"]:
                missing["bars_incomplete"] += 1
            costs = apply_costs(sim["gross_pct"], sim["exit_reason"])
            result = {
                "date": date, "next_date": next_date, "ticker": str(row["ticker"]),
                "rank": int(row["rank"]), "entry_price": float(row["close"]),
                **sim, **costs, "ambiguous": False,
            }
            # §4.2 틱 vs 분봉 — A가 D+1에 같은 종목을 거래해 틱 캡처가 있을 때만.
            ticks = load_tick_prices(root, next_date, str(row["ticker"]))
            if ticks:
                tick_sim = simulate_overnight_ticks(bars, ticks, float(row["close"]))
                result["tick"] = {k: tick_sim[k] for k in
                                  ("exit_reason", "exit_time", "exit_price", "gross_pct")}
            day_results.append(result)
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
    print("\n[부 지표 — 기록만, 판정에 안 씀 (스펙 §4.2)]")
    print(f"비용 민감도(랭크1 평균): {s['cost_sensitivity']}")
    tvb = s["tick_vs_bar"]
    print(f"틱 vs 분봉: n={tvb['n']}  평균 차이(틱-분봉) {tvb['mean_diff_pct']}  "
          f"청산 사유 일치 {tvb['reason_agree']}/{tvb['n']}")
    for x in tvb["rows"]:
        print(f"  {x['next_date']} {x['ticker']} 랭크{x['rank']}: 분봉 {x['bar_reason']} "
              f"{x['bar_pct']:+.2f}% / 틱 {x['tick_reason']} {x['tick_pct']:+.2f}%")
    if "a_correlation" in s:
        print(f"A와 같은 날 상관: {s['a_correlation']}")
    print(f"결측: {s['missing']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C1 오버나이트 체 — 후보의 D+1 F4 재생")
    parser.add_argument("--root", type=Path, default=ROOT, help="data/ 가 있는 트리")
    parser.add_argument("--json", type=Path, default=None, help="집계를 JSON으로도 저장")
    args = parser.parse_args(argv)
    results, missing = run(args.root)
    summary = summarize(results, missing=missing)
    summary["a_correlation"] = a_correlation(results, load_a_pnl(args.root))
    print_report(results, summary)
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "results": results},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n저장: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
