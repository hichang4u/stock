"""개발 트리 보유종목 제외 — 운영 F5가 개발분까지 파는 것을 막는다.

F5는 상태 파일이 아니라 브로커의 계좌 전체 hldg_qty로 매도 수량을 정한다
(f5_timeout.py:118). 두 트리가 같은 종목을 들면 먼저 청산하는 쪽이 상대의
물량까지 판다. 개발이 다음 순위로 내려가 그 상황 자체를 만들지 않는다.
"""

from src import state
from src.modules import f3_entry


async def test_flag_off_excludes_nothing(monkeypatch):
    monkeypatch.delenv("DEV_EXCLUDE_HELD_TICKERS", raising=False)
    assert await f3_entry._held_tickers_to_exclude() == set()


async def test_flag_on_returns_held_tickers(monkeypatch):
    monkeypatch.setenv("DEV_EXCLUDE_HELD_TICKERS", "1")

    async def fake_get(path, **kwargs):
        return {"rt_cd": "0", "output1": [
            {"pdno": "005930", "hldg_qty": "10"},
            {"pdno": "000660", "hldg_qty": "0"},
            {"pdno": "187660", "hldg_qty": "3"},
        ]}

    monkeypatch.setattr(f3_entry.kis_rest, "get", fake_get)
    assert await f3_entry._held_tickers_to_exclude() == {"005930", "187660"}


async def test_query_failure_excludes_nothing(monkeypatch):
    """조회 실패로 진입을 막으면 개발 트리가 아무것도 테스트하지 못한다.

    fail-open이 안전한 드문 경우다 — 최악의 결과가 '종목이 겹친다'이고,
    그 손해는 개발 물량(계좌의 30%)에 한정된다.
    """
    monkeypatch.setenv("DEV_EXCLUDE_HELD_TICKERS", "1")

    async def boom(path, **kwargs):
        raise RuntimeError("조회 실패")

    monkeypatch.setattr(f3_entry.kis_rest, "get", boom)
    assert await f3_entry._held_tickers_to_exclude() == set()


async def test_comma_formatted_quantity_is_parsed(monkeypatch):
    """브로커가 콤마 포함 수량("1,000")을 주는 실제 사례를 견뎌야 한다.

    float("1,000")은 ValueError를 낸다 — to_float()로 콤마를 벗겨야 한다.
    """
    monkeypatch.setenv("DEV_EXCLUDE_HELD_TICKERS", "1")

    async def fake_get(path, **kwargs):
        return {"rt_cd": "0", "output1": [{"pdno": "005930", "hldg_qty": "1,000"}]}

    monkeypatch.setattr(f3_entry.kis_rest, "get", fake_get)
    assert await f3_entry._held_tickers_to_exclude() == {"005930"}


async def test_unparseable_quantity_excludes_nothing(monkeypatch):
    """수량이 숫자로 전혀 해석되지 않아도(예: "abc") 진입을 막지 않는다."""
    monkeypatch.setenv("DEV_EXCLUDE_HELD_TICKERS", "1")

    async def fake_get(path, **kwargs):
        return {"rt_cd": "0", "output1": [{"pdno": "005930", "hldg_qty": "abc"}]}

    monkeypatch.setattr(f3_entry.kis_rest, "get", fake_get)
    assert await f3_entry._held_tickers_to_exclude() == set()


async def test_malformed_response_shapes_exclude_nothing(monkeypatch):
    """output1의 원소가 dict가 아니거나 응답 자체가 dict가 아니어도 죽지 않는다."""
    monkeypatch.setenv("DEV_EXCLUDE_HELD_TICKERS", "1")

    for bad_resp in (
        {"rt_cd": "0", "output1": [None]},
        None,
        [{"rt_cd": "0"}],
        "not-a-dict",
    ):

        async def fake_get(path, _resp=bad_resp, **kwargs):
            return _resp

        monkeypatch.setattr(f3_entry.kis_rest, "get", fake_get)
        assert await f3_entry._held_tickers_to_exclude() == set()


async def test_error_response_code_excludes_nothing(monkeypatch):
    """rt_cd가 "0"이 아니면(호출 제한 등) 조회 실패와 같이 취급한다."""
    monkeypatch.setenv("DEV_EXCLUDE_HELD_TICKERS", "1")

    async def fake_get(path, **kwargs):
        return {"rt_cd": "1", "msg1": "EGW00201 초당 거래건수를 초과하였습니다"}

    monkeypatch.setattr(f3_entry.kis_rest, "get", fake_get)
    assert await f3_entry._held_tickers_to_exclude() == set()


async def test_single_candidate_path_skips_a_held_ticker(monkeypatch):
    """F2가 후보를 하나만 잠그면 _run_pipeline은 _rank_final_entry_candidates를
    거치지 않고 _run_single로 바로 간다(f3_entry.py:700-706 주석). 그 경로에서도
    보유종목 제외가 걸리지 않으면, 개발이 운영과 같은 종목을 사고 F5가
    운영 물량까지 판다 — Task 10이 막으려던 사고 그대로다.
    """
    monkeypatch.setenv("DEV_EXCLUDE_HELD_TICKERS", "1")
    monkeypatch.delenv("DRY_RUN", raising=False)

    s = state.get()
    monkeypatch.setattr(s, "day_skip", False)
    monkeypatch.setattr(s, "target_ticker", None)
    monkeypatch.setattr(s, "target_candidates", [{"ticker": "005930", "name": "삼성전자"}])

    async def fake_get(path, **kwargs):
        return {"rt_cd": "0", "output1": [{"pdno": "005930", "hldg_qty": "10"}]}

    monkeypatch.setattr(f3_entry.kis_rest, "get", fake_get)

    run_single_calls = []

    async def fake_run_single(*args, **kwargs):
        run_single_calls.append((args, kwargs))

    monkeypatch.setattr(f3_entry, "_run_single", fake_run_single)

    await f3_entry._run_pipeline()

    assert run_single_calls == []


async def test_single_candidate_path_flag_off_makes_no_kis_calls(monkeypatch):
    """플래그가 꺼져 있으면 이 경로는 계좌 조회를 전혀 하지 않는다 — 운영 불변식.

    _held_tickers_to_exclude()는 플래그 확인만으로 즉시 빈 집합을 반환하므로,
    이 분기에 넣은 한 줄이 운영 경로에 새 네트워크 호출을 추가하지 않는다.
    """
    monkeypatch.delenv("DEV_EXCLUDE_HELD_TICKERS", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)

    s = state.get()
    monkeypatch.setattr(s, "day_skip", False)
    monkeypatch.setattr(s, "target_ticker", None)
    monkeypatch.setattr(s, "target_candidates", [{"ticker": "005930", "name": "삼성전자"}])

    calls = []

    async def counting_get(path, **kwargs):
        calls.append(path)
        raise AssertionError("kis_rest.get must not be called when the flag is off")

    monkeypatch.setattr(f3_entry.kis_rest, "get", counting_get)

    async def fake_run_single(*args, **kwargs):
        pass

    monkeypatch.setattr(f3_entry, "_run_single", fake_run_single)

    await f3_entry._run_pipeline()

    assert calls == []
