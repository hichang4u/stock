"""F1 갭 필터 경계값 유닛 테스트."""
import asyncio
import json
import os
import time
from unittest.mock import AsyncMock, patch

import pytest

import src.modules.f1_filter as f1_mod
from src import state as _state_mod
from src.modules.f1_filter import GAP_MAX, GAP_MIN, _fetch_all_premarket, run

# ── 헬퍼 ──────────────────────────────────────────────────────────────

def _candidate(gap_pct: float, amount: float = 1e9, ticker: str = "TEST") -> dict:
    ep = 10_000.0
    return {
        "ticker": ticker,
        "expected_price": ep,
        "prev_close": round(ep / (1 + gap_pct)),
        "gap_pct": gap_pct,
        "avg_amount_5d": amount,
        "expected_amount": amount,
        "expected_qty": 1000,
    }


def _classified_candidate(
    gap_pct: float,
    amount: float = 1e9,
    ticker: str = "TEST",
    vi_gap: float = 0.02,
) -> dict:
    c = _candidate(gap_pct, amount, ticker)
    c["vi_gap"] = vi_gap
    c.update(f1_mod._classify_gap_candidate(c))
    return c


async def _run(candidates: list[dict]) -> list[dict]:
    """_fetch_all_premarket, notifier.send, db.record_skip 모킹 후 run() 실행."""
    with (
        patch(
            "src.modules.f1_filter._fetch_all_premarket",
            new_callable=AsyncMock, return_value=candidates,
        ),
        patch("src.notifier.send", new_callable=AsyncMock),
        patch("src.db.record_skip", new_callable=AsyncMock),
    ):
        return await run()


# ── 픽스처 ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_state():
    s = _state_mod.get()
    s.day_skip = False
    s.target_ticker = None
    yield
    s.day_skip = False
    s.target_ticker = None


# ── 갭 경계값 ─────────────────────────────────────────────────────────

async def test_gap_2_5_pct_passes():
    """정확히 2.5%(GAP_MIN) → 필터 통과."""
    result = await _run([_classified_candidate(GAP_MIN)])
    assert len(result) == 1


async def test_gap_2_4_pct_excluded():
    """2.4%(GAP_MIN 미만) → 필터 제외, day_skip=True."""
    result = await _run([_classified_candidate(0.024)])
    assert result == []
    assert _state_mod.get().day_skip is True


async def test_negative_gap_is_classified_explicitly():
    """Negative gaps should not be mixed into LOW_GAP/GAP_BELOW_2."""
    candidate = _classified_candidate(-0.05)
    assert candidate["gap_band"] == "NEGATIVE_GAP"
    assert candidate["gap_reason"] == "NEGATIVE_GAP"
    assert candidate["gap_allowed"] is False


async def test_gap_core_max_allowed_when_high_gap_conditions_pass():
    """GAP_MAX 이상은 HIGH_GAP 조건 충족 시 통과."""
    result = await _run([_classified_candidate(GAP_MAX, amount=6e9, vi_gap=0.02)])
    assert len(result) == 1
    assert result[0]["gap_reason"] == "HIGH_GAP_ALLOWED"


async def test_gap_7_99_pct_passes():
    """GAP_MAX 직전 → CORE_GAP으로 통과."""
    result = await _run([_classified_candidate(GAP_MAX - 0.0001)])
    assert len(result) == 1


async def test_high_gap_excluded_when_amount_low():
    """GAP_MAX 이상 high gap은 금액 기준 미달 시 제외."""
    result = await _run([_classified_candidate(GAP_MAX + 0.001, amount=8e8, vi_gap=0.03)])
    assert result == []


async def test_high_gap_kept_when_static_vi_is_near():
    """정적 VI 근접은 실제 발동이 아니므로 고유동성 high-gap 후보를 유지한다."""
    result = await _run([_classified_candidate(GAP_MAX + 0.001, amount=6e9, vi_gap=0.005)])
    assert len(result) == 1
    assert result[0]["gap_reason"] == "HIGH_GAP_ALLOWED"


async def test_high_gap_kept_when_static_vi_distance_is_unknown():
    """정적 VI 계산 불가는 후보 제외 사유가 아니며 실제 VI는 F3가 확인한다."""
    result = await _run([_classified_candidate(GAP_MAX + 0.001, amount=6e9, vi_gap=None)])
    assert len(result) == 1
    assert result[0]["gap_reason"] == "HIGH_GAP_ALLOWED"


