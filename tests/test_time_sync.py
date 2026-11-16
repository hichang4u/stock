"""시각 오차 점검(`check_ntp`)의 로그·알림 분기.

2026-11-13 08:30 `TIME_SYNC_WARN`이 CRIT(1,953.6ms)으로 찍혔는데 텔레그램 알림이 없었다.
`check_ntp`가 로그만 남기고 notifier를 부르지 않았기 때문이다
(docs/CLOCK_DRIFT_FOLLOWUP_20261113.md). 외부 NTP 조회와 텔레그램 전송은 모두 가짜로 바꾼다.
"""

import asyncio
from types import SimpleNamespace

import pytest

from src import live, notifier
from src.utils import time_sync


class _FakeNtp:
    """`ntplib.NTPClient().request()`를 대신한다. 서버별 응답(초) 또는 예외를 돌려준다."""

    def __init__(self, replies):
        self.replies = dict(replies)
        self.asked: list[str] = []

    def install(self, monkeypatch):
        fake = self

        class _Client:
            def request(self, server, version=3):
                fake.asked.append(server)
                reply = fake.replies[server]
                if isinstance(reply, Exception):
                    raise reply
                return SimpleNamespace(offset=reply)

        monkeypatch.setattr(time_sync.ntplib, "NTPClient", _Client)
        return self


