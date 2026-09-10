import json
from datetime import datetime as real_datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.modules import paper_fast_probe as probe


def _ranking_row(ticker: str, name: str, price: int) -> dict:
    return {
        "stck_shrn_iscd": ticker,
        "hts_kor_isnm": name,
        "stck_prpr": str(price),
        "prdy_ctrt": "4.0",
        "avrg_vol": "100000",
        "acml_tr_pbmn": str(price * 100000),
    }


def _multi_row(
    ticker: str,
    name: str,
    price: int,
    prev_close: int,
    *,
    expected_qty: int = 100000,
    ask: int | None = None,
) -> dict:
    return {
        "inter_shrn_iscd": ticker,
        "inter_kor_isnm": name,
        "inter2_prpr": str(price),
        "inter2_prdy_clpr": str(prev_close),
        "inter2_askp": str(ask if ask is not None else price),
        "intr_antc_cntg_prdy_ctrt": str(round((price / prev_close - 1) * 100, 2)),
        "intr_antc_vol": str(expected_qty),
        "prdy_ctrt": str(round((price / prev_close - 1) * 100, 2)),
        "acml_vol": str(expected_qty),
        "acml_tr_pbmn": str(price * expected_qty),
        "hour_cls_code": "1",
        "mrkt_trtm_cls_name": "",
    }


def test_probe_is_hard_gated_to_explicit_paper_mode(monkeypatch):
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("PAPER_FAST_SHADOW", "1")
    monkeypatch.setenv("PAPER_FAST_HYBRID", "1")
    monkeypatch.setenv("DRY_RUN", "0")

    monkeypatch.setenv("KIS_MODE", "REAL")
    assert probe.enabled() is False
    assert probe.shadow_enabled() is False
    assert probe.hybrid_enabled() is False

    monkeypatch.setenv("KIS_MODE", "PAPER")
    assert probe.enabled() is True
    assert probe.shadow_enabled() is True
    assert probe.hybrid_enabled() is True

    monkeypatch.setenv("DRY_RUN", "1")
    assert probe.enabled() is False
    assert probe.shadow_enabled() is False
    assert probe.hybrid_enabled() is False


def test_multi_params_supports_at_most_thirty_tickers():
    tickers = [f"{index:06d}" for index in range(35)]
    params = probe._multi_params(tickers)

    assert len(params) == 60
    assert params["FID_INPUT_ISCD_1"] == "000000"
    assert params["FID_INPUT_ISCD_30"] == "000029"
    assert "FID_INPUT_ISCD_31" not in params


def test_shadow_top_n_defaults_to_full_multi_quote_capacity(monkeypatch):
    monkeypatch.delenv("PAPER_FAST_SHADOW_TOP_N", raising=False)

    assert probe._shadow_top_n() == 30


@pytest.mark.asyncio
async def test_prepare_records_four_public_calls_and_selects_shadow_shortlist(
    monkeypatch,
    tmp_path,
):
    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 28, 8, 59, 45, tzinfo=probe.KST)

    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    monkeypatch.setattr(probe, "datetime", FixedDateTime)
    probe._prepared_tickers = []

    ranking_j = [
        _ranking_row("006340", "대원전선", 14400),
        _ranking_row("000001", "코스피후보", 10500),
    ]
    multi_j = [
        _multi_row("006340", "대원전선", 14400, 13730, expected_qty=300000),
        _multi_row("000001", "코스피후보", 10500, 10000, expected_qty=20000),
    ]
    ranking_q = [
        _ranking_row("477850", "마키나락스", 25200),
        _ranking_row("439960", "코스모로보틱스", 15000),
    ]
    multi_q = [
        _multi_row("477850", "마키나락스", 25200, 24250, expected_qty=50000),
        _multi_row("439960", "코스모로보틱스", 15000, 14310, expected_qty=40000),
    ]
    get = AsyncMock(
        side_effect=[
            {"rt_cd": "0", "msg_cd": "OK", "output": ranking_j},
            {"rt_cd": "0", "msg_cd": "OK", "output": multi_j},
            {"rt_cd": "0", "msg_cd": "OK", "output": ranking_q},
            {"rt_cd": "0", "msg_cd": "OK", "output": multi_q},
        ]
    )
    monkeypatch.setattr(probe.kis_rest, "get", get)

    selected = await probe.prepare()

    assert get.await_count == 4
    assert len(selected) == 4
    assert set(selected).issubset({"006340", "000001", "477850", "439960"})

    path = next(tmp_path.glob("*.jsonl"))
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [record["event"] for record in records] == [
        "PAPER_FAST_PROBE_PREOPEN_START",
        "PAPER_FAST_PROBE_RANKING",
        "PAPER_FAST_PROBE_MULTI",
        "PAPER_FAST_PROBE_RANKING",
        "PAPER_FAST_PROBE_MULTI",
        "PAPER_FAST_PROBE_PREOPEN_DONE",
    ]
    assert records[-1]["markets"][0]["missing_tickers"] == []
    assert "authorization" not in path.read_text(encoding="utf-8").lower()
    assert "appsecret" not in path.read_text(encoding="utf-8").lower()


