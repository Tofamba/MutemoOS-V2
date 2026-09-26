"""
Unit tests for POST /api/admin/bootstrap (2026-09-26 standing-access audit).

Before this file, bootstrap_admin() had zero test coverage despite being the
one endpoint that provisions a brand-new firm's very first admin -- exactly
the operation the whole audit was about. Covers: the self-disabling
invariant, the stricter (no-silent-allow) token check, that it creates an
`invites` row rather than a `users` row directly, and the new
MUTEMO_VENDOR_DOMAINS flag added by this same audit.

Called directly as plain async functions, same convention as
tests/test_otp_and_reassignment_firm_scoping.py.
"""
import asyncio
import uuid

import pytest
from fastapi import HTTPException

import backend.main as m
from backend.main import BootstrapAdminRequest, bootstrap_admin


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
    def __init__(self, headers=None):
        self.headers = headers or {}


class _BootstrapConn:
    """existing_admin controls the SELECT EXISTS(...) check; execute() just
    records calls, matching this endpoint's real INSERT ... ON CONFLICT."""
    def __init__(self, existing_admin: bool):
        self.existing_admin = existing_admin
        self.executed = []

    async def fetchval(self, query, *args):
        q = " ".join(query.split())
        assert "SELECT EXISTS" in q and "role='admin'" in q
        return self.existing_admin

    async def execute(self, query, *args):
        self.executed.append((" ".join(query.split()), args))
        return "OK"


def _patch_common(monkeypatch, existing_admin=False):
    monkeypatch.setattr(m, "ADMIN_TOKEN", "real-admin-token")
    monkeypatch.setattr(m, "MUTEMO_VENDOR_DOMAINS", {"tofamba.com"})
    conn = _BootstrapConn(existing_admin=existing_admin)
    monkeypatch.setattr(m, "_db_pool", _FakePool(conn))

    async def fake_add_cf(email):
        return "fake-rule-id"

    async def fake_send_invite_email(email, display_name, invited_by_name):
        return True

    monkeypatch.setattr(m, "_add_email_to_cloudflare_access", fake_add_cf)
    monkeypatch.setattr(m, "_send_invite_email", fake_send_invite_email)
    return conn


# ── Token gating: stricter than require_admin_token() elsewhere ────────────

def test_refuses_with_503_when_admin_token_not_configured(monkeypatch):
    monkeypatch.setattr(m, "ADMIN_TOKEN", None)
    req = BootstrapAdminRequest(phone="+263771234567", email="admin@thefirm.co.zw", display_name="Firm Admin")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(bootstrap_admin(req, _FakeRequest()))
    assert exc_info.value.status_code == 503


def test_refuses_with_403_on_wrong_token(monkeypatch):
    _patch_common(monkeypatch)
    req = BootstrapAdminRequest(phone="+263771234567", email="admin@thefirm.co.zw", display_name="Firm Admin")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(bootstrap_admin(req, _FakeRequest(headers={"X-Admin-Token": "wrong-token"})))
    assert exc_info.value.status_code == 403


# ── Self-disabling: refuses once any active admin exists, token notwithstanding ──

def test_refuses_once_an_active_admin_already_exists(monkeypatch):
    _patch_common(monkeypatch, existing_admin=True)
    req = BootstrapAdminRequest(phone="+263771234567", email="admin@thefirm.co.zw", display_name="Firm Admin")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(bootstrap_admin(req, _FakeRequest(headers={"X-Admin-Token": "real-admin-token"})))
    assert exc_info.value.status_code == 403
    assert "already exists" in exc_info.value.detail


# ── Creates an invite, not a users row directly ─────────────────────────────

def test_success_creates_an_invite_row_not_a_users_row(monkeypatch):
    conn = _patch_common(monkeypatch, existing_admin=False)
    req = BootstrapAdminRequest(phone="+263771234567", email="admin@thefirm.co.zw", display_name="Firm Admin")

    result = asyncio.run(bootstrap_admin(req, _FakeRequest(headers={"X-Admin-Token": "real-admin-token"})))

    assert result["bootstrapped"] is True
    insert_calls = [c for c in conn.executed if c[0].startswith("INSERT INTO invites")]
    assert len(insert_calls) == 1, "bootstrap must create an invites row, not a users row directly"
    assert not any(c[0].startswith("INSERT INTO users") for c in conn.executed)
    query, args = insert_calls[0]
    assert "'admin'" in query or "role" in query  # role is hardcoded to 'admin' in the INSERT text


# ── MUTEMO_VENDOR_DOMAINS: flags, never blocks ──────────────────────────────

def test_vendor_domain_email_is_flagged_but_not_blocked(monkeypatch, capsys):
    _patch_common(monkeypatch, existing_admin=False)
    req = BootstrapAdminRequest(phone="+263771234567", email="someone@tofamba.com", display_name="Vendor Person")

    result = asyncio.run(bootstrap_admin(req, _FakeRequest(headers={"X-Admin-Token": "real-admin-token"})))

    assert result["bootstrapped"] is True  # not blocked
    assert result["vendor_domain_flag"] is True
    assert "vendor domain" in capsys.readouterr().out.lower()


def test_firm_domain_email_is_not_flagged(monkeypatch):
    _patch_common(monkeypatch, existing_admin=False)
    req = BootstrapAdminRequest(phone="+263771234567", email="partner@thefirm.co.zw", display_name="Firm Admin")

    result = asyncio.run(bootstrap_admin(req, _FakeRequest(headers={"X-Admin-Token": "real-admin-token"})))

    assert result["vendor_domain_flag"] is False


def test_vendor_domain_check_is_case_insensitive(monkeypatch):
    _patch_common(monkeypatch, existing_admin=False)
    req = BootstrapAdminRequest(phone="+263771234567", email="Someone@TOFAMBA.com", display_name="Vendor Person")

    result = asyncio.run(bootstrap_admin(req, _FakeRequest(headers={"X-Admin-Token": "real-admin-token"})))

    assert result["vendor_domain_flag"] is True
