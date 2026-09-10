"""전 세션 분봉 백필 검증.

백필이 봉을 중복시키거나 커서를 헛돌면 표본이 조용히 망가진다. 그리고 이
스크립트는 KIS를 1,300번 때리므로 시간 가드가 틀리면 장중에 A의 유량을 먹는다.
"""

import json
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from scripts import track_b_backfill
from scripts.fast_path_counterfactual import PocStop, Throttle
from scripts.track_b_backfill import (
    assert_backfill_window,
    backfill,
    fetch_session_bars,
    is_session_complete,
    merge_bars,
    needed_pairs,
    next_cursor,
)
from src.api import kis_rest

KST = ZoneInfo("Asia/Seoul")


def _bar(time_: str, close: float = 100.0) -> dict:
    return {
        "date": "20260820",
        "time": time_,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 10.0,
    }


def test_merge_dedupes_and_sorts():
    existing = [_bar("091000"), _bar("090000")]
    fetched = [_bar("090000", close=999.0), _bar("085900")]
    merged = merge_bars(existing, fetched)
    assert [b["time"] for b in merged] == ["085900", "090000", "091000"]
    # 기존 값을 유지한다. 같은 봉을 다시 받아도 캐시가 흔들리면 안 된다.
    assert merged[1]["close"] == 100.0


def test_next_cursor_is_earliest_bar():
    assert next_cursor([_bar("091000"), _bar("090500")]) == "090500"


def test_next_cursor_none_when_empty():
    assert next_cursor([]) is None


def test_window_rejects_before_1540():
    with pytest.raises(PocStop) as exc:
        assert_backfill_window(datetime(2026, 8, 28, 15, 39, 59, tzinfo=KST))
    assert exc.value.reason == "AFTER_1540_ONLY"


def test_window_allows_after_1540():
    assert_backfill_window(datetime(2026, 8, 28, 15, 40, tzinfo=KST)) is None


def _response(times: list[str]) -> dict:
    return {
        "rt_cd": "0",
        "output2": [
            {
                "stck_bsop_date": "20260820",
                "stck_cntg_hour": t,
                "stck_oprc": "100",
                "stck_hgpr": "101",
                "stck_lwpr": "99",
                "stck_prpr": "100",
                "cntg_vol": "10",
            }
            for t in times
        ],
    }


async def test_fetch_session_pages_backwards_until_session_start():
    pages = [
        _response(["093000", "092900"]),
        _response(["092800", "090000"]),
    ]
    calls: list[str] = []

    async def fake_fetch(ticker, trade_date, *, budget, hour_cursor="093000"):
        calls.append(hour_cursor)
        return pages[len(calls) - 1]

    with patch("scripts.track_b_backfill.fetch_daily_minute_bars", fake_fetch):
        bars = await fetch_session_bars(
            "005930", "20260820",
            budget=kis_rest.CallBudget(10),
            throttle=Throttle(0.0),
        )

    # 첫 커서는 장 마감, 그다음은 직전 페이지의 가장 이른 봉이다.
    assert calls == ["153000", "092900"]
    # 09:00에 닿으면 멈춘다 — 더 밀면 전일 봉이 섞인다.
    assert [b["time"] for b in bars] == ["090000", "092800", "092900", "093000"]


async def test_fetch_session_drops_other_dates():
    page = _response(["090000"])
    page["output2"].append({
        "stck_bsop_date": "20260819",
        "stck_cntg_hour": "151900",
        "stck_oprc": "1", "stck_hgpr": "1", "stck_lwpr": "1",
        "stck_prpr": "1", "cntg_vol": "1",
    })

    async def fake_fetch(ticker, trade_date, *, budget, hour_cursor="093000"):
        return page

    with patch("scripts.track_b_backfill.fetch_daily_minute_bars", fake_fetch):
        bars = await fetch_session_bars(
            "005930", "20260820",
            budget=kis_rest.CallBudget(10),
            throttle=Throttle(0.0),
        )

    assert {b["date"] for b in bars} == {"20260820"}


