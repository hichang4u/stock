"""KRX 수집기 — HTTP는 가짜(httpx.MockTransport)만 쓴다. 실제 KRX를 부르지 않는다."""

import json

import httpx
import pytest

from scripts.krx_daily_fetch import (
    AuthError,
    TransientError,
    cache_path,
    fetch_one,
    load_key,
    main,
    month_first_trading_days,
    read_rows,
    run,
    save_atomic,
    weekdays,
)

KEY = "k" * 40


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _no_sleep(_seconds: float) -> None:
    return None


def test_weekdays_skips_weekends():
    # 2019-10-04 금, 10-05 토, 10-06 일, 10-07 월
    assert weekdays("20191004", "20191007") == ["20191004", "20191007"]


def test_cache_path_layout(tmp_path):
    assert cache_path(tmp_path, "stk", "20170223") == tmp_path / "stk" / "20170223.json.gz"
    assert cache_path(tmp_path, "base/ksq", "20170201") == (
        tmp_path / "base" / "ksq" / "20170201.json.gz"
    )


def test_save_atomic_round_trips_and_leaves_no_tmp(tmp_path):
    path = cache_path(tmp_path, "stk", "20170223")
    save_atomic(path, [{"ISU_CD": "117930", "ISU_NM": "한진해운"}])
    assert read_rows(path) == [{"ISU_CD": "117930", "ISU_NM": "한진해운"}]
    assert [p.name for p in path.parent.iterdir()] == ["20170223.json.gz"]


def test_fetch_one_sends_date_and_key_and_returns_rows():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["basDd"] = request.url.params.get("basDd")
        seen["key"] = request.headers.get("AUTH_KEY")
        return httpx.Response(200, json={"OutBlock_1": [{"ISU_CD": "117930"}]})

    rows = fetch_one(_client(handler), "stk", "20170223", KEY, sleep=_no_sleep)

    assert rows == [{"ISU_CD": "117930"}]
    assert seen == {"path": "/svc/apis/sto/stk_bydd_trd", "basDd": "20170223", "key": KEY}


def test_fetch_one_returns_empty_list_on_a_holiday():
    def handler(request):
        return httpx.Response(200, json={"OutBlock_1": []})

    assert fetch_one(_client(handler), "ksq", "20191003", KEY, sleep=_no_sleep) == []


@pytest.mark.parametrize("message", ["Unauthorized Key", "Unauthorized API Call"])
def test_fetch_one_stops_immediately_on_401(message):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401, json={"respMsg": message, "respCode": "401"})

    with pytest.raises(AuthError, match=message):
        fetch_one(_client(handler), "stk", "20170223", KEY, sleep=_no_sleep)
    assert len(calls) == 1


def test_fetch_one_retries_5xx_twice_then_raises():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, text="busy")

    with pytest.raises(TransientError, match="503"):
        fetch_one(_client(handler), "stk", "20170223", KEY, sleep=_no_sleep)
    assert len(calls) == 3


def test_fetch_one_recovers_after_one_5xx():
    responses = [httpx.Response(502, text="bad"), httpx.Response(200, json={"OutBlock_1": []})]

    def handler(request):
        return responses.pop(0)

    assert fetch_one(_client(handler), "stk", "20170223", KEY, sleep=_no_sleep) == []
    assert responses == []


def test_fetch_one_treats_a_body_without_outblock_as_transient():
    def handler(request):
        return httpx.Response(200, json={"respMsg": "?"})

    with pytest.raises(TransientError, match="OutBlock_1"):
        fetch_one(_client(handler), "stk", "20170223", KEY, sleep=_no_sleep)


