import json
import types
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import src.api.server as server
from src import bars
from src.api.server import _bar_gaps, app
from src.modules import tick_capture

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_bars(tmp_path, monkeypatch):
    monkeypatch.setattr(bars, "_BARS_DIR", tmp_path)
    tick_capture.clear_tick_listeners()
    bars.reset()
    server._PRICE_SEED_FILE_CACHE.clear()
    server.live.clear_tick_history()
    yield
    bars.reset()
    server._PRICE_SEED_FILE_CACHE.clear()
    server.live.clear_tick_history()
    tick_capture.clear_tick_listeners()


def _row(minute, close, *, confirmed=True, derived=True):
    return {
        "date": "20260827", "time": f"09{minute:02d}00",
        "open": close - 5, "high": close + 10, "low": close - 10, "close": close,
        "volume": 100.0, "confirmed": confirmed,
        "tick_count": 12, "spike_dropped": 1 if minute == 0 else 0,
        "tick_derived": ({"cttr": 120.0, "askp1": None, "bidp1": None,
                          "total_askp_rsqn": None, "total_bidp_rsqn": None,
                          "vol_by_ccld": {}, "corrected": False} if derived else None),
    }


def test_bars_endpoint_returns_bars_indicators_and_meta(tmp_path):
    rows = [_row(m, 14500 + m * 10) for m in range(30)]
    (tmp_path / "20260827_006340.json").write_text(json.dumps(rows), encoding="utf-8")

    res = client.get("/api/bars", params={"date": "20260827", "ticker": "006340"})
    body = res.json()

    assert res.status_code == 200
    assert body["ticker"] == "006340"
    assert body["track"] == "B"
    assert len(body["bars"]) == 30
    assert len(body["indicators"]["sma"]) == 30
    assert len(body["indicators"]["macd"]) == 30
    assert body["meta"]["bar_count"] == 30
    assert body["meta"]["source"] == "file"


def test_indicator_arrays_align_with_the_bar_array():
    for m in range(30):
        series = bars._series.setdefault(("20260827", "006340"), {})
        series[f"09{m:02d}00"] = _row(m, 14500 + m * 10)

    body = client.get("/api/bars", params={"date": "20260827", "ticker": "006340"}).json()

    assert body["indicators"]["sma"][:19] == [None] * 19
    assert body["indicators"]["sma"][19] is not None
    assert body["meta"]["source"] == "memory"


def test_meta_counts_unconfirmed_bars_and_missing_tick_derived():
    minutes = bars._series.setdefault(("20260827", "006340"), {})
    minutes["090000"] = _row(0, 14500, confirmed=True)
    minutes["090100"] = _row(1, 14510, confirmed=False)
    minutes["090200"] = _row(2, 14520, confirmed=True, derived=False)

    body = client.get("/api/bars", params={"date": "20260827", "ticker": "006340"}).json()

    assert body["meta"]["bar_count"] == 3
    assert body["meta"]["confirmed_count"] == 2
    assert body["meta"]["tick_derived_missing"] == 1
    assert body["meta"]["spike_dropped"] == 1


def test_unknown_series_returns_empty_arrays_not_an_error():
    res = client.get("/api/bars", params={"date": "20991231", "ticker": "000000"})
    body = res.json()

    assert res.status_code == 200
    assert body["bars"] == []
    assert body["indicators"]["sma"] == []
    assert body["meta"]["bar_count"] == 0


def test_indicator_periods_are_configurable():
    for m in range(10):
        series = bars._series.setdefault(("20260827", "006340"), {})
        series[f"09{m:02d}00"] = _row(m, 14500 + m * 10)

    body = client.get(
        "/api/bars",
        params={
            "date": "20260827", "ticker": "006340",
            "sma": 3, "fast": 2, "slow": 4, "signal": 2,
        },
    ).json()

    assert body["indicators"]["sma"][2] is not None
    assert body["indicators"]["macd"][3]["macd"] is not None


def test_ticker_path_traversal_is_rejected_and_contained(tmp_path):
    # tmp_path here is the same instance the autouse isolated_bars fixture patched
    # bars._BARS_DIR to. Plant a marker file OUTSIDE that directory and try to
    # reach it with a ticker containing ".." segments.
    outside_marker = tmp_path.parent / "marker_outside.json"
    outside_marker.write_text(json.dumps([{"secret": "outside-data"}]), encoding="utf-8")

    res = client.get(
        "/api/bars",
        params={"date": "20260827", "ticker": "x/../../marker_outside"},
    )
    body = res.json()

    assert res.status_code == 200
    assert body["bars"] == []
    assert body["meta"]["source"] == "invalid"
    assert "outside-data" not in res.text