async def test_extreme_gap_excluded():
    """10%+ extreme gap is excluded."""
    result = await _run([_classified_candidate(0.10, amount=5e9, vi_gap=0.02)])
    assert result == []


async def test_empty_raw_returns_empty_and_sets_day_skip():
    """원본 데이터 없음 → 빈 리스트, day_skip=True."""
    result = await _run([])
    assert result == []
    assert _state_mod.get().day_skip is True


async def test_ranking_candidate_uses_expected_quote_when_valid():
    """예상체결 API가 유효하면 최종 gap/가격/수량/대금은 예상체결 값을 우선."""
    item = {
        "mksc_shrn_iscd": "005930",
        "hts_kor_isnm": "삼성전자",
        "prdy_ctrt": "0.00",
        "stck_prpr": "10000",
        "avrg_vol": "1000",
        "acml_tr_pbmn": "10000000",
        "acml_vol": "1000",
    }

    quote = {
        "expected_price": 10500.0,
        "expected_qty": 2000,
        "expected_amount": 21_000_000.0,
        "expected_gap_pct": 5.0,
        "prev_close": 10000.0,
    }
    with patch(
        "src.modules.f1_filter._fetch_expected_quote",
        new_callable=AsyncMock, return_value=quote,
    ):
        result = await f1_mod._parse_candidate(item)

    assert result["gap_pct"] == pytest.approx(0.05)
    assert result["expected_price"] == 10500.0
    assert result["expected_qty"] == 2000
    assert result["expected_amount"] == 21_000_000.0
    assert result["gap_source"] == "expected.antc_cnpr"
    assert result["ranking_gap_pct"] == 0.0
    assert result["expected_api_gap_pct"] == pytest.approx(0.05)
    assert result["buy_sell_ratio"] == 0.0


async def test_expected_quote_drift_can_promote_candidate_to_high_gap():
    """Ranking CORE_GAP can become HIGH_GAP when expected quote drifts above core max."""
    item = {
        "mksc_shrn_iscd": "005930",
        "hts_kor_isnm": "삼성전자",
        "prdy_ctrt": "6.00",
        "stck_prpr": "10600",
        "avrg_vol": "100000",
        "acml_tr_pbmn": "10000000",
        "acml_vol": "1000",
    }
    quote = {
        "expected_price": 10800.0,
        "expected_qty": 600000,
        "expected_amount": 6_486_000_000.0,
        "expected_gap_pct": 8.1,
        "prev_close": 10000.0,
    }

    with patch(
        "src.modules.f1_filter._fetch_expected_quote",
        new_callable=AsyncMock, return_value=quote,
    ):
        result = await f1_mod._parse_candidate(item)

    assert result["ranking_gap_pct"] == pytest.approx(0.06)
    assert result["expected_api_gap_pct"] == pytest.approx(0.081)
    assert result["gap_pct"] == pytest.approx(0.081)
    assert result["gap_band"] == "HIGH_GAP"
    assert result["gap_reason"] == "HIGH_GAP_ALLOWED"
    assert result["gap_allowed"] is True


async def test_parse_candidate_keeps_kosdaq_label_but_uses_kis_quote_market():
    """KOSDAQ rows are labeled Q locally, but KIS expected quote uses market code J."""
    item = {
        "mksc_shrn_iscd": "126640",
        "hts_kor_isnm": "화신정공",
        "prdy_ctrt": "4.00",
        "stck_prpr": "10000",
        "avrg_vol": "1000",
        "acml_tr_pbmn": "10000000",
        "acml_vol": "1000",
    }

    with patch(
        "src.modules.f1_filter._fetch_expected_quote",
        new_callable=AsyncMock, return_value=None,
    ) as quote:
        result = await f1_mod._parse_candidate(item, "Q")

    quote.assert_awaited_once_with("126640", "J")
    assert result["market"] == "Q"


async def test_parse_candidate_preserves_unrounded_prev_close_for_replay():
    """Snapshot rows should preserve the exact prev_close used to calculate vi_gap."""
    item = {
        "mksc_shrn_iscd": "005930",
        "hts_kor_isnm": "삼성전자",
        "prdy_ctrt": "3.70",
        "stck_prpr": "12345",
        "avrg_vol": "1000",
        "acml_tr_pbmn": "10000000",
        "acml_vol": "1000",
    }

    with patch(
        "src.modules.f1_filter._fetch_expected_quote",
        new_callable=AsyncMock, return_value=None,
    ):
        result = await f1_mod._parse_candidate(item)

    assert result["prev_close"] == pytest.approx(12345 / 1.037)
    assert result["prev_close"] != round(result["prev_close"])


