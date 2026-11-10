"""틱 파일을 읽는 곳 6곳이 끊긴 멤버 뒤의 멤버까지 읽는지 본다(2026-11-09 전원 차단 모양).

공용 읽기와 그 단위 테스트는 `src/utils/jsonl_gz.py`, `tests/test_jsonl_gz.py`.
"""

import json
from types import SimpleNamespace

import pytest

import scripts.replay_ticks_to_bars as replay_mod
from scripts.entry_timing_report import _read_gz
from scripts.overnight_sieve import load_tick_prices
from scripts.trail_near_miss import load_ws_ticks
from scripts.ws_frame_check import check_day
from src import db
from src.modules import tick_capture as tc
from tests.test_jsonl_gz import power_loss_file
from tests.test_ws_frame_check import _frame, _rec, _split47

DATE = "20261109"
TICKER = "267850"


def _ticks(minute: str, n: int, seq0: int = 1) -> list[dict]:
    out = []
    for i in range(n):
        ts = f"2026-11-09T09:{minute}:{i:02d}+09:00"
        out.append({"seq": seq0 + i, "source": "ws", "source_ts": ts, "received_at": ts,
                    "price": 11_000.0 + i, "qty": 1})
    return out


def _day_dir(root):
    d = root / "data" / "strategy_ticks" / DATE
    d.mkdir(parents=True)
    return d


def test_entry_timing_report_reads_member_after_power_loss(tmp_path):
    a, b = _ticks("05", 20), _ticks("11", 30, seq0=21)
    p = power_loss_file(tmp_path / f"{TICKER}.09.jsonl.gz", a, b)
    assert _read_gz(p) == a + b


def test_ws_frame_check_counts_both_members_and_flags_damage(tmp_path):
    a = [_frame(_rec(TICKER, f"0905{i:02d}")) for i in range(10)]
    b = [_frame(_rec(TICKER, f"0911{i:02d}")) for i in range(15)]
    power_loss_file(_day_dir(tmp_path) / f"{TICKER}.09.jsonl.gz", a, b)
    t = check_day(tmp_path, DATE, split=_split47)["tickers"][TICKER]
    assert t["records"] == 25
    assert t["damaged"] is True
    assert t["truncated"] is False


def test_overnight_sieve_tick_prices_include_member_after_power_loss(tmp_path):
    a, b = _ticks("05", 5), _ticks("11", 7)
    for r in a + b:
        r["source"] = "rest"
    power_loss_file(_day_dir(tmp_path) / f"{TICKER}.09.jsonl.gz", a, b)
    got = load_tick_prices(tmp_path, DATE, TICKER)
    assert got is not None and len(got) == 12
    assert got[-1] == ("091106", 11_006.0)


def test_trail_near_miss_ticks_include_member_after_power_loss(tmp_path):
    a, b = _ticks("05", 5), _ticks("11", 7, seq0=6)
    power_loss_file(_day_dir(tmp_path) / f"{TICKER}.09.jsonl.gz", a, b)
    assert [t["seq"] for t in load_ws_ticks(tmp_path, DATE, TICKER)] == list(range(1, 13))


def test_replay_feeds_member_after_power_loss(tmp_path, monkeypatch):
    a, b = _ticks("05", 5), _ticks("11", 7, seq0=6)
    power_loss_file(_day_dir(tmp_path) / f"{TICKER}.09.jsonl.gz", a, b)
    fed: list[dict] = []
    fake = SimpleNamespace(on_tick=fed.append, drain=lambda: None, _series={})
    monkeypatch.setattr(replay_mod, "bars", fake)
    monkeypatch.setattr(replay_mod, "CAPTURE_DIR", tmp_path / "data" / "strategy_ticks")
    replay_mod.replay(DATE)
    assert [t["seq"] for t in fed] == list(range(1, 13))


@pytest.fixture
async def mem():
    await db.init(":memory:")
    yield
    await db.close()


def _capture(base):
    return tc.TickCapture(trade_date=DATE, ticker=TICKER, trade_id=71, experiment_id="x",
                          entry_at="2026-11-09T09:00:24+09:00", base_dir=base)


async def test_tick_capture_resumes_and_finalizes_after_power_loss(mem, tmp_path):
    """2026-11-09: 재시작 복원은 EOFError, 15:20 마감은 zlib.error로 실패했다.

    그래서 그날 마감 manifest가 없다.
    """
    a, b = _ticks("05", 20), _ticks("11", 30, seq0=21)
    day = tmp_path / DATE
    day.mkdir()
    power_loss_file(day / f"{TICKER}.09.jsonl.gz", a, b)

    cap = _capture(tmp_path)
    cap._resume_from_existing()
    assert cap._rows_written == 50
    assert cap._seq == 50

    chunks = cap._chunk_metadata()
    got = [(c["rows"], c["first_seq"], c["last_seq"], c["seq_count"]) for c in chunks]
    assert got == [(50, 1, 50, 50)]
    assert chunks[0]["damaged_at"] == [0]

    await cap.finalize("COMPLETE", reached_expected_close=True)
    m = await db.get_price_path_manifest(DATE, TICKER, "x")
    assert m["finalized_at"]
    assert json.loads(m["chunks_json"])[0]["rows"] == 50