async def test_fetch_session_stops_when_cursor_stalls():
    """같은 페이지가 계속 오면 멈춘다. 안 멈추면 예산을 다 태운다."""
    calls = {"n": 0}

    async def fake_fetch(ticker, trade_date, *, budget, hour_cursor="093000"):
        calls["n"] += 1
        return _response(["093000"])

    with patch("scripts.track_b_backfill.fetch_daily_minute_bars", fake_fetch):
        bars = await fetch_session_bars(
            "005930", "20260820",
            budget=kis_rest.CallBudget(10),
            throttle=Throttle(0.0),
        )

    assert calls["n"] == 2
    assert [b["time"] for b in bars] == ["093000"]


def test_needed_pairs_uses_operational_ranking(tmp_path):
    # f1_selector 의 바닥 조건: gap_pct 는 [0.025, 0.100), expected_amount 는
    # 1억 이상. expected_amount 는 expected_price×volume 이 아니라 그 이름의
    # 필드(없으면 avg_amount_5d)를 읽는다.
    rows = [
        {"ticker": "000001", "gap_pct": 0.05, "prev_close": 950,
         "expected_amount": 5_000_000_000, "avg_amount_5d": 1_000_000_000},
        {"ticker": "000002", "gap_pct": 0.04, "prev_close": 960,
         "expected_amount": 3_000_000_000, "avg_amount_5d": 1_000_000_000},
    ]
    # load_universes 는 MIN_UNIVERSE_ROWS(30) 미만을 버린다. 같은 행을 늘려 채운다.
    padded = []
    for i in range(30):
        row = dict(rows[i % 2])
        row["ticker"] = f"{i:06d}"
        padded.append(row)
    path = tmp_path / "20260820_090100.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in padded), encoding="utf-8"
    )

    needed = needed_pairs(depth=5, snapshot_dir=tmp_path)
    assert set(needed) == {"20260820"}
    assert len(needed["20260820"]) <= 5


def _session(n=381):
    """실제 세션 모양 — 09:00부터 1분 간격에 단일가 종가 15:30 한 봉."""
    rows = [{"time": f"{9 + i // 60:02d}{i % 60:02d}00"} for i in range(n - 1)]
    rows.append({"time": "153000"})
    return rows


def test_session_complete_skips_full_days_only():
    assert is_session_complete(_session()) is True
    assert is_session_complete(_session()[:31]) is False
    assert is_session_complete(None) is False


def test_session_complete_accepts_a_thin_ticker_whole_day():
    """거래가 뜸해 265봉뿐인 완전한 하루를 매번 다시 받으면 예산만 태운다.

    아침에 몰린 265봉이 아니라 하루에 고르게 흩어진 265봉이어야 실제 모양이다.
    """
    full = _session()
    step = (len(full) - 1) / 264
    thin = [full[round(i * step)] for i in range(265)]

    assert len(thin) == 265
    assert thin[0]["time"] == "090000" and thin[-1]["time"] == "153000"
    assert is_session_complete(thin) is True


async def test_backfill_stops_on_budget_exhaustion(tmp_path):
    """예산이 다 떨어지면 남은 쌍을 실패로 세지 않고 즉시 멈춘다.

    CallBudget.charge()가 던지는 RequestBudgetExceeded는 RuntimeError라
    무심코 짠 ``except Exception``에도 걸린다. 그러면 예산이 없어도 루프가
    안 멈추고 남은 쌍을 전부 순회하며 failed로 센다 — 예산 컷오프와 진짜
    실패가 통계에서 구분이 안 된다.
    """
    needed = {"20260820": {"000001", "000002", "000003"}}
    calls: list[str] = []

    async def fake_fetch(ticker, trade_date, *, budget, hour_cursor="093000"):
        budget.charge()
        calls.append(ticker)
        return _response(["090000"])

    with patch("scripts.track_b_backfill.fetch_daily_minute_bars", fake_fetch):
        stats = await backfill(
            needed,
            cache_dir=tmp_path,
            budget=kis_rest.CallBudget(1),
            throttle=Throttle(0.0),
        )

    assert stats["budget_exhausted"] is True
    assert stats["failed"] == 0
    # 예산이 1콜뿐이라 첫 쌍만 시도하고 멈춘다.
    assert len(calls) == 1


