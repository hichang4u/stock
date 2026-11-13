"""REAL 서버 읽기 전용 지연 측정기 — 주문 기능이 없다.

한국투자증권 실전 서버(`openapi.koreainvestment.com:9443`)의 **시세 GET 한 종류**만 보낼 수
있다. 주문·정정·취소, 계좌·잔고·체결 조회, 토큰 발급·갱신, POST/PUT/DELETE는 이 파일에
구현돼 있지 않고 화이트리스트가 거부한다.

저장소의 다른 코드와 떨어져 있다. 표준 라이브러리만 쓰고 `src`·`main`·`dotenv`·DB를
임포트하지 않으며 환경변수를 읽지도 바꾸지도 않는다. 접속 호스트는 상수다 — `.env`의
`KIS_BASE_URL`이나 프록시 설정을 따르지 않고, 리다이렉트를 따라가지 않는다.

    python scripts/real_latency_probe.py plan
    python scripts/real_latency_probe.py preflight --credentials <.env.real> \\
        --token-cache <REAL 토큰 캐시> --token-log <발급 로그> --out <디렉터리>
    python scripts/real_latency_probe.py execute ... --no-contention-confirmed-by <누가> \\
        --i-understand-this-calls-the-real-server

- `plan`: 아무것도 읽지 않는다. 무엇을 보낼지만 보여준다.
- `preflight`: 파일의 **비밀이 아닌 사실**만 읽어 실측 조건이 맞는지 판정한다. 네트워크 없음.
- `execute`: 조건이 전부 맞을 때만 비밀을 메모리에 올려 측정한다. 맞지 않으면 네트워크를
  시도하지 않는다.

토큰은 발급하지도 갱신하지도 않는다. 이미 있는 토큰이 (1) 만료까지 여유가 있고 (2) 발급
로그로 **실전 호스트에서 발급된 것**임이 확인될 때만 쓴다. 모의 서버 토큰은 쓰지 않는다.

재는 것은 요청을 쓰기 시작한 때부터 응답 본문을 다 읽은 때까지의 왕복 시간이다. 새 연결의
DNS·TCP·TLS는 따로 재지만, 서버 처리와 대기열은 나누지 못한다. 호가 나이는 응답의 호가 접수
시각(초 단위)이 있을 때만 내고, 없으면 측정 불가다 — 왕복 시간의 절반으로 추정하지 않는다.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import re
import socket
import ssl
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urlparse

PROBE_VERSION = "real-latency-probe-1"

REAL_HOST = "openapi.koreainvestment.com"
REAL_PORT = 9443
QUOTE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
QUOTE_TR_ID = "FHKST01010200"
# (메서드, 경로, TR). 이 표에 없는 요청은 만들 수 없다.
ALLOWED_REQUESTS = frozenset({("GET", QUOTE_PATH, QUOTE_TR_ID)})
_TICKER = re.compile(r"\d{6}")

# 넘을 수 없는 한도. 명령행으로 줄일 수는 있어도 늘릴 수는 없다.
HARD_MAX_CALLS = 12
HARD_MIN_INTERVAL_SEC = 3.0
MAX_CONSECUTIVE_NETWORK_ERRORS = 2
TOKEN_EXPIRY_MARGIN = timedelta(minutes=30)
REQUEST_TIMEOUT_SEC = 10.0
MAX_BODY_BYTES = 256 * 1024

# 유동성이 큰 일반 종목 둘을 고정한다. 수익성으로 고른 것이 아니라 조회 지연을 재는 대상이다.
DEFAULT_TICKERS = ("005930", "000660")

KST = timezone(timedelta(hours=9))
# 운영 봇의 장초 준비·진입과 청산·백업 시간대. 이 안에서는 호출하지 않는다.
PROTECTED_WINDOWS_KST = ((dtime(8, 0), dtime(9, 40)), (dtime(14, 50), dtime(15, 45)))

_STOP_HTTP = {401: "AUTH_FAILED", 403: "AUTH_FAILED", 429: "RATE_LIMITED"}
# 응답 본문의 KIS 오류 코드: 유량 초과와 토큰 문제는 즉시 멈춘다.
_STOP_MSG_CD = {
    "EGW00201": "RATE_LIMITED",
    "EGW00121": "AUTH_FAILED",
    "EGW00123": "AUTH_FAILED",
}


class ForbiddenRequest(Exception):
    """화이트리스트에 없는 요청이다. 보내지 않는다."""


class ProbeRefused(Exception):
    """한도·경로·설정이 허용 범위 밖이다. 측정을 시작하지 않는다."""


def check_request(method: str, path: str, tr_id: str, params: dict[str, str]) -> None:
    """보낼 수 있는 요청인지 확인한다. 아니면 예외 — 전송 계층에 닿기 전에 막는다."""
    if (method, path, tr_id) not in ALLOWED_REQUESTS:
        raise ForbiddenRequest(f"{method} {path} [{tr_id}] is not an approved read-only request")
    if set(params) != {"FID_COND_MRKT_DIV_CODE", "FID_INPUT_ISCD"}:
        raise ForbiddenRequest("only the market code and the ticker may be sent")
    if params["FID_COND_MRKT_DIV_CODE"] != "J" or not _TICKER.fullmatch(params["FID_INPUT_ISCD"]):
        raise ForbiddenRequest("market must be J and the ticker six digits")


# ── 비밀 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Secrets:
    """메모리에만 둔다. 출력·로그·산출물에 나가지 않게 repr을 가린다."""

    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    token: str = field(repr=False)

    def __repr__(self) -> str:
        return "Secrets(****)"

    def values(self) -> tuple[str, ...]:
        return tuple(v for v in (self.app_key, self.app_secret, self.token) if v)


def redact(text: str, secrets: Sequence[str]) -> str:
    """문자열에서 비밀값을 지운다. 예외 메시지가 헤더를 품고 있을 때를 대비한다."""
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "****")
    return text


def _env_values(path: Path) -> dict[str, str]:
    """`.env` 형식 파일을 읽기만 한다. 프로세스 환경변수는 건드리지 않는다."""
    values: dict[str, str] = {}
    for line in path.read_bytes().decode("utf-8", errors="replace").splitlines():
        match = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*)", line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return values


def _guard_path(path: Path) -> None:
    """거래 DB나 그 디렉터리를 가리키는 경로는 받지 않는다."""
    lowered = [part.lower() for part in path.parts]
    if path.suffix.lower() in (".db", ".sqlite", ".sqlite3") or "db" in lowered:
        raise ProbeRefused(f"refusing a database path: {path.name}")


def inspect_credentials_file(path: Path) -> dict[str, Any]:
    """자격증명 파일의 **비밀이 아닌 사실**만 돌려준다: 모드, 접속 호스트, 키가 있는지."""
    _guard_path(path)
    values = _env_values(path)
    base = urlparse(values.get("KIS_BASE_URL", ""))
    return {
        "mode": values.get("KIS_MODE"),
        "base_host": base.hostname,
        "base_port": base.port,
        "base_scheme": base.scheme,
        "app_key_present": bool(values.get("KIS_APP_KEY")),
        "app_secret_present": bool(values.get("KIS_APP_SECRET")),
    }


def inspect_token_cache(path: Path) -> dict[str, Any]:
    """토큰 캐시의 만료 시각과 앞 4글자(발급 로그와 대조용)만 돌려준다."""
    _guard_path(path)
    cache = json.loads(path.read_bytes().decode("utf-8"))
    token = str(cache.get("access_token") or "")
    return {
        "token_present": bool(token),
        "token_prefix": token[:4],
        "expires_at": cache.get("expires_at"),
    }


def find_issue_evidence(log_paths: Sequence[Path], cache_facts: dict[str, Any]) -> dict | None:
    """발급 로그에서 이 토큰의 `TOKEN_REFRESHED` 기록을 찾는다.

    캐시 파일에는 발급 호스트가 없다. 운영 로그는 발급할 때 호스트·모드·만료 시각·토큰 앞
    4글자를 남기므로, 만료 시각과 앞 4글자가 같은 기록을 이 토큰의 발급 기록으로 본다.
    """
    for path in log_paths:
        _guard_path(path)
        for line in path.read_bytes().decode("utf-8", errors="replace").splitlines():
            if "TOKEN_REFRESHED" not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            same_expiry = row.get("expires_at") == cache_facts.get("expires_at")
            prefix = str(row.get("token_prefix") or "")[:4]
            if row.get("event") == "TOKEN_REFRESHED" and same_expiry and (
                prefix == cache_facts.get("token_prefix")
            ):
                return {
                    "host": row.get("host"),
                    "mode": row.get("mode"),
                    "issued_at": row.get("ts"),
                }
    return None


def load_secrets(credentials: Path, token_cache: Path) -> Secrets:
    """실측 직전에만 부른다. 계좌번호는 읽지 않는다."""
    values = _env_values(credentials)
    cache = json.loads(token_cache.read_bytes().decode("utf-8"))
    return Secrets(
        app_key=values.get("KIS_APP_KEY", ""),
        app_secret=values.get("KIS_APP_SECRET", ""),
        token=str(cache.get("access_token") or ""),
    )


# ── 사전 점검 ────────────────────────────────────────────────────────


def session_phase(now: datetime) -> str:
    local = now.astimezone(KST)
    if local.weekday() >= 5:
        return "CLOSED"
    clock = local.time()
    if dtime(8, 0) <= clock < dtime(9, 0):
        return "PREOPEN"
    if dtime(9, 0) <= clock < dtime(15, 30):
        return "REGULAR"
    if dtime(15, 30) <= clock < dtime(18, 0):
        return "AFTER"
    return "CLOSED"


def in_protected_window(now: datetime) -> bool:
    local = now.astimezone(KST)
    if local.weekday() >= 5:
        return False
    return any(start <= local.time() < end for start, end in PROTECTED_WINDOWS_KST)


@dataclass(frozen=True)
class Plan:
    tickers: tuple[str, ...]
    calls: int
    interval_sec: float

    def requests(self) -> list[dict[str, str]]:
        """종목을 번갈아 가며 `calls`개. 실패해도 같은 요청을 다시 보내지 않는다."""
        return [
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": self.tickers[i % len(self.tickers)]}
            for i in range(self.calls)
        ]


def make_plan(tickers: Sequence[str], calls: int, interval_sec: float) -> Plan:
    if not 1 <= calls <= HARD_MAX_CALLS:
        raise ProbeRefused(f"calls must be between 1 and {HARD_MAX_CALLS}")
    if not math.isfinite(interval_sec) or interval_sec < HARD_MIN_INTERVAL_SEC:
        raise ProbeRefused(f"interval must be at least {HARD_MIN_INTERVAL_SEC} seconds")
    if not 1 <= len(tickers) <= 2 or any(not _TICKER.fullmatch(t) for t in tickers):
        raise ProbeRefused("one or two six-digit tickers are required")
    plan = Plan(tuple(tickers), calls, float(interval_sec))
    for params in plan.requests():
        check_request("GET", QUOTE_PATH, QUOTE_TR_ID, params)
    return plan


def preflight(
    *,
    credentials: Path | None,
    token_cache: Path | None,
    token_logs: Sequence[Path],
    contention_confirmed_by: str | None,
    now: datetime,
) -> dict[str, Any]:
    """실측 조건을 판정한다. 네트워크를 쓰지 않고 비밀값을 돌려주지 않는다."""
    blockers: list[str] = []
    facts: dict[str, Any] = {
        "checked_at_utc": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "checked_at_kst": now.astimezone(KST).isoformat(timespec="seconds"),
        "session_phase": session_phase(now),
        "target": f"https://{REAL_HOST}:{REAL_PORT}",
    }

    if credentials is None or not credentials.is_file():
        blockers.append("CREDENTIALS_FILE_MISSING")
    else:
        cred = inspect_credentials_file(credentials)
        facts["credentials"] = cred
        if cred["mode"] != "REAL":
            blockers.append("CREDENTIALS_NOT_REAL_MODE")
        if (cred["base_scheme"], cred["base_host"], cred["base_port"]) != (
            "https", REAL_HOST, REAL_PORT
        ):
            blockers.append("CREDENTIALS_HOST_IS_NOT_THE_REAL_SERVER")
        if not (cred["app_key_present"] and cred["app_secret_present"]):
            blockers.append("APP_KEY_OR_SECRET_MISSING")

    if token_cache is None or not token_cache.is_file():
        # 토큰이 없으면 여기서 끝이다. 발급하지 않는다.
        blockers.append("REAL_TOKEN_CACHE_MISSING")
    else:
        cache = inspect_token_cache(token_cache)
        facts["token"] = {"present": cache["token_present"], "expires_at": cache["expires_at"]}
        expires = _parse_expiry(cache["expires_at"])
        if not cache["token_present"]:
            blockers.append("REAL_TOKEN_CACHE_MISSING")
        if expires is None:
            blockers.append("TOKEN_EXPIRY_UNKNOWN")
        elif now >= expires:
            blockers.append("TOKEN_EXPIRED")
        elif now >= expires - TOKEN_EXPIRY_MARGIN:
            blockers.append("TOKEN_TOO_CLOSE_TO_EXPIRY")
        evidence = find_issue_evidence(token_logs, cache)
        facts["token_issue_evidence"] = evidence
        if evidence is None:
            blockers.append("TOKEN_ISSUE_HOST_UNPROVEN")
        elif evidence["host"] != REAL_HOST or evidence["mode"] != "REAL":
            # 모의 서버에서 발급된 토큰이다. 실전 서버에 쓰지 않는다.
            blockers.append("TOKEN_WAS_NOT_ISSUED_BY_THE_REAL_SERVER")

    if in_protected_window(now):
        blockers.append("INSIDE_PROTECTED_OPERATING_WINDOW")
    if not (contention_confirmed_by or "").strip():
        blockers.append("NO_CONTENTION_CONFIRMATION")
    else:
        facts["contention_confirmed_by"] = contention_confirmed_by

    return {"eligible": not blockers, "blockers": blockers, "facts": facts}


def _parse_expiry(value: object) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    except ValueError:
        return None


# ── 전송 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes
    headers_at_ns: int
    done_at_ns: int
    date_header: str | None = None
    redirected_to_another_location: bool = False


class Transport(Protocol):
    kind: str

    def open(self) -> dict[str, float]: ...

    def get(self, target: str, headers: dict[str, str]) -> Reply: ...

    def close(self) -> None: ...


class HttpsTransport:
    """실전 서버로 가는 GET 하나만 할 줄 안다. 다른 호스트·메서드는 구현돼 있지 않다.

    `http.client`는 리다이렉트를 따라가지 않고 프록시 환경변수도 읽지 않는다. 인증서는
    시스템 기본 설정으로 검증한다.
    """

    kind = "REAL_HTTPS"

    def __init__(self, host: str = REAL_HOST, port: int = REAL_PORT) -> None:
        if (host, port) != (REAL_HOST, REAL_PORT):
            raise ProbeRefused("this transport only talks to the real quote server")
        self._conn: http.client.HTTPSConnection | None = None

    def open(self) -> dict[str, float]:
        """새 연결을 열며 DNS·TCP·TLS를 따로 잰다(ms)."""
        t0 = time.monotonic_ns()
        address = socket.getaddrinfo(REAL_HOST, REAL_PORT, type=socket.SOCK_STREAM)[0][4]
        t1 = time.monotonic_ns()
        raw = socket.create_connection(address[:2], timeout=REQUEST_TIMEOUT_SEC)
        t2 = time.monotonic_ns()
        context = ssl.create_default_context()
        secured = context.wrap_socket(raw, server_hostname=REAL_HOST)
        t3 = time.monotonic_ns()
        conn = http.client.HTTPSConnection(REAL_HOST, REAL_PORT, timeout=REQUEST_TIMEOUT_SEC)
        conn.sock = secured
        self._conn = conn
        return {
            "dns_ms": (t1 - t0) / 1e6,
            "tcp_ms": (t2 - t1) / 1e6,
            "tls_ms": (t3 - t2) / 1e6,
        }

    def get(self, target: str, headers: dict[str, str]) -> Reply:
        if self._conn is None:
            raise ConnectionError("connection is not open")
        self._conn.request("GET", target, headers=headers)
        response = self._conn.getresponse()
        headers_at = time.monotonic_ns()
        body = response.read(MAX_BODY_BYTES)
        return Reply(
            status=response.status,
            body=body,
            headers_at_ns=headers_at,
            done_at_ns=time.monotonic_ns(),
            date_header=response.getheader("Date"),
            redirected_to_another_location=response.getheader("Location") is not None,
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


# ── 측정 ─────────────────────────────────────────────────────────────


def _quote_age_seconds(quote_hhmmss: object, received: datetime) -> int | None:
    """호가 접수 시각(HHMMSS, 거래소)과 수신 시각(로컬)의 차. 형식이 아니면 측정 불가.

    해상도는 1초이고 로컬 시계와 거래소 시계의 차가 그대로 들어간다.
    """
    text = str(quote_hhmmss or "")
    if not re.fullmatch(r"\d{6}", text):
        return None
    hour, minute, second = int(text[:2]), int(text[2:4]), int(text[4:])
    if hour > 23 or minute > 59 or second > 59:
        return None
    local = received.astimezone(KST)
    return (local.hour * 3600 + local.minute * 60 + local.second) - (
        hour * 3600 + minute * 60 + second
    )


def _sanitize_body(body: bytes) -> dict[str, Any]:
    """응답에서 지연 해석에 필요한 공개 시세 필드만 남긴다. 나머지와 헤더는 버린다."""
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return {"parsed": False}
    if not isinstance(payload, dict):
        return {"parsed": False}
    out1 = payload.get("output1") or {}
    if isinstance(out1, list):
        out1 = out1[0] if out1 else {}
    if not isinstance(out1, dict):
        out1 = {}
    return {
        "parsed": True,
        "rt_cd": payload.get("rt_cd"),
        "msg_cd": payload.get("msg_cd"),
        "quote_time": out1.get("aspr_acpt_hour"),
        "ask1": out1.get("askp1"),
        "bid1": out1.get("bidp1"),
        "ask1_qty": out1.get("askp_rsqn1"),
        "bid1_qty": out1.get("bidp_rsqn1"),
    }


def run_probe(
    transport: Transport,
    secrets: Secrets,
    plan: Plan,
    *,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[dict[str, Any]], str]:
    """계획대로 하나씩 보낸다. 동시에 둘을 보내지 않고, 실패해도 다시 보내지 않는다.

    (표본, 종료 사유). 유량 초과·인증 실패·리다이렉트는 그 자리에서 멈추고, 네트워크 오류가
    연달아 나거나 보호 시간대에 들어서도 멈춘다.
    """
    samples: list[dict[str, Any]] = []
    stop_reason = "PLAN_COMPLETED"
    connected = False
    consecutive_errors = 0
    last_start_ns: int | None = None
    try:
        for seq, params in enumerate(plan.requests(), start=1):
            check_request("GET", QUOTE_PATH, QUOTE_TR_ID, params)
            if last_start_ns is not None:
                wait = plan.interval_sec - (monotonic_ns() - last_start_ns) / 1e9
                if wait > 0:
                    sleep(wait)
            started = now()
            if in_protected_window(started):
                stop_reason = "ENTERED_PROTECTED_OPERATING_WINDOW"
                break
            sample: dict[str, Any] = {
                "seq": seq,
                "endpoint": QUOTE_PATH,
                "tr_id": QUOTE_TR_ID,
                "ticker": params["FID_INPUT_ISCD"],
                "started_utc": started.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
                "started_kst": started.astimezone(KST).isoformat(timespec="milliseconds"),
                "session_phase": session_phase(started),
                "connection_reused": connected,
                "connect": None,
                "ok": False,
            }
            last_start_ns = monotonic_ns()
            try:
                if not connected:
                    sample["connect"] = transport.open()
                    connected = True
                request_at = monotonic_ns()
                # 간격은 요청을 실제로 보내기 시작한 시각끼리 잰다. 새 연결을 여느라 걸린
                # 시간만큼 다음 요청이 앞당겨지지 않게 한다.
                last_start_ns = request_at
                reply = transport.get(
                    f"{QUOTE_PATH}?{urlencode(params)}",
                    {
                        "authorization": f"Bearer {secrets.token}",
                        "appkey": secrets.app_key,
                        "appsecret": secrets.app_secret,
                        "tr_id": QUOTE_TR_ID,
                        "custtype": "P",
                        "content-type": "application/json; charset=utf-8",
                    },
                )
            except Exception as exc:
                # 예외 본문은 남기지 않는다(헤더가 섞여 있을 수 있다). 종류만 적는다.
                sample["error"] = type(exc).__name__
                transport.close()
                connected = False
                consecutive_errors += 1
                samples.append(sample)
                if consecutive_errors >= MAX_CONSECUTIVE_NETWORK_ERRORS:
                    stop_reason = "CONSECUTIVE_NETWORK_ERRORS"
                    break
                continue
            consecutive_errors = 0
            received = now()
            body = _sanitize_body(reply.body)
            sample.update(
                {
                    "http_status": reply.status,
                    "request_to_headers_ms": (reply.headers_at_ns - request_at) / 1e6,
                    "total_ms": (reply.done_at_ns - request_at) / 1e6,
                    "server_date_header": reply.date_header,
                    "rt_cd": body.get("rt_cd"),
                    "msg_cd": body.get("msg_cd"),
                    "quote_time": body.get("quote_time"),
                    "quote_age_s": _quote_age_seconds(body.get("quote_time"), received),
                    "ask1": body.get("ask1"),
                    "bid1": body.get("bid1"),
                    "ask1_qty": body.get("ask1_qty"),
                    "bid1_qty": body.get("bid1_qty"),
                }
            )
            sample["ok"] = reply.status == 200 and body.get("rt_cd") == "0"
            samples.append(sample)
            if reply.redirected_to_another_location or 300 <= reply.status < 400:
                stop_reason = "REDIRECT_NOT_FOLLOWED"
                break
            halt = _STOP_HTTP.get(reply.status) or _STOP_MSG_CD.get(str(body.get("msg_cd")))
            if halt:
                stop_reason = halt
                break
    finally:
        transport.close()
    return samples, stop_reason


def _percentiles(values: Sequence[float]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"n": 0, "p50": None, "p95": None, "max": None}

    def rank(q: float) -> float:
        return round(ordered[max(1, math.ceil(q * len(ordered))) - 1], 3)

    return {"n": len(ordered), "p50": rank(0.50), "p95": rank(0.95), "max": round(ordered[-1], 3)}


def summarize(samples: Sequence[dict[str, Any]], stop_reason: str) -> dict[str, Any]:
    """소표본 요약. 분위수는 nearest-rank이고 표본이 적어 꼬리를 대표하지 못한다."""
    ok = [s for s in samples if s["ok"]]
    first = [s for s in ok if not s["connection_reused"]]
    reused = [s for s in ok if s["connection_reused"]]
    regular = [s for s in ok if s["session_phase"] == "REGULAR"]
    ages = [s["quote_age_s"] for s in regular if s.get("quote_age_s") is not None]
    connects = [s["connect"] for s in samples if s.get("connect")]
    return {
        "calls": len(samples),
        "ok": len(ok),
        "errors": len(samples) - len(ok),
        "stop_reason": stop_reason,
        "session_phases": sorted({s["session_phase"] for s in samples}),
        "total_ms_new_connection": _percentiles([s["total_ms"] for s in first]),
        "total_ms_reused_connection": _percentiles([s["total_ms"] for s in reused]),
        "request_to_headers_ms_reused": _percentiles(
            [s["request_to_headers_ms"] for s in reused]
        ),
        "connect_ms": {
            key: _percentiles([c[key] for c in connects]) for key in ("dns_ms", "tcp_ms", "tls_ms")
        },
        "quote_age_s_regular_session": _percentiles(ages),
        "quote_time_missing": sum(1 for s in ok if s.get("quote_age_s") is None),
        "caveats": [
            "소표본이다. p95·max는 꼬리를 대표하지 못하고 SLA 판정에 쓰지 않는다",
            "서버 처리와 대기열은 왕복 시간에서 분리되지 않는다",
            "호가 나이는 1초 해상도이고 로컬·거래소 시계 차를 포함한다",
            "정규장 밖의 값은 장중·개장 직후 값으로 일반화하지 않는다",
            "F1 전체 스캔과 09시 진입 경로의 지연은 이 측정에 들어 있지 않다",
        ],
    }


# ── 실행 ─────────────────────────────────────────────────────────────


def _write(path: Path, value: Any) -> None:
    path.write_bytes(
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )


def run(
    mode: str,
    *,
    plan: Plan,
    credentials: Path | None = None,
    token_cache: Path | None = None,
    token_logs: Sequence[Path] = (),
    contention_confirmed_by: str | None = None,
    acknowledged: bool = False,
    out: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    transport_factory: Callable[[], Transport] = HttpsTransport,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """한 번의 실행. 반환값과 산출물에는 비밀값이 없다."""
    manifest: dict[str, Any] = {
        "probe_version": PROBE_VERSION,
        "mode": mode,
        "target": f"https://{REAL_HOST}:{REAL_PORT}",
        "allowed_requests": sorted(" ".join(item) for item in ALLOWED_REQUESTS),
        "plan": {
            "tickers": list(plan.tickers),
            "calls": plan.calls,
            "interval_sec": plan.interval_sec,
            "concurrency": 1,
            "retries": 0,
            "hard_max_calls": HARD_MAX_CALLS,
            "hard_min_interval_sec": HARD_MIN_INTERVAL_SEC,
        },
        "network_calls": 0,
        "transport_kind": None,
    }
    samples: list[dict[str, Any]] = []
    summary: dict[str, Any] | None = None
    if mode != "plan":
        check = preflight(
            credentials=credentials,
            token_cache=token_cache,
            token_logs=token_logs,
            contention_confirmed_by=contention_confirmed_by,
            now=now(),
        )
        manifest["preflight"] = check
        if mode == "execute":
            if not acknowledged:
                check["blockers"].append("REAL_SERVER_CALL_NOT_ACKNOWLEDGED")
                check["eligible"] = False
            if check["eligible"] and credentials is not None and token_cache is not None:
                secrets = load_secrets(credentials, token_cache)
                transport = transport_factory()
                manifest["transport_kind"] = transport.kind
                samples, stop_reason = run_probe(
                    transport, secrets, plan, monotonic_ns=monotonic_ns, now=now, sleep=sleep
                )
                manifest["network_calls"] = len(samples)
                summary = summarize(samples, stop_reason)
                # 혹시라도 비밀값이 표본에 섞였으면 지운다.
                cleaned = redact(json.dumps(samples, ensure_ascii=False), secrets.values())
                samples = json.loads(cleaned)
            else:
                manifest["not_executed_because"] = list(check["blockers"])
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        _write(out / "manifest.json", manifest)
        (out / "samples.jsonl").write_bytes(
            "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in samples).encode("utf-8")
        )
        _write(out / "summary.json", summary or {"calls": 0, "note": "no network call was made"})
    return {"manifest": manifest, "samples": samples, "summary": summary}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "preflight", "execute"))
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--token-cache", type=Path)
    parser.add_argument("--token-log", type=Path, action="append", default=[])
    parser.add_argument("--tickers", nargs="+", default=list(DEFAULT_TICKERS))
    parser.add_argument("--calls", type=int, default=HARD_MAX_CALLS)
    parser.add_argument("--interval-sec", type=float, default=HARD_MIN_INTERVAL_SEC)
    parser.add_argument("--no-contention-confirmed-by")
    parser.add_argument("--i-understand-this-calls-the-real-server", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        plan = make_plan(args.tickers, args.calls, args.interval_sec)
        result = run(
            args.mode,
            plan=plan,
            credentials=args.credentials,
            token_cache=args.token_cache,
            token_logs=args.token_log,
            contention_confirmed_by=args.no_contention_confirmed_by,
            acknowledged=args.i_understand_this_calls_the_real_server,
            out=args.out,
        )
    except (ProbeRefused, ForbiddenRequest) as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result["manifest"], ensure_ascii=False, indent=2) + "\n")
    if args.mode == "execute" and result["manifest"]["network_calls"] == 0:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