def test_malformed_bars_file_is_reported_as_empty_not_memory(tmp_path):
    (tmp_path / "20260827_006340.json").write_text("{not valid json", encoding="utf-8")

    res = client.get("/api/bars", params={"date": "20260827", "ticker": "006340"})
    body = res.json()

    assert res.status_code == 200
    assert body["bars"] == []
    assert body["indicators"]["sma"] == []
    assert body["meta"]["source"] == "empty"


def test_bad_date_format_is_rejected_without_an_error():
    res = client.get("/api/bars", params={"date": "2026-08-27", "ticker": "006340"})
    body = res.json()

    assert res.status_code == 200
    assert body["bars"] == []
    assert body["indicators"]["sma"] == []
    assert body["meta"]["source"] == "invalid"


@pytest.mark.parametrize(
    "period_param",
    ["sma", "fast", "slow", "signal"],
)
def test_a_non_positive_indicator_period_is_rejected_without_an_error(period_param):
    # indicators.sma/ema는 period<=0에서 ValueError를 올린다. UI가 30초마다
    # 폴링하므로 500이 나가면 안 된다.
    for m in range(30):
        bars._series.setdefault(("20260827", "006340"), {})[f"09{m:02d}00"] = _row(
            m, 14500 + m * 10
        )

    res = client.get(
        "/api/bars",
        params={"date": "20260827", "ticker": "006340", period_param: 0},
    )
    body = res.json()

    assert res.status_code == 200
    assert body["bars"] == []
    assert body["indicators"]["sma"] == []
    assert body["indicators"]["macd"] == []
    assert body["meta"]["source"] == "invalid"


@pytest.mark.parametrize("period_param", ["sma", "fast", "slow", "signal"])
def test_a_negative_or_absurd_indicator_period_is_rejected(period_param):
    for m in range(30):
        bars._series.setdefault(("20260827", "006340"), {})[f"09{m:02d}00"] = _row(
            m, 14500 + m * 10
        )

    for value in (-1, 10_000_000):
        res = client.get(
            "/api/bars",
            params={"date": "20260827", "ticker": "006340", period_param: value},
        )

        assert res.status_code == 200
        assert res.json()["meta"]["source"] == "invalid"


def test_the_server_lifespan_installs_and_starts_the_bar_collector():
    """관측 계층의 유일한 프로덕션 기동 지점이다 (FINDING 1)."""
    assert bars.on_tick not in tick_capture._tick_listeners

    with TestClient(app):
        assert bars.on_tick in tick_capture._tick_listeners
        assert bars._supervisor is not None
        assert not bars._supervisor.done()

    assert bars._supervisor is None
def test_a_continuous_series_reports_no_gaps(tmp_path):
    rows = [_row(m, 14500 + m * 10) for m in range(30)]
    (tmp_path / "20260827_006340.json").write_text(json.dumps(rows), encoding="utf-8")

    res = client.get("/api/bars", params={"date": "20260827", "ticker": "006340"})

    assert res.json()["meta"]["gaps"] == []


def test_a_missing_minute_is_reported_as_a_gap(tmp_path):
    """2026-08-28 043200이 VI로 145초 멈췄을 때의 실제 계열."""
    rows = [
        {"date": "20260828", "time": "090100", "open": 1422.0, "high": 1528.0,
         "low": 1416.0, "close": 1516.0, "volume": 294444.0, "confirmed": False,
         "tick_count": 1417, "spike_dropped": 0, "tick_derived": None},
        {"date": "20260828", "time": "090200", "open": 1514.0, "high": 1577.0,
         "low": 1491.0, "close": 1577.0, "volume": 321988.0, "confirmed": False,
         "tick_count": 1670, "spike_dropped": 0, "tick_derived": None},
        {"date": "20260828", "time": "090500", "open": 1535.0, "high": 1580.0,
         "low": 1505.0, "close": 1580.0, "volume": 353643.0, "confirmed": False,
         "tick_count": 1772, "spike_dropped": 0, "tick_derived": None},
    ]
    (tmp_path / "20260828_043200.json").write_text(json.dumps(rows), encoding="utf-8")

    res = client.get("/api/bars", params={"date": "20260828", "ticker": "043200"})
    gaps = res.json()["meta"]["gaps"]

    assert gaps == [{
        "after": "090200", "resume": "090500",
        "index": 2, "missing": 2, "jump_pct": -2.66,
    }]


