"""C1 종가 스크리닝 — 순수 함수만 검사한다. 네트워크는 부르지 않는다.

규칙 임계값은 스펙 §2.2에 고정돼 있다. 여기서 값을 바꾸면 스펙부터 바꿔야 한다.
"""

import asyncio
import json
from typing import Any

import pytest

from scripts.overnight_screen import (
    AMOUNT_MULTIPLE_MIN,
    CALL_BUDGET,
    CHANGE_MAX,
    CHANGE_MIN,
    CLOSE_POSITION_MIN,
    _kis_fetchers,
    build_row,
    classify_calendar_probe,
    close_position,
    evaluate,
    is_excluded_name,
    is_trading_day,
    main,
    merge_calendar,
    parse_daily,
    rank_candidates,
    ranking_params,
    screen,
    write_calendar,
    write_candidates,
)


def _row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ticker": "000001", "name": "정상전자", "change_pct": 5.0,
        "open": 10000.0, "high": 11000.0, "low": 9900.0, "close": 10800.0,
        "amount": 5_000_000_000.0, "avg_amount_20d": 1_000_000_000.0,
    }
    base.update(over)
    base["close_position"] = close_position(base["high"], base["low"], base["close"])
    base["amount_multiple"] = (
        base["amount"] / base["avg_amount_20d"] if base["avg_amount_20d"] else None
    )
    return base


def test_thresholds_match_spec():
    assert (CHANGE_MIN, CHANGE_MAX, CLOSE_POSITION_MIN, AMOUNT_MULTIPLE_MIN) == (
        3.0, 25.0, 0.8, 2.0
    )


def test_close_position_is_fraction_of_range_and_none_when_flat():
    assert close_position(110.0, 100.0, 108.0) == 0.8
    assert close_position(100.0, 100.0, 100.0) is None


def test_evaluate_passes_a_textbook_close():
    assert evaluate(_row()) is None


def test_evaluate_rejects_each_rule_with_its_reason():
    assert evaluate(_row(change_pct=2.9)) == "CHANGE_PCT"
    assert evaluate(_row(change_pct=25.0)) == "CHANGE_PCT"
    assert evaluate(_row(close=10700.0)) == "CLOSE_POSITION"       # (10700-9900)/1100 = 0.727
    assert evaluate(_row(high=100.0, low=100.0, close=100.0)) == "CLOSE_POSITION"
    assert evaluate(_row(amount=1_900_000_000.0)) == "AMOUNT_MULTIPLE"
    assert evaluate(_row(amount=900_000_000.0, avg_amount_20d=100_000_000.0)) == "AMOUNT_FLOOR"
    assert evaluate(_row(close=999.0, high=1000.0, low=990.0)) == "PRICE_FLOOR"
    assert evaluate(_row(name="KODEX 200")) == "NAME_EXCLUDED"
    assert evaluate(_row(avg_amount_20d=None)) == "NO_HISTORY"


def test_excluded_names_cover_etf_etn_spac():
    assert is_excluded_name("TIGER 반도체")
    assert is_excluded_name("삼성 KRX 2X ETN")
    assert is_excluded_name("하나32호스팩")
    assert is_excluded_name("RISE 200")
    assert is_excluded_name("KIWOOM 200")
    assert not is_excluded_name("성호전자")


def test_rank_orders_by_amount_multiple_then_change_and_caps_at_five():
    rows = [
        _row(ticker="A", amount=3_000_000_000.0, change_pct=4.0),
        _row(ticker="B", amount=5_000_000_000.0, change_pct=4.0),
        _row(ticker="C", amount=5_000_000_000.0, change_pct=9.0),
        _row(ticker="D", change_pct=2.0),                      # 거부, 순위 밖
        _row(ticker="E", amount=2_500_000_000.0),
        _row(ticker="F", amount=2_400_000_000.0),
        _row(ticker="G", amount=2_300_000_000.0),
    ]
    ranked = rank_candidates(rows)
    assert [r["ticker"] for r in ranked] == ["C", "B", "A", "E", "F"]
    assert [r["rank"] for r in ranked] == [1, 2, 3, 4, 5]


