"""오늘 화면 연결 상태·시세 표시의 회귀(2026-11-21 리뷰).

1. 토큰 최종 실패(TOKEN_REFRESH_FAIL)가 같은 날의 앞선 성공을 덮어야 한다. 시각 점검은
   TIME_SYNC_WARN이라도 수준이 CRIT(오차 500ms 이상)이면 경고가 아니라 위험이다.
2. /api/status는 봇의 매매 루프 위에서 돈다. 상태 로그 읽기는 작업 스레드에서, 붙은 줄만
   읽는다. 깨진 UTF-8·잘린 줄·객체 아닌 줄이 응답을 막지 않고, 동시 조회가 다른 날의 위치와
   판정을 섞지 않는다.
3. 화면 시세(observed_price)는 화면 종목의 틱만 쓴다. current_price는 호환 필드로 그대로 둔다.
"""
import json
import threading

import pytest

pytest.importorskip("fastapi")

import src.api.server as server  # noqa: E402
from src.api.status_logic import (  # noqa: E402
    health_from_logs,
    health_rows_from_bytes,
    merge_health,
)


def _ev(event, ts, level="INFO", **extra):
    return {"event": event, "ts": f"2026-11-20T{ts}+09:00", "level": level, **extra}


# ── 1. 판정 ──────────────────────────────────────────────────────────────


def test_token_terminal_failure_after_same_day_success_is_fail():
    health = health_from_logs([
        _ev("TOKEN_LOADED_FROM_CACHE", "08:30:00"),
        _ev("TOKEN_REFRESH_ATTEMPT_FAIL", "09:09:50", "WARN"),  # 시도별 실패는 결과가 아니다
        _ev("TOKEN_REFRESH_FAIL", "09:10:00", "CRIT"),
    ])
    assert health["token"]["status"] == "fail"
    assert health["token"]["event"] == "TOKEN_REFRESH_FAIL"


def test_token_success_after_failure_recovers():
    health = health_from_logs([
        _ev("TOKEN_REFRESH_FAIL", "09:10:00", "CRIT"),
        _ev("TOKEN_REFRESHED", "09:20:00"),
    ])
    assert health["token"]["status"] == "ok"


def test_attempt_failures_alone_do_not_replace_last_result():
    health = health_from_logs([
        _ev("TOKEN_REFRESHED", "08:30:00"),
        _ev("TOKEN_REFRESH_HTTP_ERR", "09:00:00", "WARN"),
    ])
    assert health["token"]["status"] == "ok"


@pytest.mark.parametrize("level,expected", [
    ("WARN", "warn"), ("warning", "warn"), ("CRIT", "crit"), ("critical", "crit"),
    ("CRITICAL", "crit"), ("error", "crit"), ("ERROR", "crit"),
])
def test_time_sync_warn_severity_follows_level(level, expected):
    health = health_from_logs([_ev("TIME_SYNC_WARN", "08:40:00", level, offset_ms=620)])
    assert health["ntp"]["status"] == expected


def test_time_sync_error_and_recovery():
    only_error = health_from_logs([_ev("TIME_SYNC_ERROR", "08:40:00", "CRIT")])
    assert only_error["ntp"]["status"] == "error"
    health = health_from_logs([
        _ev("TIME_SYNC_WARN", "08:40:00", "CRIT", offset_ms=620),
        _ev("TIME_SYNC_OK", "08:50:00", offset_ms=4),
    ])
    assert health["ntp"]["status"] == "ok"


def test_merge_keeps_older_kinds_and_takes_newer():
    old = health_from_logs([_ev("TOKEN_REFRESHED", "08:30:00"), _ev("TIME_SYNC_OK", "08:30:01")])
    new = health_from_logs([_ev("TOKEN_REFRESH_FAIL", "09:10:00", "CRIT")])
    merged = merge_health(old, new)
    assert merged["token"]["status"] == "fail"
    assert merged["ntp"]["status"] == "ok"


def test_rows_from_bytes_skips_malformed_partial_and_non_objects():
    chunk = (
        b'{"event":"TOKEN_REFRESHED","ts":"x"}\n'
        b'\xff\xfe{"event":"TOKEN_REFRESH_FAIL"}\n'        # 깨진 UTF-8
        b'{"event":"TIME_SYNC_OK","ts":\n'                   # 잘린 줄
        b'["TOKEN_REFRESH_FAIL"]\n'                          # 객체 아님
        b'"TOKEN_REFRESH_FAIL"\n'                            # 문자열
        b'{"event":"TICK"}\n'                                # 상태 사건 아님(표시만 없음)
    )
    rows = health_rows_from_bytes(chunk)
    assert [r["event"] for r in rows] == ["TOKEN_REFRESHED"]
    assert health_from_logs([None, [], "x", 3, *rows])["token"]["status"] == "ok"


# ── 2. 이어 읽기·작업 스레드·동시성 ────────────────────────────────────────


@pytest.fixture
def log_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_LOG_DIR", tmp_path)
    today = {"v": "20261120"}
    monkeypatch.setattr(server, "_today", lambda: today["v"])
    server._HEALTH_READER.reset()
    yield tmp_path, today
    server._HEALTH_READER.reset()


