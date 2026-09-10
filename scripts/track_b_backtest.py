"""트랙 B 진입 규칙 비교 하네스 — 읽기 전용.

사전 등록한 세 축(R1·R2·R3)을 같은 표본에 돌려 스펙 §5의 관문으로 판정한다.
청산은 세 축 모두 트랙 A와 같은 한 벌이라 진입 시각만 비교된다.

strategy_backtest.py 는 09:00~09:30 장벽 모델에 묶여 있어 재사용하지 않는다.
유니버스 로딩과 봉 캐시 I/O만 가져다 쓴다.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.strategy_backtest import BAR_CACHE_DIR, load_universes, read_cached_bars  # noqa: E402
from scripts.track_b_rules import DEFAULT_PARAMS, RULES, build_context, resolve_exit  # noqa: E402
from src import warmup as warmup_mod  # noqa: E402
from src.modules import f1_selector  # noqa: E402

SIGNAL_START = "093500"
ENTRY_DEADLINE = "140000"
DEPTH = 5

# 스펙 §5.2 — 체결 가정을 셋 얹어 부호가 유지되는지만 본다. 최댓값을 고르지 않는다.
SLIPPAGES = (0.000, 0.002, 0.004)
MIN_ENTRY_DAYS = 3


def find_signal(
    bars_by_ticker: dict[str, list[dict]],
    ranked_tickers: list[str],
    rule_key: str,
    params: dict,
    warmup_by_ticker: dict[str, list[dict]] | None = None,
) -> dict | None:
    """봉을 시간 순으로 훑어 첫 신호에서 멈춘다.

    같은 봉에서 둘 이상이 신호를 내면 최상위 랭크를 고른다. 랭크 1이 나중에
    신호를 낼지 기다리는 해석은 실시간에 구현 불가라 쓰지 않는다.
    """
    rule = RULES[rule_key]
    warm = warmup_by_ticker or {}
    contexts = {
        t: build_context(bars, params, warmup=warm.get(t))
        for t, bars in bars_by_ticker.items()
        if bars
    }
    times = sorted({
        b["time"]
        for t in ranked_tickers
        for b in bars_by_ticker.get(t, [])
        if SIGNAL_START <= b["time"] <= ENTRY_DEADLINE
    })
    for time_ in times:
        for rank, ticker in enumerate(ranked_tickers, start=1):
            bars = bars_by_ticker.get(ticker)
            ctx = contexts.get(ticker)
            if not bars or ctx is None:
                continue
            idx = next((i for i, b in enumerate(bars) if b["time"] == time_), None)
            if idx is None or ctx["gap_block"][idx]:
                continue
            if rule(bars, idx, ctx, params):
                return {
                    "ticker": ticker, "signal_idx": idx,
                    "signal_time": time_, "rank": rank,
                }
    return None


def simulate_day(
    date: str,
    universe: list[dict],
    bars_by_ticker: dict[str, list[dict]],
    rule_key: str,
    params: dict,
    *,
    slippage: float = 0.0,
    depth: int = DEPTH,
    warmup_by_ticker: dict[str, list[dict]] | None = None,
    warmup_days: int = 1,
    ranked_tickers: list[str] | None = None,
) -> dict | None:
    """하루 한 건. 신호가 없거나 진입가가 없으면 None(미진입)이다.

    반환 행의 ``warmup_bars``·``warmed``는 실제로 진입한 종목의 워밍업 상태다
    (``src.warmup.meta``) — 산출물만 보고도 어느 모드에서 나온 값인지 알 수
    있어야 22거래일 기존 표본과 비교 가능하다(스펙 §8).
    """
    # 복원된 유니버스(재생 스펙 §2.3)에는 순위를 다시 매길 속성이 없다.
    # 기록된 순서를 그대로 자른다.
    if ranked_tickers is None:
        ranked = f1_selector.rank_candidates(universe)[:depth]
        ranked_tickers = [str(r["ticker"]) for r in ranked if r.get("ticker")]
    else:
        ranked_tickers = [str(t) for t in ranked_tickers][:depth]
    signal = find_signal(
        bars_by_ticker, ranked_tickers, rule_key, params,
        warmup_by_ticker=warmup_by_ticker,
    )
    if signal is None:
        return None

    bars = bars_by_ticker[signal["ticker"]]
    entry_idx = signal["signal_idx"] + 1
    if entry_idx >= len(bars):
        return None
    entry_price = bars[entry_idx]["open"] * (1 + slippage)
    if entry_price <= 0:
        return None

    exit_ = resolve_exit(bars, entry_idx, entry_price)
    warm = (warmup_by_ticker or {}).get(signal["ticker"]) or []
    warmup_state = warmup_mod.meta(warm, warmup_days)
    return {
        "date": date,
        "ticker": signal["ticker"],
        "rank": signal["rank"],
        "signal_time": signal["signal_time"],
        "entry_time": bars[entry_idx]["time"],
        "entry_price": entry_price,
        "ambiguous": exit_["ambiguous"],
        "reason": exit_["high_first"]["reason"],
        "exit_time": exit_["high_first"]["exit_time"],
        "pct": exit_["pct"],
        "warmup_bars": warmup_state["warmup_bars"],
        "warmed": warmup_state["warmed"],
    }


def bootstrap_ci(
    values: list[float],
    *,
    seed: int = 20260828,
    resamples: int = 10000,
    alpha: float = 0.05,
) -> tuple[float | None, float | None]:
    """일별 손익 평균의 백분위 부트스트랩 CI. 시드를 고정해 재현 가능하게 둔다."""
    if not values:
        return (None, None)
    rng = random.Random(seed)
    n = len(values)
    means = sorted(
        sum(rng.choice(values) for _ in range(n)) / n for _ in range(resamples)
    )
    lo = means[int(resamples * alpha / 2)]
    hi = means[min(resamples - 1, int(resamples * (1 - alpha / 2)))]
    return (lo, hi)


def correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return None


def run_axis(
    universes: dict[str, list[dict]],
    bars: dict[str, dict[str, list[dict]]],
    rule_key: str,
    params: dict,
    *,
    slippage: float = 0.0,
    warmup: dict[str, dict[str, list[dict]]] | None = None,
    warmup_days: int = 1,
    ranked_by_date: dict[str, list[str]] | None = None,
) -> list[dict]:
    rows = []
    warm = warmup or {}
    for date in sorted(universes):
        result = simulate_day(
            date, universes[date], bars.get(date, {}), rule_key, params,
            slippage=slippage, warmup_by_ticker=warm.get(date),
            warmup_days=warmup_days,
            ranked_tickers=(ranked_by_date or {}).get(date),
        )
        if result is not None:
            rows.append(result)
    return rows


def sign_stability(
    universes: dict[str, list[dict]],
    bars: dict[str, dict[str, list[dict]]],
    rule_key: str,
    params: dict,
    warmup: dict[str, dict[str, list[dict]]] | None = None,
    ranked_by_date: dict[str, list[str]] | None = None,
) -> list[int]:
    """체결 가정 셋에서의 합계 부호. 하나라도 다르면 관문 2 탈락이다."""
    signs = []
    for slip in SLIPPAGES:
        rows = run_axis(universes, bars, rule_key, params,
                        slippage=slip, warmup=warmup,
                        ranked_by_date=ranked_by_date)
        total = sum(r["pct"] for r in rows if r["pct"] is not None)
        signs.append(0 if total == 0 else (1 if total > 0 else -1))
    return signs


def gate_report(axis_results: dict[str, dict], a_daily: dict[str, float | None]) -> dict:
    """스펙 §5의 관문을 그대로 적용한다. 통과·탈락과 사유를 함께 남긴다."""
    report = {}
    for key, data in axis_results.items():
        rows = data["rows"]
        judged = [r for r in rows if not r["ambiguous"] and r["pct"] is not None]
        pcts = [r["pct"] for r in judged]
        entry_days = len(rows)

        gate1_pass = entry_days >= MIN_ENTRY_DAYS
        gate1_reason = (
            "" if gate1_pass
            else f"진입일 {entry_days}일 < {MIN_ENTRY_DAYS}일 — 판정 불가"
        )

        signs = [s for s in data["slippage_signs"] if s != 0]
        gate2_pass = len(set(signs)) <= 1
        gate2_reason = (
            "" if gate2_pass
            else f"체결 가정에 따라 부호가 뒤집힌다: {data['slippage_signs']}"
        )

        paired_b, paired_a = [], []
        for row in judged:
            a_pct = a_daily.get(row["date"])
            if a_pct is not None:      # None 을 상관계수에 넣으면 TypeError 다
                paired_b.append(row["pct"])
                paired_a.append(a_pct)
        # A가 미진입이거나 AMBIGUOUS라 판정 못 한 날. 둘을 섞어 세므로 문서에
        # 적을 때 "A의 손익이 없는 날"이라고 쓴다.
        a_missing_days = [d for d, v in a_daily.items() if v is None]
        covered = sum(1 for r in rows if r["date"] in a_missing_days)

        lo, hi = bootstrap_ci(pcts)
        report[key] = {
            "entry_days": entry_days,
            "judged_days": len(judged),
            "ambiguous_days": entry_days - len(judged),
            "total_pct": sum(pcts),
            "win_rate": (sum(1 for p in pcts if p > 0) / len(pcts)) if pcts else None,
            "ci_low": lo,
            "ci_high": hi,
            "ci_includes_zero": (lo is not None and hi is not None and lo <= 0 <= hi),
            "corr_with_a": correlation(paired_b, paired_a),
            "a_missing_day_coverage": covered,
            "gate1_pass": gate1_pass,
            "gate1_reason": gate1_reason,
            "gate2_pass": gate2_pass,
            "gate2_reason": gate2_reason,
        }
    return report


def previous_trading_date(dates: list[str], date: str) -> str | None:
    """실제 거래일 목록에서 바로 앞 날짜.

    달력을 쓰지 않는다 — 대체공휴일(20260817)처럼 유니버스에 없는 날을
    자동으로 건너뛴다.
    """
    ordered = sorted(dates)
    if date not in ordered:
        return None
    i = ordered.index(date)
    return ordered[i - 1] if i > 0 else None


def load_warmup(
    date: str, ticker: str, dates: list[str], days: int,
    cache_dir: Path = BAR_CACHE_DIR,
) -> list[dict]:
    """전 거래일 봉을 시간 순으로 이어 붙인다.

    요청한 ``days``일을 가장 가까운 전 거래일부터 거슬러 올라가며 채우다가
    캐시에 없는 날을 만나면 그 자리에서 멈추고 그때까지 모은 봉만 돌려준다 —
    ``days=3``인데 이틀 전 파일이 없으면 하루치만 돌아온다. 그러니 반환값은
    "0봉 아니면 요청한 만큼"이 아니라 0봉부터 요청 일수 전체 분량까지 어디든
    될 수 있다. 그 길이가 지표를 데우기에 충분한지는 여기서 판정하지 않는다
    — 소비하는 쪽이 ``warmup.usable()``로 문턱(``WARMUP_MIN_BARS``)을 적용한다.
    """
    if days <= 0:
        return []
    out: list[dict] = []
    cursor = date
    for _ in range(days):
        previous = previous_trading_date(dates, cursor)
        if previous is None:
            break
        cursor = previous
        cached = read_cached_bars(cursor, ticker, cache_dir)
        if not cached:
            break
        out = sorted(cached, key=lambda r: r["time"]) + out
    return out


def load_bars_for(
    universes: dict[str, list[dict]], *, depth: int = DEPTH,
    cache_dir: Path = BAR_CACHE_DIR,
    ranked_by_date: dict[str, list[str]] | None = None,
) -> tuple[dict[str, dict[str, list[dict]]], dict[str, int]]:
    """캐시에서만 읽는다. 없는 쌍은 세어서 보고한다 — 조용히 빠지면 안 된다.

    ``ranked_by_date``가 있으면 복원된 유니버스(재생 스펙 §2.3)를 쓰는
    것이므로 순위를 다시 매기지 않고 기록된 순서를 그대로 자른다.
    """
    bars: dict[str, dict[str, list[dict]]] = {}
    stats = {"pairs": 0, "missing": 0, "partial": 0}
    for date, rows in universes.items():
        if ranked_by_date is not None:
            tickers = [str(t) for t in ranked_by_date.get(date, [])[:depth]]
        else:
            ranked = f1_selector.rank_candidates(rows)[:depth]
            tickers = [str(row.get("ticker") or "") for row in ranked]
        day: dict[str, list[dict]] = {}
        for ticker in tickers:
            if not ticker:
                continue
            stats["pairs"] += 1
            cached = read_cached_bars(date, ticker, cache_dir)
            if not cached:
                stats["missing"] += 1
                continue
            if max(b["time"] for b in cached) < "140000":
                stats["partial"] += 1
                continue
            day[ticker] = cached
        if day:
            bars[date] = day
    return bars, stats


def a_daily_from_baseline() -> dict[str, float | None]:
    """트랙 A의 일별 손익 — 기존 하네스의 현행 정책(BASELINE)으로 낸다.

    실계좌 체결이 아니라 같은 봉 위의 시뮬레이션이라 B와 대칭이다. 미진입일은
    None으로 두고 커버리지 계산에 쓴다.
    """
    from scripts.strategy_backtest import BASELINE, load_bar_cache, tickers_needed
    from scripts.strategy_backtest import load_universes as _lu
    from scripts.strategy_backtest import simulate_day as a_simulate_day

    universes = _lu()
    bars = load_bar_cache(tickers_needed(universes, [BASELINE]))
    daily: dict[str, float | None] = {}
    for date in sorted(universes):
        # 인자 순서에 주의한다 — (date, universe, policy, bars) 다.
        row = a_simulate_day(date, universes[date], BASELINE, bars.get(date, {}))
        daily[date] = row.get("realized_pct") if row.get("entered") else None
    return daily


def count_warmed(warmup_by_date: dict[str, dict[str, list[dict]]]) -> int:
    """실제로 데워진 쌍의 수 — 봉 수가 아니라 실행 시점과 같은 판정을 쓴다.

    한때 이 자리가 ``len(rows) >= WARMUP_MIN_BARS``였다. 개수 문턱이 하한보다 높던
    동안에는 우연히 같은 값을 찍었지만, 하한이 수렴 요건으로 내려간 뒤로는 "봉이
    그만큼 있다"는 뜻일 뿐이라 오전만 긴 파일을 데운 것으로 과다 보고한다.
    """
    return sum(
        1
        for day in warmup_by_date.values()
        for rows in day.values()
        if warmup_mod.covers_session(rows)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="트랙 B 진입 규칙 비교")
    parser.add_argument("--depth", type=int, default=DEPTH)
    parser.add_argument(
        "--warmup-days", type=int, default=1,
        help="지표에 먹일 전 거래일 수. 0이면 구 동작(일 단위 초기화)",
    )
    parser.add_argument("--out", default="", help="결과 JSON 경로")
    parser.add_argument(
        "--universes", type=Path, default=None,
        help="replay_universe.py 산출물. 주면 f1_snapshots 대신 이것을 쓴다",
    )
    args = parser.parse_args(argv)

    universes: dict[str, list[dict]]
    if args.universes is not None:
        from scripts.track_b_backfill import load_restored_universes

        ranked_by_date = load_restored_universes(args.universes)
        universes = {date: [] for date in ranked_by_date}
    else:
        ranked_by_date = None
        universes = load_universes()
    bars, stats = load_bars_for(
        universes, depth=args.depth, ranked_by_date=ranked_by_date
    )
    dates = sorted(universes)
    warmup = {
        date: {
            ticker: load_warmup(date, ticker, dates, args.warmup_days)
            for ticker in day
        }
        for date, day in bars.items()
    }
    warmed_pairs = count_warmed(warmup)
    total_pairs = sum(len(day) for day in warmup.values())
    print(f"표본: {len(bars)}거래일 / 쌍 {stats['pairs']} "
          f"(없음 {stats['missing']}, 09:00~09:30만 {stats['partial']})")
    print(f"워밍업: {args.warmup_days}일 요청 / 실제 데운 쌍 "
          f"{warmed_pairs}/{total_pairs}")
    if stats["missing"] + stats["partial"] > 0:
        print("  ! 전 세션 봉이 없는 쌍이 있다. track_b_backfill.py 를 먼저 돌린다.")

    axis_results = {}
    for key in sorted(RULES):
        rows = run_axis(
            universes, bars, key, DEFAULT_PARAMS,
            warmup=warmup, warmup_days=args.warmup_days,
            ranked_by_date=ranked_by_date,
        )
        axis_results[key] = {
            "rows": rows,
            "slippage_signs": sign_stability(
                universes, bars, key, DEFAULT_PARAMS, warmup=warmup,
                ranked_by_date=ranked_by_date,
            ),
        }

    report = gate_report(axis_results, a_daily_from_baseline())
    for key in sorted(report):
        r = report[key]
        print(f"\n[{key}] 진입 {r['entry_days']}일 / 판정 {r['judged_days']}일 "
              f"(AMBIGUOUS {r['ambiguous_days']})")
        print(f"  합계 {r['total_pct']:+.2f}%  승률 "
              f"{'-' if r['win_rate'] is None else format(r['win_rate'] * 100, '.1f')}%")
        print(f"  일평균 95% CI [{r['ci_low']}, {r['ci_high']}] "
              f"{'— 0을 포함한다 (차이 없음)' if r['ci_includes_zero'] else ''}")
        print(f"  A와의 상관 {r['corr_with_a']}  A 미진입일 커버 "
              f"{r['a_missing_day_coverage']}일")
        print(f"  관문1 {'통과' if r['gate1_pass'] else '탈락 — ' + r['gate1_reason']}")
        print(f"  관문2 {'통과' if r['gate2_pass'] else '탈락 — ' + r['gate2_reason']}")

    if args.universes is not None:
        print(
            "  주의: 복원 유니버스에는 종목 속성이 없어 관문 3(트랙 A 상관)은 "
            "의미 없는 값이다. 관문 1·2·4만 읽어라.",
            flush=True,
        )

    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "report": report,
                    "axes": axis_results,
                    "warmup_days": args.warmup_days,
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n결과: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
