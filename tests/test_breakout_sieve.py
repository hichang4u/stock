"""H5 봉인 판정 — 임시 git 저장소와 합성 Panel 만 쓴다. 실제 캐시·로그를 건드리지 않는다."""

import json
import subprocess

import pytest

from scripts.breakout_panel import build_panel
from scripts.breakout_sieve import (
    INCOMPLETE_DATA,
    VERDICT,
    SealError,
    base_snapshot_gaps,
    main,
    manifest_sha256,
    run_verdict,
    verdict_runs,
)
from scripts.krx_daily_fetch import cache_path, save_atomic, weekdays

DAYS = weekdays("20100104", "20110331")


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "README").write_bytes(b"x")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def fake_panel(*, missing_every=0, secugrp="주권"):
    """종가가 날마다 1원씩 오르는 두 종목 — 2011년에는 매일 신고가."""

    def row(ticker, d, i):
        c = 1000 + i
        return {"BAS_DD": d, "ISU_CD": ticker, "ISU_NM": "종목", "TDD_OPNPRC": str(c),
                "TDD_HGPRC": str(c), "TDD_LWPRC": str(c), "TDD_CLSPRC": str(c),
                "CMPPREVDD_PRC": "1" if i else "0", "ACC_TRDVAL": "2000000000"}

    def get_daily(market, d):
        i = DAYS.index(d)
        if missing_every and market == "ksq" and i % missing_every == 0:
            return None
        return [row("000010" if market == "stk" else "000020", d, i)]

    def info(code, group):
        return {"ISU_SRT_CD": code, "SECUGRP_NM": group, "KIND_STKCERT_TP_NM": "보통주",
                "SECT_TP_NM": "", "ISU_ABBRV": "종목"}

    base = [("stk", DAYS[0], [info("000010", secugrp), info("000020", "주권")])]
    return build_panel(DAYS, get_daily, base)


def seed_base_snapshots(cache_dir):
    """DAYS 의 달마다 첫 평일에 두 시장 기본정보 파일(빈 목록)을 써 둔다."""
    seen = set()
    for d in DAYS:
        month = d[:6]
        if month in seen:
            continue
        seen.add(month)
        save_atomic(cache_path(cache_dir, "base/stk", d), [])
        save_atomic(cache_path(cache_dir, "base/ksq", d), [])


def test_main_refuses_without_the_verdict_flag():
    assert main([]) == 2


def test_dirty_tree_is_refused_before_loading(repo, tmp_path):
    (repo / "untracked.txt").write_bytes(b"x")

    def must_not_load(_cache_dir):
        raise AssertionError("데이터를 올리기 전에 막아야 한다")

    with pytest.raises(SealError, match="깨끗"):
        run_verdict(repo, tmp_path / "cache", tmp_path / "log.jsonl", load=must_not_load)


def test_first_run_logs_a_verdict_and_a_second_needs_a_reason(repo, tmp_path):
    cache = tmp_path / "cache"
    seed_base_snapshots(cache)
    log = tmp_path / "h5" / "verdict_log.jsonl"
    record = run_verdict(repo, cache, log, load=lambda _d: fake_panel())

    assert record["status"] == VERDICT
    assert set(record["verdict"]["gates"]) == {"G1", "G2", "G3", "G4", "G5"}
    assert "equity" in record["recorded"] and "counts" in record["recorded"]
    assert record["commits"]["head"]
    assert verdict_runs(log) == 1

    with pytest.raises(SealError, match="rerun-reason"):
        run_verdict(repo, cache, log, load=lambda _d: fake_panel())

    again = run_verdict(repo, cache, log, "판정기 버그 수정 뒤 재실행",
                        load=lambda _d: fake_panel())
    assert again["rerun_reason"] == "판정기 버그 수정 뒤 재실행"
    assert again["prior_verdicts"] == 1
    assert verdict_runs(log) == 2


def test_too_many_missing_days_are_logged_and_refused(repo, tmp_path):
    log = tmp_path / "log.jsonl"
    with pytest.raises(SealError, match="결측"):
        run_verdict(repo, tmp_path / "cache", log, load=lambda _d: fake_panel(missing_every=50))

    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert [x["status"] for x in lines] == [INCOMPLETE_DATA]
    assert verdict_runs(log) == 0


def test_unseen_category_values_are_refused(repo, tmp_path):
    cache = tmp_path / "cache"
    seed_base_snapshots(cache)
    with pytest.raises(SealError, match="범주값"):
        run_verdict(repo, cache, tmp_path / "log.jsonl",
                    load=lambda _d: fake_panel(secugrp="신종증권"))


def test_manifest_is_deterministic_and_tracks_file_sizes(tmp_path):
    save_atomic(cache_path(tmp_path, "stk", "20170223"), [{"ISU_CD": "117930"}])
    first = manifest_sha256(tmp_path)
    assert first == manifest_sha256(tmp_path)
    assert first[1] == 1
    save_atomic(cache_path(tmp_path, "stk", "20170223"), [{"ISU_CD": "117930", "X": "y" * 50}])
    assert manifest_sha256(tmp_path)[0] != first[0]


def test_incomplete_base_snapshot_pair_is_refused(repo, tmp_path):
    cache = tmp_path / "cache"
    seed_base_snapshots(cache)
    cache_path(cache, "base/ksq", DAYS[0]).unlink()
    log = tmp_path / "log.jsonl"

    with pytest.raises(SealError, match="기본정보"):
        run_verdict(repo, cache, log, load=lambda _d: fake_panel())

    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert [x["status"] for x in lines] == [INCOMPLETE_DATA]
    assert verdict_runs(log) == 0


def test_base_snapshot_gaps_flags_a_month_with_no_snapshot_at_all(tmp_path):
    cache = tmp_path / "cache"
    seed_base_snapshots(cache)
    first_month_day = DAYS[0]
    cache_path(cache, "base/stk", first_month_day).unlink()
    cache_path(cache, "base/ksq", first_month_day).unlink()

    gaps = base_snapshot_gaps(cache, DAYS)

    assert f"month/{first_month_day[:6]}" in gaps
