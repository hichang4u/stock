"""WS 체결 프레임의 원시 필드 보존 — 파서 → F4 → 캡처 3계층.

H0STCNT0 응답에서 현재 파서는 인덱스 0·1·2·12만 해석하고 나머지를 버린다.
체결강도·체결구분·최우선호가 등은 공식 명세로 인덱스가 확인되기 전까지
해석할 수 없지만, 버린 필드는 나중에 복원할 방법이 없다. 그래서 해석은
하지 않고 원시 배열을 그대로 캡처에 남긴다.

이 파일은 "해석하지 않고 보존한다"만 검증한다. 인덱스별 의미 검증은 KIS
공식 명세 확인 후 별도 테스트로 추가한다.
"""

import gzip
import json

import pytest

from src import db
from src.api import kis_ws
from src.modules import tick_capture as tc


def _cnt_frame(*, ticker="005930", hms="091015", price="10300", vol="7",
               extra=None):
    """H0STCNT0 프레임. 0=종목 1=체결시간 2=현재가 12=체결량 (검증된 인덱스)."""
    fields = [""] * 13
    fields[0] = ticker
    fields[1] = hms
    fields[2] = price
    fields[12] = vol
    if extra:
        fields += list(extra)
    return "0|H0STCNT0|001|" + "^".join(fields)


# ── 계층 1: 파서 ─────────────────────────────────────────────────────


def _parse_one(frame: str) -> dict:
    """_parse_tick은 dict | None이다. 테스트 프레임은 늘 파싱되므로 여기서 좁힌다."""
    tick = kis_ws._parse_tick(frame)
    assert tick is not None
    return tick



def test_parse_tick_preserves_every_raw_field() -> None:
    """해석하지 않은 필드도 순서 그대로 남아야 한다."""
    tail = ["101", "202", "1.23", "5"]
    tick = _parse_one(_cnt_frame(extra=tail))
    assert tick["raw"][:13] == ["005930", "091015", "10300", "", "", "", "",
                                "", "", "", "", "", "7"]
    assert tick["raw"][13:] == tail


def test_parse_tick_raw_is_not_shared_with_caller_mutation() -> None:
    """호출부가 raw를 바꿔도 다음 파싱에 영향이 없어야 한다."""
    a = _parse_one(_cnt_frame())
    a["raw"].append("MUTATED")
    b = _parse_one(_cnt_frame())
    assert "MUTATED" not in b["raw"]


def test_parse_tick_keeps_existing_interpreted_fields() -> None:
    """원시 보존이 기존 해석 필드를 밀어내면 안 된다."""
    tick = _parse_one(_cnt_frame(price="10300", vol="7"))
    assert tick["price"] == 10300.0
    assert tick["qty"] == 7
    assert tick["ticker"] == "005930"
    assert tick["exchange_time"] == "091015"


# ── 계층 2: 캡처 기록 ────────────────────────────────────────────────


@pytest.fixture
async def mem():
    await db.init(":memory:")
    yield
    await db.close()


def _capture(tmp_path):
    return tc.TickCapture(
        trade_date="20260813",
        ticker="005930",
        trade_id=27,
        experiment_id="baseline-x",
        entry_at="2026-08-13T09:10:10+09:00",
        base_dir=tmp_path,
    )


def _rows(tmp_path):
    out = []
    for gzp in sorted((tmp_path / "20260813").glob("005930.*.jsonl.gz")):
        with gzip.open(gzp, "rt", encoding="utf-8") as f:
            out += [json.loads(x) for x in f if x.strip()]
    return out


async def test_capture_row_persists_raw_fields(mem, tmp_path) -> None:
    cap = _capture(tmp_path)
    cap.start()
    ts = "2026-08-13T09:10:10+09:00"
    cap.enqueue({"source_ts": ts, "received_at": ts, "price": 10_000.0,
                 "qty": 10, "source": "ws", "valid": True,
                 "raw": ["005930", "091010", "10000", "X", "Y"]})
    await cap.finalize("COMPLETE", reached_expected_close=True)

    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["raw"] == ["005930", "091010", "10000", "X", "Y"]


async def test_capture_row_omits_raw_when_absent(mem, tmp_path) -> None:
    """REST 백업 틱에는 원시 프레임이 없다. None으로 기록한다."""
    cap = _capture(tmp_path)
    cap.start()
    ts = "2026-08-13T09:10:10+09:00"
    cap.enqueue({"source_ts": ts, "received_at": ts, "price": 10_000.0,
                 "qty": 0, "source": "rest", "valid": True})
    await cap.finalize("COMPLETE", reached_expected_close=True)

    assert _rows(tmp_path)[0]["raw"] is None


async def test_schema_version_bumped(mem, tmp_path) -> None:
    """행 모양이 바뀌었으므로 판독기가 구분할 수 있어야 한다."""
    assert tc.SCHEMA_VERSION != "tick-schema-1"

    cap = _capture(tmp_path)
    cap.start()
    await cap.finalize("COMPLETE", reached_expected_close=True)
    m = await db.get_price_path_manifest("20260813", "005930", "baseline-x")
    assert m is not None
    assert m["schema_version"] == tc.SCHEMA_VERSION


# ── 계층 3: F4 배선 ──────────────────────────────────────────────────


async def test_f4_forwards_raw_from_ws_tick_to_capture(monkeypatch) -> None:
    """파서가 준 raw가 캡처까지 도달해야 한다."""
    from src.modules import f4_tracking

    captured = {}
    monkeypatch.setattr(f4_tracking, "_price_observation_active", lambda *a, **k: True)
    monkeypatch.setattr(f4_tracking.live, "push_tick", lambda *a, **k: None)
    monkeypatch.setattr(f4_tracking.tick_capture, "enqueue",
                        lambda row: captured.update(row))
    monkeypatch.setattr(f4_tracking.state, "get",
                        lambda: type("S", (), {"position_status": "CLOSED",
                                               "target_ticker": "005930"})())

    tick = _parse_one(_cnt_frame(extra=["A", "B"]))
    await f4_tracking._handle_price_tick(
        10300.0, "005930", f4_tracking.SpikeFilter(),
        source="ws", tick_meta=tick,
    )
    assert captured["raw"] == tick["raw"]


async def test_f4_rest_tick_has_no_raw(monkeypatch) -> None:
    """REST 경로는 원시 프레임이 없으므로 raw가 None이어야 한다."""
    from src.modules import f4_tracking

    captured = {}
    monkeypatch.setattr(f4_tracking, "_price_observation_active", lambda *a, **k: True)
    monkeypatch.setattr(f4_tracking.live, "push_tick", lambda *a, **k: None)
    monkeypatch.setattr(f4_tracking.tick_capture, "enqueue",
                        lambda row: captured.update(row))
    monkeypatch.setattr(f4_tracking.state, "get",
                        lambda: type("S", (), {"position_status": "CLOSED",
                                               "target_ticker": "005930"})())

    await f4_tracking._handle_price_tick(
        10300.0, "005930", f4_tracking.SpikeFilter(),
        source="rest", tick_meta={"source_ts": "2026-08-13T09:10:10+09:00"},
    )
    assert captured["raw"] is None
