"""
Unit tests for the 2026-08-27 shared-device session hardening pass:

1. A sliding idle timeout (SESSION_IDLE_TIMEOUT_SECONDS, sessions.last_active)
   layered on top of the existing 7-day absolute expires_at -- a session
   idle-timed-out is treated as unauthenticated by get_current_user(),
   session_auth_middleware, and auth_status() alike, even though expires_at
   is still far in the future.
2. /api/auth/logout now also calls _revoke_cloudflare_access_session() with
   the logged-out user's email, ending Cloudflare Access's own session in
   front of this app, not just our own sessions row.

Called directly as plain async functions, same convention as
tests/test_otp_and_reassignment_firm_scoping.py (see that file's docstring).
Fakes model the WHERE clauses' outcome directly (an idle-expired row simply
isn't returned by fetchrow) rather than simulating Postgres's own interval
arithmetic -- what's under test is that each call site's SQL actually
includes the idle check and acts correctly on its result, not Postgres's
date math itself.
"""
import asyncio
import uuid

import pytest
from starlette.responses import JSONResponse

import backend.main as m
from backend.main import get_current_user, auth_status, logout, session_auth_middleware


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


class _FakeRequest:
    def __init__(self, cookies=None, path="/api/matters"):
        self.cookies = cookies or {}
        self.url = type("U", (), {"path": path})()
        self.headers = {}


# ── get_current_user(): idle-expired session treated as unauthenticated ────

class _SessionConn:
    """valid=True -> the fetchrow's WHERE clause (expires_at AND last_active
    idle check) would match this row in real Postgres; valid=False -> it
    wouldn't (either genuinely idle-expired or absolutely expired), so the
    fake returns None, exactly as the real WHERE clause would filter it out."""
    def __init__(self, valid: bool):
        self.valid = valid
        self.executed = []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        assert "last_active > NOW() - make_interval" in q, "idle-window check missing from query"
        return {"id": uuid.uuid4(), "firm_id": m.FIRM_ID, "phone": "+263771234567",
                "email": "lawyer@example.com", "role": "associate",
                "display_name": "Test Lawyer"} if self.valid else None

    async def execute(self, query, *args):
        self.executed.append((" ".join(query.split()), args))
        return "OK"


def test_get_current_user_rejects_idle_expired_session(monkeypatch):
    monkeypatch.setattr(m, "AUTH_ENABLED", True)
    conn = _SessionConn(valid=False)
    monkeypatch.setattr(m, "_db_pool", _FakePool(conn))

    result = asyncio.run(get_current_user(_FakeRequest(cookies={"mutemo_session": "tok"})))
    assert result is None


def test_get_current_user_accepts_session_within_idle_window(monkeypatch):
    monkeypatch.setattr(m, "AUTH_ENABLED", True)
    conn = _SessionConn(valid=True)
    monkeypatch.setattr(m, "_db_pool", _FakePool(conn))

    result = asyncio.run(get_current_user(_FakeRequest(cookies={"mutemo_session": "tok"})))
    assert result is not None
    assert result["display_name"] == "Test Lawyer"


# ── session_auth_middleware: idle-expired -> 401, valid -> touched + passed through ──

class _MiddlewareConn:
    def __init__(self, valid: bool):
        self.valid = valid
        self.touch_calls = []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        assert "last_active > NOW() - make_interval" in q, "idle-window check missing from query"
        return {"token": "tok"} if self.valid else None

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("UPDATE sessions SET last_active"):
            self.touch_calls.append(args)
        return "OK"


async def _fake_call_next(request):
    return "PASSED_THROUGH"