def _daily_row(date: str, close: float, amount: float, high=None, low=None, open_=None) -> dict:
    return {
        "stck_bsop_date": date, "stck_clpr": str(close),
        "stck_oprc": str(open_ if open_ is not None else close),
        "stck_hgpr": str(high if high is not None else close),
        "stck_lwpr": str(low if low is not None else close),
        "acml_vol": "1000", "acml_tr_pbmn": str(amount),
    }


def test_parse_daily_reads_today_and_prior_20_day_mean():
    # 최신순으로 오지만 순서에 기대지 않는다. 20260921이 오늘, 앞 20일 평균은 1e9.
    rows = [_daily_row("20260921", 108.0, 5e9, high=110.0, low=99.0, open_=100.0)]
    rows += [_daily_row(f"2026{8 + i // 30:02d}{1 + i % 30:02d}", 100.0, 1e9) for i in range(25)]
    rows.reverse()
    parsed = parse_daily(rows, "20260921")
    assert parsed["close"] == 108.0 and parsed["high"] == 110.0 and parsed["low"] == 99.0
    assert parsed["amount"] == 5e9
    assert parsed["avg_amount_20d"] == 1e9
    assert parsed["history_days"] == 20


def test_parse_daily_marks_short_history_and_missing_today():
    rows = [_daily_row("20260921", 108.0, 5e9)] + [
        _daily_row(f"202609{d:02d}", 100.0, 1e9) for d in range(1, 6)
    ]
    parsed = parse_daily(rows, "20260921")
    assert parsed["avg_amount_20d"] is None and parsed["history_days"] == 5
    assert parse_daily(rows, "20260922") is None


def test_is_trading_day_true_iff_calendar_ticker_has_a_bar_for_date():
    # 휴장일 가드(F1): 랭킹은 휴장일에도 전 거래일 값을 돌려주므로, 달력 종목
    # (005930)의 일봉에 그날 행이 있는지만 본다.
    rows = [_daily_row("20260921", 108.0, 5e9)]
    assert is_trading_day(rows, "20260921") is True
    assert is_trading_day(rows, "20260922") is False   # 휴장일 — 전 거래일 봉만 있음
    assert is_trading_day([], "20260921") is False


def test_classify_calendar_probe_distinguishes_failure_from_holiday():
    # 프로브 실패(KIS 오류·토큰 만료·네트워크 전송 실패 — fetch_daily가 전부
    # ([], "KIS_ERROR:...")로 돌려준다)를 휴장일로 오인하면 안 된다(F1 라운드 2).
    rows = [_daily_row("20260921", 108.0, 5e9)]
    assert classify_calendar_probe(rows, None, "20260921") == "TRADING_DAY"
    assert classify_calendar_probe(rows, None, "20260922") == "HOLIDAY"
    assert classify_calendar_probe([], "KIS_ERROR:EGW00123", "20260921") == "FAILED"
    # 오류가 있으면 output2가 우연히 비어 있지 않아도(예: 부분 응답) 여전히 FAILED다.
    assert classify_calendar_probe(rows, "EXCEPTION:RuntimeError", "20260921") == "FAILED"


def test_build_row_carries_raw_fields_and_daily_failure():
    ranking = {"mksc_shrn_iscd": "000001", "hts_kor_isnm": "정상전자", "prdy_ctrt": "5.00",
               "stck_prpr": "108", "acml_tr_pbmn": "5000000000"}
    daily = {"open": 100.0, "high": 110.0, "low": 99.0, "close": 108.0, "amount": 5e9,
             "avg_amount_20d": 1e9, "history_days": 20}
    row = build_row("20260921", ranking, daily, None)
    assert row["ticker"] == "000001" and row["date"] == "20260921"
    assert row["change_pct"] == 5.0 and row["close_position"] == close_position(110.0, 99.0, 108.0)
    assert row["amount_multiple"] == 5.0
    assert row["raw"]["ranking"] is ranking and row["raw"]["daily"] is daily
    assert row["rejected_reason"] is None

    failed = build_row("20260921", ranking, None, "KIS_ERROR:EGW00123")
    assert failed["rejected_reason"] == "DAILY_FAILED"
    assert failed["raw"]["daily_error"] == "KIS_ERROR:EGW00123"
    assert failed["close"] == 108.0  # 랭킹의 현재가로라도 채운다