def _snapshot(tmp_path, date, tickers):
    """스냅샷 한 장을 만든다.

    load_universes 는 MIN_UNIVERSE_ROWS(30) 미만인 스냅샷을 통째로 버린다
    (scripts/strategy_backtest.py:70). 기존 테스트가 쓰는 패딩 방식을 그대로
    따라 30행을 채운다.
    """
    rows = [{
        "ticker": t, "gap_pct": 0.05, "prev_close": 1000,
        "expected_amount": 5_000_000_000, "avg_amount_5d": 1_000_000_000,
    } for t in tickers]
    while len(rows) < 30:
        filler = dict(rows[0])
        filler["ticker"] = f"9{len(rows):05d}"
        rows.append(filler)
    (tmp_path / f"{date}_090100.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
        encoding="utf-8",
    )


def test_needed_pairs_adds_the_previous_session_for_warmup(tmp_path):
    """워밍업 1일이면 각 날짜의 종목이 전 거래일 쌍에도 들어간다.

    두 날의 스냅샷을 같게 만들어, 랭킹 내부 구현에 기대지 않고 집합 관계만
    본다 — 어느 종목이 랭크 1인지는 이 테스트의 관심사가 아니다.
    """
    for date in ("20260814", "20260818"):
        _snapshot(tmp_path, date, ["005930", "000660"])

    cold = track_b_backfill.needed_pairs(depth=5, snapshot_dir=tmp_path,
                                         warmup_days=0)
    hot = track_b_backfill.needed_pairs(depth=5, snapshot_dir=tmp_path,
                                        warmup_days=1)

    assert set(cold) == {"20260814", "20260818"}
    # 20260818의 워밍업은 20260814다. 그날 쌍이 08-18의 종목을 흡수한다.
    assert hot["20260814"] >= cold["20260818"]
    assert hot["20260818"] == cold["20260818"]


def test_needed_pairs_warmup_does_not_invent_days_outside_the_universe(tmp_path):
    """유니버스의 첫 날은 그 앞이 없다 — 없는 날짜를 만들어내면 안 된다."""
    _snapshot(tmp_path, "20260814", ["005930"])

    pairs = track_b_backfill.needed_pairs(depth=5, snapshot_dir=tmp_path,
                                          warmup_days=1)

    assert set(pairs) == {"20260814"}


def test_needed_pairs_reads_a_restored_universe_file(tmp_path):
    """복원된 유니버스에는 순위를 다시 매길 속성이 없다. 순서를 그대로 쓴다."""
    path = tmp_path / "universes.json"
    path.write_text(
        json.dumps({
            "days": {
                "20260910": [
                    {"rank": 1, "ticker": "111111"},
                    {"rank": 2, "ticker": "222222"},
                    {"rank": 3, "ticker": "333333"},
                ]
            }
        }),
        encoding="utf-8",
    )

    needed = needed_pairs(depth=2, universes_path=path)

    assert needed == {"20260910": {"111111", "222222"}}


def test_needed_pairs_restored_universe_still_adds_warmup_days(tmp_path):
    """워밍업은 입력 출처와 무관하게 적용된다."""
    path = tmp_path / "universes.json"
    path.write_text(
        json.dumps({
            "days": {
                "20260909": [{"rank": 1, "ticker": "999999"}],
                "20260910": [{"rank": 1, "ticker": "111111"}],
            }
        }),
        encoding="utf-8",
    )

    needed = needed_pairs(depth=5, universes_path=path, warmup_days=1)

    assert needed["20260910"] == {"111111"}
    assert needed["20260909"] == {"999999", "111111"}