def test_middleware_401s_an_idle_expired_session(monkeypatch):
    monkeypatch.setattr(m, "AUTH_ENABLED", True)
    monkeypatch.setattr(m, "ADMIN_TOKEN", None)
    monkeypatch.setattr(m, "LEGAL_FEED_SERVICE_TOKEN", None)
    conn = _MiddlewareConn(valid=False)
    monkeypatch.setattr(m, "_db_pool", _FakePool(conn))

    req = _FakeRequest(cookies={"mutemo_session": "tok"}, path="/api/matters")
    result = asyncio.run(session_auth_middleware(req, _fake_call_next))

    assert isinstance(result, JSONResponse)
    assert result.status_code == 401
    assert conn.touch_calls == []  # never touched -- the session was rejected


def test_middleware_touches_last_active_and_passes_through_a_valid_session(monkeypatch):
    monkeypatch.setattr(m, "AUTH_ENABLED", True)
    conn = _MiddlewareConn(valid=True)
    monkeypatch.setattr(m, "_db_pool", _FakePool(conn))

    req = _FakeRequest(cookies={"mutemo_session": "tok"}, path="/api/matters")
    result = asyncio.run(session_auth_middleware(req, _fake_call_next))

    assert result == "PASSED_THROUGH"
    assert len(conn.touch_calls) == 1
    assert conn.touch_calls[0] == ("tok",)


# ── auth_status(): cleans up idle-expired rows too, touches on success ─────

class _AuthStatusConn:
    def __init__(self, valid: bool):
        self.valid = valid
        self.delete_calls = []
        self.touch_calls = []

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("DELETE FROM sessions"):
            assert "last_active < NOW() - make_interval" in q, "idle cleanup missing from DELETE"
            self.delete_calls.append(args)
        elif q.startswith("UPDATE sessions SET last_active"):
            self.touch_calls.append(args)
        return "OK"

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        assert "last_active > NOW() - make_interval" in q, "idle-window check missing from query"
        if not self.valid:
            return None
        return {"token": "tok", "id": uuid.uuid4(), "phone": "+263771234567",
                "role": "associate", "display_name": "Test Lawyer", "initials": "TL"}


def test_auth_status_deletes_idle_expired_sessions_and_reports_unauthenticated(monkeypatch):
    monkeypatch.setattr(m, "AUTH_ENABLED", True)
    conn = _AuthStatusConn(valid=False)
    monkeypatch.setattr(m, "_db_pool", _FakePool(conn))

    result = asyncio.run(auth_status(_FakeRequest(cookies={"mutemo_session": "tok"})))

    assert result == {"auth_enabled": True, "authenticated": False, "firm_name": m.FIRM_NAME}
    assert len(conn.delete_calls) == 1
    assert conn.touch_calls == []


def test_auth_status_touches_last_active_on_a_valid_session(monkeypatch):
    monkeypatch.setattr(m, "AUTH_ENABLED", True)
    conn = _AuthStatusConn(valid=True)
    monkeypatch.setattr(m, "_db_pool", _FakePool(conn))

    result = asyncio.run(auth_status(_FakeRequest(cookies={"mutemo_session": "tok"})))

    assert result["authenticated"] is True
    assert result["display_name"] == "Test Lawyer"
    assert len(conn.touch_calls) == 1


# ── logout(): looks up email, revokes Cloudflare Access, deletes session ───

class _LogoutConn:
    def __init__(self, email):
        self.email = email
        self.deleted_tokens = []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        assert q.startswith("SELECT u.email FROM sessions")
        return {"email": self.email} if self.email else None

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("DELETE FROM sessions"):
            self.deleted_tokens.append(args[0])
        return "OK"


class _FakeResponse:
    def __init__(self):
        self.deleted_cookies = []

    def delete_cookie(self, name):
        self.deleted_cookies.append(name)


def test_logout_revokes_cloudflare_access_with_the_users_email(monkeypatch):
    conn = _LogoutConn(email="lawyer@example.com")
    monkeypatch.setattr(m, "_db_pool", _FakePool(conn))
    revoke_calls = []

    async def fake_revoke(email):
        revoke_calls.append(email)
        return True

    monkeypatch.setattr(m, "_revoke_cloudflare_access_session", fake_revoke)

    req = _FakeRequest(cookies={"mutemo_session": "tok"})
    resp = _FakeResponse()
    result = asyncio.run(logout(req, resp))

    assert result == {"logged_out": True}
    assert conn.deleted_tokens == ["tok"]
    assert revoke_calls == ["lawyer@example.com"]
    assert resp.deleted_cookies == ["mutemo_session"]