def test_ranking_params_only_overrides_the_change_range():
    from src.modules.paper_fast_probe import _ranking_params
    base = _ranking_params("0001")
    ours = ranking_params("0001")
    assert ours["fid_rsfl_rate1"] == f"{CHANGE_MIN:.1f}"
    assert ours["fid_rsfl_rate2"] == f"{CHANGE_MAX:.1f}"
    assert {k: v for k, v in ours.items() if not k.startswith("fid_rsfl")} == {
        k: v for k, v in base.items() if not k.startswith("fid_rsfl")
    }


def _ranking_output(*tickers: str) -> list[dict]:
    return [
        {"mksc_shrn_iscd": t, "hts_kor_isnm": f"종목{t}", "prdy_ctrt": "5.00",
         "stck_prpr": "10800", "acml_tr_pbmn": "5000000000"}
        for t in tickers
    ]


def test_screen_joins_ranking_with_daily_and_records_rejections_and_summary():
    async def fetch_ranking(market: str) -> list[dict]:
        if market == "0001":
            return _ranking_output("000001", "000002")
        return _ranking_output("000003")

    async def fetch_daily(ticker: str) -> tuple[list[dict], str | None]:
        if ticker == "000003":
            return [], "KIS_ERROR:EGW00123"
        rows = [_daily_row("20260921", 10800.0, 5e9, high=11000.0, low=9900.0, open_=10000.0)]
        rows += [_daily_row(f"202608{d:02d}", 100.0, 1e9) for d in range(1, 26)]
        if ticker == "000002":
            # 종가 위치 0.45
            rows[0] = _daily_row("20260921", 10400.0, 5e9, high=11000.0, low=9900.0)
        return rows, None

    rows, summary = asyncio.run(
        screen("20260921", fetch_ranking=fetch_ranking, fetch_daily=fetch_daily)
    )
    by_ticker = {r["ticker"]: r for r in rows}
    assert by_ticker["000001"]["rank"] == 1 and by_ticker["000001"]["rejected_reason"] is None
    assert by_ticker["000002"]["rank"] is None
    assert by_ticker["000002"]["rejected_reason"] == "CLOSE_POSITION"
    assert by_ticker["000003"]["rejected_reason"] == "DAILY_FAILED"
    assert summary == {
        "summary": True, "date": "20260921", "universe": 3, "candidates": 1, "degraded": True,
    }


def test_screen_dedupes_tickers_across_markets_and_marks_zero_candidate_days():
    async def fetch_ranking(market: str) -> list[dict]:
        return _ranking_output("000001")

    async def fetch_daily(ticker: str) -> tuple[list[dict], str | None]:
        return [_daily_row("20260921", 10800.0, 5e9, high=11000.0, low=9900.0)] + [
            _daily_row(f"202608{d:02d}", 100.0, 4e9) for d in range(1, 26)   # 배수 1.25
        ], None

    rows, summary = asyncio.run(
        screen("20260921", fetch_ranking=fetch_ranking, fetch_daily=fetch_daily)
    )
    assert len(rows) == 1 and rows[0]["rejected_reason"] == "AMOUNT_MULTIPLE"
    assert summary["universe"] == 1 and summary["candidates"] == 0 and not summary["degraded"]


