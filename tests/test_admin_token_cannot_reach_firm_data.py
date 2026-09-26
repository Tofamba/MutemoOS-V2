"""
Unit tests proving MUTEMO_ADMIN_TOKEN alone cannot reach firm-private data
(2026-09-26 standing-access audit).

session_auth_middleware treats a valid X-Admin-Token as sufficient to pass
its own 401 gate (see the comment above that check in backend/main.py) --
that bypass exists for /api/admin/* tooling. The invariant this file proves
is that the bypass stops there: every ordinary firm-data route still calls
get_current_user()/_check_permission(), which key off the session cookie
alone and know nothing about X-Admin-Token, so a request carrying only the
token and no session still ends up unauthenticated at the route level.

Called directly as plain async functions, same convention as
tests/test_session_hardening.py (which this borrows its fakes from).
"""
import asyncio

import pytest
from fastapi import HTTPException

import backend.main as m
from backend.main import get_current_user, session_auth_middleware, _check_permission


class _FakeRequest:
    def __init__(self, headers=None, cookies=None, path="/api/matters"):
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.url = type("U", (), {"path": path})()


def test_middleware_passes_admin_token_through_but_no_session_exists(monkeypatch):
    """Confirms the documented bypass: session_auth_middleware DOES let an
    admin-token-only request reach the route handler (that's intentional,
    for /api/admin/*) -- the real gate is what happens next."""
    monkeypatch.setattr(m, "AUTH_ENABLED", True)
    monkeypatch.setattr(m, "ADMIN_TOKEN", "real-admin-token")
    monkeypatch.setattr(m, "_db_pool", None)  # no session cookie anyway, never queried

    req = _FakeRequest(headers={"X-Admin-Token": "real-admin-token"},
                        cookies={}, path="/api/clients")

    called = {"passed": False}

    async def call_next(request):
        called["passed"] = True
        return "handler-reached"

    result = asyncio.run(session_auth_middleware(req, call_next))
    assert called["passed"] is True
    assert result == "handler-reached"


@pytest.mark.parametrize("path", ["/api/clients", "/api/matters", "/api/clients/some-id/aml-cdd-report"])
def test_get_current_user_ignores_admin_token_header_entirely(monkeypatch, path):
    """The actual data gate: get_current_user() only ever looks at the
    mutemo_session cookie. An admin-token header with no session cookie
    must resolve to no user, on every firm-data path, not just one."""
    monkeypatch.setattr(m, "AUTH_ENABLED", True)
    monkeypatch.setattr(m, "_db_pool", None)  # would only be touched if a cookie were present

    req = _FakeRequest(headers={"X-Admin-Token": "real-admin-token"}, cookies={}, path=path)

    user = asyncio.run(get_current_user(req))
    assert user is None


def test_check_permission_401s_when_admin_token_was_the_only_credential(monkeypatch):
    """End-to-end of the invariant: middleware bypass + no real user together
    still land on a 401 at the permission check, for both partner-tier and
    admin-only permissions."""
    monkeypatch.setattr(m, "AUTH_ENABLED", True)
    monkeypatch.setattr(m, "_db_pool", None)

    req = _FakeRequest(headers={"X-Admin-Token": "real-admin-token"}, cookies={}, path="/api/clients")
    user = asyncio.run(get_current_user(req))

    for permission in ("client:read", "admin:users", "reports:rbz_compliance"):
        with pytest.raises(HTTPException) as exc_info:
            _check_permission(user, permission)
        assert exc_info.value.status_code == 401