def test_logout_still_completes_cleanly_with_no_session_row(monkeypatch):
    """No cookie/no matching row -- e.g. a double logout, or a session that
    already expired naturally. Must not error, must still clear the cookie
    (idempotent, matches the pre-existing behavior this builds on)."""
    conn = _LogoutConn(email=None)
    monkeypatch.setattr(m, "_db_pool", _FakePool(conn))
    revoke_calls = []
    monkeypatch.setattr(m, "_revoke_cloudflare_access_session",
                         lambda email: revoke_calls.append(email))

    req = _FakeRequest(cookies={"mutemo_session": "tok"})
    resp = _FakeResponse()
    result = asyncio.run(logout(req, resp))

    assert result == {"logged_out": True}
    assert revoke_calls == []  # never called -- no email to revoke
    assert resp.deleted_cookies == ["mutemo_session"]


def test_logout_with_no_cookie_at_all_does_not_touch_the_db(monkeypatch):
    conn = _LogoutConn(email="lawyer@example.com")
    monkeypatch.setattr(m, "_db_pool", _FakePool(conn))

    req = _FakeRequest(cookies={})  # no mutemo_session cookie
    resp = _FakeResponse()
    result = asyncio.run(logout(req, resp))

    assert result == {"logged_out": True}
    assert conn.deleted_tokens == []
    assert resp.deleted_cookies == ["mutemo_session"]


# ── _revoke_cloudflare_access_session(): defensive, never raises ───────────

def test_revoke_cloudflare_returns_false_when_vars_not_configured(monkeypatch):
    monkeypatch.setattr(m, "_get_cf_vars", lambda: (None, None, None))
    result = asyncio.run(m._revoke_cloudflare_access_session("lawyer@example.com"))
    assert result is False


def test_revoke_cloudflare_returns_true_on_a_real_success_response(monkeypatch):
    monkeypatch.setattr(m, "_get_cf_vars", lambda: ("fake-token", "fake-account-id", None))

    class _FakeCFResponse:
        status_code = 200
        text = '{"success": true}'
        def json(self):
            return {"success": True}

    class _FakeHttpClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, headers=None, json=None):
            assert "/access/organizations/revoke_user" in url
            assert json == {"email": "lawyer@example.com"}
            return _FakeCFResponse()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeHttpClient())

    result = asyncio.run(m._revoke_cloudflare_access_session("lawyer@example.com"))
    assert result is True


def test_revoke_cloudflare_returns_false_on_403_without_raising(monkeypatch):
    """The real, live-relevant case if CLOUDFLARE_API_TOKEN lacks the
    Organizations-write scope this endpoint needs -- must degrade to a
    logged no-op, never break logout."""
    monkeypatch.setattr(m, "_get_cf_vars", lambda: ("fake-token", "fake-account-id", None))

    class _FakeCFResponse:
        status_code = 403
        text = '{"success": false, "errors": [{"message": "Authentication error"}]}'
        def json(self):
            return {"success": False}

    class _FakeHttpClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, headers=None, json=None):
            return _FakeCFResponse()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeHttpClient())

    result = asyncio.run(m._revoke_cloudflare_access_session("lawyer@example.com"))
    assert result is False


def test_revoke_cloudflare_returns_false_on_network_error_without_raising(monkeypatch):
    monkeypatch.setattr(m, "_get_cf_vars", lambda: ("fake-token", "fake-account-id", None))

    class _FakeHttpClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, headers=None, json=None):
            raise ConnectionError("simulated network failure")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeHttpClient())

    result = asyncio.run(m._revoke_cloudflare_access_session("lawyer@example.com"))
    assert result is False
