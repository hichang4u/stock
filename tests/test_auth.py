import json
from unittest.mock import AsyncMock

import pytest

import src.api.auth as auth


@pytest.fixture(autouse=True)
def _writable_auth_tree(monkeypatch):
    """기본은 발급 가능한 트리. 읽기 전용 동작은 각 테스트가 명시적으로 켠다."""
    monkeypatch.setattr(auth.tree_role, "read_role", lambda root: "prod")
    monkeypatch.delenv("KIS_AUTH_READONLY", raising=False)


class _FakeResponse:
    status_code = 500

    def json(self):
        return {}


class _FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return _FakeResponse()


@pytest.mark.asyncio
async def test_refresh_sends_critical_alert_after_exhausted_attempts(monkeypatch):
    notify = AsyncMock()

    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda *args, **kwargs: _FakeClient())
    monkeypatch.setattr(auth.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(auth.notifier, "send", notify)

    assert await auth.refresh() == ""
    notify.assert_awaited_once_with(
        "TOKEN_REFRESH_FAIL",
        level="CRIT",
        message="KIS 토큰 갱신 실패",
    )


@pytest.mark.asyncio
async def test_refresh_ws_key_sends_critical_alert_after_exhausted_attempts(monkeypatch):
    notify = AsyncMock()

    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda *args, **kwargs: _FakeClient())
    monkeypatch.setattr(auth.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(auth.notifier, "send", notify)

    assert await auth.refresh_ws_key() == ""
    notify.assert_awaited_once_with(
        "WS_KEY_REFRESH_FAIL",
        level="CRIT",
        message="실시간 접속키 갱신 실패",
    )

class _SuccessResponse:
    status_code = 200

    def json(self):
        return {
            "access_token": "token-value",
            "access_token_token_expired": "2026-07-08 08:30:23",
        }


@pytest.mark.asyncio
async def test_refresh_uses_configurable_auth_timeout_and_logs_diagnostics(monkeypatch, tmp_path):
    captured = {}

    class _SuccessClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return _SuccessResponse()

    logs = []
    monkeypatch.setenv("AUTH_DIR", str(tmp_path))
    monkeypatch.setenv("KIS_BASE_URL", "https://openapivts.koreainvestment.com:29443")
    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("KIS_AUTH_CONNECT_TIMEOUT_SEC", "15.5")
    monkeypatch.setattr(auth.httpx, "AsyncClient", _SuccessClient)
    monkeypatch.setattr(auth, "log", lambda event, **kwargs: logs.append((event, kwargs)))

    assert await auth.refresh() == "token-value"

    timeout = captured["timeout"]
    assert timeout.connect == 15.5
    assert timeout.read == 10.0
    assert timeout.write == 10.0
    assert timeout.pool == 5.0

    event, payload = logs[-1]
    assert event == "TOKEN_REFRESHED"
    assert payload["attempt"] == 1
    assert payload["host"] == "openapivts.koreainvestment.com"
    assert payload["mode"] == "PAPER"
    assert "elapsed_ms" in payload


def test_save_cache_skips_the_write_in_a_dev_tree(monkeypatch, tmp_path):
    """개발 트리의 AUTH_DIR은 운영을 가리킨다 — 갱신이 운영 캐시를 덮으면 안 된다."""
    logs: list[str] = []
    monkeypatch.setenv("AUTH_DIR", str(tmp_path))
    monkeypatch.setattr(auth.tree_role, "read_role", lambda root: "dev")
    monkeypatch.setattr(auth, "log", lambda event, **kwargs: logs.append(event))

    auth._save_cache("DEV_TOKEN", "2099-12-31 23:59:59")

    assert list(tmp_path.iterdir()) == []
    assert "TOKEN_CACHE_WRITE_SKIPPED" in logs


def test_save_cache_writes_in_a_prod_tree(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_DIR", str(tmp_path))
    monkeypatch.setattr(auth.tree_role, "read_role", lambda root: "prod")

    auth._save_cache("PROD_TOKEN", "2099-12-31 23:59:59")

    cached = json.loads((tmp_path / "token_cache.json").read_text(encoding="utf-8"))
    assert cached["access_token"] == "PROD_TOKEN"


def test_save_cache_writes_when_the_role_is_unknown(monkeypatch, tmp_path):
    """역할 불명은 쓰기를 막지 않는다 — 기동 경로가 이미 fail-closed로 거른다."""
    monkeypatch.setenv("AUTH_DIR", str(tmp_path))
    monkeypatch.setattr(auth.tree_role, "read_role", lambda root: None)

    auth._save_cache("UNKNOWN_TOKEN", "2099-12-31 23:59:59")

    cached = json.loads((tmp_path / "token_cache.json").read_text(encoding="utf-8"))
    assert cached["access_token"] == "UNKNOWN_TOKEN"


@pytest.mark.asyncio
async def test_revoke_refuses_in_a_dev_tree(monkeypatch):
    """폐기는 공유 토큰을 KIS에서 죽인다 — 개발이 운영 인증을 끊으면 안 된다."""
    logs: list[str] = []
    posted = AsyncMock()
    monkeypatch.setattr(auth.tree_role, "read_role", lambda root: "dev")
    monkeypatch.setattr(auth, "log", lambda event, **kwargs: logs.append(event))
    monkeypatch.setattr(auth.httpx, "AsyncClient", posted)

    assert await auth.revoke("SOME_TOKEN") is False
    posted.assert_not_called()
    assert "TOKEN_REVOKE_REFUSED_DEV" in logs


@pytest.mark.asyncio
async def test_refresh_refuses_to_issue_when_readonly(monkeypatch):
    """개발은 발급하지 않는다 — 같은 앱키 재발급이 운영 토큰을 무효화할 수 있다."""
    logs: list[str] = []
    notify = AsyncMock()
    posted = AsyncMock()
    monkeypatch.setattr(auth.tree_role, "read_role", lambda root: "dev")
    monkeypatch.setattr(auth, "log", lambda event, **kwargs: logs.append(event))
    monkeypatch.setattr(auth.notifier, "send", notify)
    monkeypatch.setattr(auth.httpx, "AsyncClient", posted)

    assert await auth.refresh() == ""
    posted.assert_not_called()
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "TOKEN_ISSUE_REFUSED_READONLY"
    assert notify.await_args.kwargs["level"] == "CRIT"
    assert "TOKEN_ISSUE_REFUSED_READONLY" in logs


@pytest.mark.asyncio
async def test_load_or_refresh_still_uses_a_valid_shared_cache_when_readonly(
    monkeypatch, tmp_path
):
    """읽기 전용이어도 유효한 운영 캐시는 그대로 쓴다."""
    monkeypatch.setenv("AUTH_DIR", str(tmp_path))
    monkeypatch.setattr(auth.tree_role, "read_role", lambda root: "dev")
    (tmp_path / "token_cache.json").write_text(
        json.dumps({"access_token": "SHARED", "expires_at": "2099-12-31 23:59:59"}),
        encoding="utf-8",
    )

    assert await auth.load_or_refresh() == "SHARED"


def test_readonly_flag_overrides_the_role_in_both_directions(monkeypatch):
    """REAL 전환 시 개발이 자기 토큰을 발급해야 하므로 플래그가 역할을 이긴다."""
    monkeypatch.setattr(auth.tree_role, "read_role", lambda root: "dev")
    monkeypatch.setenv("KIS_AUTH_READONLY", "0")
    assert auth._auth_is_readonly() is False

    monkeypatch.setattr(auth.tree_role, "read_role", lambda root: "prod")
    monkeypatch.setenv("KIS_AUTH_READONLY", "1")
    assert auth._auth_is_readonly() is True


def test_readonly_defaults_to_the_tree_role_when_the_flag_is_absent(monkeypatch):
    """플래그를 .env에서 빠뜨려도 개발은 안전한 쪽으로 기운다."""
    monkeypatch.delenv("KIS_AUTH_READONLY", raising=False)
    monkeypatch.setattr(auth.tree_role, "read_role", lambda root: "dev")
    assert auth._auth_is_readonly() is True

    monkeypatch.setattr(auth.tree_role, "read_role", lambda root: "prod")
    assert auth._auth_is_readonly() is False