def test_the_gap_index_points_at_the_bar_that_resumes(tmp_path):
    """차트가 이 인덱스로 두 캔들 사이에 경계를 긋는다. 어긋나면 엉뚱한 곳에 선이 간다."""
    rows = [_row(m, 14500) for m in (0, 1, 2, 7, 8)]
    (tmp_path / "20260827_006340.json").write_text(json.dumps(rows), encoding="utf-8")

    body = client.get("/api/bars", params={"date": "20260827", "ticker": "006340"}).json()
    gap = body["meta"]["gaps"][0]

    assert gap["missing"] == 4
    assert body["bars"][gap["index"]]["time"] == gap["resume"] == "090700"
    assert body["bars"][gap["index"] - 1]["time"] == gap["after"] == "090200"


def test_several_gaps_are_all_reported_in_order(tmp_path):
    rows = [_row(m, 14500) for m in (0, 1, 5, 6, 20)]
    (tmp_path / "20260827_006340.json").write_text(json.dumps(rows), encoding="utf-8")

    gaps = client.get(
        "/api/bars", params={"date": "20260827", "ticker": "006340"}
    ).json()["meta"]["gaps"]

    assert [(g["after"], g["resume"], g["missing"]) for g in gaps] == [
        ("090100", "090500", 3),
        ("090600", "092000", 13),
    ]


def test_a_bar_with_an_unreadable_time_does_not_invent_a_gap(tmp_path):
    """시각을 못 읽는 봉은 건너뛴다 — 추정해서 없던 갭을 만들지 않는다."""
    rows = [_row(0, 14500), _row(1, 14500), _row(2, 14500)]
    rows[1]["time"] = "??????"
    (tmp_path / "20260827_006340.json").write_text(json.dumps(rows), encoding="utf-8")

    gaps = client.get(
        "/api/bars", params={"date": "20260827", "ticker": "006340"}
    ).json()["meta"]["gaps"]

    assert gaps == []


def test_a_gap_without_usable_prices_still_reports_the_missing_minutes():
    """가격을 못 읽어도 빠진 분은 센다 — jump_pct만 포기한다.

    엔드포인트가 아니라 헬퍼를 직접 부른다. close가 None인 봉 파일은
    indicators가 먼저 걸려 갭 계산에 닿지도 않기 때문이다.
    """
    gaps = _bar_gaps([
        {"time": "090000", "open": 100.0, "close": None},
        {"time": "090500", "open": 110.0, "close": 110.0},
    ])

    assert gaps[0]["missing"] == 4
    assert gaps[0]["jump_pct"] is None