def test_screen_isolates_a_daily_fetch_exception_to_one_ticker():
    async def fetch_ranking(market: str) -> list[dict]:
        return _ranking_output("000001", "000002")

    async def fetch_daily(ticker: str) -> tuple[list[dict], str | None]:
        if ticker == "000002":
            raise RuntimeError("boom")
        rows = [_daily_row("20260921", 10800.0, 5e9, high=11000.0, low=9900.0, open_=10000.0)]
        rows += [_daily_row(f"202608{d:02d}", 100.0, 1e9) for d in range(1, 26)]
        return rows, None

    rows, summary = asyncio.run(
        screen("20260921", fetch_ranking=fetch_ranking, fetch_daily=fetch_daily)
    )
    by_ticker = {r["ticker"]: r for r in rows}
    assert by_ticker["000002"]["rejected_reason"] == "DAILY_FAILED"
    assert by_ticker["000002"]["raw"]["daily_error"] == "EXCEPTION:RuntimeError"
    assert by_ticker["000001"]["rank"] == 1
    assert summary["degraded"] is True


def test_screen_stops_calling_fetch_daily_once_the_call_budget_is_exceeded():
    # F2: 두 번째 종목에서 RequestBudgetExceeded가 나면 세 번째는 fetch_daily를
    # 아예 부르지 않고 바로 DAILY_FAILED로 남긴다 — 호출 2회로 끝나야 한다.
    from src.api.kis_rest import RequestBudgetExceeded

    calls: list[str] = []

    async def fetch_ranking(market: str) -> list[dict]:
        if market == "0001":
            return _ranking_output("000001", "000002")
        return _ranking_output("000003")

    async def fetch_daily(ticker: str) -> tuple[list[dict], str | None]:
        calls.append(ticker)
        if ticker == "000002":
            raise RequestBudgetExceeded("call budget exceeded: used=100 max=100")
        rows = [_daily_row("20260921", 10800.0, 5e9, high=11000.0, low=9900.0, open_=10000.0)]
        rows += [_daily_row(f"202608{d:02d}", 100.0, 1e9) for d in range(1, 26)]
        return rows, None

    rows, summary = asyncio.run(
        screen("20260921", fetch_ranking=fetch_ranking, fetch_daily=fetch_daily)
    )
    assert calls == ["000001", "000002"]   # 000003은 부르지 않았다
    by_ticker = {r["ticker"]: r for r in rows}
    assert by_ticker["000002"]["rejected_reason"] == "DAILY_FAILED"
    assert by_ticker["000002"]["raw"]["daily_error"] == "EXCEPTION:RequestBudgetExceeded"
    assert by_ticker["000003"]["rejected_reason"] == "DAILY_FAILED"
    assert by_ticker["000003"]["raw"]["daily_error"] == "EXCEPTION:RequestBudgetExceeded"
    assert summary["degraded"] is True


def test_kis_fetchers_binds_ranking_and_daily_tr_ids_and_date(monkeypatch):
    # M10: 네트워크 없이 kis_rest.get 자체를 가짜로 바꿔 두 콜의 kwargs만 검사한다.
    # _kis_fetchers는 kis_rest를 함수 안에서 늦게 import하므로 모듈 객체에 패치한다.
    import src.api.kis_rest as kis_rest
    from scripts.catalyst_label import DAILY_TR
    from scripts.fast_path_counterfactual import Throttle
    from src.modules.paper_fast_probe import RANKING_TR_ID

    calls: list[dict] = []

    async def fake_get(path, **kwargs):
        calls.append(kwargs)
        return {"rt_cd": "0", "output": [], "output2": []}

    monkeypatch.setattr(kis_rest, "get", fake_get)

    fetch_ranking, fetch_daily = _kis_fetchers(
        kis_rest.CallBudget(CALL_BUDGET), Throttle(0.0), "20260921"
    )
    asyncio.run(fetch_ranking("0001"))
    asyncio.run(fetch_daily("005930"))

    assert len(calls) == 2
    ranking_call, daily_call = calls
    # 랭킹도 일봉처럼 kis_rest의 백오프 재시도에 맡긴다 — 마감 후 랭킹은 유량이
    # 한가하므로 RATE_LIMIT에서 바로 포기하지 않는다(E4).
    assert ranking_call["stop_on_rate_limit"] is False
    assert ranking_call["tr_id"] == RANKING_TR_ID
    assert daily_call["stop_on_rate_limit"] is False
    assert daily_call["tr_id"] == DAILY_TR
    assert daily_call["params"]["FID_INPUT_DATE_2"] == "20260921"