@pytest.mark.asyncio
async def test_prepare_skips_after_market_open(monkeypatch, tmp_path):
    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 28, 9, 0, 0, tzinfo=probe.KST)

    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    monkeypatch.setattr(probe, "datetime", FixedDateTime)
    probe._prepared_tickers = ["006340"]
    get = AsyncMock()
    monkeypatch.setattr(probe.kis_rest, "get", get)

    assert await probe.prepare() == []

    get.assert_not_awaited()
    assert probe._prepared_tickers == []
    records = [
        json.loads(line)
        for line in next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["event"] == "PAPER_FAST_PROBE_PREOPEN_SKIPPED"
    assert records[-1]["reason"] == "MARKET_OPEN"


@pytest.mark.asyncio
async def test_prepare_is_noop_in_real_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("KIS_MODE", "REAL")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    get = AsyncMock()
    monkeypatch.setattr(probe.kis_rest, "get", get)

    assert await probe.prepare() == []
    get.assert_not_awaited()
    assert list(tmp_path.glob("*")) == []


@pytest.mark.asyncio
async def test_open_boundary_is_noop_in_real_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("KIS_MODE", "REAL")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    get = AsyncMock()
    monkeypatch.setattr(probe.kis_rest, "get", get)

    await probe.observe_open_boundary()

    get.assert_not_awaited()
    assert list(tmp_path.glob("*")) == []


@pytest.mark.asyncio
async def test_open_boundary_observes_prepared_tickers_without_trading(
    monkeypatch,
    tmp_path,
):
    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 28, 9, 0, 0, 300000, tzinfo=probe.KST)

    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    monkeypatch.setenv("PAPER_FAST_PROBE_OPEN_OFFSET_MS", "300")
    monkeypatch.setattr(probe, "datetime", FixedDateTime)
    probe._prepared_tickers = ["006340", "477850", "439960"]
    get = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "OK",
            "output": [
                _multi_row("006340", "대원전선", 14500, 13730, ask=14510),
                _multi_row("477850", "마키나락스", 25200, 24250, ask=25250),
                _multi_row("439960", "코스모로보틱스", 15480, 14310, ask=15490),
            ],
        }
    )
    monkeypatch.setattr(probe.kis_rest, "get", get)

    await probe.observe_open_boundary()

    get.assert_awaited_once()
    assert get.await_args.args[0] == probe.MULTI_PRICE_PATH
    records = [
        json.loads(line)
        for line in next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["event"] == "PAPER_FAST_PROBE_OPEN_DONE"
    assert records[-1]["valid_ask_count"] == 3
    assert records[-1]["requested_tickers"] == ["006340", "477850", "439960"]
    assert records[-1]["shadow_candidate_count"] == 2
    assert probe.open_quality_ok() is True
    assert probe.get_last_open_quality()["reason"] == "COMPLETE"
    assert records[-1]["filter_total_count"] == 3
    assert records[-1]["filter_pass_count"] == 2
    assert records[-1]["filter_high_gap_rejected_count"] == 1


@pytest.mark.asyncio
async def test_open_boundary_marks_missing_response_as_incomplete(monkeypatch, tmp_path):
    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 28, 9, 0, 0, 300000, tzinfo=probe.KST)

    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    monkeypatch.setattr(probe, "datetime", FixedDateTime)
    probe._prepared_tickers = ["005930", "000660"]
    monkeypatch.setattr(
        probe.kis_rest,
        "get",
        AsyncMock(return_value={
            "rt_cd": "0",
            "output": [_multi_row("005930", "삼성전자", 10300, 10000)],
        }),
    )

    candidates = await probe.observe_open_boundary()

    assert [c["ticker"] for c in candidates] == ["005930"]
    assert probe.open_quality_ok() is False
    quality = probe.get_last_open_quality()
    assert quality["reason"] == "MISSING_TICKERS"
    assert quality["missing_tickers"] == ["000660"]


@pytest.mark.asyncio
async def test_open_boundary_does_not_preserve_invalid_ask_candidate(monkeypatch, tmp_path):
    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 28, 9, 0, 0, 300000, tzinfo=probe.KST)

    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    monkeypatch.setattr(probe, "datetime", FixedDateTime)
    probe._prepared_tickers = ["005930"]
    monkeypatch.setattr(
        probe.kis_rest,
        "get",
        AsyncMock(return_value={
            "rt_cd": "0",
            "output": [_multi_row("005930", "삼성전자", 10300, 10000, ask=0)],
        }),
    )

    candidates = await probe.observe_open_boundary()

    assert candidates == []
    assert probe.open_quality_ok() is False
    quality = probe.get_last_open_quality()
    assert quality["reason"] == "INVALID_ASK"
    assert quality["invalid_ask_tickers"] == ["005930"]