def test_api_bars_reports_warmup_state_when_no_previous_day_exists(tmp_path):
    """전일 파일이 없으면 warmed=False로 정직하게 남긴다."""
    (tmp_path / "20260901_005930.json").write_text(json.dumps([
        {"time": "090000", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
    ]), encoding="utf-8")

    body = client.get("/api/bars?date=20260901&ticker=005930").json()

    assert body["warmup"] == {"warmup_days": 0, "warmup_bars": 0, "warmed": False}
    assert len(body["indicators"]["sma"]) == len(body["bars"])


def test_api_bars_indicator_arrays_stay_aligned_with_the_day(tmp_path):
    """워밍업이 있어도 지표 배열은 당일 봉 길이로 잘려 나오고, 캔들은 당일 것뿐이다."""
    prev = [{"time": f"09{m:02d}00", "open": 10, "high": 10, "low": 10,
             "close": 10.0 + m, "volume": 5} for m in range(60)]
    today = [{"time": f"09{m:02d}00", "open": 20, "high": 20, "low": 20,
              "close": 20.0 + m, "volume": 5} for m in range(3)]
    (tmp_path / "20260831_005930.json").write_text(json.dumps(prev), encoding="utf-8")
    (tmp_path / "20260901_005930.json").write_text(json.dumps(today), encoding="utf-8")

    body = client.get(
        "/api/bars?date=20260901&ticker=005930&prev=20260831"
    ).json()

    assert body["warmup"]["warmup_bars"] == 60
    assert body["warmup"]["warmed"] is False      # 60 < WARMUP_MIN_BARS
    assert len(body["bars"]) == 3
    assert len(body["indicators"]["sma"]) == 3
    assert len(body["indicators"]["macd"]) == 3
    assert len(body["indicators"]["ma"]["5"]) == 3
    assert len(body["indicators"]["vol_ma"]["5"]) == 3


def test_api_bars_degrades_to_unwarmed_when_prev_file_is_missing_or_malformed(tmp_path):
    """prev가 가리키는 파일이 없거나 깨져 있어도 에러 없이 미워밍업으로 남는다."""
    today = [{"time": "090000", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
    (tmp_path / "20260901_005930.json").write_text(json.dumps(today), encoding="utf-8")
    (tmp_path / "20260830_005930.json").write_text("{not valid json", encoding="utf-8")

    missing = client.get(
        "/api/bars?date=20260901&ticker=005930&prev=20260828"
    )
    malformed = client.get(
        "/api/bars?date=20260901&ticker=005930&prev=20260830"
    )

    for res in (missing, malformed):
        assert res.status_code == 200
        body = res.json()
        assert body["warmup"] == {"warmup_days": 0, "warmup_bars": 0, "warmed": False}
        assert len(body["bars"]) == 1
        assert len(body["indicators"]["sma"]) == 1


# ── /api/status 가격흐름 앞 구간 보완(minute_price_seed) ─────────────────────
# 2026-11-16: 13:28에 '거래 전체'를 골라도 오전 가격선이 없었다. 메모리 분 이력은 최근
# 180분(10:28~)뿐이었고 화면은 저장된 분봉(09:00~)을 읽지 않았다.
_SEED_DATE = "20260827"
_SEED_TICKER = "010170"
_SEED_OPEN = datetime(2026, 8, 27, 9, 0, tzinfo=server.KST)


def _seed_bars(count: int = 390) -> list[dict]:
    return [
        {
            "date": _SEED_DATE,
            "time": (_SEED_OPEN + timedelta(minutes=i)).strftime("%H%M%S"),
            "open": 13_000.0, "high": 13_500.0, "low": 12_900.0, "close": 13_000.0 + i,
            "volume": 10.0, "confirmed": True, "tick_count": 3, "spike_dropped": 0,
            "tick_derived": None,
        }
        for i in range(count)
    ]


def _write_seed_file(tmp_path, rows, ticker: str = _SEED_TICKER):
    path = tmp_path / f"{_SEED_DATE}_{ticker}.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def _fill_memory_minutes(first_minute: int, count: int, ticker: str = _SEED_TICKER) -> None:
    """live의 분 이력을 개장 뒤 first_minute분부터 count분 채운다(가격은 봉과 다르게 둔다)."""
    for i in range(first_minute, first_minute + count):
        server.live._minute_history.append({
            "ts": (_SEED_OPEN + timedelta(minutes=i)).isoformat(),
            "ticker": ticker, "price": 20_000.0 + i, "tick_count": 5,
        })


def _closed_position(monkeypatch, *, status: str = "CLOSED", entry_at: str | None = None):
    s = server.state.get()
    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: [])
    monkeypatch.setattr(s, "position_status", status)
    monkeypatch.setattr(s, "target_ticker", _SEED_TICKER)
    monkeypatch.setattr(s, "observe_ticker", None)
    monkeypatch.setattr(s, "entry_price", 13_000.0)
    monkeypatch.setattr(
        s, "entry_at", entry_at if entry_at is not None else "2026-08-27T09:00:28.498553+09:00"
    )
    return s


async def _status() -> dict:
    return json.loads(bytes((await server.api_status()).body).decode("utf-8"))


async def test_status_seeds_the_morning_that_fell_out_of_the_minute_buffer(tmp_path, monkeypatch):
    _write_seed_file(tmp_path, _seed_bars())
    _closed_position(monkeypatch)
    _fill_memory_minutes(88, 180)  # 10:28~13:27 — 180분 한도를 채운 상태

    payload = await _status()
    seed = payload["minute_price_seed"]
    meta = payload["minute_price_seed_meta"]

    assert len(payload["minute_price_history"]) == 180
    assert payload["minute_price_history"][0]["ts"] == "2026-08-27T10:28:00+09:00"
    assert seed[0] == {"ts": "2026-08-27T09:00:00+09:00", "price": 13_000.0}
    assert seed[-1] == {"ts": "2026-08-27T10:27:00+09:00", "price": 13_087.0}
    assert len(seed) == 88
    # 메모리가 가진 분은 시드에 넣지 않는다 — 겹치는 분은 메모리가 우선이다.
    memory_minutes = {row["ts"] for row in payload["minute_price_history"]}
    assert not memory_minutes & {row["ts"] for row in seed}
    assert meta["source"] == "file" and meta["date"] == _SEED_DATE
    assert meta["count"] == 88 and meta["missing_minutes"] == 0 and meta["stale"] is False


async def test_status_seed_restores_the_whole_day_after_a_restart_emptied_memory(
    tmp_path, monkeypatch,
):
    _write_seed_file(tmp_path, _seed_bars())
    _closed_position(monkeypatch)

    payload = await _status()

    assert payload["minute_price_history"] == []
    assert len(payload["minute_price_seed"]) == 390
    assert payload["minute_price_seed"][-1]["ts"] == "2026-08-27T15:29:00+09:00"


async def test_status_seed_starts_at_the_entry_minute(tmp_path, monkeypatch):
    _write_seed_file(tmp_path, _seed_bars())
    _closed_position(monkeypatch, entry_at="2026-08-27T09:41:10+09:00")
    _fill_memory_minutes(88, 30)

    payload = await _status()

    assert payload["minute_price_seed"][0]["ts"] == "2026-08-27T09:41:00+09:00"
    assert len(payload["minute_price_seed"]) == 47  # 09:41~10:27


async def test_status_seed_is_not_read_when_memory_still_covers_the_entry(
    tmp_path, monkeypatch,
):
    _write_seed_file(tmp_path, _seed_bars())
    _closed_position(monkeypatch, status="HOLDING")
    _fill_memory_minutes(0, 40)
    reads: list[tuple[str, str]] = []
    monkeypatch.setattr(
        server, "_stored_bar_rows",
        lambda date, ticker: reads.append((date, ticker)) or ([], "none", False),
    )

    payload = await _status()

    assert reads == []
    assert payload["minute_price_seed"] == []
    assert payload["minute_price_seed_meta"]["source"] == "skipped"


async def test_status_seed_prefers_the_live_series_over_the_file(tmp_path, monkeypatch):
    _write_seed_file(tmp_path, _seed_bars(5))
    series = bars._series.setdefault((_SEED_DATE, _SEED_TICKER), {})
    for row in _seed_bars(60):
        series[row["time"]] = row
    _closed_position(monkeypatch, status="HOLDING")
    _fill_memory_minutes(50, 10)

    payload = await _status()

    assert payload["minute_price_seed_meta"]["source"] == "memory"
    assert len(payload["minute_price_seed"]) == 50  # 09:00~09:49


async def test_status_seed_file_is_parsed_once_until_it_changes(tmp_path, monkeypatch):
    path = _write_seed_file(tmp_path, _seed_bars(100))
    _closed_position(monkeypatch)
    _fill_memory_minutes(90, 10)
    parses: list[int] = []

    def counting_loads(text):
        parses.append(len(text))
        return json.loads(text)

    monkeypatch.setattr(
        server, "json", types.SimpleNamespace(loads=counting_loads, dumps=json.dumps)
    )

    for _ in range(3):
        assert len((await _status())["minute_price_seed"]) == 90
    assert len(parses) == 1

    path.write_text(json.dumps(_seed_bars(100)[:80]), encoding="utf-8")
    assert len((await _status())["minute_price_seed"]) == 80
    assert len(parses) == 2


async def test_status_seed_survives_a_half_written_or_missing_file(tmp_path, monkeypatch):
    path = _write_seed_file(tmp_path, _seed_bars(100))
    _closed_position(monkeypatch)
    _fill_memory_minutes(90, 10)
    assert (await _status())["minute_price_seed_meta"]["stale"] is False

    # 쓰는 도중 잘린 파일 — 지난 판독을 그대로 쓰고 stale로 알린다.
    path.write_text(json.dumps(_seed_bars(100))[:500], encoding="utf-8")
    payload = await _status()
    assert len(payload["minute_price_seed"]) == 90
    assert payload["minute_price_seed_meta"]["source"] == "file"
    assert payload["minute_price_seed_meta"]["stale"] is True

    # 지웠다 다시 쓰는 교체 도중 — 파일이 잠깐 없어도 같다.
    path.unlink()
    payload = await _status()
    assert len(payload["minute_price_seed"]) == 90
    assert payload["minute_price_seed_meta"]["stale"] is True


async def test_status_seed_reports_an_unreadable_file_without_inventing_prices(
    tmp_path, monkeypatch,
):
    (tmp_path / f"{_SEED_DATE}_{_SEED_TICKER}.json").write_text("[{not json", encoding="utf-8")
    _closed_position(monkeypatch)
    _fill_memory_minutes(90, 10)

    payload = await _status()

    assert payload["minute_price_seed"] == []
    assert payload["minute_price_seed_meta"]["source"] == "error"
    assert len(payload["minute_price_history"]) == 10


async def test_status_seed_reports_a_missing_file_and_bad_prices(tmp_path, monkeypatch):
    _closed_position(monkeypatch)
    _fill_memory_minutes(90, 10)
    payload = await _status()
    assert payload["minute_price_seed"] == []
    assert payload["minute_price_seed_meta"]["source"] == "none"

    rows = _seed_bars(10)
    rows[3]["close"] = 0
    rows[4]["close"] = "13004"
    rows[5]["close"] = None
    _write_seed_file(tmp_path, rows)
    payload = await _status()
    assert [row["ts"][11:16] for row in payload["minute_price_seed"]] == [
        "09:00", "09:01", "09:02", "09:06", "09:07", "09:08", "09:09",
    ]
    assert payload["minute_price_seed_meta"]["invalid"] == 3
    # 09:03~09:05(3분) + 09:10~10:29(메모리 첫 분 10:30 전까지 80분)
    assert payload["minute_price_seed_meta"]["missing_minutes"] == 83


async def test_status_seed_follows_a_ticker_switch(tmp_path, monkeypatch):
    _write_seed_file(tmp_path, _seed_bars(30))
    other = _seed_bars(30)
    for row in other:
        row["close"] += 50_000.0
    _write_seed_file(tmp_path, other, ticker="065450")
    s = _closed_position(monkeypatch)

    first = await _status()
    assert first["minute_price_seed"][0]["price"] == 13_000.0

    monkeypatch.setattr(s, "target_ticker", "065450")
    second = await _status()
    assert second["minute_price_seed"][0]["price"] == 63_000.0
    assert server._PRICE_SEED_FILE_CACHE["key"] == (_SEED_DATE, "065450")


async def test_status_seed_for_an_observed_ticker_covers_that_day_only(tmp_path, monkeypatch):
    _write_seed_file(tmp_path, _seed_bars(120), ticker="065450")
    s = server.state.get()
    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: [])
    monkeypatch.setattr(server, "_read_f1_snapshot", lambda: (None, []))
    monkeypatch.setattr(server, "_today_start", lambda: _SEED_OPEN.replace(hour=0, minute=0))
    monkeypatch.setattr(s, "position_status", "IDLE")
    monkeypatch.setattr(s, "target_ticker", None)
    monkeypatch.setattr(s, "observe_ticker", "065450")
    monkeypatch.setattr(s, "entry_at", None)
    _fill_memory_minutes(100, 20, ticker="065450")

    payload = await _status()

    assert payload["ticker"] is None and payload["observe_ticker"] == "065450"
    assert len(payload["minute_price_seed"]) == 100  # 09:00~10:39
    assert payload["minute_price_seed_meta"]["date"] == _SEED_DATE


async def test_status_seed_is_skipped_without_a_position_or_an_entry_time(
    tmp_path, monkeypatch,
):
    _write_seed_file(tmp_path, _seed_bars(30))
    s = _closed_position(monkeypatch)
    monkeypatch.setattr(s, "entry_at", None)
    payload = await _status()
    # 진입 시각을 모르면 진입 전 봉을 걸러낼 수 없다.
    assert payload["minute_price_seed"] == []
    assert payload["minute_price_seed_meta"]["source"] == "skipped"

    monkeypatch.setattr(s, "position_status", "IDLE")
    monkeypatch.setattr(s, "target_ticker", None)
    payload = await _status()
    assert payload["minute_price_seed"] == []
    assert payload["minute_price_seed_meta"]["source"] == "skipped"


async def test_status_seed_failure_never_breaks_the_status_response(tmp_path, monkeypatch):
    _closed_position(monkeypatch)

    def boom(date, ticker):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(server, "_stored_bar_rows", boom)

    payload = await _status()

    assert payload["position_status"] == "CLOSED"
    assert payload["minute_price_seed"] == []
    assert payload["minute_price_seed_meta"]["source"] == "error"