def test_write_candidates_puts_summary_last(tmp_path):
    path = tmp_path / "20260921.jsonl"
    write_candidates(path, [{"ticker": "000001", "rank": 1}], {"summary": True, "candidates": 1})
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["ticker"] == "000001"
    assert lines[-1]["summary"] is True


def test_write_candidates_leaves_no_tmp_file_behind(tmp_path):
    path = tmp_path / "20260921.jsonl"
    write_candidates(path, [{"ticker": "000001", "rank": 1}], {"summary": True, "candidates": 1})
    assert path.exists()
    assert not path.with_suffix(".jsonl.tmp").exists()


# --- A: 거래일 달력(calendar.json) ------------------------------------------------


def test_merge_calendar_unions_and_sorts():
    assert merge_calendar(["20260918", "20260921"], {"20260919", "20260921"}) == [
        "20260918", "20260919", "20260921",
    ]


def test_merge_calendar_with_no_existing_file_is_just_the_fetched_set():
    assert merge_calendar([], {"20260921", "20260918"}) == ["20260918", "20260921"]


def test_write_calendar_creates_file_and_merges_with_existing(tmp_path):
    path = tmp_path / "data" / "overnight" / "calendar.json"
    write_calendar(path, {"20260918", "20260919"})
    assert json.loads(path.read_text(encoding="utf-8")) == ["20260918", "20260919"]

    write_calendar(path, {"20260921"})   # 다음 실행 — 합집합으로 합친다
    assert json.loads(path.read_text(encoding="utf-8")) == [
        "20260918", "20260919", "20260921",
    ]
    assert not path.with_suffix(".json.tmp").exists()


def test_write_calendar_ignores_a_corrupt_existing_file(tmp_path):
    path = tmp_path / "data" / "overnight" / "calendar.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    write_calendar(path, {"20260921"})
    assert json.loads(path.read_text(encoding="utf-8")) == ["20260921"]


def test_screen_propagates_a_fetch_ranking_exception():
    # 랭킹 호출 자체가 죽으면(네트워크·PocStop이 아닌 일반 예외) screen()이 삼키지
    # 않고 그대로 올려야 한다 — 일봉 실패(DAILY_FAILED)와 달리 유니버스가 없으면
    # 그 시장 전체가 결측이라 조용히 넘어갈 수 없다.
    async def fetch_ranking(market: str) -> list[dict]:
        raise RuntimeError("boom")

    async def fetch_daily(ticker: str) -> tuple[list[dict], str | None]:
        return [], "KIS_ERROR:UNUSED"

    with pytest.raises(RuntimeError):
        asyncio.run(screen("20260921", fetch_ranking=fetch_ranking, fetch_daily=fetch_daily))


# --- D: main_async 종료 코드 -------------------------------------------------------


def _install_main_async_fakes(monkeypatch, *, fetch_ranking, fetch_daily):
    """main_async가 함수 안에서 늦게 import하는 세 이름을 그 원본 모듈에 패치한다."""
    import scripts.overnight_screen as overnight_screen_module
    import scripts.track_b_backfill as track_b_backfill_module
    import src.api.auth as auth_module

    def fake_kis_fetchers(budget, throttle, date):
        return fetch_ranking, fetch_daily

    async def fake_load_or_refresh():
        return "token"

    monkeypatch.setattr(overnight_screen_module, "_kis_fetchers", fake_kis_fetchers)
    monkeypatch.setattr(auth_module, "load_or_refresh", fake_load_or_refresh)
    monkeypatch.setattr(track_b_backfill_module, "assert_paper_mode", lambda: None)