def test_merge_candidates_preserves_fast_and_prefers_fresh_fast_duplicate():
    fast = [
        {"ticker": "005930", "gap_pct": 0.04, "gap_allowed": True,
         "expected_amount": 2e9, "avg_amount_5d": 1e9, "vi_gap": 0.03},
        {"ticker": "000660", "gap_pct": 0.05, "gap_allowed": True,
         "expected_amount": 3e9, "avg_amount_5d": 1e9, "vi_gap": 0.03},
    ]
    legacy = [
        {"ticker": "005930", "gap_pct": 0.031, "gap_allowed": True,
         "expected_amount": 1e9, "avg_amount_5d": 1e9, "vi_gap": 0.03},
        {"ticker": "035420", "gap_pct": 0.045, "gap_allowed": True,
         "expected_amount": 4e9, "avg_amount_5d": 1e9, "vi_gap": 0.03},
    ]

    merged = probe.merge_candidates(fast, legacy)

    assert {c["ticker"] for c in merged} == {"005930", "000660", "035420"}
    samsung = next(c for c in merged if c["ticker"] == "005930")
    assert samsung["gap_pct"] == 0.04


def test_multi_candidate_derives_expected_price_when_current_is_previous_close():
    row = _multi_row("005930", "삼성전자", 227500, 220000, expected_qty=1000)
    row["inter2_prpr"] = "220000"
    row["intr_antc_cntg_vrss"] = "7500"

    candidate = probe._candidate_from_multi(row, "J", {})

    assert candidate is not None
    assert candidate["current_price"] == 220000
    assert candidate["expected_price"] == 227500
    assert candidate["gap_pct"] == pytest.approx(0.0341)


@pytest.mark.parametrize(
    "name",
    ["KODEX 200", "TIGER 인버스", "시장대표 레버리지", "미국채 선물 ETF"],
)
def test_multi_candidate_excludes_non_common_stock_products(name):
    row = _multi_row("123456", name, 10300, 10000, expected_qty=1000)

    assert probe._candidate_from_multi(row, "J", {}) is None


def test_shadow_compare_records_top_three_overlap_without_mutating_candidates(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("PAPER_FAST_SHADOW", "1")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    probe._open_candidates = [
        {"ticker": "005930", "gap_pct": 0.0341},
        {"ticker": "332570", "gap_pct": 0.031},
    ]
    legacy = [
        {"ticker": "005930", "gap_pct": 0.034},
        {"ticker": "319400", "gap_pct": 0.058},
    ]

    fields = probe.compare_with_legacy(legacy)

    assert fields["rank1_match"] is True
    assert fields["top3_overlap_count"] == 1
    assert legacy[0]["gap_pct"] == 0.034
    records = [
        json.loads(line)
        for line in next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["event"] == "PAPER_FAST_SHADOW_COMPARE"


def test_shadow_validation_summary_counts_only_timely_complete_days(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    monkeypatch.setenv("PAPER_FAST_SHADOW_REQUIRED_DAYS", "10")
    valid = [
        {
            "ts": "2026-08-12T09:00:00.300000+09:00",
            "event": "PAPER_FAST_PROBE_OPEN_DONE",
            "quality": {"ok": True},
        },
        {
            "ts": "2026-08-12T09:01:07+09:00",
            "event": "PAPER_FAST_SHADOW_COMPARE",
            "rank1_match": True,
            "top3_overlap_count": 2,
        },
    ]
    delayed = [
        {
            "ts": "2026-08-13T09:00:00.300000+09:00",
            "event": "PAPER_FAST_PROBE_OPEN_DONE",
            "quality": {"ok": True},
        },
        {
            "ts": "2026-08-13T09:05:01+09:00",
            "event": "PAPER_FAST_SHADOW_COMPARE",
            "rank1_match": False,
            "top3_overlap_count": 0,
        },
    ]
    for name, records in (("20260812.jsonl", valid), ("20260813.jsonl", delayed)):
        (tmp_path / name).write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )

    summary = probe.shadow_validation_summary()

    assert summary == {
        "observed_days": 1,
        "required_days": 10,
        "remaining_days": 9,
        "validation_complete": False,
        "observed_dates": ["20260812"],
        "rank1_match_days": 1,
        "top3_overlap_total": 2,
        "scanned_files": 2,
        "scan_file_limit": 32,
        "skipped_untimely_days": ["20260813"],
        "skipped_incomplete_days": [],
        "skipped_closed_days": [],
        "parse_error_lines": 0,
    }


def test_shadow_validation_summary_handles_missing_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path / "missing"))

    summary = probe.shadow_validation_summary()

    assert summary["observed_days"] == 0
    assert summary["scanned_files"] == 0
    assert summary["skipped_untimely_days"] == []
    assert summary["parse_error_lines"] == 0


