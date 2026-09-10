"""개발 트리 보유종목 제외 — 운영 F5가 개발분까지 파는 것을 막는다.

F5는 상태 파일이 아니라 브로커의 계좌 전체 hldg_qty로 매도 수량을 정한다
(f5_timeout.py:118). 두 트리가 같은 종목을 들면 먼저 청산하는 쪽이 상대의
물량까지 판다. 개발이 다음 순위로 내려가 그 상황 자체를 만들지 않는다.
"""

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
