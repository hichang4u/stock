"""H0STCNT0 프레임을 헤더 건수로 쪼갠다 — 필드 수 고정 가정을 버린다.

2026-09-14 KIS가 레코드 끝에 `MARKET_CLS_CODE`를 더해 46 → 47필드가 됐다. 파서는
46의 배수만 쪼갰으므로 다건 프레임에서 첫 체결만 F4에 넘겼다
(`docs/WS_47FIELD_PARSER_FOLLOWUP_20261001.md`).

프레임은 `암호화|TR_ID|건수|데이터`다. 공식 구버전 샘플과 백테스터 모두 3번째 값을
레코드 건수로 읽는다. 값 개수를 건수로 나눠 정렬되면 그 길이를 쓰고, 정렬되지 않으면
알려진 필드 수(47, 46)로 다시 시도한다. 그래도 안 되면 첫 레코드만 쓰고 경보를 남긴다.
"""

from collections import Counter

import pytest

from src.api import kis_ws


def _record(hms: str, price: str, vol: str = "1", n: int = 47) -> list[str]:
    f = [""] * n
    f[0], f[1], f[2], f[12] = "005930", hms, price, vol
    return f


def _frame(records: list[list[str]], count: str) -> str:
    return f"0|H0STCNT0|{count}|" + "^".join(v for r in records for v in r)


@pytest.fixture
def logs(monkeypatch):
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(kis_ws, "log", lambda event, **kw: seen.append((event, kw)))
    monkeypatch.setattr(kis_ws, "_frame_anomaly_counts", Counter(), raising=False)
    return seen


def test_47_field_records_are_split_by_header_count(logs):
    recs = [_record("091015", "10300"), _record("091016", "10310"), _record("091017", "10290")]
    ticks = kis_ws._parse_ticks(_frame(recs, "003"))
    assert [t["price"] for t in ticks] == [10300.0, 10310.0, 10290.0]
    assert all(len(t["raw"]) == 47 for t in ticks)
    assert logs == []


def test_a_future_field_count_is_still_split_by_header_count(logs):
    recs = [_record("091015", "10300", n=48), _record("091016", "10310", n=48)]
    ticks = kis_ws._parse_ticks(_frame(recs, "002"))
    assert [t["price"] for t in ticks] == [10300.0, 10310.0]
    assert logs == []


def test_unreadable_header_falls_back_to_the_known_field_count(logs):
    recs = [_record("091015", "10300"), _record("091016", "10310")]
    ticks = kis_ws._parse_ticks(_frame(recs, "xx"))
    assert [t["price"] for t in ticks] == [10300.0, 10310.0]


def test_header_count_that_disagrees_with_the_data_is_reported(logs):
    recs = [_record("091015", "10300"), _record("091016", "10310"), _record("091017", "10290")]
    ticks = kis_ws._parse_ticks(_frame(recs, "002"))
    assert [t["price"] for t in ticks] == [10300.0, 10310.0, 10290.0]
    assert [e for e, _ in logs] == ["WS_FRAME_HEADER_MISMATCH"]
    assert logs[0][1]["header_count"] == 2
    assert logs[0][1]["records"] == 3


def test_unsplittable_frame_keeps_the_first_record_and_is_reported(logs):
    raw = _record("091015", "10300") + ["junk"] * 5
    ticks = kis_ws._parse_ticks("0|H0STCNT0|002|" + "^".join(raw))
    assert len(ticks) == 1
    assert ticks[0]["price"] == 10300.0
    assert [e for e, _ in logs] == ["WS_FRAME_UNSPLIT"]
    assert logs[0][1]["values"] == 52


def test_repeated_anomalies_are_not_logged_every_frame(logs):
    raw = "0|H0STCNT0|002|" + "^".join(_record("091015", "10300") + ["junk"] * 5)
    for _ in range(3):
        kis_ws._parse_ticks(raw)
    assert [e for e, _ in logs] == ["WS_FRAME_UNSPLIT"]
    assert kis_ws._frame_anomaly_counts["WS_FRAME_UNSPLIT"] == 3


def test_ticks_carry_their_frame_position(logs):
    recs = [_record("091015", "10300"), _record("091016", "10310")]
    ticks = kis_ws._parse_ticks(_frame(recs, "002"))
    assert [(t["frame_count"], t["frame_size"], t["frame_index"]) for t in ticks] == [
        (2, 2, 0), (2, 2, 1),
    ]


def test_frame_count_is_none_when_the_header_is_unreadable(logs):
    ticks = kis_ws._parse_ticks(_frame([_record("091015", "10300")], "xx"))
    assert ticks[0]["frame_count"] is None
    assert ticks[0]["frame_size"] == 1
