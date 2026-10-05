r"""트랙 A 성과 리포트 — 운영 DB 실거래의 CAGR·MDD. 읽기 전용.

설계: docs/superpowers/specs/2026-10-04-performance-report-design.md

    .\.venv\Scripts\python.exe scripts\performance_report.py --track A --root D:\Private\stock-prod

결과는 이 트리의 data/performance/track_a/<실행ID>/ 에 네 파일로 남는다. 대표값은 전략 자본
기준 보수 손익이고, 1년 미만 CAGR은 참고로 표시한다.
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

from scripts.performance import write_run  # noqa: E402

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


def print_report(summary: dict, run_dir: Path) -> None:
    print("[트랙 A 성과 — 전략 자본, 보수 비용 대표]")
    print(_line("전체", summary["overall"]))
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
    parser.add_argument("--track", choices=["A"], required=True)
    parser.add_argument("--root", type=Path, default=ROOT, help="운영 DB가 있는 트리")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "data" / "performance")
    parser.add_argument("--until", default=None, help="YYYYMMDD까지만")
    args = parser.parse_args(argv)
    # 운영 PC 콘솔은 cp949라 em dash(—) 같은 문자를 못 찍는다. 죽지 말고 치환한다.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")

    trades, source = load_track_a(args.root, until=args.until)
    run_dir, summary = write_run(
        args.out_dir, "track_a", trades, TRACK_A_SEGMENTS, now=datetime.now(KST),
        manifest_extra={"source": source, "account_capital_krw": ACCOUNT_CAPITAL_KRW},
        extra_summary={"account_reference": account_reference(trades)},
        extra_warnings=("계좌 기준 지표는 참고용 — 모의 계좌 분기 교체, 초기 소량 거래.",),
    )
    print_report(summary, run_dir)
    return 0 if summary["overall"]["conservative"]["n"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
