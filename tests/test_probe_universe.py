"""빠른 경로 날의 유니버스 복원 — 프로브 덤프에서 30종목 개장 시세를 되살린다.

2026-09-11 fast path 승격 뒤 f1_snapshots 는 통과 후보(1~3행)만 남아
load_universes 의 30행 필터에 걸렸고, 트랙 B 백필이 09/10 이후 한 날도 대상에
넣지 못했다. 프로브 파일(data/paper_fast_probe/<date>.jsonl)에는 그날 개장
멀티시세 30행이 그대로 있으므로 거기서 복원한다.
"""

import json
from pathlib import Path

from scripts.probe_universe import load_probe_universe
from scripts.strategy_backtest import load_universes


def _open_row(ticker: str, gap_pct: float, prev_close: float = 10000.0, qty: int = 1000) -> dict:
    price = prev_close * (1 + gap_pct / 100)
    return {
        "inter_shrn_iscd": ticker, "inter_kor_isnm": f"종목{ticker}",
        "inter2_prpr": str(prev_close), "inter2_prdy_clpr": str(prev_close),
        "intr_antc_vol": str(qty), "intr_antc_cntg_prdy_ctrt": f"{gap_pct:.2f}",
        "intr_antc_cntg_vrss": str(price - prev_close), "acml_vol": "0",
        "acml_tr_pbmn": "0", "inter2_askp": str(price), "hour_cls_code": "B",
    }


def _write_probe(path: Path, tickers: list[str], gaps: list[float]) -> None:
    lines = [
        {"event": "PAPER_FAST_PROBE_PREOPEN_START", "phase": "PREOPEN"},
        {"event": "PAPER_FAST_PROBE_RANKING", "phase": "PREOPEN", "market": "J",
         "response": {"output": [
             {"mksc_shrn_iscd": t, "hts_kor_isnm": f"종목{t}", "stck_prpr": "10000",
              "avrg_vol": "100000"} for t in tickers]}},
        {"event": "PAPER_FAST_PROBE_OPEN_MULTI", "phase": "OPEN", "market": "SHORTLIST",
         "response": {"output": [_open_row(t, g) for t, g in zip(tickers, gaps)]}},
        {"event": "PAPER_FAST_PROBE_OPEN_DONE", "phase": "OPEN", "shadow_tickers": [tickers[0]]},
    ]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in lines), encoding="utf-8")


def test_load_probe_universe_returns_every_open_row_not_only_accepted(tmp_path):
    tickers = [f"{i:06d}" for i in range(1, 31)]
    gaps = [5.0] * 3 + [0.5] * 27  # 3개만 갭 범위, 나머지는 프로브가 거부했을 행
    probe = tmp_path / "20260918.jsonl"
    _write_probe(probe, tickers, gaps)

    rows = load_probe_universe(probe)

    assert len(rows) == 30
    by_ticker = {r["ticker"]: r for r in rows}
    assert by_ticker["000001"]["gap_pct"] == 0.05 and by_ticker["000001"]["gap_allowed"] is True
    assert by_ticker["000010"]["gap_allowed"] is False
    assert by_ticker["000001"]["avg_amount_5d"] == 100000 * 10000  # 랭킹 avrg_vol × 가격


def test_load_probe_universe_uses_the_last_completed_cycle_only(tmp_path):
    tickers = [f"{i:06d}" for i in range(1, 31)]
    probe = tmp_path / "20260918.jsonl"
    _write_probe(probe, tickers, [5.0] * 30)
    # 두 번째 사이클이 시작만 하고 OPEN_DONE 없이 끝나면 첫 사이클 결과가 남는다.
    with probe.open("a", encoding="utf-8") as fh:
        fh.write("\n" + json.dumps({"event": "PAPER_FAST_PROBE_PREOPEN_START", "phase": "PREOPEN"}))
    assert len(load_probe_universe(probe)) == 30
    assert load_probe_universe(tmp_path / "missing.jsonl") == []


def test_load_universes_falls_back_to_the_probe_dump_for_thin_snapshots(tmp_path):
    snapshots = tmp_path / "f1_snapshots"
    probes = tmp_path / "paper_fast_probe"
    snapshots.mkdir()
    probes.mkdir()
    # 빠른 경로 날: 스냅샷은 통과 후보 1행뿐, 프로브에는 30행.
    (snapshots / "20260918_090000.jsonl").write_text(
        json.dumps({"ticker": "000001", "gap_pct": 0.05}), encoding="utf-8"
    )
    _write_probe(probes / "20260918.jsonl", [f"{i:06d}" for i in range(1, 31)], [5.0] * 30)
    # 레거시 날: 스냅샷 55행이면 그대로 쓴다(프로브가 있어도).
    (snapshots / "20260922_090157.jsonl").write_text(
        "\n".join(json.dumps({"ticker": f"{i:06d}", "gap_pct": 0.03}) for i in range(55)),
        encoding="utf-8",
    )

    without = load_universes(snapshots)
    with_probe = load_universes(snapshots, probe_dir=probes)

    assert set(without) == {"20260922"}
    assert set(with_probe) == {"20260918", "20260922"}
    assert len(with_probe["20260918"]) == 30
    assert len(with_probe["20260922"]) == 55