async def test_fetch_expected_quote_uses_kis_quote_market_code():
    """Expected quote API sends the KIS quote market code."""
    async def fake_get(*args, **kwargs):
        assert kwargs["params"]["FID_COND_MRKT_DIV_CODE"] == "J"
        assert kwargs["params"]["FID_INPUT_ISCD"] == "126640"
        return {
            "rt_cd": "0",
            "output2": {
                "antc_cnpr": "10500",
                "antc_vol": "2000",
                "antc_cntg_prdy_ctrt": "5.00",
                "antc_cntg_vrss": "500",
            },
        }

    with patch("src.api.kis_rest.get", new=fake_get):
        result = await f1_mod._fetch_expected_quote("126640", "J")

    assert result["expected_price"] == 10500
    assert result["expected_qty"] == 2000


async def test_fetch_all_premarket_requests_high_gap_band_from_api():
    """KIS ranking API must include 7~10% rows so local HIGH_GAP classification can run."""
    seen_rates = []

    async def fake_get(*args, **kwargs):
        seen_rates.append(kwargs["params"]["fid_rsfl_rate2"])
        return {"rt_cd": "0", "output": []}

    with (
        patch("src.api.kis_rest.get", new=fake_get),
        patch("src.modules.f1_filter.F1_MARKET_INTERVAL_SEC", 0),
    ):
        await _fetch_all_premarket()

    assert seen_rates == ["10.0", "10.0"]


async def test_fetch_all_premarket_waits_between_markets(monkeypatch):
    """F1 should pause before KOSDAQ ranking to avoid KIS burst limits."""
    markets = []

    async def fake_get(*args, **kwargs):
        params = kwargs["params"]
        markets.append((params["fid_cond_mrkt_div_code"], params["fid_input_iscd"]))
        return {"rt_cd": "0", "output": []}

    with (
        patch("src.api.kis_rest.get", new=fake_get),
        patch("src.modules.f1_filter.F1_MARKET_INTERVAL_SEC", 1.5),
        patch("src.modules.f1_filter.asyncio.sleep", new_callable=AsyncMock) as sleep,
    ):
        await _fetch_all_premarket()

    assert markets == [("J", "0001"), ("J", "1001")]
    sleep.assert_awaited_once_with(1.5)


async def test_fetch_all_premarket_enriches_expected_quotes_with_limited_concurrency(monkeypatch):
    (
        "Expected quote enrichment should be concurrent without exceeding the "
        "configured slot count."
    )
    monkeypatch.setattr(f1_mod, "F1_EXPECTED_QUOTE_CONCURRENCY", 3)
    active = 0
    max_active = 0

    async def fake_get(*args, **kwargs):
        input_iscd = kwargs["params"]["fid_input_iscd"]
        if input_iscd == "1001":
            return {"rt_cd": "0", "output": []}
        return {
            "rt_cd": "0",
            "output": [
                {
                    "mksc_shrn_iscd": f"{i:06d}",
                    "hts_kor_isnm": f"TEST{i}",
                    "prdy_ctrt": "4.00",
                    "stck_prpr": "10000",
                    "avrg_vol": "1000",
                    "acml_tr_pbmn": "10000000",
                    "acml_vol": "1000",
                }
                for i in range(1, 10)
            ],
        }

    async def fake_quote(ticker, market):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return None

    with (
        patch("src.api.kis_rest.get", new=fake_get),
        patch("src.modules.f1_filter._fetch_expected_quote", new=fake_quote),
        patch("src.modules.f1_filter.F1_MARKET_INTERVAL_SEC", 0),
    ):
        result = await _fetch_all_premarket()

    assert len(result) == 9
    assert 1 < max_active <= 3


