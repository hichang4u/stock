import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from src import notifier
from src.utils import tree_role
from src.utils.logger import log

KST = ZoneInfo("Asia/Seoul")
_ROOT = Path(__file__).resolve().parents[2]
_EXPIRY_BUFFER_MIN = 10  # 만료 N분 전 선제 갱신

_token: str = ""
_ws_key: str = ""
_MAX_REFRESH_ATTEMPTS = 3
_REFRESH_BACKOFF_SEC = (2.0, 5.0)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _auth_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=_env_float("KIS_AUTH_CONNECT_TIMEOUT_SEC", 15.0),
        read=_env_float("KIS_AUTH_READ_TIMEOUT_SEC", 10.0),
        write=_env_float("KIS_AUTH_WRITE_TIMEOUT_SEC", 10.0),
        pool=_env_float("KIS_AUTH_POOL_TIMEOUT_SEC", 5.0),
    )


def _retry_delay(attempt: int) -> float:
    index = max(0, min(attempt - 1, len(_REFRESH_BACKOFF_SEC) - 1))
    return _REFRESH_BACKOFF_SEC[index]


def _host(url: str) -> str:
    return urlparse(url).hostname or ""


def _mask(value: str) -> str:
    return value[:4] + "****" if len(value) > 4 else "****"


def _cache_path() -> Path:
    return Path(os.getenv("AUTH_DIR", "data/auth")) / "token_cache.json"


def _load_cache() -> dict:
    p = _cache_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _auth_is_readonly() -> bool:
    """개발 트리는 토큰을 발급하지 않는다 — 운영이 유일한 발급자다 (스펙 5절).

    개발의 AUTH_DIR은 운영 디렉터리를 가리킨다. APP_KEY가 하나뿐이라 캐시를
    나눠 봐야 같은 앱키 재발급이 운영 토큰을 무효화할 수 있으므로, 개발은
    운영이 발급해 둔 캐시를 읽기만 한다.

    `KIS_AUTH_READONLY`가 명시되면 그 값이 이긴다. 운영이 REAL로 가면 PAPER
    토큰은 발급 호스트가 달라 개발이 자기 AUTH_DIR에서 직접 발급해야 하므로,
    끌 수 있어야 한다(스펙 6-5절). 값이 없으면 역할이 기본값을 정한다 —
    개발이면 읽기 전용. 플래그를 .env에서 빠뜨려도 안전한 쪽으로 기운다.
    """
    raw = os.getenv("KIS_AUTH_READONLY", "").strip()
    if raw:
        return raw.lower() not in ("0", "false", "no", "off")
    return tree_role.read_role(_ROOT) == tree_role.ROLE_DEV


def _save_cache(token: str, expires_at: str = "") -> None:
    """원자적 쓰기 (tmp → rename). 읽기 전용 트리에서는 건너뛴다."""
    if _auth_is_readonly():
        log("TOKEN_CACHE_WRITE_SKIPPED", level="INFO",
            auth_dir=os.getenv("AUTH_DIR", "data/auth"))
        return
    p = _cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"access_token": token, "expires_at": expires_at}, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(p)