def test_shadow_validation_summary_tolerates_broken_and_mixed_timezone_lines(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    path = tmp_path / "20260814.jsonl"
    path.write_text(
        "\n".join(
            [
                '{"event":"UNRELATED","bad":',
                '{"event":"PAPER_FAST_PROBE_OPEN_DONE",',
                json.dumps(
                    {
                        "ts": "2026-08-14T09:00:00.300000",
                        "event": "PAPER_FAST_PROBE_OPEN_DONE",
                        "quality": {"ok": True},
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-08-14T09:01:00+09:00",
                        "event": "PAPER_FAST_SHADOW_COMPARE",
                        "rank1_match": False,
                        "top3_overlap_count": 1,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = probe.shadow_validation_summary()

    assert summary["observed_days"] == 1
    assert summary["parse_error_lines"] == 1


def test_shadow_validation_summary_caps_files_and_reports_complete(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    monkeypatch.setenv("PAPER_FAST_SHADOW_REQUIRED_DAYS", "10")
    monkeypatch.setenv("PAPER_FAST_SHADOW_SCAN_FILE_LIMIT", "10")
    for day in range(1, 12):
        date = f"202609{day:02d}"
        records = [
            {
                "ts": f"2026-09-{day:02d}T09:00:00.300000+09:00",
                "event": "PAPER_FAST_PROBE_OPEN_DONE",
                "quality": {"ok": True},
            },
            {
                "ts": f"2026-09-{day:02d}T09:01:00+09:00",
                "event": "PAPER_FAST_SHADOW_COMPARE",
                "rank1_match": True,
                "top3_overlap_count": 3,
            },
        ]
        (tmp_path / f"{date}.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )

    summary = probe.shadow_validation_summary()

    assert summary["observed_days"] == 10
    assert summary["validation_complete"] is True
    assert summary["remaining_days"] == 0
    assert summary["rank1_match_days"] == 10
    assert summary["top3_overlap_total"] == 30
    assert summary["scanned_files"] == 10
    assert summary["observed_dates"] == [f"202609{day:02d}" for day in range(2, 12)]


def test_shadow_progress_logging_is_exception_isolated(monkeypatch):
    events = []
    monkeypatch.setattr(probe, "shadow_enabled", lambda: True)
    monkeypatch.setattr(
        probe,
        "shadow_validation_summary",
        lambda: (_ for _ in ()).throw(RuntimeError("broken history")),
    )
    monkeypatch.setattr(
        probe,
        "log",
        lambda event, **fields: events.append((event, fields)),
    )

    probe.log_shadow_validation_progress()

    assert events[0][0] == "PAPER_FAST_SHADOW_PROGRESS_ERROR"


@pytest.mark.asyncio
async def test_open_boundary_skips_when_scheduler_is_too_late(monkeypatch, tmp_path):
    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 28, 9, 0, 5, tzinfo=probe.KST)

    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    monkeypatch.setenv("PAPER_FAST_PROBE_OPEN_OFFSET_MS", "300")
    monkeypatch.setenv("PAPER_FAST_PROBE_OPEN_MAX_LATENESS_MS", "2500")
    monkeypatch.setattr(probe, "datetime", FixedDateTime)
    probe._prepared_tickers = ["006340"]
    get = AsyncMock()
    monkeypatch.setattr(probe.kis_rest, "get", get)

    await probe.observe_open_boundary()

    get.assert_not_awaited()
    records = [
        json.loads(line)
        for line in next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["event"] == "PAPER_FAST_PROBE_OPEN_SKIPPED"
    assert records[-1]["reason"] == "TOO_LATE"


@pytest.mark.asyncio
async def test_open_boundary_recovers_tickers_from_preopen_record(
    monkeypatch,
    tmp_path,
):
    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 28, 9, 0, 0, 300000, tzinfo=probe.KST)

    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    monkeypatch.setenv("PAPER_FAST_PROBE_OPEN_OFFSET_MS", "300")
    monkeypatch.setattr(probe, "datetime", FixedDateTime)
    probe._prepared_tickers = []
    path = tmp_path / "20260728.jsonl"
    path.write_text(
        json.dumps({
            "event": "PAPER_FAST_PROBE_PREOPEN_DONE",
            "selected_tickers": ["006340", "477850"],
        })
        + "\n",
        encoding="utf-8",
    )
    get = AsyncMock(return_value={"rt_cd": "0", "output": []})
    monkeypatch.setattr(probe.kis_rest, "get", get)

    await probe.observe_open_boundary()

    assert get.await_args.kwargs["params"]["FID_INPUT_ISCD_1"] == "006340"
    assert get.await_args.kwargs["params"]["FID_INPUT_ISCD_2"] == "477850"


def test_load_persisted_open_candidates_restores_completed_selection(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    now = real_datetime(2026, 7, 28, 9, 1, tzinfo=probe.KST)
    path = tmp_path / "20260728.jsonl"
    rows = [
        _multi_row("005930", "Samsung", 10400, 10000),
        _multi_row("000660", "Hynix", 10500, 10000),
    ]
    records = [
        {"event": "PAPER_FAST_PROBE_PREOPEN_START"},
        {
            "event": "PAPER_FAST_PROBE_RANKING",
            "market": "J",
            "response": {
                "output": [
                    _ranking_row("005930", "Samsung", 10400),
                    _ranking_row("000660", "Hynix", 10500),
                ],
            },
        },
        {
            "event": "PAPER_FAST_PROBE_OPEN_MULTI",
            "phase": "OPEN",
            "response": {"output": rows},
        },
        {
            "event": "PAPER_FAST_PROBE_OPEN_DONE",
            "shadow_tickers": ["000660", "005930"],
        },
    ]
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    restored_path, candidates = probe.load_persisted_open_candidates(now)

    assert restored_path == path
    assert [candidate["ticker"] for candidate in candidates] == ["000660", "005930"]
    assert candidates[0]["name"] == "Hynix"
    assert candidates[0]["gap_allowed"] is True
    assert candidates[0]["gap_source"].startswith("fast.")


def _write_probe_file(tmp_path, records) -> Path:
    path = tmp_path / "20260728.jsonl"
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


def _completed_cycle(tickers: list[tuple[str, str, int]]) -> list[dict]:
    """PREOPEN_START ~ OPEN_DONE 한 사이클 레코드를 만든다."""
    return [
        {"event": "PAPER_FAST_PROBE_PREOPEN_START"},
        {
            "event": "PAPER_FAST_PROBE_RANKING",
            "market": "J",
            "response": {
                "output": [
                    _ranking_row(ticker, name, price)
                    for ticker, name, price in tickers
                ],
            },
        },
        {
            "event": "PAPER_FAST_PROBE_OPEN_MULTI",
            "phase": "OPEN",
            "response": {
                "output": [
                    _multi_row(ticker, name, price, 10000)
                    for ticker, name, price in tickers
                ],
            },
        },
        {
            "event": "PAPER_FAST_PROBE_OPEN_DONE",
            "shadow_tickers": [ticker for ticker, _, _ in tickers],
        },
    ]


def test_load_persisted_open_candidates_uses_latest_completed_cycle(
    monkeypatch,
    tmp_path,
):
    """같은 파일에 사이클이 여러 번 있으면 마지막 완료 사이클만 남는다."""
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    now = real_datetime(2026, 7, 28, 9, 1, tzinfo=probe.KST)
    path = _write_probe_file(
        tmp_path,
        _completed_cycle([("005930", "Samsung", 10400)])
        + _completed_cycle([("000660", "Hynix", 10500)]),
    )

    restored_path, candidates = probe.load_persisted_open_candidates(now)

    assert restored_path == path
    assert [candidate["ticker"] for candidate in candidates] == ["000660"]


def test_load_persisted_open_candidates_discards_unfinished_later_cycle(
    monkeypatch,
    tmp_path,
):
    """새 사이클이 시작만 하고 완주하지 못했으면 이전 사이클 결과를 재사용하지 않는다.

    재사용하면 개장 관측이 실패한 날에 어제 같은 선정을 통과한 것처럼 보인다.
    """
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    now = real_datetime(2026, 7, 28, 9, 1, tzinfo=probe.KST)
    _write_probe_file(
        tmp_path,
        _completed_cycle([("005930", "Samsung", 10400)])
        + [{"event": "PAPER_FAST_PROBE_PREOPEN_START"}],
    )

    restored_path, candidates = probe.load_persisted_open_candidates(now)

    assert restored_path is None
    assert candidates == []


def test_load_persisted_open_candidates_ignores_open_done_without_rows(
    monkeypatch,
    tmp_path,
):
    """OPEN_MULTI 없이 도착한 OPEN_DONE은 복구 근거가 없으므로 무시한다.

    직전 사이클의 개장 응답을 빌려 쓰면 다른 날/다른 종목 데이터가 섞인다.
    """
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    now = real_datetime(2026, 7, 28, 9, 1, tzinfo=probe.KST)
    _write_probe_file(
        tmp_path,
        _completed_cycle([("005930", "Samsung", 10400)])
        + [{"event": "PAPER_FAST_PROBE_OPEN_DONE", "shadow_tickers": ["000660"]}],
    )

    restored_path, candidates = probe.load_persisted_open_candidates(now)

    # 마지막 OPEN_DONE은 버려지고 직전 완료 사이클 결과가 유지된다.
    assert restored_path is not None
    assert [candidate["ticker"] for candidate in candidates] == ["005930"]


def test_load_persisted_open_candidates_survives_corrupt_and_null_records(
    monkeypatch,
    tmp_path,
):
    """잘린 마지막 줄과 shadow_tickers=null이 있어도 예외 없이 처리한다.

    이 함수는 /api/f1 응답 경로라 예외가 나면 대시보드가 500으로 죽는다.
    """
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    now = real_datetime(2026, 7, 28, 9, 1, tzinfo=probe.KST)
    path = tmp_path / "20260728.jsonl"
    records = _completed_cycle([("005930", "Samsung", 10400)])
    body = "\n".join(json.dumps(record) for record in records)
    body += "\n" + json.dumps(
        {"event": "PAPER_FAST_PROBE_OPEN_DONE", "shadow_tickers": None}
    )
    body += '\n{"event": "PAPER_FAST_PROBE_OPEN_DO'  # 쓰다 만 마지막 줄
    path.write_text(body, encoding="utf-8")

    restored_path, candidates = probe.load_persisted_open_candidates(now)

    assert restored_path == path
    assert [candidate["ticker"] for candidate in candidates] == ["005930"]


def test_load_persisted_open_candidates_skips_unknown_accepted_ticker(
    monkeypatch,
    tmp_path,
):
    """shadow_tickers에 개장 응답에 없는 종목이 있으면 조용히 건너뛴다(KeyError 금지)."""
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    now = real_datetime(2026, 7, 28, 9, 1, tzinfo=probe.KST)
    records = _completed_cycle([("005930", "Samsung", 10400)])
    records[-1]["shadow_tickers"] = ["999999", "005930"]
    _write_probe_file(tmp_path, records)

    _restored_path, candidates = probe.load_persisted_open_candidates(now)

    assert [candidate["ticker"] for candidate in candidates] == ["005930"]


# ── 휴장일 감지 (모의투자 전용) ─────────────────────────────────────────
#
# CTCA0903R(국내휴장일조회)이 모의투자 미지원이라 PAPER 는 평일 가드밖에 없다.
# 2026-08-17(광복절 대체공휴일, 월)에 F1 이 60종목을 조회하고 주문까지 전송했다.
# 장전 멀티시세 응답에 이미 판별 신호가 있으므로 추가 호출 없이 막는다.


def _closed_row(ticker: str, *, stale_volume: int = 5_000_000) -> dict:
    """휴장일 응답. 예상체결 필드가 비고 전일 거래량이 그대로 내려온다."""
    return {
        "inter_shrn_iscd": ticker,
        "inter_kor_isnm": f"종목{ticker}",
        "inter2_prpr": "10000",
        "inter2_prdy_clpr": "10000",
        "inter2_askp": "10000",
        "hour_cls_code": "0",
        "intr_antc_vol": "0",
        "acml_vol": str(stale_volume),
    }


def _open_row(ticker: str, *, hour_cls: str = "B", expected_qty: int = 5_000) -> dict:
    """개장일 응답. 동시호가가 돌아 예상체결수량이 있고 누적거래량은 작다."""
    return {
        "inter_shrn_iscd": ticker,
        "inter_kor_isnm": f"종목{ticker}",
        "inter2_prpr": "10300",
        "inter2_prdy_clpr": "10000",
        "inter2_askp": "10310",
        "hour_cls_code": hour_cls,
        "intr_antc_vol": str(expected_qty),
        "acml_vol": "1500",
    }


def test_market_closed_detects_holiday_response():
    """2026-08-17 실제 응답 형태: 전 종목 hour_cls_code='0', 예상수량 0, 전일 거래량."""
    rows = [_closed_row(f"00{i:04d}") for i in range(30)]
    verdict = probe.evaluate_market_closed(rows)
    assert verdict["closed"] is True
    assert verdict["reason"] == "STALE_SESSION"


def test_market_open_response_is_never_flagged_closed():
    rows = [_open_row(f"00{i:04d}") for i in range(30)]
    assert probe.evaluate_market_closed(rows)["closed"] is False


def test_single_halted_ticker_does_not_flag_the_whole_market():
    """2026-08-13 개장일에도 hour_cls_code='0' 인 종목이 1개 있었다.

    any 로 판정하면 정상 거래일을 휴장으로 오인해 그날 매매를 통째로 잃는다.
    """
    rows = [_open_row(f"00{i:04d}") for i in range(29)]
    rows.append(_open_row("009999", hour_cls="0", expected_qty=0))
    assert probe.evaluate_market_closed(rows)["closed"] is False


def test_small_sample_never_declares_closed():
    """표본이 얇으면 판단하지 않는다. 거래일을 놓치는 쪽이 더 큰 손실이다."""
    rows = [_closed_row(f"00{i:04d}") for i in range(3)]
    verdict = probe.evaluate_market_closed(rows)
    assert verdict["closed"] is False
    assert verdict["reason"] == "SAMPLE_TOO_SMALL"


def test_empty_rows_never_declare_closed():
    assert probe.evaluate_market_closed([])["closed"] is False


def test_zero_volume_response_is_not_treated_as_holiday():
    """거래량이 0으로만 내려오는 응답 이상은 휴장 근거가 아니다(fail-open)."""
    rows = [_closed_row(f"00{i:04d}", stale_volume=0) for i in range(30)]
    verdict = probe.evaluate_market_closed(rows)
    assert verdict["closed"] is False
    assert verdict["reason"] == "VOLUME_NOT_STALE"


def test_expected_quantity_present_blocks_closed_verdict():
    """예상체결수량이 하나라도 있으면 동시호가가 돈 것이다."""
    rows = [_closed_row(f"00{i:04d}") for i in range(29)]
    live = _closed_row("009999")
    live["intr_antc_vol"] = "1000"
    rows.append(live)
    verdict = probe.evaluate_market_closed(rows)
    assert verdict["closed"] is False
    assert verdict["reason"] == "EXPECTED_QTY_PRESENT"


def test_verdict_carries_evidence_for_the_log():
    rows = [_closed_row(f"00{i:04d}") for i in range(30)]
    verdict = probe.evaluate_market_closed(rows)
    assert verdict["total_count"] == 30
    assert verdict["hour_cls_codes"] == ["0"]
    assert verdict["expected_qty_zero_count"] == 30
    assert verdict["median_acml_vol"] == 5_000_000


# 실제 응답 회귀 — 2026-08-17(광복절 대체공휴일) / 2026-08-18(정상 개장) 장전
# 멀티시세에서 그대로 뽑은 12행이다. 합성 픽스처가 실제 스키마와 어긋나면
# 판정기가 통과해도 운영에서 안 걸린다.

def _real_row(ticker: str, cls_code: str, antc_vol: str, acml_vol: str) -> dict:
    """실제 응답에서 뽑은 멀티시세 1행. 값은 그대로, 표 모양만 100자 안에 넣는다."""
    return {
        "inter_shrn_iscd": ticker,
        "hour_cls_code": cls_code,
        "intr_antc_vol": antc_vol,
        "acml_vol": acml_vol,
    }


_REAL_HOLIDAY_ROWS_20260817 = [
    _real_row("001210", "0", "0", "23913143"),
    _real_row("005930", "0", "0", "21669476"),
    _real_row("088350", "0", "0", "14537788"),
    _real_row("073240", "0", "0", "11897318"),
    _real_row("002990", "0", "0", "10000380"),
    _real_row("092200", "0", "0", "6544969"),
    _real_row("006340", "0", "0", "5674735"),
    _real_row("005360", "0", "0", "5512875"),
    _real_row("007110", "0", "0", "4982802"),
    _real_row("047040", "0", "0", "4858982"),
    _real_row("014160", "0", "0", "4600312"),
    _real_row("000660", "0", "0", "4520990"),
]

_REAL_TRADING_ROWS_20260818 = [
    _real_row("002820", "B", "2526", "22700"),
    _real_row("001520", "B", "15834", "10000"),
    _real_row("088350", "B", "62862", "6320"),
    _real_row("092200", "B", "41810", "4745"),
    _real_row("006340", "B", "28392", "2715"),
    _real_row("009830", "B", "24154", "2182"),
    _real_row("028670", "B", "21532", "2001"),
    _real_row("001740", "B", "9924", "1961"),
    _real_row("001550", "B", "6934", "1700"),
    _real_row("010690", "B", "7374", "1665"),
    _real_row("003350", "B", "4942", "1526"),
    _real_row("047040", "B", "26535", "1411"),
]


def test_real_20260817_holiday_response_is_detected():
    verdict = probe.evaluate_market_closed(_REAL_HOLIDAY_ROWS_20260817)
    assert verdict["closed"] is True
    assert verdict["reason"] == "STALE_SESSION"


def test_real_20260818_trading_response_is_not_flagged():
    assert probe.evaluate_market_closed(_REAL_TRADING_ROWS_20260818)["closed"] is False


@pytest.mark.asyncio
async def test_prepare_flags_market_closed_and_records_evidence(monkeypatch, tmp_path):
    """장전 프로브가 휴장을 판정하고 근거를 파일에 남긴다.

    시장을 나눠 재면 각 시장 응답이 표본 하한(10행)에 못 미쳐 판정을 놓친다.
    두 시장을 합쳐 한 표본으로 봐야 한다.
    """

    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 17, 8, 59, 45, tzinfo=probe.KST)

    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    monkeypatch.setattr(probe, "datetime", FixedDateTime)
    probe._prepared_tickers = []

    def closed_rows(prefix: str) -> list[dict]:
        return [
            {
                "inter_shrn_iscd": f"{prefix}{i:04d}",
                "inter_kor_isnm": f"종목{i}",
                "inter2_prpr": "10000",
                "inter2_prdy_clpr": "10000",
                "inter2_askp": "10000",
                "hour_cls_code": "0",
                "intr_antc_vol": "0",
                "prdy_ctrt": "1.5",
                "acml_vol": "5000000",
            }
            for i in range(6)
        ]

    ranking = [_ranking_row(f"00{i:04d}", f"종목{i}", 10000) for i in range(6)]
    get = AsyncMock(
        side_effect=[
            {"rt_cd": "0", "msg_cd": "OK", "output": ranking},
            {"rt_cd": "0", "msg_cd": "OK", "output": closed_rows("00")},
            {"rt_cd": "0", "msg_cd": "OK", "output": ranking},
            {"rt_cd": "0", "msg_cd": "OK", "output": closed_rows("01")},
        ]
    )
    monkeypatch.setattr(probe.kis_rest, "get", get)

    await probe.prepare()

    assert probe.market_closed_detected() is True
    verdict = probe.get_market_closed_verdict()
    assert verdict["reason"] == "STALE_SESSION"
    assert verdict["total_count"] == 12

    path = next(tmp_path.glob("*.jsonl"))
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    events = [record["event"] for record in records]
    assert "PAPER_FAST_PROBE_MARKET_CLOSED" in events
    assert records[-1]["market_closed"]["closed"] is True


@pytest.mark.asyncio
async def test_prepare_resets_stale_closed_verdict_between_days(monkeypatch, tmp_path):
    """어제 휴장 판정이 남아 오늘 거래를 막으면 안 된다."""

    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 18, 8, 59, 45, tzinfo=probe.KST)

    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    monkeypatch.setattr(probe, "datetime", FixedDateTime)
    probe._last_closed_verdict = {"closed": True, "reason": "STALE_SESSION"}

    ranking = [_ranking_row("006340", "대원전선", 14400)]
    multi = [_multi_row("006340", "대원전선", 14400, 13730, expected_qty=300000)]
    get = AsyncMock(
        side_effect=[
            {"rt_cd": "0", "msg_cd": "OK", "output": ranking},
            {"rt_cd": "0", "msg_cd": "OK", "output": multi},
            {"rt_cd": "0", "msg_cd": "OK", "output": ranking},
            {"rt_cd": "0", "msg_cd": "OK", "output": multi},
        ]
    )
    monkeypatch.setattr(probe.kis_rest, "get", get)

    await probe.prepare()

    assert probe.market_closed_detected() is False


def _write_shadow_day(
    directory, date: str, *, ts: str, closed: bool = False, rank1_match: bool = True
) -> None:
    """검증 집계가 하루로 세는 최소 기록(OPEN_DONE + SHADOW_COMPARE)을 만든다."""
    records = []
    if closed:
        rows = [
            {
                "inter_shrn_iscd": f"00{i:04d}",
                "hour_cls_code": "0",
                "intr_antc_vol": "0",
                "acml_vol": "5000000",
            }
            for i in range(12)
        ]
        records.append(
            {
                "event": "PAPER_FAST_PROBE_OPEN_MULTI",
                "ts": f"{ts}+09:00",
                "response": {"output": rows},
            }
        )
    records.append(
        {
            "event": "PAPER_FAST_PROBE_OPEN_DONE",
            "ts": f"{ts}+09:00",
            "quality": {"ok": True, "reason": "COMPLETE"},
        }
    )
    compare: dict[str, Any] = {
        "event": "PAPER_FAST_SHADOW_COMPARE",
        "ts": f"{ts}+09:00",
        "rank1_match": rank1_match,
        "top3_overlap_count": 1,
    }
    records.append(compare)
    (directory / f"{date}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("shadow_flag", ["1"])
def test_shadow_summary_excludes_days_the_market_was_closed(
    monkeypatch, tmp_path, shadow_flag
):
    """휴장일을 검증일로 세면 10일 게이트가 실제보다 빨리 차 버린다.

    2026-08-17(대체공휴일)은 감지 로직이 붙기 전에 기록돼 정상일처럼 남아 있다.
    집계도 같은 판정기로 다시 걸러야 과거 기록이 계수를 부풀리지 않는다.
    """
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    monkeypatch.setenv("PAPER_FAST_SHADOW", shadow_flag)
    _write_shadow_day(tmp_path, "20260814", ts="2026-08-14T09:00:01")
    _write_shadow_day(tmp_path, "20260817", ts="2026-08-17T09:00:01", closed=True)
    _write_shadow_day(tmp_path, "20260818", ts="2026-08-18T09:00:01")

    summary = probe.shadow_validation_summary()

    assert summary["observed_dates"] == ["20260814", "20260818"]
    assert summary["observed_days"] == 2
    assert summary["skipped_closed_days"] == ["20260817"]


def test_shadow_summary_counts_normal_days_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    monkeypatch.setenv("PAPER_FAST_SHADOW", "1")
    _write_shadow_day(tmp_path, "20260819", ts="2026-08-19T09:00:01")
    _write_shadow_day(tmp_path, "20260820", ts="2026-08-20T09:00:01")

    summary = probe.shadow_validation_summary()

    assert summary["observed_days"] == 2
    assert summary["skipped_closed_days"] == []