def _router(answers: dict):
    """(엔드포인트, basDd) → 행 목록 또는 HTTP 상태 코드. 없으면 빈 목록(휴장)."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        bas_dd = request.url.params.get("basDd")
        calls.append((endpoint, bas_dd))
        answer = answers.get((endpoint, bas_dd), [])
        if answer == 401:
            return httpx.Response(401, json={"respMsg": "Unauthorized API Call", "respCode": "401"})
        if isinstance(answer, int):
            return httpx.Response(answer, text="err")
        return httpx.Response(200, json={"OutBlock_1": answer})

    return handler, calls


ROW = [{"ISU_CD": "005930"}]


def test_run_saves_both_markets_every_weekday_then_the_monthly_base(tmp_path):
    handler, calls = _router({("stk_bydd_trd", "20170201"): ROW})
    stats = run(tmp_path, KEY, client=_client(handler), start="20170201", end="20170202",
                sleep=_no_sleep)

    assert calls == [
        ("stk_bydd_trd", "20170201"), ("ksq_bydd_trd", "20170201"),
        ("stk_bydd_trd", "20170202"), ("ksq_bydd_trd", "20170202"),
        ("stk_isu_base_info", "20170201"), ("ksq_isu_base_info", "20170201"),
    ]
    assert (stats.calls, stats.saved, stats.failed, stats.stopped) == (6, 6, 0, "")
    assert read_rows(cache_path(tmp_path, "stk", "20170201")) == ROW
    assert cache_path(tmp_path, "base/ksq", "20170201").exists()


def test_run_is_idempotent(tmp_path):
    save_atomic(cache_path(tmp_path, "stk", "20170201"), ROW)
    handler, calls = _router({})
    stats = run(tmp_path, KEY, client=_client(handler), start="20170201", end="20170201",
                sleep=_no_sleep)

    assert ("stk_bydd_trd", "20170201") not in calls
    assert stats.skipped_existing == 1


def test_run_stops_at_the_budget(tmp_path):
    handler, calls = _router({})
    stats = run(tmp_path, KEY, client=_client(handler), start="20170201", end="20170202",
                budget=3, sleep=_no_sleep)

    assert (stats.calls, stats.stopped) == (3, "BUDGET")
    assert len(calls) == 3
    assert not cache_path(tmp_path, "ksq", "20170202").exists()


def test_run_stops_immediately_on_an_auth_error(tmp_path):
    handler, calls = _router({("stk_bydd_trd", "20170201"): 401})
    stats = run(tmp_path, KEY, client=_client(handler), start="20170201", end="20170202",
                sleep=_no_sleep)

    assert len(calls) == 1
    assert stats.stopped.startswith("AUTH")
    assert "Unauthorized API Call" in stats.stopped
    assert not any(tmp_path.rglob("*.json.gz"))


def test_run_records_a_transient_failure_continues_and_defers_that_month(tmp_path):
    handler, calls = _router({("stk_bydd_trd", "20170201"): 503,
                              ("stk_bydd_trd", "20170202"): ROW})
    stats = run(tmp_path, KEY, client=_client(handler), start="20170201", end="20170202",
                sleep=_no_sleep)

    assert stats.failed == 1
    assert not cache_path(tmp_path, "stk", "20170201").exists()
    assert cache_path(tmp_path, "stk", "20170202").exists()
    lines = (tmp_path / "failures.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["basDd"] == "20170201"
    # 2월 1일 파일이 없으니 2월의 첫 거래일을 아직 정할 수 없다 → 기본정보를 받지 않는다
    assert not any(endpoint.endswith("isu_base_info") for endpoint, _ in calls)


def test_month_first_trading_day_skips_an_all_empty_day(tmp_path):
    for kind in ("stk", "ksq"):
        save_atomic(cache_path(tmp_path, kind, "20191003"), [])      # 개천절
    save_atomic(cache_path(tmp_path, "stk", "20191004"), ROW)
    save_atomic(cache_path(tmp_path, "ksq", "20191004"), [])

    assert month_first_trading_days(tmp_path, ["20191003", "20191004"]) == ["20191004"]


def test_load_key_prefers_the_environment(monkeypatch):
    monkeypatch.setenv("KRX_AUTH_KEY", "e" * 40)
    assert load_key() == "e" * 40


def test_main_refuses_without_a_key(monkeypatch, tmp_path):
    # conftest 가 STOCK_SKIP_DOTENV=1 로 .env 를 막아 둔다
    monkeypatch.delenv("KRX_AUTH_KEY", raising=False)
    assert main(["--cache-dir", str(tmp_path)]) == 2