def _candidates_path(root, date):
    return root / "data" / "overnight" / "candidates" / f"{date}.jsonl"


def _calendar_path(root):
    return root / "data" / "overnight" / "calendar.json"


def test_main_async_holiday_returns_zero_writes_no_candidates_but_writes_calendar(
    tmp_path, monkeypatch
):
    calendar_rows = [_daily_row("20260918", 100.0, 1e9)]   # 20260921 봉 없음 — 휴장일

    async def fetch_ranking(market: str) -> list[dict]:
        return []   # HOLIDAY면 screen()까지 가지 않으므로 호출되지 않는다

    async def fetch_daily(ticker: str) -> tuple[list[dict], str | None]:
        return calendar_rows, None

    _install_main_async_fakes(monkeypatch, fetch_ranking=fetch_ranking, fetch_daily=fetch_daily)

    rc = main(["--root", str(tmp_path), "--date", "20260921"])

    assert rc == 0
    assert not _candidates_path(tmp_path, "20260921").exists()
    assert _calendar_path(tmp_path).exists()
    assert json.loads(_calendar_path(tmp_path).read_text(encoding="utf-8")) == ["20260918"]


def test_main_async_calendar_probe_failed_returns_two_and_writes_nothing(tmp_path, monkeypatch):
    async def fetch_ranking(market: str) -> list[dict]:
        return []

    async def fetch_daily(ticker: str) -> tuple[list[dict], str | None]:
        return [], "KIS_ERROR:X"

    _install_main_async_fakes(monkeypatch, fetch_ranking=fetch_ranking, fetch_daily=fetch_daily)

    rc = main(["--root", str(tmp_path), "--date", "20260921"])

    assert rc == 2
    assert not _candidates_path(tmp_path, "20260921").exists()
    assert not _calendar_path(tmp_path).exists()


def test_main_async_empty_universe_returns_two_and_writes_no_candidates_file(
    tmp_path, monkeypatch
):
    calendar_rows = [_daily_row("20260921", 100.0, 1e9)]   # 프로브 통과 — 거래일

    async def fetch_ranking(market: str) -> list[dict]:
        return []   # 두 시장 모두 빈 랭킹

    async def fetch_daily(ticker: str) -> tuple[list[dict], str | None]:
        return calendar_rows, None

    _install_main_async_fakes(monkeypatch, fetch_ranking=fetch_ranking, fetch_daily=fetch_daily)

    rc = main(["--root", str(tmp_path), "--date", "20260921"])

    assert rc == 2
    assert not _candidates_path(tmp_path, "20260921").exists()


def test_main_async_normal_run_writes_candidates_with_summary_last_and_calendar(
    tmp_path, monkeypatch
):
    calendar_rows = [_daily_row("20260921", 100.0, 1e9)]   # 프로브 통과 — 거래일

    async def fetch_ranking(market: str) -> list[dict]:
        if market == "0001":
            return _ranking_output("000001")
        return []

    async def fetch_daily(ticker: str) -> tuple[list[dict], str | None]:
        if ticker == "005930":
            return calendar_rows, None
        rows = [_daily_row("20260921", 10800.0, 5e9, high=11000.0, low=9900.0, open_=10000.0)]
        rows += [_daily_row(f"202608{d:02d}", 100.0, 1e9) for d in range(1, 26)]
        return rows, None

    _install_main_async_fakes(monkeypatch, fetch_ranking=fetch_ranking, fetch_daily=fetch_daily)

    rc = main(["--root", str(tmp_path), "--date", "20260921"])

    assert rc == 0
    path = _candidates_path(tmp_path, "20260921")
    assert path.exists()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines[-1]["summary"] is True
    assert any(r.get("ticker") == "000001" and r.get("rank") == 1 for r in lines[:-1])
    assert _calendar_path(tmp_path).exists()
