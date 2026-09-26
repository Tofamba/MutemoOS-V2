"""
Unit tests distinguishing "no session" from "fake session" in
get_current_user() (2026-09-26 standing-access audit).

get_current_user() has two, deliberately different, code paths: when
AUTH_ENABLED is False it returns a synthetic "Demo User" partner dict with
no real identity (the local/dev fallback); when AUTH_ENABLED is True it
must return either a real user row or None -- NEVER the synthetic dict,
regardless of why the session lookup failed (missing cookie, no DB pool,
unrecognized token, idle-expired). Mixing these up would mean a runtime
failure in a properly-configured, AUTH_ENABLED=True deployment silently
degrades into unauthenticated full access instead of a 401/500.

Called directly as plain async functions, same convention as
tests/test_session_hardening.py (fakes copied from there).
"""
import asyncio
import uuid

import backend.main as m
from backend.main import get_current_user


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
    def __init__(self, cookies=None):
        self.cookies = cookies or {}


class _NoMatchConn:
    """Models every real 'no valid session' outcome the WHERE clause could
    produce (unknown token, idle-expired, absolutely expired) -- they're all
    indistinguishable from fetchrow's point of view: no row comes back."""
    async def fetchrow(self, query, *args):
        return None


SYNTHETIC_ROLE = "partner"
SYNTHETIC_DISPLAY_NAME = "Demo User"


def _assert_not_synthetic(result):
    assert result is None, (
        f"AUTH_ENABLED=True must never fall back to the synthetic dev user; got {result!r}"
    )


def test_no_cookie_at_all_returns_none_not_synthetic_user(monkeypatch):
    monkeypatch.setattr(m, "AUTH_ENABLED", True)
    monkeypatch.setattr(m, "_db_pool", _FakePool(_NoMatchConn()))
    result = asyncio.run(get_current_user(_FakeRequest(cookies={})))
    _assert_not_synthetic(result)


def test_unrecognized_token_returns_none_not_synthetic_user(monkeypatch):
    monkeypatch.setattr(m, "AUTH_ENABLED", True)
    monkeypatch.setattr(m, "_db_pool", _FakePool(_NoMatchConn()))
    result = asyncio.run(get_current_user(_FakeRequest(cookies={"mutemo_session": "not-a-real-token"})))
    _assert_not_synthetic(result)


def test_no_db_pool_returns_none_not_synthetic_user(monkeypatch):
    """A DB outage must fail closed, not fail open into the demo user."""
    monkeypatch.setattr(m, "AUTH_ENABLED", True)
    monkeypatch.setattr(m, "_db_pool", None)
    result = asyncio.run(get_current_user(_FakeRequest(cookies={"mutemo_session": "some-token"})))
    _assert_not_synthetic(result)


def test_auth_disabled_does_return_the_synthetic_user_by_contract(monkeypatch):
    """Contrast case, not a hole: this IS the documented dev/demo fallback,
    and only fires when AUTH_ENABLED is False (guarded on Railway by the
    separate startup check requiring MUTEMO_ALLOW_DEV_AUTH)."""
    monkeypatch.setattr(m, "AUTH_ENABLED", False)
    result = asyncio.run(get_current_user(_FakeRequest(cookies={})))
    assert result is not None
    assert result["role"] == SYNTHETIC_ROLE
    assert result["display_name"] == SYNTHETIC_DISPLAY_NAME
    assert result["id"] is None  # no real identity, by design
