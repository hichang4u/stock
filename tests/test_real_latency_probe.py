"""REAL 읽기 전용 지연 측정기(scripts/real_latency_probe.py).

**전부 모의 전송 계층이다. 이 테스트는 네트워크를 쓰지 않고 실제 서버를 부르지 않는다.**
자격증명·토큰·로그는 임시 디렉터리에 만든 가짜다.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import real_latency_probe as probe

KST = probe.KST
FAKE_KEY = "PSfakeAppKey0123456789"
FAKE_SECRET = "fakeAppSecret+/=abcdefghijklmnopqrstuvwxyz"
FAKE_TOKEN = "eyJfake.token.value-0123456789"
# 평일(수요일) 정규장, 보호 시간대 밖.
MIDDAY = datetime(2027, 1, 6, 10, 30, 0, tzinfo=KST)
PARAMS = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": "005930"}


class Clock:
    """가짜 시계. `sleep`은 기다리지 않고 시간만 민다."""

    def __init__(self, start: datetime = MIDDAY) -> None:
        self.start = start
        self.ns = 0
        self.sleeps: list[float] = []

    def monotonic_ns(self) -> int:
        return self.ns

    def now(self) -> datetime:
        return self.start + timedelta(microseconds=self.ns / 1_000)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.ns += int(seconds * 1e9)


def _body(quote_time="103000", rt_cd="0", msg_cd="MCA00000"):
    return json.dumps(
        {
            "rt_cd": rt_cd,
            "msg_cd": msg_cd,
            "msg1": "ok",
            "output1": {
                "aspr_acpt_hour": quote_time, "askp1": "70100", "bidp1": "70000",
                "askp_rsqn1": "1200", "bidp_rsqn1": "900", "total_askp_rsqn": "999999",
            },
            "output2": {"antc_cnpr": "70050"},
        }
    ).encode()


class FakeTransport:
    """정해 둔 응답을 순서대로 돌려주는 모의 전송 계층."""

    kind = "FAKE"

    def __init__(self, clock: Clock, script: list, *, rtt_ms: float = 40.0) -> None:
        self.clock = clock
        self.script = list(script)
        self.rtt_ms = rtt_ms
        self.opens = 0
        self.calls: list[tuple[str, dict]] = []
        self.call_started_ns: list[int] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.closed = 0

    def open(self) -> dict[str, float]:
        self.opens += 1
        step = self.script[0] if self.script else None
        if step == "connect-error":
            self.script.pop(0)
            raise ConnectionRefusedError("cannot connect")
        self.clock.ns += int(30e6)
        return {"dns_ms": 5.0, "tcp_ms": 10.0, "tls_ms": 15.0}

    def get(self, target: str, headers: dict[str, str]) -> probe.Reply:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.calls.append((target, dict(headers)))
        self.call_started_ns.append(self.clock.ns)
        try:
            step = self.script.pop(0) if self.script else (200, _body())
            if step == "network-error":
                # 실제 예외 메시지에 헤더가 섞여 나오는 최악의 경우를 흉내 낸다.
                raise TimeoutError(f"timed out sending {headers['authorization']}")
            status, body, *rest = step
            extra = rest[0] if rest else {}
            self.clock.ns += int(self.rtt_ms * 0.6e6)
            headers_at = self.clock.ns
            self.clock.ns += int(self.rtt_ms * 0.4e6)
            return probe.Reply(
                status=status, body=body, headers_at_ns=headers_at, done_at_ns=self.clock.ns,
                date_header="Wed, 06 Jan 2027 01:30:00 GMT", **extra,
            )
        finally:
            self.in_flight -= 1

    def close(self) -> None:
        self.closed += 1


SECRETS = probe.Secrets(app_key=FAKE_KEY, app_secret=FAKE_SECRET, token=FAKE_TOKEN)


def _probe(script, *, calls=4, interval=3.0, clock=None, rtt_ms=40.0):
    clock = clock or Clock()
    transport = FakeTransport(clock, script, rtt_ms=rtt_ms)
    samples, stop = probe.run_probe(
        transport, SECRETS, probe.make_plan(["005930", "000660"], calls, interval),
        monotonic_ns=clock.monotonic_ns, now=clock.now, sleep=clock.sleep,
    )
    return samples, stop, transport, clock


# ── 화이트리스트 ─────────────────────────────────────────────────────


def test_the_only_request_that_can_be_built_is_the_quote_get():
    probe.check_request("GET", probe.QUOTE_PATH, probe.QUOTE_TR_ID, PARAMS)

    assert probe.ALLOWED_REQUESTS == {
        ("GET", "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn", "FHKST01010200")
    }


@pytest.mark.parametrize(
    ("method", "path", "tr_id"),
    [
        ("POST", probe.QUOTE_PATH, probe.QUOTE_TR_ID),
        ("PUT", probe.QUOTE_PATH, probe.QUOTE_TR_ID),
        ("DELETE", probe.QUOTE_PATH, probe.QUOTE_TR_ID),
        ("POST", "/uapi/domestic-stock/v1/trading/order-cash", "TTTC0012U"),
        ("POST", "/uapi/domestic-stock/v1/trading/order-cash", "TTTC0011U"),
        ("POST", "/uapi/domestic-stock/v1/trading/order-rvsecncl", "TTTC0013U"),
        ("GET", "/uapi/domestic-stock/v1/trading/inquire-balance", "TTTC8434R"),
        ("GET", "/uapi/domestic-stock/v1/trading/inquire-psbl-order", "TTTC8908R"),
        ("GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", "TTTC0081R"),
        ("POST", "/oauth2/tokenP", ""),
        ("POST", "/oauth2/Approval", ""),
        ("GET", probe.QUOTE_PATH, "TTTC0012U"),
        ("GET", probe.QUOTE_PATH + "/../../trading/order-cash", probe.QUOTE_TR_ID),
        ("get", probe.QUOTE_PATH, probe.QUOTE_TR_ID),
    ],
)
def test_orders_account_queries_token_issue_and_other_methods_are_refused(method, path, tr_id):
    with pytest.raises(probe.ForbiddenRequest):
        probe.check_request(method, path, tr_id, PARAMS)


@pytest.mark.parametrize(
    "params",
    [
        {**PARAMS, "CANO": "12345678"},
        {**PARAMS, "ACNT_PRDT_CD": "01"},
        {"FID_COND_MRKT_DIV_CODE": "J"},
        {**PARAMS, "FID_INPUT_ISCD": "005930&CANO=1"},
        {**PARAMS, "FID_COND_MRKT_DIV_CODE": "NX"},
        {**PARAMS, "FID_INPUT_ISCD": "ABCDEF"},
    ],
)
def test_account_fields_or_odd_parameters_cannot_ride_along(params):
    with pytest.raises(probe.ForbiddenRequest):
        probe.check_request("GET", probe.QUOTE_PATH, probe.QUOTE_TR_ID, params)


def test_transport_only_knows_the_real_quote_host_and_one_verb():
    assert (probe.REAL_HOST, probe.REAL_PORT) == ("openapi.koreainvestment.com", 9443)
    for host, port in (
        ("openapivts.koreainvestment.com", 29443),
        ("openapi.koreainvestment.com", 443),
        ("evil.example.com", 9443),
    ):
        with pytest.raises(probe.ProbeRefused):
            probe.HttpsTransport(host, port)
    public = {name for name in dir(probe.HttpsTransport) if not name.startswith("_")}
    assert public == {"kind", "open", "get", "close"}
    # 생성만으로는 연결하지 않는다.
    assert probe.HttpsTransport()._conn is None


def test_module_is_isolated_from_the_bot_the_database_and_the_environment():
    """표준 라이브러리만 쓴다. 봇·DB·환경변수·프록시에 닿는 코드가 없고 쓰기 메서드도 없다.

    소스 글자가 아니라 구문 트리로 본다(설명문에 나오는 단어는 코드가 아니다).
    """
    tree = ast.parse(Path(probe.__file__).read_bytes().decode("utf-8"))
    modules: set[str] = set()
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))

    assert modules == {
        "__future__", "argparse", "collections.abc", "dataclasses", "datetime", "http.client",
        "json", "math", "pathlib", "re", "socket", "ssl", "sys", "time", "typing", "urllib.parse",
    }
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "os" not in names
    assert not attributes & {"environ", "getenv", "putenv", "urlopen", "set_tunnel"}
    strings = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]
    # 코드에 등장하는 메서드 문자열은 GET뿐이고, 주문·계좌·토큰 경로는 어디에도 없다.
    assert not {"POST", "PUT", "DELETE", "PATCH"} & set(strings)
    assert "GET" in strings
    for fragment in ("/trading/", "oauth2", "order-cash", "tokenP", "Approval"):
        assert not [text for text in strings if fragment in text]
    paths = [text for text in strings if text.startswith("/uapi/")]
    assert paths == [probe.QUOTE_PATH]


# ── 한도와 간격 ──────────────────────────────────────────────────────


def test_hard_limits_cannot_be_raised_from_the_command_line():
    assert (probe.HARD_MAX_CALLS, probe.HARD_MIN_INTERVAL_SEC) == (12, 3.0)
    assert probe.make_plan(["005930"], 12, 3.0).calls == 12
    out_of_range = ((13, 3.0), (0, 3.0), (100, 3.0), (12, 2.99), (12, 0.0), (12, float("nan")))
    for calls, interval in out_of_range:
        with pytest.raises(probe.ProbeRefused):
            probe.make_plan(["005930"], calls, interval)
    for tickers in ([], ["005930", "000660", "035420"], ["5930"], ["005930", "x"]):
        with pytest.raises(probe.ProbeRefused):
            probe.make_plan(tickers, 4, 3.0)


def test_plan_alternates_the_two_fixed_tickers():
    plan = probe.make_plan(list(probe.DEFAULT_TICKERS), 5, 3.0)

    assert [r["FID_INPUT_ISCD"] for r in plan.requests()] == [
        "005930", "000660", "005930", "000660", "005930",
    ]


def test_calls_are_sequential_and_start_at_least_three_seconds_apart():
    samples, stop, transport, clock = _probe([], calls=12)

    assert stop == "PLAN_COMPLETED" and len(samples) == len(transport.calls) == 12
    assert transport.max_in_flight == 1
    gaps = [b - a for a, b in zip(transport.call_started_ns, transport.call_started_ns[1:])]
    assert min(gaps) >= 3.0e9
    assert len(clock.sleeps) == 11 and all(s > 0 for s in clock.sleeps)


def test_slow_calls_do_not_add_extra_waiting_but_never_overlap():
    samples, _stop, transport, clock = _probe([], calls=3, rtt_ms=5_000.0)

    # 호출 하나가 간격보다 오래 걸리면 다음 호출은 끝난 뒤 바로 시작한다(겹치지 않는다).
    assert clock.sleeps == []
    assert transport.max_in_flight == 1
    assert [s["total_ms"] for s in samples] == pytest.approx([5_000.0] * 3)


def test_run_never_exceeds_twelve_calls_even_when_every_call_fails():
    script = [(500, b"oops")] * 50

    samples, stop, transport, _clock = _probe(script, calls=12)

    assert len(transport.calls) == 12 and len(samples) == 12
    assert stop == "PLAN_COMPLETED"
    assert all(not s["ok"] for s in samples)


# ── 즉시 중단 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("step", "reason"),
    [
        ((429, b"{}"), "RATE_LIMITED"),
        ((401, b"{}"), "AUTH_FAILED"),
        ((403, b"{}"), "AUTH_FAILED"),
        ((200, _body(rt_cd="1", msg_cd="EGW00201")), "RATE_LIMITED"),
        ((500, _body(rt_cd="1", msg_cd="EGW00123")), "AUTH_FAILED"),
        ((302, b"", {"redirected_to_another_location": True}), "REDIRECT_NOT_FOLLOWED"),
        ((301, b""), "REDIRECT_NOT_FOLLOWED"),
        ((200, _body(), {"redirected_to_another_location": True}), "REDIRECT_NOT_FOLLOWED"),
    ],
)
def test_rate_limit_auth_failure_and_redirect_stop_the_run_at_once(step, reason):
    samples, stop, transport, _clock = _probe([(200, _body()), step, (200, _body())], calls=12)

    assert stop == reason
    assert len(transport.calls) == 2 and len(samples) == 2
    assert transport.closed >= 1
    # 같은 호스트의 같은 경로만 불렀다 — 리다이렉트 대상을 따라가지 않았다.
    assert all(target.startswith(probe.QUOTE_PATH + "?") for target, _ in transport.calls)


def test_two_network_errors_in_a_row_stop_the_run_without_retrying():
    samples, stop, transport, _clock = _probe(
        [(200, _body()), "network-error", "network-error", (200, _body())], calls=12
    )

    assert stop == "CONSECUTIVE_NETWORK_ERRORS"
    assert [s.get("error") for s in samples] == [None, "TimeoutError", "TimeoutError"]
    assert len(transport.calls) == 3
    # 실패한 요청을 같은 종목으로 다시 보내지 않았다 — 계획의 다음 종목으로 넘어갔다.
    assert [s["ticker"] for s in samples] == ["005930", "000660", "005930"]


def test_a_single_network_error_is_counted_and_the_next_call_reconnects():
    samples, stop, transport, _clock = _probe(
        [(200, _body()), "network-error", (200, _body()), (200, _body())], calls=4
    )

    assert stop == "PLAN_COMPLETED" and len(samples) == 4
    assert [s["ok"] for s in samples] == [True, False, True, True]
    assert [s["connection_reused"] for s in samples] == [False, True, False, True]
    assert transport.opens == 2
    assert samples[2]["connect"] == {"dns_ms": 5.0, "tcp_ms": 10.0, "tls_ms": 15.0}


def test_connect_failures_count_as_calls_and_stop_after_two():
    samples, stop, transport, _clock = _probe(["connect-error", "connect-error"], calls=12)

    assert stop == "CONSECUTIVE_NETWORK_ERRORS"
    assert len(samples) == 2 and transport.calls == []
    assert [s["error"] for s in samples] == ["ConnectionRefusedError"] * 2


def test_run_stops_when_the_clock_enters_a_protected_window():
    # 14:49:54에 시작해 3초 간격 — 세 번째 호출이 14:50을 넘는다.
    clock = Clock(datetime(2027, 1, 6, 14, 49, 54, tzinfo=KST))

    samples, stop, transport, _clock = _probe([], calls=12, clock=clock)

    assert stop == "ENTERED_PROTECTED_OPERATING_WINDOW"
    assert len(samples) == len(transport.calls) == 2


# ── 시각·누락·집계 ───────────────────────────────────────────────────


def test_sample_keeps_both_clocks_the_split_times_and_the_connection_state():
    samples, _stop, _transport, _clock = _probe([], calls=2)
    first, second = samples

    assert first["started_kst"].startswith("2027-01-06T10:30:00")
    assert first["started_utc"].startswith("2027-01-06T01:30:00")
    assert first["endpoint"] == probe.QUOTE_PATH and first["tr_id"] == "FHKST01010200"
    assert first["session_phase"] == "REGULAR"
    assert first["connection_reused"] is False
    assert first["connect"] == {"dns_ms": 5.0, "tcp_ms": 10.0, "tls_ms": 15.0}
    # 왕복 시간은 요청을 쓰기 시작한 때부터다. 연결 시간은 따로 둔다.
    assert first["total_ms"] == pytest.approx(40.0)
    assert first["request_to_headers_ms"] == pytest.approx(24.0)
    assert second["connection_reused"] is True and second["connect"] is None
    assert second["http_status"] == 200 and second["ok"] is True


def test_quote_age_comes_from_the_quote_timestamp_and_is_never_estimated():
    late = Clock(datetime(2027, 1, 6, 10, 30, 2, tzinfo=KST))
    with_time, *_ = _probe([(200, _body(quote_time="103000"))], calls=1, clock=late)
    without_time, *_ = _probe([(200, _body(quote_time=""))], calls=1)
    malformed, *_ = _probe([(200, _body(quote_time="1030"))], calls=1)
    impossible, *_ = _probe([(200, _body(quote_time="256199"))], calls=1)

    assert with_time[0]["quote_age_s"] == 2
    # 시각이 없으면 측정 불가다. 왕복 시간의 절반 같은 값으로 채우지 않는다.
    for samples in (without_time, malformed, impossible):
        assert samples[0]["quote_age_s"] is None
        assert samples[0]["total_ms"] == pytest.approx(40.0)


def test_summary_separates_new_and_reused_connections_and_counts_errors():
    samples, stop, _transport, _clock = _probe(
        [(200, _body()), (200, _body(quote_time="")), "network-error", (500, b"x"), (200, _body())],
        calls=5,
    )

    summary = probe.summarize(samples, stop)

    assert (summary["calls"], summary["ok"], summary["errors"]) == (5, 3, 2)
    assert summary["total_ms_new_connection"]["n"] == 1
    assert summary["total_ms_reused_connection"] == {"n": 2, "p50": 40.0, "p95": 40.0, "max": 40.0}
    assert summary["connect_ms"]["tls_ms"]["n"] == 2
    assert summary["quote_time_missing"] == 1
    assert summary["quote_age_s_regular_session"]["n"] == 2
    assert summary["session_phases"] == ["REGULAR"]
    assert any("소표본" in text for text in summary["caveats"])
    assert any("SLA" in text for text in summary["caveats"])


def test_quote_age_is_not_reported_outside_the_regular_session():
    after_close = Clock(datetime(2027, 1, 6, 16, 0, 0, tzinfo=KST))
    samples, stop, *_ = _probe([(200, _body(quote_time="153000"))] * 3, calls=3, clock=after_close)

    summary = probe.summarize(samples, stop)

    assert samples[0]["session_phase"] == "AFTER" and samples[0]["quote_age_s"] == 1_800
    # 장 마감 뒤의 호가 시각은 멈춰 있다. 그 차를 '호가 나이'로 집계하지 않는다.
    assert summary["quote_age_s_regular_session"]["n"] == 0
    assert summary["session_phases"] == ["AFTER"]


def test_percentiles_use_nearest_rank_on_small_samples():
    assert probe._percentiles([10.0, 20.0, 30.0, 40.0]) == {
        "n": 4, "p50": 20.0, "p95": 40.0, "max": 40.0,
    }
    assert probe._percentiles([]) == {"n": 0, "p50": None, "p95": None, "max": None}


@pytest.mark.parametrize(
    ("moment", "phase", "protected"),
    [
        (datetime(2027, 1, 6, 7, 59, tzinfo=KST), "CLOSED", False),
        (datetime(2027, 1, 6, 8, 0, tzinfo=KST), "PREOPEN", True),
        (datetime(2027, 1, 6, 9, 11, tzinfo=KST), "REGULAR", True),
        (datetime(2027, 1, 6, 9, 39, 59, tzinfo=KST), "REGULAR", True),
        (datetime(2027, 1, 6, 9, 40, tzinfo=KST), "REGULAR", False),
        (datetime(2027, 1, 6, 14, 49, 59, tzinfo=KST), "REGULAR", False),
        (datetime(2027, 1, 6, 14, 50, tzinfo=KST), "REGULAR", True),
        (datetime(2027, 1, 6, 15, 15, tzinfo=KST), "REGULAR", True),
        (datetime(2027, 1, 6, 15, 44, 59, tzinfo=KST), "AFTER", True),
        (datetime(2027, 1, 6, 15, 45, tzinfo=KST), "AFTER", False),
        (datetime(2027, 1, 6, 18, 0, tzinfo=KST), "CLOSED", False),
        (datetime(2027, 1, 9, 10, 30, tzinfo=KST), "CLOSED", False),
        (datetime(2027, 1, 6, 1, 30, tzinfo=timezone.utc), "REGULAR", False),
    ],
)
def test_session_phase_and_protected_windows(moment, phase, protected):
    assert probe.session_phase(moment) == phase
    assert probe.in_protected_window(moment) is protected


# ── 사전 점검: 인증 조건 ─────────────────────────────────────────────


def _files(tmp_path, *, mode="REAL", host="openapi.koreainvestment.com:9443",
           expires="2027-01-07 08:29:30", issue_host="openapi.koreainvestment.com",
           issue_mode="REAL", token=FAKE_TOKEN, log_expires=None, write_cache=True):
    credentials = tmp_path / ".env.real"
    credentials.write_bytes(
        (
            f"KIS_MODE={mode}\nKIS_BASE_URL=https://{host}\nKIS_APP_KEY={FAKE_KEY}\n"
            f"KIS_APP_SECRET={FAKE_SECRET}\nKIS_ACCT_NO=1234567801\n"
        ).encode()
    )
    cache = tmp_path / "auth" / "token_cache.json"
    cache.parent.mkdir(exist_ok=True)
    if write_cache:
        cache.write_bytes(json.dumps({"access_token": token, "expires_at": expires}).encode())
    log = tmp_path / "20270106.jsonl"
    row = {
        "ts": "2027-01-06T08:29:30+09:00", "event": "TOKEN_REFRESHED", "host": issue_host,
        "mode": issue_mode, "expires_at": log_expires or expires,
        "token_prefix": token[:4] + "****",
    }
    log.write_bytes((json.dumps(row) + "\n").encode())
    return {"credentials": credentials, "token_cache": cache, "token_logs": [log]}


def _preflight(files, *, now=MIDDAY, confirmed="operator"):
    return probe.preflight(contention_confirmed_by=confirmed, now=now, **files)


def test_preflight_passes_only_with_a_real_issued_unexpired_token(tmp_path):
    result = _preflight(_files(tmp_path))

    assert result == {
        "eligible": True,
        "blockers": [],
        "facts": result["facts"],
    }
    assert result["facts"]["token_issue_evidence"]["host"] == "openapi.koreainvestment.com"
    assert result["facts"]["session_phase"] == "REGULAR"


@pytest.mark.parametrize(
    ("overrides", "blocker"),
    [
        ({"write_cache": False}, "REAL_TOKEN_CACHE_MISSING"),
        ({"token": ""}, "REAL_TOKEN_CACHE_MISSING"),
        # 모의 서버에서 발급된 토큰은 실전 서버에 쓰지 않는다.
        ({"issue_host": "openapivts.koreainvestment.com", "issue_mode": "PAPER"},
         "TOKEN_WAS_NOT_ISSUED_BY_THE_REAL_SERVER"),
        ({"issue_mode": "PAPER"}, "TOKEN_WAS_NOT_ISSUED_BY_THE_REAL_SERVER"),
        ({"log_expires": "2027-01-05 08:29:30"}, "TOKEN_ISSUE_HOST_UNPROVEN"),
        ({"expires": "2027-01-06 10:29:59"}, "TOKEN_EXPIRED"),
        ({"expires": "2027-01-06 10:59:59"}, "TOKEN_TOO_CLOSE_TO_EXPIRY"),
        ({"expires": "soon"}, "TOKEN_EXPIRY_UNKNOWN"),
        ({"mode": "PAPER"}, "CREDENTIALS_NOT_REAL_MODE"),
        ({"host": "openapivts.koreainvestment.com:29443"},
         "CREDENTIALS_HOST_IS_NOT_THE_REAL_SERVER"),
        ({"host": "openapi.koreainvestment.com:443"}, "CREDENTIALS_HOST_IS_NOT_THE_REAL_SERVER"),
    ],
)
def test_preflight_blocks_when_the_authentication_condition_is_not_met(
    tmp_path, overrides, blocker
):
    result = _preflight(_files(tmp_path, **overrides))

    assert result["eligible"] is False
    assert blocker in result["blockers"]


def test_token_with_just_over_thirty_minutes_left_is_accepted(tmp_path):
    assert _preflight(_files(tmp_path, expires="2027-01-06 11:00:01"))["eligible"] is True


def test_preflight_blocks_inside_protected_windows_and_without_a_contention_check(tmp_path):
    files = _files(tmp_path)
    opening = datetime(2027, 1, 6, 9, 11, tzinfo=KST)

    assert _preflight(files, now=opening)["blockers"] == ["INSIDE_PROTECTED_OPERATING_WINDOW"]
    assert _preflight(files, confirmed=None)["blockers"] == ["NO_CONTENTION_CONFIRMATION"]
    assert _preflight(files, confirmed="   ")["blockers"] == ["NO_CONTENTION_CONFIRMATION"]


def test_missing_files_block_without_reading_anything(tmp_path):
    result = probe.preflight(
        credentials=tmp_path / "nope", token_cache=None, token_logs=[],
        contention_confirmed_by="operator", now=MIDDAY,
    )

    assert result["blockers"] == ["CREDENTIALS_FILE_MISSING", "REAL_TOKEN_CACHE_MISSING"]


def test_database_paths_are_refused(tmp_path):
    for path in (tmp_path / "db" / "trading.db", tmp_path / "trading.db", tmp_path / "DB" / "x"):
        with pytest.raises(probe.ProbeRefused):
            probe.inspect_credentials_file(path)
        with pytest.raises(probe.ProbeRefused):
            probe.inspect_token_cache(path)
    with pytest.raises(probe.ProbeRefused):
        probe.find_issue_evidence([tmp_path / "db" / "log.jsonl"], {})


def test_preflight_result_holds_no_secret(tmp_path):
    result = _preflight(_files(tmp_path))
    text = json.dumps(result)

    for secret in (FAKE_KEY, FAKE_SECRET, FAKE_TOKEN, "1234567801", FAKE_TOKEN[:4]):
        assert secret not in text
    assert result["facts"]["credentials"]["app_key_present"] is True


# ── 실행 모드 ────────────────────────────────────────────────────────


def _no_network():
    raise AssertionError("no transport may be created in this mode")


def _run(mode, tmp_path, *, files=None, clock=None, script=(), **overrides):
    clock = clock or Clock()
    files = _files(tmp_path) if files is None else files
    transports: list[FakeTransport] = []

    def factory():
        transports.append(FakeTransport(clock, list(script)))
        return transports[-1]

    kwargs = {
        "plan": probe.make_plan(list(probe.DEFAULT_TICKERS), 4, 3.0),
        "contention_confirmed_by": "operator",
        "acknowledged": True,
        "out": tmp_path / "out",
        "now": clock.now,
        "transport_factory": factory,
        "monotonic_ns": clock.monotonic_ns,
        "sleep": clock.sleep,
        **files,
        **overrides,
    }
    return probe.run(mode, **kwargs), transports


def test_plan_mode_reads_nothing_and_calls_nothing(tmp_path):
    result = probe.run(
        "plan", plan=probe.make_plan(["005930"], 12, 3.0), credentials=tmp_path / "missing",
        token_cache=tmp_path / "missing", out=tmp_path / "out", transport_factory=_no_network,
    )

    assert result["manifest"]["network_calls"] == 0
    assert "preflight" not in result["manifest"]
    assert result["manifest"]["plan"] == {
        "tickers": ["005930"], "calls": 12, "interval_sec": 3.0, "concurrency": 1, "retries": 0,
        "hard_max_calls": 12, "hard_min_interval_sec": 3.0,
    }


def test_preflight_mode_never_touches_the_network_even_when_eligible(tmp_path):
    result, transports = _run("preflight", tmp_path, transport_factory=_no_network)

    assert result["manifest"]["preflight"]["eligible"] is True
    assert result["manifest"]["network_calls"] == 0 and transports == []
    summary = json.loads((tmp_path / "out" / "summary.json").read_text(encoding="utf-8"))
    assert summary == {"calls": 0, "note": "no network call was made"}
    assert (tmp_path / "out" / "samples.jsonl").read_bytes() == b""


@pytest.mark.parametrize(
    "overrides",
    [
        {"issue_host": "openapivts.koreainvestment.com", "issue_mode": "PAPER"},
        {"write_cache": False},
        {"expires": "2027-01-06 10:00:00"},
    ],
)
def test_execute_makes_no_network_attempt_when_the_token_condition_fails(tmp_path, overrides):
    files = _files(tmp_path, **overrides)

    result, transports = _run("execute", tmp_path, files=files, transport_factory=_no_network)

    manifest = result["manifest"]
    assert manifest["network_calls"] == 0 and manifest["transport_kind"] is None
    assert manifest["not_executed_because"] == manifest["preflight"]["blockers"] != []
    assert transports == []


def test_execute_needs_the_explicit_acknowledgement(tmp_path):
    result, _ = _run("execute", tmp_path, acknowledged=False, transport_factory=_no_network)

    assert result["manifest"]["not_executed_because"] == ["REAL_SERVER_CALL_NOT_ACKNOWLEDGED"]
    assert result["manifest"]["network_calls"] == 0


def test_execute_with_a_mock_transport_records_calls_and_labels_them_as_not_real(tmp_path):
    result, transports = _run("execute", tmp_path)

    manifest = result["manifest"]
    assert manifest["network_calls"] == 4 and len(transports[0].calls) == 4
    # 모의 전송 계층의 호출은 실제 호출로 적히지 않는다.
    assert manifest["transport_kind"] == "FAKE" != probe.HttpsTransport.kind
    assert result["summary"]["calls"] == 4 and result["summary"]["ok"] == 4
    lines = (tmp_path / "out" / "samples.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4 and json.loads(lines[0])["ticker"] == "005930"
    sent_headers = transports[0].calls[0][1]
    assert sent_headers["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert sent_headers["tr_id"] == "FHKST01010200"
    assert transports[0].calls[0][0] == (
        probe.QUOTE_PATH + "?FID_COND_MRKT_DIV_CODE=J&FID_INPUT_ISCD=005930"
    )


def test_no_secret_reaches_the_outputs_even_when_an_error_message_carries_one(tmp_path):
    result, transports = _run(
        "execute", tmp_path, script=[(200, _body()), "network-error", (200, _body())]
    )

    assert transports[0].calls, "the mock transport was called"
    written = b"".join(p.read_bytes() for p in (tmp_path / "out").iterdir())
    text = written.decode("utf-8") + json.dumps(result, ensure_ascii=False)
    for secret in (FAKE_KEY, FAKE_SECRET, FAKE_TOKEN, "Bearer", "1234567801", "authorization"):
        assert secret not in text
    assert result["samples"][1]["error"] == "TimeoutError"


def test_secrets_never_show_in_repr_and_redaction_removes_them():
    assert repr(SECRETS) == "Secrets(****)" == str(SECRETS)
    message = f"failed: Bearer {FAKE_TOKEN} appkey={FAKE_KEY} secret={FAKE_SECRET}"

    cleaned = probe.redact(message, SECRETS.values())

    assert cleaned == "failed: Bearer **** appkey=**** secret=****"
    assert probe.redact("nothing here", ["", FAKE_TOKEN]) == "nothing here"


def test_response_is_reduced_to_public_quote_fields():
    body = json.dumps(
        {
            "rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상처리",
            "output1": {"aspr_acpt_hour": "103000", "askp1": "1", "bidp1": "2",
                        "askp_rsqn1": "3", "bidp_rsqn1": "4", "askp2": "9"},
            "output2": {"antc_cnpr": "5", "stck_shrn_iscd": "005930"},
            "unexpected_account_field": "1234567801",
        }
    ).encode()

    cleaned = probe._sanitize_body(body)

    assert cleaned == {
        "parsed": True, "rt_cd": "0", "msg_cd": "MCA00000", "quote_time": "103000",
        "ask1": "1", "bid1": "2", "ask1_qty": "3", "bid1_qty": "4",
    }
    assert probe._sanitize_body(b"<html>") == {"parsed": False}
    assert probe._sanitize_body(b"[1, 2]") == {"parsed": False}


def test_cli_refuses_out_of_range_plans_and_reports_zero_calls(tmp_path, capsys):
    assert probe.main(["plan", "--calls", "13"]) == 2
    assert probe.main(["plan", "--interval-sec", "1"]) == 2
    assert probe.main(["plan"]) == 0
    assert json.loads(capsys.readouterr().out)["network_calls"] == 0
    # 토큰 캐시가 없으면 execute도 네트워크 없이 끝나고 종료 코드로 알린다.
    code = probe.main(
        ["execute", "--credentials", str(tmp_path / "none"), "--out", str(tmp_path / "o"),
         "--no-contention-confirmed-by", "operator", "--i-understand-this-calls-the-real-server"]
    )
    assert code == 3
    manifest = json.loads((tmp_path / "o" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["network_calls"] == 0
    assert "REAL_TOKEN_CACHE_MISSING" in manifest["not_executed_because"]
