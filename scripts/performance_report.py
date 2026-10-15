r"""트랙 성과 리포트 — 운영 데이터의 CAGR·MDD. 읽기 전용.

설계: docs/superpowers/specs/2026-10-04-performance-report-design.md

    .\.venv\Scripts\python.exe scripts\performance_report.py --track A --root D:\Private\stock-prod
    .\.venv\Scripts\python.exe scripts\performance_report.py --track C1 --root D:\Private\stock-prod

결과는 이 트리의 data/performance/track_<a|c1>/<실행ID>/ 에 네 파일로 남는다. 대표값은 전략
자본 기준 보수 손익이고, 1년 미만 CAGR은 참고로 표시한다.

C1(종가 매수 → 익일 F4 청산 그림자)은 overnight_sieve의 주 표본(랭크 1)을 같은 형식으로 내고,
같은 기간 트랙 A를 나란히 붙인다. 그림자라 체결 가정이 낙관적이다. C1 손익은 사전등록 첫
점검일(C1 스펙 §4.1, 2026-10-31) 전에는 보지 않으므로 그 전에는 실행을 거부한다.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.performance import (  # noqa: E402
    annotate,
    compute_metrics,
    period_of,
    write_run,
)

KST = ZoneInfo("Asia/Seoul")
ACCOUNT_CAPITAL_KRW = 500_000_000

# 구간 경계와 그때 바뀐 조건(설명). 실제 복수 변경은 구간 안 지문 개수로 자동 검사한다.
TRACK_A_SEGMENTS: list[dict] = [
    {"name": "초기 운영", "start": "20260702", "end": "20260806",
     "changed": ["(PAPER 표시 전, 지문 기록 없음)"]},
    {"name": "PAPER 레거시", "start": "20260807", "end": "20260910",
     "changed": ["실행 모드·지문 기록 시작, 진입 안전장치 강화 묶음"]},
    {"name": "빠른 경로", "start": "20260911", "end": "20261001",
     "changed": ["빠른 경로 하이브리드 활성(PAPER_FAST_HYBRID=1). 0914~1001은 WS 파서 결함 기간"]},
    {"name": "파서 수정 후", "start": "20261002", "end": None,
     "changed": ["WS 파서 헤더 분할 수정", "상한가 잠김 예외"]},
]


def load_track_a(root: Path, until: str | None = None) -> tuple[list[dict], dict]:
    path = root / "data" / "db" / "trading.db"
    if not path.exists():
        raise FileNotFoundError(f"운영 DB가 없다: {path} (trading.db)")
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "select * from trades where track='A' and status='CLOSED' order by date, id"
        ).fetchall()
    finally:
        con.close()

    trades: list[dict] = []
    for r in rows:
        if until and str(r["date"]) > until:
            continue
        gross = r["pnl_pct"]
        if gross is None and r["entry_price"] and r["exit_price"]:
            gross = (float(r["exit_price"]) / float(r["entry_price"]) - 1) * 100
        excluded = None
        if r["close_reason"] == "MANUAL":
            excluded = "MANUAL"
        elif gross is None:
            excluded = "NO_PNL"
        trades.append({
            "trade_id": str(r["id"]), "ticker": str(r["ticker"]), "track": "A",
            "entry_at": str(r["entry_at"]), "exit_at": str(r["exit_at"]),
            "gross_pct": None if gross is None else float(gross),
            "exit_reason": str(r["close_reason"] or "UNKNOWN"), "excluded": excluded,
            "meta": {"date": r["date"], "name": r["name"],
                     "strategy_fingerprint": r["strategy_fingerprint"],
                     "experiment_id": r["experiment_id"],
                     "execution_mode": r["execution_mode"], "pnl_amount": r["pnl_amount"]},
        })
    used = [t for t in trades if not t["excluded"]]
    source = {"db_path": str(path), "rows_read": len(trades), "rows_used": len(used),
              "first_date": trades[0]["meta"]["date"] if trades else None,
              "last_date": trades[-1]["meta"]["date"] if trades else None, "until": until}
    return trades, source


def account_reference(trades: list[dict], capital: float = ACCOUNT_CAPITAL_KRW) -> dict:
    """계좌 기준 참고 지표 — 원 단위 누적 손익과 낙폭. 대표값이 아니다."""
    xs = sorted((t for t in trades if not t.get("excluded")), key=lambda t: t["exit_at"])
    cum, peak, mdd = 0.0, 0.0, 0.0
    for trade in xs:
        cum += float((trade.get("meta") or {}).get("pnl_amount") or 0.0)
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return {"capital_krw": capital, "total_pnl_krw": cum, "total_return": cum / capital,
            "mdd_krw": mdd, "mdd": mdd / capital,
            "note": "모의 계좌 분기 교체와 초기 소량 거래 때문에 참고용"}


C1_FIRST_REVIEW = "20261031"   # C1 스펙 §4.1 첫 점검 — 그 전에는 손익을 보지 않는다
C1_SEGMENTS: list[dict] = [
    {"name": "C1 그림자", "start": "20260923", "end": None,
     "changed": ["사전등록 규칙(C1 스펙 §2) — 등록 뒤 바꾸지 않는다"]},
]
C1_ENTRY_TIME = "15:30:00"     # 종가(마감 동시호가) 진입 가정


def c1_review_allowed(today: str) -> bool:
    return today >= C1_FIRST_REVIEW


def _today() -> str:
    return datetime.now(KST).strftime("%Y%m%d")


def _iso_day(day: str) -> str:
    return f"{day[:4]}-{day[4:6]}-{day[6:8]}"


def c1_trades(results: list[dict]) -> list[dict]:
    """overnight_sieve 주 표본(랭크 1, 분봉 완결, 기업 행위 의심 제외) → 표준 거래.

    진입은 D-0 15:30(종가), 청산은 D+1 재생 청산 시각이다. 비용은 performance가 같은 상수로
    다시 매기므로 보수 손익이 C1 판정값(net_slip_pct)과 같다.
    """
    from scripts.overnight_sieve import _primary_rank1

    trades: list[dict] = []
    for r in _primary_rank1(results):
        hms = str(r.get("exit_time") or "151500").ljust(6, "0")
        trades.append({
            "trade_id": f"C1-{r['date']}-{r['ticker']}", "ticker": str(r["ticker"]),
            "track": "C1",
            "entry_at": f"{_iso_day(str(r['date']))}T{C1_ENTRY_TIME}+09:00",
            "exit_at": f"{_iso_day(str(r['next_date']))}T{hms[:2]}:{hms[2:4]}:{hms[4:6]}+09:00",
            "gross_pct": float(r["gross_pct"]), "exit_reason": str(r["exit_reason"]),
            "excluded": None,
            "meta": {"date": r["date"], "next_date": r["next_date"], "rank": r["rank"],
                     "gap_pct": r.get("gap_pct")},
        })
    return trades


def same_period(trades: list[dict], period: dict | None) -> list[dict]:
    """``period``(start·end, ISO 날짜) 안에 청산한 거래만."""
    if period is None:
        return []
    return [t for t in trades if period["start"] <= str(t["exit_at"])[:10] <= period["end"]]


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:+.2f}%"


def _line(label: str, block: dict) -> str:
    m = block["conservative"]
    if m["cagr_suppressed"]:
        ref = " (30일 미만)"
    elif m["cagr_reference_only"]:
        ref = " (참고)"
    else:
        ref = ""
    win = "—" if m["win_rate"] is None else f"{m['win_rate'] * 100:.0f}%"
    return (f"{label:<14} n={m['n']:>3}  CAGR {_pct(m['cagr'])}{ref}  MDD {_pct(m['mdd'])}  "
            f"총수익 {_pct(m['total_return'])}  승률 {win}")


def print_report(summary: dict, run_dir: Path, title: str = "트랙 A") -> None:
    print(f"[{title} 성과 — 전략 자본, 보수 비용 대표]")
    print(_line("전체", summary["overall"]))
    same = summary.get("track_a_same_period")
    if same is not None:
        print(_line("같은 기간 A", same))
    for seg in summary["segments"]:
        flag = "  ※ 복수 변경" if seg["multiple_changes"] else ""
        print(_line(seg["name"], seg) + flag)
    for year in summary["years"]:
        print(_line(str(year["year"]), year))
    acc = summary.get("account_reference") or {}
    if acc:
        print(f"계좌 기준(참고): 누적 {acc['total_pnl_krw']:+,.0f}원  "
              f"MDD {acc['mdd_krw']:+,.0f}원")
    print(f"제외: {summary['excluded']}  모르는 청산 사유: {summary['unknown_reasons']}")
    for warning in summary["warnings"]:
        print(f"- {warning}")
    print(f"저장: {run_dir}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="트랙 성과 리포트 (CAGR·MDD)")
    parser.add_argument("--track", choices=["A", "C1"], required=True)
    parser.add_argument("--root", type=Path, default=ROOT, help="운영 DB가 있는 트리")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "data" / "performance")
    parser.add_argument("--until", default=None, help="YYYYMMDD까지만")
    parser.add_argument(
        "--allow-early", action="store_true",
        help=f"C1: 첫 점검일({C1_FIRST_REVIEW}) 전에도 낸다(사전등록 위반 — 쓰지 않는다)",
    )
    args = parser.parse_args(argv)
    # 운영 PC 콘솔은 cp949라 em dash(—) 같은 문자를 못 찍는다. 죽지 말고 치환한다.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")

    if args.track == "C1":
        return _main_c1(args)

    trades, source = load_track_a(args.root, until=args.until)
    run_dir, summary = write_run(
        args.out_dir, "track_a", trades, TRACK_A_SEGMENTS, now=datetime.now(KST),
        manifest_extra={"source": source, "account_capital_krw": ACCOUNT_CAPITAL_KRW},
        extra_summary={"account_reference": account_reference(trades)},
        extra_warnings=("계좌 기준 지표는 참고용 — 모의 계좌 분기 교체, 초기 소량 거래.",),
    )
    print_report(summary, run_dir)
    return 0 if summary["overall"]["conservative"]["n"] else 2


def _main_c1(args: argparse.Namespace) -> int:
    if not args.allow_early and not c1_review_allowed(_today()):
        print(f"C1 손익은 첫 점검일({C1_FIRST_REVIEW}) 전에는 보지 않는다(C1 스펙 §4.1 사전등록). "
              "아무것도 계산하지 않았다.")
        return 3
    from scripts.overnight_sieve import run as sieve_run

    results, missing = sieve_run(args.root, write_results=False)
    if args.until:
        results = [r for r in results if str(r["date"]) <= args.until]
    trades = c1_trades(results)

    same_block = None
    try:
        a_trades, _ = load_track_a(args.root, until=args.until)
    except FileNotFoundError:
        a_trades = []
    a_same = [t for t in annotate(same_period(a_trades, period_of(trades)), [])
              if not t.get("excluded")]
    if trades:
        same_block = {"period": period_of(a_same),
                      "conservative": compute_metrics(a_same, "net_conservative_pct"),
                      "cost_only": compute_metrics(a_same, "net_cost_pct")}

    run_dir, summary = write_run(
        args.out_dir, "track_c1", trades, C1_SEGMENTS, now=datetime.now(KST),
        holding_overnight=True,
        manifest_extra={"source": {"root": str(args.root), "sieve_missing": missing,
                                   "until": args.until}},
        extra_summary={"track_a_same_period": same_block, "c1_missing": missing},
        extra_warnings=(
            "C1은 그림자다 — 종가에 원하는 수량이 체결됐다고 가정하고(진입 슬리피지 0) 1분봉"
            "(저가 먼저)으로 청산을 재생한다. 같은 기간 A(실제 체결)보다 좋게 나와도 C1이 낫다는"
            " 뜻이 아니다. 차이는 15:20 병행 기록과 ② 모의 실주문으로 잰다.",
            "판정은 이 보고서가 아니라 C1 스펙 §4.1(n ≥ 50, 평균·CI 하한·상위 2건 제거)이다.",
        ),
    )
    print_report(summary, run_dir, title="C1 그림자")
    return 0 if summary["overall"]["conservative"]["n"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
