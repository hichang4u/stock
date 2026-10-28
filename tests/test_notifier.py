import asyncio

import pytest

from src import notifier


def test_alert_text_is_simple_three_line_format():
    text = notifier._format_alert_text(
        "ENTRY_EXECUTED",
        level="INFO",
        message="진입: 005930 10주 @ 75,000원",
        ticker="005930",
        name="삼성전자",
    )

    assert text == "\n".join([
        "[알림] 매수 체결",
        "종목 : 005930 삼성전자",
        "내용 : 진입: 005930 10주 @ 75,000원",
    ])


def test_unknown_alert_uses_event_title_and_empty_stock():
    text = notifier._format_alert_text("SOME_NEW_EVENT", level="WARN")

    assert text == "\n".join([
        "[확인] Some New Event",
        "종목 : -",
        "내용 : Some New Event",
    ])


def test_format_stock_accepts_code_only_and_name_only():
    code_only = notifier._format_alert_text(
        "TRAILING_STOP",
        level="INFO",
        message="TRAILING 청산",
        ticker="005930",
    )
    name_only = notifier._format_alert_text(
        "TRAILING_STOP",
        level="INFO",
        message="TRAILING 청산",
        name="삼성전자",
    )

    assert "종목 : 005930" in code_only
    assert "종목 : 삼성전자" in name_only


@pytest.mark.asyncio
async def test_send_filters_non_actionable_alerts():
    notifier._queue = asyncio.Queue()

    await notifier.send("TARGET_LOCKED", level="INFO", message="target locked")
    await notifier.send("F2_FAIL_F1_RETRY", level="WARN", message="retrying")

    assert notifier._queue.empty()


@pytest.mark.asyncio
async def test_send_allows_trade_no_trade_and_failure_alerts():
    notifier._queue = asyncio.Queue()

    await notifier.send("ENTRY_EXECUTED", level="INFO", message="buy filled", ticker="005930")
    await notifier.send("TRAILING_STOP", level="INFO", message="sell filled", ticker="005930")
    await notifier.send("ENTRY_FAIL", level="WARN", message="entry failed", ticker="005930")
    await notifier.send("NO_TARGET", level="INFO", message="no target")
    await notifier.send("VI_FILTER_ALL_EXCLUDED", level="WARN", message="vi excluded")
    await notifier.send("GAP_CHANGED", level="WARN", message="gap changed", ticker="005930")
    await notifier.send("F2_RETRY_EXHAUSTED", level="WARN", message="no final target")
    await notifier.send("TIMEOUT_ORDER_FAILED", level="CRIT", message="manual close required")

    assert notifier._queue.qsize() == 8


@pytest.mark.asyncio
async def test_send_allows_error_level_alerts():
    notifier._queue = asyncio.Queue()

    await notifier.send("SOME_ERROR", level="ERROR", message="boom")

    assert notifier._queue.qsize() == 1


@pytest.mark.asyncio
async def test_process_restart_alert_requires_critical_level():
    notifier._queue = asyncio.Queue()

    await notifier.send("PROCESS_RESTART_DETECTED", level="WARN", message="restart")
    assert notifier._queue.empty()

    await notifier.send("PROCESS_RESTART_DETECTED", level="CRIT", message="restart")
    assert notifier._queue.qsize() == 1


def test_notifier_accepts_legacy_and_standard_error_levels():
    assert notifier._should_send_alert("SOME_ERROR", level="CRIT") is True
    assert notifier._should_send_alert("SOME_ERROR", level="error") is True
    assert notifier._should_send_alert("SOME_WARNING", level="WARN") is False


@pytest.mark.asyncio
async def test_market_closed_alert_passes_filter_and_reaches_queue():
    notifier._queue = asyncio.Queue()

    await notifier.send(
        "MARKET_CLOSED", level="INFO", message="휴장일 감지(20260712). 당일 거래 없음."
    )

    assert notifier._queue.qsize() == 1
    text = notifier._queue.get_nowait()
    assert "휴장일" in text


