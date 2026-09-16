"""내구 틱 캡처 코어 — 순서·gzip·gap·회전·manifest·재시작 안전 테스트."""
import gzip
import json
from datetime import datetime

import pytest

from src import db
from src.modules import tick_capture as tc


@pytest.fixture
async def mem():
    await db.init(":memory:")
    yield
    await db.close()


def _tick(seq_hint, hour="09", minute="10", second="10", price=10_000.0):
    ts = f"2026-08-13T{hour}:{minute}:{second}+09:00"
    return {
        "source_ts": ts,
        "received_at": ts,
        "price": price,
        "qty": 10,
        "source": "ws",
        "valid": True,
    }


def _new_capture(tmp_path, ticker="005930"):
    return tc.TickCapture(
        trade_date="20260813",
        ticker=ticker,
        trade_id=27,
        experiment_id="baseline-x",
        entry_at="2026-08-13T09:10:10+09:00",
        base_dir=tmp_path,
    )


def _read_all_rows(tmp_path, ticker="005930"):
    rows = []
    for gzp in sorted((tmp_path / "20260813").glob(f"{ticker}.*.jsonl.gz")):
        with gzip.open(gzp, "rt", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rows.append(json.loads(line))
    return rows


async def test_seq_monotonic_and_gzip_roundtrip(mem, tmp_path):
    cap = _new_capture(tmp_path)
    cap.start()
    for i in range(3):
        cap.enqueue(_tick(i, second=f"1{i}", price=10_000 + i))
    await cap.finalize("COMPLETE", reached_expected_close=True)

    rows = _read_all_rows(tmp_path)
    assert [r["seq"] for r in rows] == [1, 2, 3]
    assert [r["price"] for r in rows] == [10_000, 10_001, 10_002]

    m = await db.get_price_path_manifest("20260813", "005930", "baseline-x")
    assert m["data_complete"] == 1
    assert m["reached_expected_close"] == 1
    chunks = json.loads(m["chunks_json"])
    assert chunks and all("sha256" in c and c["rows"] >= 1 for c in chunks)
    assert m["first_source_ts"] and m["last_source_ts"]


async def test_source_ts_reversal_counted_separately_from_seq_gaps(mem, tmp_path):
    cap = _new_capture(tmp_path)
    cap.start()
    cap.enqueue(_tick(0, second="20"))
    cap.enqueue(_tick(1, second="10"))  # 거래소 시각 역전(순서 뒤바뀜)
    await cap.finalize("COMPLETE", reached_expected_close=True)
    m = await db.get_price_path_manifest("20260813", "005930", "baseline-x")
    # 역전은 seq_gaps가 아니라 source_ts_reversals로 기록되고, 실제 seq는 연속이다.
    assert m["source_ts_reversals"] >= 1
    assert m["seq_gaps"] == 0


def _at(hour="09", minute="10", second="10"):
    return datetime.fromisoformat(f"2026-08-13T{hour}:{minute}:{second}+09:00")


async def test_short_ws_outage_covered_by_next_sample_stays_complete(mem, tmp_path):
    """모의서버는 매시 정각에 WS를 끊고 2초 뒤 다시 붙는다. 다음 표본까지의 공백이
    임계값 안이면 경로는 완전하다 — 단절 횟수는 별도 컬럼에 그대로 남는다."""
    cap = _new_capture(tmp_path)
    cap.start()
    cap.enqueue(_tick(0))
    cap.mark_ws_disconnect(at=_at(second="10"))
    cap.enqueue(_tick(1, second="12"))  # 2초 뒤 재수신
    await cap.finalize("COMPLETE", reached_expected_close=True)
    m = await db.get_price_path_manifest("20260813", "005930", "baseline-x")
    assert m["data_complete"] == 1
    assert m["missing_reason"] is None
    assert m["ws_disconnects"] == 1


async def test_ws_outage_longer_than_threshold_forces_incomplete(mem, tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "MAX_WS_OUTAGE_SEC", 10.0)
    cap = _new_capture(tmp_path)
    cap.start()
    cap.enqueue(_tick(0))
    cap.mark_ws_disconnect(at=_at(second="10"))
    cap.enqueue(_tick(1, second="30"))  # 20초 공백
    await cap.finalize("COMPLETE", reached_expected_close=True)
    m = await db.get_price_path_manifest("20260813", "005930", "baseline-x")
    assert m["data_complete"] == 0
    assert m["missing_reason"] == "WS_LOSS"
    assert m["ws_disconnects"] == 1


async def test_rest_sample_closes_a_ws_outage(mem, tmp_path, monkeypatch):
    """WS가 끊긴 동안 REST 백업이 표본을 남기면 그 표본이 커버리지를 잇는다."""
    monkeypatch.setattr(tc, "MAX_WS_OUTAGE_SEC", 10.0)
    cap = _new_capture(tmp_path)
    cap.start()
    cap.enqueue(_tick(0))
    cap.mark_ws_disconnect(at=_at(second="10"))
    rest = _tick(1, second="13")
    rest["source"] = "rest"
    rest["source_ts"] = None
    cap.enqueue(rest)
    cap.enqueue(_tick(2, second="40"))  # WS 틱은 30초 뒤에야 돌아온다
    await cap.finalize("COMPLETE", reached_expected_close=True)
    m = await db.get_price_path_manifest("20260813", "005930", "baseline-x")
    assert m["data_complete"] == 1


async def test_ws_outage_still_open_at_finalize_counts_to_finalize_time(
    mem, tmp_path, monkeypatch
):
    monkeypatch.setattr(tc, "MAX_WS_OUTAGE_SEC", 10.0)
    cap = _new_capture(tmp_path)
    cap.start()
    cap.enqueue(_tick(0))
    cap.mark_ws_disconnect(at=_at(second="10"))
    await cap.finalize("COMPLETE", reached_expected_close=True, now=_at(second="50"))
    m = await db.get_price_path_manifest("20260813", "005930", "baseline-x")
    assert m["data_complete"] == 0
    assert m["missing_reason"] == "WS_LOSS"


async def test_ws_reconnect_closes_the_outage_before_the_next_sample(
    mem, tmp_path, monkeypatch
):
    """2026-09-11: 09:00:01 단절 → 09:00:03 재접속(구독 요청 송신) → 첫 체결 틱은
    09:00:15. 소켓이 살아 있는 동안의 침묵은 거래가 없었다는 뜻이지 손실이
    아니므로, 공백은 재접속 시각에서 닫혀야 한다."""
    monkeypatch.setattr(tc, "MAX_WS_OUTAGE_SEC", 10.0)
    cap = _new_capture(tmp_path)
    cap.start()
    cap.enqueue(_tick(0))
    cap.mark_ws_disconnect(at=_at(second="10"))
    cap.mark_ws_reconnect(at=_at(second="12"))
    cap.enqueue(_tick(1, second="40"))  # 첫 표본은 30초 뒤
    await cap.finalize("COMPLETE", reached_expected_close=True)
    m = await db.get_price_path_manifest("20260813", "005930", "baseline-x")
    assert m["data_complete"] == 1
    assert m["missing_reason"] is None
    assert m["ws_disconnects"] == 1
    assert cap._max_ws_outage_sec == pytest.approx(2.0)


async def test_queued_sample_before_reconnect_closes_the_outage_first(
    mem, tmp_path, monkeypatch
):
    """재접속은 즉시 반영되지만 표본은 0.5초 드레인 뒤에 쓰인다. 큐에 이미 들어온
    단절 이후 표본이 재접속보다 앞서면 그 표본이 공백을 닫아야 한다."""
    monkeypatch.setattr(tc, "MAX_WS_OUTAGE_SEC", 10.0)
    cap = _new_capture(tmp_path)
    cap.start()
    cap.enqueue(_tick(0))
    cap.mark_ws_disconnect(at=_at(second="10"))
    rest = _tick(1, second="19")  # 큐에만 있고 아직 드레인되지 않음
    rest["source"] = "rest"
    rest["source_ts"] = None
    cap.enqueue(rest)
    cap.mark_ws_reconnect(at=_at(second="21"))  # 표본 기준이면 9초, 재접속 기준이면 11초
    await cap.finalize("COMPLETE", reached_expected_close=True)
    m = await db.get_price_path_manifest("20260813", "005930", "baseline-x")
    assert m["data_complete"] == 1
    assert cap._max_ws_outage_sec == pytest.approx(9.0)


async def test_slow_ws_reconnect_is_still_a_loss(mem, tmp_path, monkeypatch):
    """2026-09-08: 재접속 자체가 수백 초 걸린 날은 그대로 WS_LOSS다."""
    monkeypatch.setattr(tc, "MAX_WS_OUTAGE_SEC", 10.0)
    cap = _new_capture(tmp_path)
    cap.start()
    cap.enqueue(_tick(0))
    cap.mark_ws_disconnect(at=_at(second="10"))
    cap.mark_ws_reconnect(at=_at(second="40"))  # 30초 만에 재접속
    cap.enqueue(_tick(1, second="41"))
    await cap.finalize("COMPLETE", reached_expected_close=True)
    m = await db.get_price_path_manifest("20260813", "005930", "baseline-x")
    assert m["data_complete"] == 0
    assert m["missing_reason"] == "WS_LOSS"


async def test_worst_ws_outage_is_the_max_not_the_last(mem, tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "MAX_WS_OUTAGE_SEC", 10.0)
    cap = _new_capture(tmp_path)
    cap.start()
    cap.enqueue(_tick(0))
    cap.mark_ws_disconnect(at=_at(second="10"))
    cap.enqueue(_tick(1, second="40"))  # 30초 — 초과
    cap.mark_ws_disconnect(at=_at(second="40"))
    cap.enqueue(_tick(2, second="42"))  # 2초 — 정상
    await cap.finalize("COMPLETE", reached_expected_close=True)
    m = await db.get_price_path_manifest("20260813", "005930", "baseline-x")
    assert m["data_complete"] == 0
    assert m["ws_disconnects"] == 2


async def test_rest_backfill_ranges_tracked(mem, tmp_path):
    cap = _new_capture(tmp_path)
    cap.start()
    cap.enqueue(_tick(0, second="10"))  # ws
    rest = _tick(1, second="40")
    rest["source"] = "rest"
    rest["source_ts"] = None  # REST는 거래소 시각 없음
    cap.enqueue(rest)
    await cap.finalize("COMPLETE", reached_expected_close=True)
    m = await db.get_price_path_manifest("20260813", "005930", "baseline-x")
    ranges = json.loads(m["rest_backfill_ranges_json"])
    assert len(ranges) == 1
    assert ranges[0]["start"] and ranges[0]["end"]


async def test_initial_incomplete_manifest_at_start(mem, tmp_path):
    cap = _new_capture(tmp_path)
    cap.start()
    await cap.write_initial_manifest()
    m = await db.get_price_path_manifest("20260813", "005930", "baseline-x")
    assert m is not None
    assert m["data_complete"] == 0
    assert m["missing_reason"] == "IN_PROGRESS"
    await cap.finalize("COMPLETE", reached_expected_close=True)


async def test_hourly_chunk_rotation(mem, tmp_path):
    cap = _new_capture(tmp_path)
    cap.start()
    cap.enqueue(_tick(0, hour="09", minute="30"))
    cap.enqueue(_tick(1, hour="10", minute="05"))
    await cap.finalize("COMPLETE", reached_expected_close=True)
    chunk_files = sorted((tmp_path / "20260813").glob("005930.*.jsonl.gz"))
    assert [p.name for p in chunk_files] == [
        "005930.09.jsonl.gz",
        "005930.10.jsonl.gz",
    ]


async def test_incomplete_reason_recorded(mem, tmp_path):
    cap = _new_capture(tmp_path)
    cap.start()
    cap.enqueue(_tick(0))
    await cap.finalize("MANUAL_STOP", reached_expected_close=False)
    m = await db.get_price_path_manifest("20260813", "005930", "baseline-x")
    assert m["data_complete"] == 0
    assert m["missing_reason"] == "MANUAL_STOP"
    assert m["reached_expected_close"] == 0


async def test_enqueue_never_raises_and_writer_errors_marked(mem, tmp_path, monkeypatch):
    cap = _new_capture(tmp_path)
    cap.start()

    def boom(_row):
        raise RuntimeError("disk full")

    monkeypatch.setattr(cap, "_write_row", boom)
    # enqueue는 예외를 절대 전파하지 않는다(주문 경로 격리).
    cap.enqueue(_tick(0))
    cap.enqueue(_tick(1))
    await cap.finalize("COMPLETE", reached_expected_close=True)
    m = await db.get_price_path_manifest("20260813", "005930", "baseline-x")
    # 쓰기 오류가 있으면 완전으로 표기하지 않는다.
    assert m["data_complete"] == 0
    assert m["missing_reason"] == "WRITER_ERROR"


async def test_restart_resume_no_truncate_no_duplicate_seq(mem, tmp_path):
    cap1 = _new_capture(tmp_path)
    cap1.start()
    for i in range(3):
        cap1.enqueue(_tick(i, second=f"1{i}"))
    await cap1.finalize("PROCESS_SHUTDOWN", reached_expected_close=False)
    rows_after_1 = _read_all_rows(tmp_path)
    assert [r["seq"] for r in rows_after_1] == [1, 2, 3]

    # 재시작: 같은 거래일·종목으로 새 캡처가 이어쓴다.
    cap2 = _new_capture(tmp_path)
    cap2.start()
    for i in range(2):
        cap2.enqueue(_tick(i, minute="11", second=f"0{i}"))
    await cap2.finalize("COMPLETE", reached_expected_close=True)

    rows = _read_all_rows(tmp_path)
    seqs = [r["seq"] for r in rows]
    assert seqs == [1, 2, 3, 4, 5]  # no truncation, no duplicate/skip
    assert len(seqs) == len(set(seqs))
    m = await db.get_price_path_manifest("20260813", "005930", "baseline-x")
    # 재시작은 사전 시각/행 상태를 모두 복원하고 이전 중단을 표시한다.
    assert m["first_received_at"] == rows[0]["received_at"]
    assert m["last_received_at"] == rows[-1]["received_at"]
    assert m["data_complete"] == 0
    assert m["missing_reason"] == "RESTART_GAP"


async def test_rest_tick_does_not_erase_exchange_timestamp_bounds(mem, tmp_path):
    """REST 표본은 거래소 시각이 없다 — 그 None이 경계를 지우면 안 된다.

    장 마감 후 REST 백업이 15:14까지 도는 실운영에서는 마지막 행이 거의 항상 REST다.
    덮어쓰면 last_source_ts가 NULL이 되어 진입~15:15 커버리지 판정이 무너진다.
    """
    cap = _new_capture(tmp_path)
    cap.start()
    first_rest = _tick(0, second="05")  # 첫 행이 REST인 경우도 함께 검증
    first_rest["source"] = "rest"
    first_rest["source_ts"] = None
    cap.enqueue(first_rest)
    cap.enqueue(_tick(1, second="10"))  # ws
    cap.enqueue(_tick(2, minute="30", second="20"))  # ws
    last_rest = _tick(3, hour="15", minute="10", second="00")
    last_rest["source"] = "rest"
    last_rest["source_ts"] = None
    cap.enqueue(last_rest)
    await cap.finalize("COMPLETE", reached_expected_close=True)

    m = await db.get_price_path_manifest("20260813", "005930", "baseline-x")
    assert m["first_source_ts"] == "2026-08-13T09:10:10+09:00"
    assert m["last_source_ts"] == "2026-08-13T09:30:20+09:00"
    # 수신 시각 경계는 REST 행을 포함해 그대로 이어진다.
    assert m["first_received_at"] == "2026-08-13T09:10:05+09:00"
    assert m["last_received_at"] == "2026-08-13T15:10:00+09:00"


async def test_manifest_does_not_embed_per_tick_sequence_numbers(mem, tmp_path):
    """chunks_json은 행 수에 비례해 커지면 안 된다(주문 경로와 같은 DB 쓰기)."""
    cap = _new_capture(tmp_path)
    cap.start()
    for i in range(50):
        cap.enqueue(_tick(i, second=f"{10 + i % 40:02d}"))
    await cap.finalize("COMPLETE", reached_expected_close=True)

    m = await db.get_price_path_manifest("20260813", "005930", "baseline-x")
    chunks = json.loads(m["chunks_json"])
    assert chunks
    for chunk in chunks:
        assert "seqs" not in chunk
        assert chunk["first_seq"] == 1
        assert chunk["last_seq"] == 50
        assert chunk["seq_count"] == 50
    assert m["seq_gaps"] == 0


async def test_seq_gaps_counts_missing_numbers_after_chunk_loss(mem, tmp_path):
    cap = _new_capture(tmp_path)
    cap.start()
    cap.enqueue(_tick(0, hour="09", second="10"))
    cap.enqueue(_tick(1, hour="10", second="10"))
    cap.enqueue(_tick(2, hour="11", second="10"))
    await cap.finalize("COMPLETE", reached_expected_close=True)
    # 중간 chunk가 유실되면 seq 1..3 중 2가 비어 gap 1로 잡혀야 한다.
    (tmp_path / "20260813" / "005930.10.jsonl.gz").unlink()

    cap2 = _new_capture(tmp_path)
    chunks = cap2._chunk_metadata()
    assert cap2._seq_gaps(chunks) == 1


async def test_resume_runs_off_the_event_loop(mem, tmp_path):
    """복원 스캔은 호출 스레드를 막지 않는다(장중 재시작 시 스탑 감시가 먼저다)."""
    cap1 = _new_capture(tmp_path)
    cap1.start()
    for i in range(5):
        cap1.enqueue(_tick(i, second=f"{10 + i:02d}"))
    await cap1.finalize("PROCESS_SHUTDOWN", reached_expected_close=False)

    cap2 = _new_capture(tmp_path)
    cap2.start()
    # start() 직후에는 아직 복원 전이다 — 스캔이 인라인이었다면 이미 끝나 있다.
    assert cap2._resumed is False
    cap2.enqueue(_tick(9, minute="12", second="00"))
    await cap2.finalize("COMPLETE", reached_expected_close=True)

    # 복원을 기다린 뒤 드레인하므로 seq는 겹치지 않고 이어진다.
    seqs = [r["seq"] for r in _read_all_rows(tmp_path)]
    assert seqs == [1, 2, 3, 4, 5, 6]


async def test_soft_limit_warning_logged_without_dropping_data(mem, tmp_path, monkeypatch):
    """계획 §용량: 한도 초과 시 기록을 버리지 않고 경고와 실제 용량만 남긴다."""
    events = []
    monkeypatch.setattr(tc, "log", lambda event, **kw: events.append((event, kw)))
    monkeypatch.setattr(tc, "SOFT_LIMIT_MB", 0.000001)  # 사실상 항상 초과

    cap = _new_capture(tmp_path)
    cap.start()
    cap.enqueue(_tick(0))
    await cap.finalize("COMPLETE", reached_expected_close=True)

    warned = [kw for event, kw in events if event == "TICK_CAPTURE_SOFT_LIMIT_EXCEEDED"]
    assert len(warned) == 1
    assert warned[0]["compressed_bytes"] > 0  # 실제 용량을 그대로 남긴다
    # 경고만 남기고 데이터·완전성 판정은 그대로 둔다.
    assert len(_read_all_rows(tmp_path)) == 1
    m = await db.get_price_path_manifest("20260813", "005930", "baseline-x")
    assert m["data_complete"] == 1
