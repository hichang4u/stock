"""KRX 수집기 — HTTP는 가짜(httpx.MockTransport)만 쓴다. 실제 KRX를 부르지 않는다."""

import httpx
import pytest

from scripts.krx_daily_fetch import (
    AuthError,
    TransientError,
    cache_path,
    fetch_one,
    read_rows,
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