def test_format_stock_infers_current_target_name():
    from src import state

    state.get().target_ticker = "005930"
    state.get().target_name = "삼성전자"
    try:
        assert notifier._format_stock("005930") == "005930 삼성전자"
        assert notifier._format_stock("000660") == "000660"
    finally:
        state.get().target_ticker = None
        state.get().target_name = None


@pytest.mark.asyncio
async def test_send_allows_vi_entry_blocked_alert():
    notifier._queue = asyncio.Queue()

    await notifier.send("VI_ENTRY_BLOCKED", level="WARN", message="vi active", ticker="072770")

    assert notifier._queue.qsize() == 1


# ── 전송 실패 기록·다음 알림에 덧붙이기 (docs/NOTIFICATION_LOSS_FOLLOWUP_20261026.md 나·라) ──
# 2026-10-26: 응답 타임아웃 3회로 NOTIFICATION_FAILED(필드 없음)가 남았지만, 사용자는 같은 매수 체결
# 알림을 3번 받았다. 응답만 늦고 메시지는 도착했을 수 있다 — 그래서 "실패"와 "확인 못 함"을 가른다.

import httpx  # noqa: E402

TOKEN = "123456:SECRET-token"
URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
ALERT = notifier._format_alert_text(
    "ENTRY_EXECUTED", "info", "체결가 9,565원", ticker="396300", name="HT로보틱스"
)


def _factory(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _no_sleep(_sec):
    return None


@pytest.fixture
def events(monkeypatch):
    out = []
    monkeypatch.setattr(notifier, "log", lambda event, **kw: out.append((event, kw)))
    notifier._unconfirmed.clear()
    yield out
    notifier._unconfirmed.clear()


async def _deliver(handler, text=ALERT):
    return await notifier._deliver(
        _factory(handler), URL, "chat", text, token=TOKEN, sleep=_no_sleep
    )


async def test_read_timeouts_log_alert_and_error_types(events):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("", request=request)

    assert await _deliver(handler) is False
    assert len(calls) == 3  # 재시도 횟수는 그대로
    [(event, kw)] = [e for e in events if e[0] == "NOTIFICATION_FAILED"]
    assert kw["alert"] == "[알림] 매수 체결 · 396300 HT로보틱스"
    assert kw["attempts"] == 3
    assert kw["errors"] == ["ReadTimeout", "ReadTimeout", "ReadTimeout"]
    assert kw["error"] == "ReadTimeout('')"
    assert kw["delivery"] == "UNCONFIRMED"


async def test_rejected_response_is_logged_with_status(events):
    def handler(request):
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})

    assert await _deliver(handler) is False
    [(event, kw)] = [e for e in events if e[0] == "NOTIFICATION_FAILED"]
    assert kw["status"] == 400
    assert "chat not found" in kw["body"]
    assert kw["attempts"] == 1  # 거부는 재시도하지 않는다(기존 동작)
    assert kw["delivery"] == "REJECTED"


async def test_rate_limited_three_times_is_logged(events):
    def handler(request):
        return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 1}})

    assert await _deliver(handler) is False
    [(event, kw)] = [e for e in events if e[0] == "NOTIFICATION_FAILED"]
    assert kw["status"] == 429 and kw["attempts"] == 3
    assert kw["delivery"] == "REJECTED"


