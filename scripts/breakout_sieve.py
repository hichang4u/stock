"""H5 판정기 CLI — 봉인된 판정을 한 번 실행한다 (스펙 §6.3~§6.4).

실데이터를 읽는 경로는 --verdict 하나뿐이다. 개발과 테스트는 합성 픽스처만 쓴다.
결과는 data/h5/verdict_log.jsonl 에 추가만 하고, 사람이 스펙 §9 표로 옮긴다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.breakout_gates import (  # noqa: E402
    equity_curve_mdd,
    evaluate,
    recorded_metrics,
    samples_from_trades,
)
from scripts.breakout_panel import KNOWN_KIND, KNOWN_SECUGRP, Panel, load_panel  # noqa: E402
from scripts.breakout_rules import run_rules  # noqa: E402
from scripts.krx_daily_fetch import BASE_KINDS, DEFAULT_CACHE  # noqa: E402

SPEC = "docs/superpowers/specs/2026-09-30-h5-daily-breakout-hypothesis.md"
CODE_FILES = (
    "scripts/breakout_sieve.py",
    "scripts/breakout_panel.py",
    "scripts/breakout_rules.py",
    "scripts/breakout_gates.py",
    "scripts/krx_daily_fetch.py",
)
DEFAULT_LOG = ROOT / "data" / "h5" / "verdict_log.jsonl"
MAX_MISSING_RATIO = 0.01
VERDICT = "VERDICT"
INCOMPLETE_DATA = "INCOMPLETE_DATA"


class SealError(RuntimeError):
    """봉인 조건을 어겼다 — 판정을 계산하지 않는다."""


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def check_clean(root: Path) -> None:
    if _git(root, "status", "--porcelain"):
        raise SealError("워킹트리가 깨끗하지 않다 — 커밋되지 않은 변경으로 판정하지 않는다")


def commit_ids(root: Path) -> dict[str, str]:
    return {
        "head": _git(root, "rev-parse", "HEAD"),
        "spec": _git(root, "log", "-1", "--format=%H", "--", SPEC),
        "code": _git(root, "log", "-1", "--format=%H", "--", *CODE_FILES),
    }


def manifest_sha256(cache_dir: Path) -> tuple[str, int]:
    """캐시 파일 목록(상대 경로·크기)의 SHA-256. 판정이 어떤 데이터를 봤는지 고정한다."""
    digest = hashlib.sha256()
    count = 0
    for path in sorted(cache_dir.rglob("*.json.gz")):
        rel = path.relative_to(cache_dir).as_posix()
        digest.update(f"{rel}|{path.stat().st_size};".encode())
        count += 1
    return digest.hexdigest(), count


def base_snapshot_gaps(cache_dir: Path, calendar: list[str]) -> list[str]:
    """스냅샷 날짜마다 두 시장이 모두 있고, 달력의 모든 달에 짝이 맞는 스냅샷이 있어야 한다."""
    dates = {kind: {p.name[:8] for p in (cache_dir / kind).glob("*.json.gz")}
             for kind in BASE_KINDS}
    every = set().union(*dates.values())
    gaps = sorted(f"{kind}/{d}" for d in every for kind in BASE_KINDS if d not in dates[kind])
    paired_months = {d[:6] for d in set.intersection(*dates.values())}
    gaps += sorted(f"month/{m}" for m in {d[:6] for d in calendar} - paired_months)
    return gaps


def unknown_categories(panel: Panel) -> dict[str, list[str]]:
    unknown = {
        "SECUGRP_NM": sorted(panel.categories.get("SECUGRP_NM", set()) - KNOWN_SECUGRP),
        "KIND_STKCERT_TP_NM": sorted(
            panel.categories.get("KIND_STKCERT_TP_NM", set()) - KNOWN_KIND),
    }
    return {field: values for field, values in unknown.items() if values}


def verdict_runs(log_path: Path) -> int:
    if not log_path.exists():
        return 0
    return sum(
        1 for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("status") == VERDICT
    )


def append_log(log_path: Path, record: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8", newline="") as fh:
        print(json.dumps(record, ensure_ascii=False), file=fh)


def close_on(panel: Panel) -> Callable[[str, str], float | None]:
    def lookup(ticker: str, day: str) -> float | None:
        s = panel.series.get(ticker)
        if s is None:
            return None
        i = s.pos(day)
        return s.adj_close(i) if i >= 0 else None

    return lookup


def top_events(panel: Panel, limit: int = 10) -> list[dict]:
    events = [
        (abs(math.log(f)), t, d, f)
        for t, s in panel.series.items() for d, f in s.events if f > 0
    ]
    events.sort(reverse=True)
    return [{"ticker": t, "date": d, "factor": f} for _, t, d, f in events[:limit]]


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else math.nan


def run_verdict(
    root: Path,
    cache_dir: Path,
    log_path: Path,
    rerun_reason: str = "",
    *,
    load: Callable[[Path], Panel] = load_panel,
) -> dict:
    check_clean(root)
    prior = verdict_runs(log_path)
    if prior and not rerun_reason.strip():
        raise SealError(
            f"판정 기록이 이미 {prior}건 있다 — 다시 돌리려면 --rerun-reason 이 필요하다")
    base_record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "commits": commit_ids(root),
        "rerun_reason": rerun_reason.strip(),
        "prior_verdicts": prior,
    }

    panel = load(cache_dir)
    ratio = panel.missing_ratio()
    if ratio > MAX_MISSING_RATIO:
        append_log(log_path, {**base_record, "status": INCOMPLETE_DATA, "missing_ratio": ratio,
                              "missing_days": len(panel.missing)})
        raise SealError(f"결측 {ratio:.2%} — 한도 1% 초과. 다시 받은 뒤 실행한다")
    gaps = base_snapshot_gaps(cache_dir, panel.calendar)
    if gaps:
        append_log(log_path, {**base_record, "status": INCOMPLETE_DATA,
                              "base_snapshot_gaps": gaps[:20], "base_gap_count": len(gaps)})
        raise SealError(
            f"기본정보 스냅샷 누락 {len(gaps)}건 (예: {gaps[:3]}) — 다시 받은 뒤 실행한다")
    unknown = unknown_categories(panel)
    if unknown:
        raise SealError(f"처음 보는 범주값 {unknown} — 스펙에 처리를 기록한 뒤 다시 실행한다")
    digest, files = manifest_sha256(cache_dir)

    run = run_rules(panel)
    samples, dropped = samples_from_trades(run.trades)
    matched_sizes = [t.matched_n for t in run.trades if t.matched_n > 0]
    record = {
        **base_record,
        "status": VERDICT,
        "manifest_sha256": digest,
        "manifest_files": files,
        "missing_ratio": ratio,
        "unknown_days": len(panel.unknown_days),
        "verdict": evaluate(samples),
        "recorded": {
            **recorded_metrics(samples),
            "trades_entered": len(run.trades),
            "dropped": dict(dropped),
            "counts": dict(run.counts),
            "event_study": {
                "n_net": len(run.event_net),
                "n_excess": len(run.event_excess),
                "mean_net": _mean(run.event_net),
                "mean_excess": _mean(run.event_excess),
            },
            "universe_size_by_year": {
                year: {"min": min(v), "median": statistics.median(v), "max": max(v)}
                for year, v in sorted(run.universe_sizes.items())
            },
            "matched_control_size": {
                "min": min(matched_sizes) if matched_sizes else 0,
                "median": statistics.median(matched_sizes) if matched_sizes else 0,
            },
            "corporate_events": sum(len(s.events) for s in panel.series.values()),
            "top_events": top_events(panel),
            "no_basis_rows": sum(s.no_basis for s in panel.series.values()),
            "equity": equity_curve_mdd(samples, close_on(panel), panel.calendar),
            "sect_values": sorted(panel.categories.get("SECT_TP_NM", set())),
        },
    }
    append_log(log_path, record)
    return record


def print_report(record: dict) -> None:
    v = record["verdict"]
    r = record["recorded"]
    print(f"판정: {'통과' if v['passed'] else '실패'} — 완료 거래 {v['n']}건")
    for gate, ok in v["gates"].items():
        print(f"  {gate}: {'통과' if ok else '실패'}")
    print(f"  A 평균 {v['mean_net']:+.4%} [{r['lower_net']:+.4%}, {r['upper_net']:+.4%}]")
    print(f"  X 평균 {v['mean_excess']:+.4%} [{v['lower_excess']:+.4%}, {v['upper_excess']:+.4%}]")
    print(f"  상위 1% 제거 — A {v['trimmed_net']:+.4%} · X {v['trimmed_excess']:+.4%}")
    for period, mean in v["period_excess"].items():
        print(f"  {period} X 평균 " + ("없음" if mean is None else f"{mean:+.4%}"))
    print(f"  HEAD {record['commits']['head'][:7]} · 매니페스트 {record['manifest_sha256'][:12]}")
    print("  스펙 §9 표로 옮긴다.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="H5 봉인 판정 (스펙 §6.4)")
    parser.add_argument("--verdict", action="store_true",
                        help="봉인된 판정을 실행한다 — 실데이터를 읽는다")
    parser.add_argument("--rerun-reason", default="", help="두 번째 이후 실행의 사유")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)

    if not args.verdict:
        print("실데이터 판정은 --verdict 로만 한다 (스펙 §6.4). 개발·테스트는 합성 픽스처를 쓴다.")
        return 2
    try:
        record = run_verdict(args.root, args.cache_dir, args.log, args.rerun_reason)
    except SealError as exc:
        print(f"봉인 거부: {exc}", file=sys.stderr)
        return 3
    print_report(record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