def _is_expiring(cache: dict) -> bool:
    """만료 N분 이내이거나 만료 정보 없으면 True → 선제 갱신 대상."""
    expires_at_str = cache.get("expires_at")
    if not expires_at_str:
        return True
    try:
        expires_at = datetime.strptime(expires_at_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        return datetime.now(KST) >= expires_at - timedelta(minutes=_EXPIRY_BUFFER_MIN)
    except Exception:
        return True


async def refresh() -> str:
    """
    KIS Access Token 재발급 (PRD §5-1).
    실패 시 2초 간격 3회 재시도. 전부 실패 시 CRIT 알림.
    """
    global _token
    if _auth_is_readonly():
        # 조용히 실패하면 개발이 왜 안 도는지 알 수 없다. 크게 알린다.
        log("TOKEN_ISSUE_REFUSED_READONLY", level="CRIT",
            auth_dir=os.getenv("AUTH_DIR", "data/auth"))
        await notifier.send(
            "TOKEN_ISSUE_REFUSED_READONLY", level="CRIT",
            message="운영 토큰 만료 — 읽기 전용 트리는 발급하지 않는다. 운영을 확인하라",
        )
        return ""

    base_url = os.getenv("KIS_BASE_URL", "")
    url = f"{base_url}/oauth2/tokenP"
    payload = {
        "grant_type": "client_credentials",
        "appkey": os.getenv("KIS_APP_KEY", ""),
        "appsecret": os.getenv("KIS_APP_SECRET", ""),
    }
    timeout = _auth_timeout()
    mode = os.getenv("KIS_MODE", "PAPER")
    host = _host(url)
    for attempt in range(1, _MAX_REFRESH_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload)
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            if resp.status_code == 200:
                data = resp.json()
                _token = data["access_token"]
                expires_at = data.get("access_token_token_expired", "")
                _save_cache(_token, expires_at)
                log("TOKEN_REFRESHED", level="INFO",
                    token_prefix=_mask(_token), expires_at=expires_at,
                    attempt=attempt, elapsed_ms=elapsed_ms, host=host, mode=mode)
                return _token
            log("TOKEN_REFRESH_HTTP_ERR", level="WARN",
                attempt=attempt, max_attempts=_MAX_REFRESH_ATTEMPTS,
                status=resp.status_code, elapsed_ms=elapsed_ms,
                host=host, mode=mode)
        except Exception as e:
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            next_delay_sec = _retry_delay(attempt) if attempt < _MAX_REFRESH_ATTEMPTS else 0
            log(
                "TOKEN_REFRESH_ATTEMPT_FAIL",
                level="WARN",
                attempt=attempt,
                max_attempts=_MAX_REFRESH_ATTEMPTS,
                error=repr(e),
                exception_type=type(e).__name__,
                elapsed_ms=elapsed_ms,
                next_delay_sec=next_delay_sec,
                host=host,
                mode=mode,
            )
        if attempt < _MAX_REFRESH_ATTEMPTS:
            await asyncio.sleep(_retry_delay(attempt))

    log("TOKEN_REFRESH_FAIL", level="CRIT")
    await notifier.send("TOKEN_REFRESH_FAIL", level="CRIT", message="KIS 토큰 갱신 실패")
    return ""


async def load_or_refresh() -> str:
    """캐시에서 로드. 만료 10분 이내이면 선제 갱신."""
    global _token
    cache = _load_cache()
    if cache.get("access_token") and not _is_expiring(cache):
        _token = cache["access_token"]
        log("TOKEN_LOADED_FROM_CACHE", level="INFO")
        return _token
    return await refresh()


async def refresh_ws_key() -> str:
    """
    WebSocket 전용 접속키 발급 (PRD §6-3).
    OAuth access_token과 별개. /oauth2/Approval 엔드포인트.
    유효기간 24시간 — subscribe() 진입 시 1회 호출로 충분.
    """
    global _ws_key
    base_url = os.getenv("KIS_BASE_URL", "")
    url = f"{base_url}/oauth2/Approval"
    payload = {
        "grant_type": "client_credentials",
        "appkey": os.getenv("KIS_APP_KEY", ""),
        "secretkey": os.getenv("KIS_APP_SECRET", ""),
    }
    timeout = _auth_timeout()
    mode = os.getenv("KIS_MODE", "PAPER")
    host = _host(url)
    for attempt in range(1, _MAX_REFRESH_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload)
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            if resp.status_code == 200:
                _ws_key = resp.json().get("approval_key", "")
                log("WS_KEY_REFRESHED", level="INFO", key_prefix=_mask(_ws_key),
                    attempt=attempt, elapsed_ms=elapsed_ms, host=host, mode=mode)
                return _ws_key
            log("WS_KEY_HTTP_ERR", level="WARN",
                attempt=attempt, max_attempts=_MAX_REFRESH_ATTEMPTS,
                status=resp.status_code, elapsed_ms=elapsed_ms,
                host=host, mode=mode)
        except Exception as e:
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            next_delay_sec = _retry_delay(attempt) if attempt < _MAX_REFRESH_ATTEMPTS else 0
            log(
                "WS_KEY_ATTEMPT_FAIL",
                level="WARN",
                attempt=attempt,
                max_attempts=_MAX_REFRESH_ATTEMPTS,
                error=repr(e),
                exception_type=type(e).__name__,
                elapsed_ms=elapsed_ms,
                next_delay_sec=next_delay_sec,
                host=host,
                mode=mode,
            )
        if attempt < _MAX_REFRESH_ATTEMPTS:
            await asyncio.sleep(_retry_delay(attempt))

    log("WS_KEY_REFRESH_FAIL", level="CRIT")
    await notifier.send("WS_KEY_REFRESH_FAIL", level="CRIT", message="실시간 접속키 갱신 실패")
    return ""


async def revoke(token: str = "") -> bool:
    """접근토큰 폐기 [인증-002]. token 생략 시 현재 세션 토큰 폐기.

    읽기 전용 트리에서는 거부한다. APP_KEY가 하나라 폐기는 운영이 쓰는 바로
    그 토큰을 KIS에서 죽이고, 운영 캐시 파일까지 지운다.
    """
    global _token
    if _auth_is_readonly():
        log("TOKEN_REVOKE_REFUSED_DEV", level="WARN",
            reason="읽기 전용 트리는 공유 토큰을 폐기할 수 없다")
        return False
    target = token or _token
    if not target:
        log("TOKEN_REVOKE_SKIP", level="WARN", reason="no_token")
        return False

    base_url = os.getenv("KIS_BASE_URL", "")
    url = f"{base_url}/oauth2/revokeP"
    payload = {
        "appkey":    os.getenv("KIS_APP_KEY", ""),
        "appsecret": os.getenv("KIS_APP_SECRET", ""),
        "token":     target,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
        data = resp.json()
        code = data.get("code", "")
        msg  = data.get("message", "").strip()
        if resp.status_code == 200 and str(code) == "200":
            _token = ""
            p = _cache_path()
            if p.exists():
                p.unlink(missing_ok=True)
            log("TOKEN_REVOKED", level="INFO", code=code, msg=msg)
            return True
        log("TOKEN_REVOKE_FAIL", level="WARN",
            status=resp.status_code, code=code, msg=msg)
        return False
    except Exception as e:
        log("TOKEN_REVOKE_ERR", level="WARN", error=repr(e))
        return False


def get() -> str:
    return _token


def get_ws_key() -> str:
    return _ws_key