async def test_bot_token_never_reaches_the_log(events):
    def handler(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    await _deliver(handler)
    [(event, kw)] = [e for e in events if e[0] == "NOTIFICATION_FAILED"]
    assert TOKEN not in repr(kw)
    assert "***" in kw["error"]


async def test_next_alert_carries_the_failed_ones_then_clears(events):
    def timeout(request):
        raise httpx.ReadTimeout("", request=request)

    def reject(request):
        return httpx.Response(400, json={"ok": False})

    await _deliver(timeout)
    other = notifier._format_alert_text("TRAILING_STOP", "info", "청산", ticker="396300",
                                        name="HT로보틱스")
    await _deliver(reject, text=other)

    sent = []

    def ok(request):
        sent.append(request.read().decode("utf-8"))
        return httpx.Response(200, json={"ok": True})

    nxt = notifier._format_alert_text("NO_TARGET", "info", "오늘 진입 없음")
    assert await _deliver(ok, text=nxt) is True
    body = sent[0].encode().decode("unicode_escape") if "\\u" in sent[0] else sent[0]
    assert "앞선 알림 2건" in body
    assert "[알림] 매수 체결 · 396300 HT로보틱스 (확인 못 함 — 도착·중복 가능)" in body
    assert "[알림] 매도 체결 · 396300 HT로보틱스 (거부됨)" in body
    assert notifier._unconfirmed == []
    assert [e for e in events if e[0] == "NOTIFICATION_UNCONFIRMED_REPORTED"][0][1]["count"] == 2

    sent.clear()
    assert await _deliver(ok, text=nxt) is True
    assert "앞선 알림" not in sent[0]  # 한 번 알렸으면 다시 붙이지 않는다


async def test_note_lists_at_most_three_alerts(events):
    def timeout(request):
        raise httpx.ReadTimeout("", request=request)

    for i in range(5):
        await _deliver(timeout, text=notifier._format_alert_text("ENTRY_FAIL", "warn", f"#{i}"))

    sent = []

    def ok(request):
        sent.append(request.read().decode("utf-8"))
        return httpx.Response(200, json={"ok": True})

    await _deliver(ok, text=ALERT)
    body = sent[0].encode().decode("unicode_escape") if "\\u" in sent[0] else sent[0]
    assert "앞선 알림 5건" in body
    assert body.count("[확인] 진입 실패") == 3
    assert "외 2건" in body


async def test_success_logs_nothing(events):
    def ok(request):
        return httpx.Response(200, json={"ok": True})

    assert await _deliver(ok) is True
    assert events == []


# 2026-10-28: 마감 청산 알림이 2번 왔는데 로그에 아무것도 없었다. 1회차가 응답 타임아웃
# (메시지는 도착)이고 2회차가 성공하면 지금까지는 흔적이 남지 않았다
# (docs/NOTIFICATION_LOSS_FOLLOWUP_20261026.md 8절 아).


def _fail_then_ok(exc_type, fails=1):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) <= fails:
            raise exc_type("", request=request)
        return httpx.Response(200, json={"ok": True})

    return handler, calls


async def test_success_after_a_read_timeout_is_logged_as_possible_duplicate(events):
    handler, calls = _fail_then_ok(httpx.ReadTimeout)
    assert await _deliver(handler) is True
    assert len(calls) == 2
    [(event, kw)] = events
    assert event == "NOTIFICATION_RETRIED"
    assert kw["alert"] == "[알림] 매수 체결 · 396300 HT로보틱스"
    assert kw["attempts"] == 2
    assert kw["errors"] == ["ReadTimeout"]
    assert kw["possible_duplicate"] is True
    assert notifier._unconfirmed == []  # 성공했으니 미확인 목록에는 넣지 않는다


async def test_success_after_connect_errors_is_not_a_duplicate(events):
    """연결 자체가 안 됐으면 메시지는 닿지 않았다."""
    handler, _ = _fail_then_ok(httpx.ConnectError, fails=2)
    assert await _deliver(handler) is True
    [(event, kw)] = events
    assert event == "NOTIFICATION_RETRIED"
    assert kw["attempts"] == 3
    assert kw["errors"] == ["ConnectError", "ConnectError"]
    assert kw["possible_duplicate"] is False


async def test_success_after_rate_limit_is_not_a_duplicate(events):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 1}})
        return httpx.Response(200, json={"ok": True})

    assert await _deliver(handler) is True
    [(event, kw)] = events
    assert event == "NOTIFICATION_RETRIED"
    assert kw["attempts"] == 2
    assert kw["status"] == 429
    assert kw["possible_duplicate"] is False


def test_retried_event_has_a_label():
    from src.utils.logger import event_label

    assert event_label("NOTIFICATION_RETRIED") != "NOTIFICATION_RETRIED(NOTIFICATION_RETRIED)"