@pytest.fixture
def harness(monkeypatch):
    """로그를 모으고, 알림 큐를 새로 만들고, 단조 시계를 손으로 돌린다."""
    logs: list[tuple[str, dict]] = []
    clock = SimpleNamespace(now=1000.0)

    def fake_log(event, level="INFO", **fields):
        logs.append((event, {"level": level, **fields}))

    monkeypatch.setattr(time_sync, "log", fake_log)
    monkeypatch.setattr(time_sync.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(time_sync, "_crit_alerted_at", None)
    monkeypatch.setattr(notifier, "_queue", asyncio.Queue())
    monkeypatch.setattr(live, "ntp_level", "OK")
    monkeypatch.setattr(live, "ntp_offset_ms", -1.0)
    return SimpleNamespace(logs=logs, clock=clock)


def _queued() -> list[str]:
    items = []
    while not notifier._queue.empty():
        items.append(notifier._queue.get_nowait())
    return items


def _events(harness, name):
    return [fields for event, fields in harness.logs if event == name]


def test_crit_offset_queues_one_telegram_alert(harness, monkeypatch):
    _FakeNtp({"ntp.test": 1.9536}).install(monkeypatch)

    offset = time_sync.check_ntp(["ntp.test"])

    assert offset == pytest.approx(1953.6)
    assert live.ntp_level == "CRIT"
    assert live.ntp_offset_ms == 1953.6
    (entry,) = _events(harness, "TIME_SYNC_WARN")
    assert entry["level"] == "CRIT"
    assert entry["offset_ms"] == 1953.6
    assert entry["signed_offset_ms"] == 1953.6
    assert entry["alert"] == "QUEUED"
    (text,) = _queued()
    assert text == "\n".join([
        "[긴급] 시스템 시각 오차",
        "종목 : -",
        "내용 : 시스템 시각 오차 1954ms(로컬 시계가 늦다). 확인 필요.",
    ])


def test_crit_alert_says_fast_when_local_clock_is_ahead(harness, monkeypatch):
    _FakeNtp({"ntp.test": -0.75}).install(monkeypatch)

    offset = time_sync.check_ntp(["ntp.test"])

    assert offset == pytest.approx(750.0)
    (entry,) = _events(harness, "TIME_SYNC_WARN")
    assert entry["offset_ms"] == 750.0
    assert entry["signed_offset_ms"] == -750.0
    (text,) = _queued()
    assert "내용 : 시스템 시각 오차 750ms(로컬 시계가 빠르다). 확인 필요." in text


def test_warn_offset_logs_without_alert(harness, monkeypatch):
    _FakeNtp({"ntp.test": 0.3}).install(monkeypatch)

    offset = time_sync.check_ntp(["ntp.test"])

    assert offset == pytest.approx(300.0)
    assert live.ntp_level == "WARN"
    (entry,) = _events(harness, "TIME_SYNC_WARN")
    assert entry["level"] == "WARN"
    assert "alert" not in entry
    assert _queued() == []


def test_ok_offset_logs_without_alert(harness, monkeypatch):
    _FakeNtp({"ntp.test": 0.0024}).install(monkeypatch)

    offset = time_sync.check_ntp(["ntp.test"])

    assert offset == pytest.approx(2.4)
    assert live.ntp_level == "OK"
    (entry,) = _events(harness, "TIME_SYNC_OK")
    assert entry["offset_ms"] == 2.4
    assert _events(harness, "TIME_SYNC_WARN") == []
    assert _queued() == []


@pytest.mark.parametrize(
    ("offset_sec", "level", "alerts"),
    [(0.2, "OK", 0), (0.2001, "WARN", 0), (0.5, "WARN", 0), (0.5001, "CRIT", 1)],
)
def test_thresholds_are_unchanged(harness, monkeypatch, offset_sec, level, alerts):
    """200ms·500ms 경계는 그대로다. 알림은 CRIT에서만 나간다."""
    _FakeNtp({"ntp.test": offset_sec}).install(monkeypatch)

    time_sync.check_ntp(["ntp.test"])

    assert live.ntp_level == level
    assert len(_queued()) == alerts


def test_repeated_crit_within_cooldown_alerts_once(harness, monkeypatch):
    """재시작이 잇따르거나 점검이 겹쳐도 같은 CRIT을 쏟아내지 않는다."""
    _FakeNtp({"ntp.test": 2.0}).install(monkeypatch)

    time_sync.check_ntp(["ntp.test"])
    harness.clock.now += 60.0
    time_sync.check_ntp(["ntp.test"])
    harness.clock.now += time_sync.CRIT_ALERT_COOLDOWN_SEC - 61.0
    time_sync.check_ntp(["ntp.test"])

    assert [e["alert"] for e in _events(harness, "TIME_SYNC_WARN")] == [
        "QUEUED", "SUPPRESSED", "SUPPRESSED",
    ]
    assert all(e["level"] == "CRIT" for e in _events(harness, "TIME_SYNC_WARN"))
    assert len(_queued()) == 1


def test_crit_alerts_again_after_cooldown(harness, monkeypatch):
    """08:30 일일 점검은 24시간 간격이라 시계가 틀린 채면 매일 알린다."""
    _FakeNtp({"ntp.test": 2.0}).install(monkeypatch)

    time_sync.check_ntp(["ntp.test"])
    harness.clock.now += 24 * 60 * 60.0
    time_sync.check_ntp(["ntp.test"])

    assert [e["alert"] for e in _events(harness, "TIME_SYNC_WARN")] == ["QUEUED", "QUEUED"]
    assert len(_queued()) == 2


def test_recovery_rearms_the_alert(harness, monkeypatch):
    """정상(200ms 이하)으로 돌아오면 다음 CRIT은 쿨다운 안이어도 바로 알린다."""
    ntp = _FakeNtp({"ntp.test": 2.0}).install(monkeypatch)

    time_sync.check_ntp(["ntp.test"])
    harness.clock.now += 60.0
    ntp.replies["ntp.test"] = 0.001
    time_sync.check_ntp(["ntp.test"])
    harness.clock.now += 60.0
    ntp.replies["ntp.test"] = 2.5
    time_sync.check_ntp(["ntp.test"])

    assert live.ntp_level == "CRIT"
    assert [e["alert"] for e in _events(harness, "TIME_SYNC_WARN")] == ["QUEUED", "QUEUED"]
    assert len(_queued()) == 2


def test_warn_between_crits_does_not_rearm_the_alert(harness, monkeypatch):
    """오차가 500ms 언저리를 오가도 CRIT마다 다시 알리지 않는다. 정상으로 돌아와야 풀린다."""
    ntp = _FakeNtp({"ntp.test": 0.52}).install(monkeypatch)

    time_sync.check_ntp(["ntp.test"])
    harness.clock.now += 60.0
    ntp.replies["ntp.test"] = 0.48
    time_sync.check_ntp(["ntp.test"])
    harness.clock.now += 60.0
    ntp.replies["ntp.test"] = 0.53
    time_sync.check_ntp(["ntp.test"])

    assert [(e["level"], e.get("alert")) for e in _events(harness, "TIME_SYNC_WARN")] == [
        ("CRIT", "QUEUED"), ("WARN", None), ("CRIT", "SUPPRESSED"),
    ]
    assert len(_queued()) == 1


def test_alert_failure_does_not_break_the_check_and_is_retried(harness, monkeypatch):
    """알림 큐가 예외를 내도 점검 결과와 로그는 남고, 다음 점검에서 쿨다운 없이 다시 시도한다."""
    _FakeNtp({"ntp.test": 2.0}).install(monkeypatch)
    real_send_nowait = notifier.send_nowait
    broken = SimpleNamespace(on=True, calls=0)

    def flaky(event, level="info", message="", ticker=None, name=None):
        broken.calls += 1
        if broken.on:
            raise RuntimeError("queue broken")
        return real_send_nowait(event, level, message, ticker=ticker, name=name)

    monkeypatch.setattr(notifier, "send_nowait", flaky)

    offset = time_sync.check_ntp(["ntp.test"])

    assert offset == pytest.approx(2000.0)
    assert live.ntp_level == "CRIT"
    (entry,) = _events(harness, "TIME_SYNC_WARN")
    assert entry["level"] == "CRIT"
    assert entry["alert"] == "FAILED"
    (failed,) = _events(harness, "TIME_SYNC_ALERT_FAILED")
    assert failed["level"] == "WARN"
    assert "queue broken" in failed["error"]
    assert _queued() == []

    broken.on = False
    harness.clock.now += 60.0
    time_sync.check_ntp(["ntp.test"])

    assert broken.calls == 2
    assert [e["alert"] for e in _events(harness, "TIME_SYNC_WARN")] == ["FAILED", "QUEUED"]
    assert len(_queued()) == 1


def test_falls_back_to_next_server_and_alerts_on_its_offset(harness, monkeypatch):
    ntp = _FakeNtp({"down.test": OSError("timed out"), "up.test": 1.2}).install(monkeypatch)

    offset = time_sync.check_ntp(["down.test", "up.test"])

    assert offset == pytest.approx(1200.0)
    assert ntp.asked == ["down.test", "up.test"]
    (fallback,) = _events(harness, "TIME_SYNC_FALLBACK")
    assert fallback["ntp_server"] == "down.test"
    (entry,) = _events(harness, "TIME_SYNC_WARN")
    assert entry["ntp_server"] == "up.test"
    assert len(_queued()) == 1


def test_all_servers_failing_keeps_existing_behaviour(harness, monkeypatch):
    """조회 실패는 지금처럼 CRIT 로그만 남긴다. 알림 정책(PRD)은 오차 500ms 초과뿐이다."""
    _FakeNtp({"a.test": OSError("no route"), "b.test": OSError("timed out")}).install(monkeypatch)

    offset = time_sync.check_ntp(["a.test", "b.test"])

    assert offset == -1.0
    assert live.ntp_level == "ERROR"
    assert live.ntp_offset_ms == -1.0
    (entry,) = _events(harness, "TIME_SYNC_ERROR")
    assert entry["level"] == "CRIT"
    assert "timed out" in entry["error"]
    assert _queued() == []


def test_query_failure_does_not_rearm_a_pending_crit(harness, monkeypatch):
    """조회 실패는 회복이 아니다. 쿨다운을 풀지 않는다."""
    ntp = _FakeNtp({"ntp.test": 2.0}).install(monkeypatch)

    time_sync.check_ntp(["ntp.test"])
    harness.clock.now += 60.0
    ntp.replies["ntp.test"] = OSError("timed out")
    time_sync.check_ntp(["ntp.test"])
    harness.clock.now += 60.0
    ntp.replies["ntp.test"] = 2.0
    time_sync.check_ntp(["ntp.test"])

    assert [e["alert"] for e in _events(harness, "TIME_SYNC_WARN")] == ["QUEUED", "SUPPRESSED"]
    assert len(_queued()) == 1


def test_check_ntp_only_enqueues_and_never_sends(harness, monkeypatch):
    """점검은 큐에 넣기만 한다. 전송(네트워크)은 notifier 워커가 한다."""
    import httpx

    def no_network(*args, **kwargs):
        raise AssertionError("check_ntp must not open an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    _FakeNtp({"ntp.test": 2.0}).install(monkeypatch)

    time_sync.check_ntp(["ntp.test"])

    assert len(_queued()) == 1


@pytest.mark.asyncio
async def test_queued_crit_reaches_the_worker_delivery(harness, monkeypatch):
    """`check_ntp`가 넣은 알림을 워커가 꺼내 전송 함수에 그대로 넘긴다(전송은 가짜)."""
    _FakeNtp({"ntp.test": 1.9536}).install(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat")
    monkeypatch.setattr(notifier, "_SEND_INTERVAL", 0)
    delivered: list[str] = []

    async def fake_deliver(client_factory, url, chat_id, text, *, token="", sleep=asyncio.sleep):
        delivered.append(text)
        return True

    monkeypatch.setattr(notifier, "_deliver", fake_deliver)

    time_sync.check_ntp(["ntp.test"])
    worker = asyncio.create_task(notifier.worker())
    try:
        await asyncio.wait_for(notifier._queue.join(), timeout=2)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    assert len(delivered) == 1
    assert delivered[0].startswith("[긴급] 시스템 시각 오차")