async def test_fetch_all_premarket_logs_progress_while_fetching(monkeypatch):
    events = []
    monkeypatch.setattr(f1_mod, "F1_PROGRESS_LOG_EVERY", 2)

    async def fake_get(*args, **kwargs):
        input_iscd = kwargs["params"]["fid_input_iscd"]
        if input_iscd == "1001":
            return {"rt_cd": "0", "output": []}
        return {
            "rt_cd": "0",
            "output": [
                {
                    "mksc_shrn_iscd": f"{i:06d}",
                    "hts_kor_isnm": f"TEST{i}",
                    "prdy_ctrt": "4.00",
                    "stck_prpr": "10000",
                    "avrg_vol": "1000",
                    "acml_tr_pbmn": "10000000",
                    "acml_vol": "1000",
                }
                for i in range(1, 4)
            ],
        }

    with (
        patch("src.api.kis_rest.get", new=fake_get),
        patch(
            "src.modules.f1_filter._fetch_expected_quote",
            new_callable=AsyncMock, return_value=None,
        ),
        patch("src.modules.f1_filter.F1_MARKET_INTERVAL_SEC", 0),
        patch("src.modules.f1_filter.log", lambda event, **kwargs: events.append((event, kwargs))),
    ):
        await _fetch_all_premarket()

    event_names = [event for event, _ in events]
    assert "F1_FETCH_START" in event_names
    assert "F1_EXPECTED_QUOTE_START" in event_names
    assert "F1_EXPECTED_QUOTE_PROGRESS" in event_names
    assert "F1_EXPECTED_QUOTE_DONE" in event_names
    progress = [kwargs for event, kwargs in events if event == "F1_EXPECTED_QUOTE_PROGRESS"]
    assert progress[-1]["completed"] == 3
    assert progress[-1]["total"] == 3
    assert progress[-1]["eligible_count"] == 3
    assert progress[-1]["quote_fallback_count"] == 3
    assert progress[-1]["error_count"] == 0