def _line(obj) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def test_reader_reads_only_appended_complete_lines(log_dir):
    tmp, _ = log_dir
    path = tmp / "20261120.jsonl"
    path.write_bytes(_line(_ev("TOKEN_LOADED_FROM_CACHE", "08:30:00")))
    assert server._health_snapshot()["token"]["status"] == "ok"

    # 쓰는 중인 마지막 줄(줄바꿈 전)은 아직 읽지 않는다.
    partial = _line(_ev("TOKEN_REFRESH_FAIL", "09:10:00", "CRIT"))
    with open(path, "ab") as fh:
        fh.write(partial[:-10])
    assert server._health_snapshot()["token"]["status"] == "ok"
    with open(path, "ab") as fh:
        fh.write(partial[-10:])
    assert server._health_snapshot()["token"]["status"] == "fail"

    # 깨진 줄이 붙어도 응답은 계속되고, 뒤의 성공이 복구한다.
    with open(path, "ab") as fh:
        fh.write(b'\xff\xfe"TOKEN_ broken\n[1,2]\n' + _line(_ev("TOKEN_REFRESHED", "09:20:00")))
    assert server._health_snapshot()["token"]["status"] == "ok"


def test_reader_restarts_on_truncation_and_day_change(log_dir):
    tmp, today = log_dir
    path = tmp / "20261120.jsonl"
    path.write_bytes(_line(_ev("TIME_SYNC_WARN", "08:40:00", "CRIT", offset_ms=620)) * 3)
    assert server._health_snapshot()["ntp"]["status"] == "crit"
    path.write_bytes(_line(_ev("TIME_SYNC_OK", "08:50:00", offset_ms=4)))  # 더 짧게 교체
    assert server._health_snapshot()["ntp"]["status"] == "ok"

    (tmp / "20261121.jsonl").write_bytes(
        _line({"event": "TOKEN_REFRESHED", "ts": "2026-11-21T08:30:00+09:00"}))
    today["v"] = "20261121"
    snap = server._health_snapshot()
    assert snap["log_date"] == "20261121"
    assert snap["ntp"] is None  # 전날 판정이 새 날로 넘어오지 않는다
    assert snap["token"]["ts"].startswith("2026-11-21")


def test_concurrent_snapshots_never_mix_day_and_value(log_dir):
    tmp, today = log_dir
    day1, day2 = tmp / "20261120.jsonl", tmp / "20261121.jsonl"
    day1.write_bytes(b"")
    errors: list[str] = []
    stop = threading.Event()

    def writer():
        for i in range(400):
            with open(day1, "ab") as fh:
                ts = f"2026-11-20T08:{i // 60:02d}:{i % 60:02d}+09:00"
                fh.write(_line({"event": "TIME_SYNC_OK", "ts": ts, "offset_ms": i}))
            if i == 200:
                day2.write_bytes(_line({"event": "TIME_SYNC_OK", "ts": "2026-11-21T08:00:00+09:00",
                                        "offset_ms": 9999}))
                today["v"] = "20261121"
        stop.set()

    def reader():
        last = -1
        while not stop.is_set():
            snap = server._health_snapshot()
            ntp = snap["ntp"]
            if ntp is None:
                continue
            day = ntp["ts"][:10].replace("-", "")
            if day != snap["log_date"]:
                errors.append(f"mixed {snap['log_date']} with {ntp['ts']}")
            if day == "20261120":
                if ntp["offset_ms"] < last:
                    errors.append(f"went back {last} -> {ntp['offset_ms']}")
                last = ntp["offset_ms"]

    threads = [threading.Thread(target=reader) for _ in range(6)]
    threads.append(threading.Thread(target=writer))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors[:5]
    final = server._health_snapshot()
    assert final["log_date"] == "20261121" and final["ntp"]["offset_ms"] == 9999


@pytest.mark.asyncio
async def test_status_reads_health_off_the_event_loop_thread(monkeypatch):
    s = server.state.get()
    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: [])
    monkeypatch.setattr(s, "position_status", "IDLE")
    monkeypatch.setattr(s, "target_ticker", None)
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def fake_snapshot():
        seen.append(threading.get_ident())
        return {"log_date": None, "token": None, "ntp": None, "ws": None}

    monkeypatch.setattr(server, "_health_snapshot", fake_snapshot)
    payload = json.loads((await server.api_status()).body.decode("utf-8"))
    assert seen and seen[0] != loop_thread
    assert payload["health"]["log_date"] is None


@pytest.mark.asyncio
async def test_status_survives_health_reader_failure(monkeypatch):
    s = server.state.get()
    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: [])
    monkeypatch.setattr(s, "position_status", "IDLE")
    monkeypatch.setattr(s, "target_ticker", None)

    def boom():
        raise OSError("disk gone")

    monkeypatch.setattr(server, "_health_snapshot", boom)
    payload = json.loads((await server.api_status()).body.decode("utf-8"))
    assert payload["health"]["error"] is True
    assert payload["health"]["token"] is None


