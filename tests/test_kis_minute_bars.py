from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.api import kis_minute_bars as mb

KST = ZoneInfo("Asia/Seoul")


def _row(time_, o, h, low, c, v):
    return {
        "stck_bsop_date": "20260827",
        "stck_cntg_hour": time_,
        "stck_oprc": str(o),
        "stck_hgpr": str(h),
        "stck_lwpr": str(low),
        "stck_prpr": str(c),
        "cntg_vol": str(v),
    }


def test_parse_sorts_bars_by_date_and_time():
    resp = {"output2": [_row("093500", 1, 2, 1, 2, 10), _row("093400", 1, 2, 1, 1, 5)]}

    bars, issues = mb.parse_minute_bars(resp)

    assert [b["time"] for b in bars] == ["093400", "093500"]
    assert issues == {"empty_bar": 0, "field_missing": 0}


def test_parse_counts_rows_with_missing_fields_and_excludes_them():
    resp = {"output2": [_row("093500", 1, 2, 1, 2, 10), {"stck_cntg_hour": "093600"}, {}]}

    bars, issues = mb.parse_minute_bars(resp)

    assert len(bars) == 1
    assert issues["field_missing"] == 1
    assert issues["empty_bar"] == 1


def test_parse_falls_back_to_output_key():
    bars, _ = mb.parse_minute_bars({"output": [_row("093500", 1, 2, 1, 2, 10)]})

    assert len(bars) == 1


def test_parse_raises_when_no_row_container_exists():
    with pytest.raises(mb.MinuteBarError):
        mb.parse_minute_bars({"rt_cd": "0"})


def test_forbidden_window_covers_0900_to_0911():
    assert mb.in_forbidden_window(datetime(2026, 8, 27, 9, 0, 0, tzinfo=KST))
    assert mb.in_forbidden_window(datetime(2026, 8, 27, 9, 10, 59, tzinfo=KST))
    assert not mb.in_forbidden_window(datetime(2026, 8, 27, 9, 11, 0, tzinfo=KST))
    assert not mb.in_forbidden_window(datetime(2026, 8, 27, 8, 59, 59, tzinfo=KST))


async def test_fetch_uses_background_priority_and_stops_on_rate_limit(monkeypatch):
    seen = {}

    async def fake_get(path, **kwargs):
        seen["path"] = path
        seen.update(kwargs)
        return {"rt_cd": "0", "output2": []}

    monkeypatch.setattr(mb.kis_rest, "get", fake_get)

    await mb.fetch_minute_bars("006340")

    assert seen["path"] == mb.MINUTE_PATH
    assert seen["tr_id"] == mb.MINUTE_TR
    assert seen["request_priority"] == mb.kis_rest.REQUEST_PRIORITY_BACKGROUND
    assert seen["stop_on_rate_limit"] is True
    assert seen["params"]["FID_INPUT_ISCD"] == "006340"


async def test_fetch_raises_on_nonzero_rt_cd(monkeypatch):
    async def fake_get(path, **kwargs):
        return {"rt_cd": "1", "msg_cd": "EGW00123"}

    monkeypatch.setattr(mb.kis_rest, "get", fake_get)

    with pytest.raises(mb.MinuteBarError):
        await mb.fetch_minute_bars("006340")


async def test_fetch_day_bars_stops_when_cursor_makes_no_progress(monkeypatch):
    calls = {"n": 0}

    async def fake_get(path, **kwargs):
        calls["n"] += 1
        return {"rt_cd": "0", "output2": [_row("093500", 1, 2, 1, 2, 10)]}

    monkeypatch.setattr(mb.kis_rest, "get", fake_get)

    bars, issues = await mb.fetch_day_bars("006340", max_pages=10)

    assert len(bars) == 1          # 중복 제거
    assert calls["n"] == 2         # 두 번째 페이지에서 새 봉 0 → 중단