def test_save_candidate_snapshot_rotates_old_files(tmp_path, monkeypatch):
    """F1 snapshots keep only the newest configured files."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("F1_SAVE_SNAPSHOT", "1")
    monkeypatch.setattr(f1_mod, "F1_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setattr(f1_mod, "F1_SNAPSHOT_KEEP", 2)

    for i in range(3):
        old = tmp_path / f"20260101_00000{i}.jsonl"
        old.write_text(json.dumps({"ticker": f"OLD{i}"}) + "\n", encoding="utf-8")
        old_mtime = time.time() - (10 - i)
        old.touch()
        os.utime(old, (old_mtime, old_mtime))

    f1_mod._save_candidate_snapshot([{"ticker": "NEW"}])

    files = sorted(tmp_path.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    assert len(files) == 2
    assert any("NEW" in p.read_text(encoding="utf-8") for p in files)


async def test_no_target_retries_before_day_skip():
    """08:58 전에는 빈 필터 결과를 즉시 day_skip 처리하지 않고 재조회."""
    fetch = AsyncMock(side_effect=[
        [_classified_candidate(0.0)],
        [_classified_candidate(0.0)],
    ])
    with (
        patch("src.modules.f1_filter._fetch_all_premarket", fetch),
        patch("src.modules.f1_filter._should_retry", side_effect=[True, False]),
        patch("asyncio.sleep", new_callable=AsyncMock) as sleep,
        patch("src.notifier.send", new_callable=AsyncMock),
        patch("src.db.record_skip", new_callable=AsyncMock),
    ):
        result = await run()

    assert result == []
    assert fetch.await_count == 2
    sleep.assert_awaited_once()
    assert _state_mod.get().day_skip is True


async def test_selector_empty_result_records_no_target(monkeypatch):
    """Selector-floor exclusions should follow the same NO_TARGET path as gap empty."""
    monkeypatch.setattr(f1_mod.f1_selector, "MIN_EXPECTED_AMOUNT", 2e9)
    candidate = _classified_candidate(0.05, amount=1e9)
    fetch = AsyncMock(return_value=[candidate])
    record_skip = AsyncMock()

    with (
        patch("src.modules.f1_filter._fetch_all_premarket", fetch),
        patch("src.notifier.send", new_callable=AsyncMock),
        patch("src.db.record_skip", record_skip),
    ):
        result = await run()

    assert result == []
    assert _state_mod.get().day_skip is True
    record_skip.assert_awaited_once()
    assert "SELECTION_FILTER_EMPTY" in record_skip.await_args.args[2]


async def test_non_terminal_fallback_empty_does_not_set_no_target(monkeypatch):
    candidate = _classified_candidate(0.0)
    fetch = AsyncMock(return_value=[candidate])
    notify = AsyncMock()
    record_skip = AsyncMock()
    monkeypatch.setattr(f1_mod, "_should_retry", lambda: False)

    with (
        patch("src.modules.f1_filter._fetch_all_premarket", fetch),
        patch("src.notifier.send", notify),
        patch("src.db.record_skip", record_skip),
    ):
        result = await run(terminal_on_empty=False)

    assert result == []
    assert _state_mod.get().day_skip is False
    notify.assert_not_awaited()
    record_skip.assert_not_awaited()


# ── 유동성 필터 ───────────────────────────────────────────────────────

async def test_liquidity_top_10_pct_single_result():
    """10종목 통과 → 상위 10%(1개) 반환, 유동성 최고 종목 선택."""
    candidates = [
        _classified_candidate(0.05, amount=float(i) * 1e9, ticker=f"TICK{i:02d}")
        for i in range(1, 11)  # i=1~10, 최댓값 i=10 → 10e9
    ]
    result = await _run(candidates)
    assert len(result) == 10
    assert [c["ticker"] for c in result] == [
        "TICK10",
        "TICK09",
        "TICK08",
        "TICK07",
        "TICK06",
        "TICK05",
        "TICK04",
        "TICK03",
        "TICK02",
        "TICK01",
    ]


async def test_liquidity_min_one_result():
    """1종목 통과 시 무조건 1개 반환 (max(1, floor(1*0.1)) = 1)."""
    result = await _run([_classified_candidate(0.05)])
    assert len(result) == 1


async def test_liquidity_top_10_of_20():
    """20종목 통과 → 상위 10%(2개) 반환."""
    candidates = [
        _classified_candidate(0.05, amount=float(i) * 1e9, ticker=f"TICK{i:02d}")
        for i in range(1, 21)
    ]
    result = await _run(candidates)
    assert len(result) == 10
    amounts = {c["avg_amount_5d"] for c in result}
    assert amounts == {float(i) * 1e9 for i in range(11, 21)}


async def test_day_skip_returns_early():
    """day_skip=True 시 즉시 빈 리스트 반환 (API 호출 없음)."""
    _state_mod.get().day_skip = True
    mock_fetch = AsyncMock()
    with patch("src.modules.f1_filter._fetch_all_premarket", mock_fetch):
        result = await run()
    assert result == []
    mock_fetch.assert_not_awaited()


async def test_fetch_excludes_etf_etn_leverage_inverse_products():
    """KIS 원본 응답에서 ETF/ETN/레버리지/인버스 상품은 F1 후보에서 제외."""
    events = []

    async def fake_get(*args, **kwargs):
        input_iscd = kwargs["params"]["fid_input_iscd"]
        if input_iscd == "1001":
            return {"rt_cd": "0", "output": []}
        return {
            "rt_cd": "0",
            "output": [
                {
                    "mksc_shrn_iscd": "252670",
                    "hts_kor_isnm": "KODEX 200선물인버스2X",
                    "prdy_ctrt": "4.35",
                    "stck_prpr": "72",
                    "avrg_vol": "1000000",
                    "acml_tr_pbmn": "72000000",
                    "acml_vol": "1000000",
                },
                {
                    "mksc_shrn_iscd": "0197X0",
                    "hts_kor_isnm": "SOL SK하이닉스선물단일종목인버스2X",
                    "prdy_ctrt": "4.20",
                    "stck_prpr": "7515",
                    "avrg_vol": "1000000",
                    "acml_tr_pbmn": "7515000000",
                    "acml_vol": "1000000",
                },
                {
                    "mksc_shrn_iscd": "005930",
                    "hts_kor_isnm": "삼성전자",
                    "prdy_ctrt": "4.00",
                    "stck_prpr": "80000",
                    "avrg_vol": "1000000",
                    "acml_tr_pbmn": "80000000000",
                    "acml_vol": "1000000",
                },
            ],
        }

    with (
        patch("src.api.kis_rest.get", new=fake_get),
        patch(
            "src.modules.f1_filter._fetch_expected_quote",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("src.modules.f1_filter.F1_MARKET_INTERVAL_SEC", 0),
        patch("src.modules.f1_filter.log", lambda event, **kwargs: events.append((event, kwargs))),
    ):
        result = await _fetch_all_premarket()

    assert [c["ticker"] for c in result] == ["005930"]
    done = [fields for event, fields in events if event == "F1_EXPECTED_QUOTE_DONE"][0]
    assert done["processed_count"] == 3
    assert done["eligible_count"] == 1
    assert done["skipped_count"] == 2
    assert done["skip_reasons"] == {
        "EXCLUDED_PRODUCT": 1,
        "INVALID_TICKER": 1,
    }
    assert result[0]["name"] == "삼성전자"