# ── 3. 화면 시세는 화면 종목의 틱만 ─────────────────────────────────────────


def test_observed_quote_uses_only_matching_ticker(monkeypatch):
    monkeypatch.setattr(server.live, "last_tick_price", 50_000.0)
    monkeypatch.setattr(server.live, "last_tick_ticker", "005930")
    rows = [{"ts": "2026-11-20T15:19:58+09:00", "ticker": "123690", "price": 6420.0}]
    assert server._observed_quote("123690", rows) == (6420.0, "2026-11-20T15:19:58+09:00")
    # 틱 이력이 없을 때 마지막 틱이 다른 종목이면 쓰지 않는다
    assert server._observed_quote("123690", []) == (None, None)
    monkeypatch.setattr(server.live, "last_tick_ticker", "123690")
    assert server._observed_quote("123690", []) == (50_000.0, None)
    assert server._observed_quote(None, []) == (None, None)


@pytest.mark.asyncio
async def test_status_observed_price_ignores_other_ticker_last_tick(monkeypatch):
    s = server.state.get()
    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: [])
    for key, value in {"position_status": "ENTERING", "target_ticker": "123690",
                       "entry_price": 6650.0, "entry_at": None}.items():
        monkeypatch.setattr(s, key, value)
    monkeypatch.setattr(server.live, "last_tick_price", 50_000.0)
    monkeypatch.setattr(server.live, "last_tick_ticker", "005930")
    payload = json.loads((await server.api_status()).body.decode("utf-8"))
    assert payload["current_price"] == 50_000.0  # 호환 필드는 그대로
    assert payload["observed_price"] is None
    assert payload["observed_last_at"] is None


# ── 2b. 읽기 실패는 앞서 읽은 판정을 "지금 값"으로 내지 않는다 ─────────────────


@pytest.mark.asyncio
async def test_read_failure_reports_error_then_retries_from_same_offset(log_dir, monkeypatch):
    import builtins

    tmp, _ = log_dir
    path = tmp / "20261120.jsonl"
    path.write_bytes(_line(_ev("TOKEN_REFRESHED", "08:30:00")))
    first = await server._health_snapshot_async()
    assert first["token"]["status"] == "ok" and "error" not in first

    with open(path, "ab") as fh:
        fh.write(_line(_ev("TOKEN_REFRESH_FAIL", "09:10:00", "CRIT")))
    real_open = builtins.open

    def denied(file, *args, **kwargs):
        if str(file) == str(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", denied)
    failed = await server._health_snapshot_async()
    assert failed["error"] is True and failed["error_type"] == "PermissionError"
    assert failed["token"] is None and failed["ntp"] is None  # 지난 ok를 지금 값처럼 내지 않는다
    assert failed["log_date"] == "20261120"

    monkeypatch.setattr(builtins, "open", real_open)
    retried = await server._health_snapshot_async()
    assert "error" not in retried
    assert retried["token"]["status"] == "fail"  # 같은 위치부터 다시 읽어 최종 실패를 본다


@pytest.mark.asyncio
async def test_listing_and_stat_failures_are_errors_but_missing_dir_is_not(log_dir, monkeypatch):
    tmp, _ = log_dir
    (tmp / "20261120.jsonl").write_bytes(_line(_ev("TOKEN_REFRESHED", "08:30:00")))
    real_stat = type(tmp).stat

    def stat_denied(self, *args, **kwargs):
        if self.name == "20261120.jsonl":
            raise PermissionError(13, "Permission denied")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(type(tmp), "stat", stat_denied)
    snap = await server._health_snapshot_async()
    assert snap["error"] is True and snap["token"] is None
    monkeypatch.setattr(type(tmp), "stat", real_stat)

    def list_denied(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(type(tmp), "iterdir", list_denied)
    snap = await server._health_snapshot_async()
    assert snap["error"] is True and snap["log_date"] is None
    monkeypatch.undo()

    monkeypatch.setattr(server, "_LOG_DIR", tmp / "missing")
    snap = await server._health_snapshot_async()
    assert "error" not in snap and snap == {"log_date": None, "token": None, "ntp": None,
                                            "ws": None}


def test_same_path_replacement_restarts_from_the_top(log_dir):
    tmp, _ = log_dir
    path = tmp / "20261120.jsonl"
    path.write_bytes(_line(_ev("TOKEN_REFRESH_FAIL", "09:10:00", "CRIT")))
    assert server._health_snapshot()["token"]["status"] == "fail"
    # 같은 이름의 새 파일로 교체(더 길게): 크기만으로는 못 알아챈다
    replacement = tmp / "new.tmp"
    # 새 파일의 성공 줄은 옛 읽은 위치 앞에 있다 — 이어 읽으면 놓친다.
    replacement.write_bytes(_line(_ev("TOKEN_REFRESHED", "09:20:00"))
                            + _line(_ev("TICK", "09:20:01", price=6650)) * 3)
    replacement.replace(path)
    snap = server._health_snapshot()
    if path.stat().st_ino:
        assert snap["token"]["status"] == "ok" and snap["token"]["ts"].endswith("09:20:00+09:00")