async def test_fetch_day_bars_walks_the_cursor_backwards(monkeypatch):
    """가짜 응답이 커서를 실제로 존중하게 두고 다중 페이지를 확인한다.

    위 테스트의 가짜는 커서와 무관하게 같은 행을 돌려주므로, 커서가 페이지마다
    제대로 넘어가는지는 암묵적으로만 검증됐다 (스펙 §17.3). 여기서는 커서보다
    이른 봉만 돌려주게 해서, 커서가 틀리면 봉이 빠지거나 무한히 돈다.
    """
    session = ["093500", "093600", "093700", "093800", "093900"]
    cursors = []

    async def fake_get(path, **kwargs):
        cursor = kwargs["params"]["FID_INPUT_HOUR_1"]
        cursors.append(cursor)
        earlier = [t for t in session if not cursor or t < cursor]
        page = earlier[-2:]        # 한 페이지에 2봉씩, 최신 것부터
        return {"rt_cd": "0",
                "output2": [_row(t, 1, 2, 1, 2, 10) for t in page]}

    monkeypatch.setattr(mb.kis_rest, "get", fake_get)

    bars, _ = await mb.fetch_day_bars("006340", max_pages=10)

    assert [b["time"] for b in bars] == session
    assert cursors == ["", "093800", "093600", "093500"]


async def test_fetch_session_drops_bars_from_another_date(monkeypatch):
    """KIS는 휴장일 요청을 가장 가까운 거래일로 조용히 대체한다.

    커서가 09:00을 넘어가도 전일 봉이 섞인다. 요청 날짜가 아닌 봉을 버리지 않으면
    워밍업이 엉뚱한 날 봉을 먹는다.
    """
    pages = [
        {"rt_cd": "0", "output2": [
            {"stck_bsop_date": "20260828", "stck_cntg_hour": "150000",
             "stck_oprc": "10", "stck_hgpr": "11", "stck_lwpr": "9",
             "stck_prpr": "10", "cntg_vol": "5"},
            {"stck_bsop_date": "20260827", "stck_cntg_hour": "145900",
             "stck_oprc": "20", "stck_hgpr": "21", "stck_lwpr": "19",
             "stck_prpr": "20", "cntg_vol": "5"},
        ]},
        {"rt_cd": "0", "output2": []},
    ]
    calls = []

    async def fake_daily(ticker, trade_date, *, budget, hour_cursor="153000"):
        calls.append((ticker, trade_date, hour_cursor))
        return pages[min(len(calls) - 1, len(pages) - 1)]

    monkeypatch.setattr(mb, "fetch_daily_minute_bars", fake_daily)

    bars = await mb.fetch_session("20260828", "006340")

    assert [b["date"] for b in bars] == ["20260828"]
    assert [b["time"] for b in bars] == ["150000"]


async def test_fetch_session_stops_once_it_reaches_the_open(monkeypatch):
    """09:00에 닿으면 더 밀지 않는다 — 페이지를 낭비하지 않는다."""
    calls = []

    async def fake_daily(ticker, trade_date, *, budget, hour_cursor="153000"):
        calls.append(hour_cursor)
        return {"rt_cd": "0", "output2": [
            {"stck_bsop_date": "20260828", "stck_cntg_hour": "090000",
             "stck_oprc": "10", "stck_hgpr": "11", "stck_lwpr": "9",
             "stck_prpr": "10", "cntg_vol": "5"},
        ]}

    monkeypatch.setattr(mb, "fetch_daily_minute_bars", fake_daily)

    bars = await mb.fetch_session("20260828", "006340")

    assert len(calls) == 1
    assert [b["time"] for b in bars] == ["090000"]


async def test_fetch_session_raises_on_a_failed_response(monkeypatch):
    async def fake_daily(ticker, trade_date, *, budget, hour_cursor="153000"):
        return {"rt_cd": "1", "msg_cd": "EGW00123"}

    monkeypatch.setattr(mb, "fetch_daily_minute_bars", fake_daily)

    with pytest.raises(mb.MinuteBarError):
        await mb.fetch_session("20260828", "006340")
