"""
Unit tests for Part 2 of the multi-tenancy hardening pass: firm_id added
to otp_store and matter_reassignments as defense-in-depth. Not currently
exploitable under Option B (one deployment per firm) -- these confirm the
new column is actually written/read, not just present in the schema.

Called directly as plain async functions, same convention as
tests/test_document_provenance.py.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import backend.main as m
from backend.main import OTPRequestBody, OTPVerifyBody, ReassignRequest, request_otp, verify_otp


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


# ── request_otp: INSERT binds firm_id ────────────────────────────────────

class _RequestOtpConn:
    def __init__(self):
        self.executed = []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if "SELECT email FROM users" in q:
            return {"email": "lawyer@example.com"}
        raise NotImplementedError(f"unhandled query: {q}")

    async def execute(self, query, *args):
        self.executed.append((" ".join(query.split()), args))
        return "OK"


def test_request_otp_insert_binds_firm_id(monkeypatch):
    conn = _RequestOtpConn()
    monkeypatch.setattr(m, "_db_pool", _FakePool(conn))
    monkeypatch.setattr(m, "AUTH_ENABLED", True)
    monkeypatch.setattr(m, "_send_otp_code", lambda *a, **k: "sms")

    asyncio.run(request_otp(OTPRequestBody(phone="+263771234567")))

    insert_calls = [c for c in conn.executed if c[0].startswith("INSERT INTO otp_store")]
    assert len(insert_calls) == 1
    query, args = insert_calls[0]
    assert "firm_id" in query
    assert m.FIRM_ID in args


# ── verify_otp: every otp_store query scopes by firm_id ──────────────────

class _VerifyOtpConn:
    def __init__(self, entry):
        self.entry = entry
        self.executed = []
        self.fetched = []

    async def execute(self, query, *args):
        self.executed.append((" ".join(query.split()), args))
        return "OK"

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        self.fetched.append((q, args))
        if q.startswith("SELECT * FROM otp_store"):
            return self.entry
        if q.startswith("SELECT * FROM users"):
            return {"id": uuid.uuid4(), "firm_id": m.FIRM_ID, "phone": "+263771234567",
                     "email": None, "role": "partner", "display_name": "Test User"}
        raise NotImplementedError(f"unhandled query: {q}")


def test_verify_otp_select_scopes_by_firm_id(monkeypatch):
    entry = {"phone": "+263771234567", "code": "123456", "attempts": 0,
              "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5)}
    conn = _VerifyOtpConn(entry)
    monkeypatch.setattr(m, "_db_pool", _FakePool(conn))
    monkeypatch.setattr(m, "AUTH_ENABLED", True)

    class _FakeResponse:
        def set_cookie(self, *a, **k):
            pass

    asyncio.run(verify_otp(OTPVerifyBody(phone="+263771234567", code="123456"), _FakeResponse()))

    select_calls = [q for q, a in conn.fetched if q.startswith("SELECT * FROM otp_store")]
    assert len(select_calls) == 1
    assert "firm_id" in select_calls[0]

    update_calls = [c for c in conn.executed if c[0].startswith("UPDATE otp_store")]
    assert len(update_calls) == 1
    assert "firm_id" in update_calls[0][0]
    assert m.FIRM_ID in update_calls[0][1]

    # Two DELETE FROM otp_store calls happen on the success path: the
    # unscoped housekeeping sweep ("expires_at < NOW()", correctly firm-
    # agnostic -- it's cleaning up expired rows across the board) and the
    # phone-specific one clearing this entry, which must be firm-scoped.
    phone_delete_calls = [c for c in conn.executed if c[0].startswith("DELETE FROM otp_store WHERE phone=")]
    assert len(phone_delete_calls) == 1
    assert "firm_id" in phone_delete_calls[0][0]
    assert m.FIRM_ID in phone_delete_calls[0][1]


def test_verify_otp_too_many_attempts_delete_scopes_by_firm_id(monkeypatch):
    entry = {"phone": "+263771234567", "code": "123456", "attempts": m.MAX_OTP_ATTEMPTS,
              "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5)}
    conn = _VerifyOtpConn(entry)
    monkeypatch.setattr(m, "_db_pool", _FakePool(conn))
    monkeypatch.setattr(m, "AUTH_ENABLED", True)

    class _FakeResponse:
        def set_cookie(self, *a, **k):
            pass

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(verify_otp(OTPVerifyBody(phone="+263771234567", code="000000"), _FakeResponse()))
    assert exc_info.value.status_code == 429

    phone_delete_calls = [c for c in conn.executed if c[0].startswith("DELETE FROM otp_store WHERE phone=")]
    assert len(phone_delete_calls) == 1
    assert "firm_id" in phone_delete_calls[0][0]
    assert m.FIRM_ID in phone_delete_calls[0][1]


# ── reassign_matter_spec: INSERT binds firm_id ───────────────────────────

class _ReassignConn:
    def __init__(self, matter):
        self.matter = matter
        self.executed = []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT * FROM matters"):
            return self.matter
        if "organisation_roles" in q:
            return {"role": "ops_manager"}
        raise NotImplementedError(f"unhandled query: {q}")

    async def execute(self, query, *args):
        self.executed.append((" ".join(query.split()), args))
        return "OK"


def test_reassign_matter_insert_binds_the_matters_own_firm_id(monkeypatch):
    from backend.main import reassign_matter_spec

    matter_id = uuid.uuid4()
    to_lawyer_id = uuid.uuid4()
    matter = {"id": matter_id, "firm_id": m.FIRM_ID, "assigned_lawyer_id": None}
    conn = _ReassignConn(matter)
    monkeypatch.setattr(m, "_db_pool", _FakePool(conn))

    class _FakeRequest:
        pass

    async def fake_get_current_user(request):
        return {"id": uuid.uuid4(), "firm_id": m.FIRM_ID, "role": "partner"}

    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)

    asyncio.run(reassign_matter_spec(
        str(matter_id), ReassignRequest(to_lawyer_id=str(to_lawyer_id), reason="Coverage"), _FakeRequest()
    ))

    insert_calls = [c for c in conn.executed if c[0].startswith("INSERT INTO matter_reassignments")]
    assert len(insert_calls) == 1
    query, args = insert_calls[0]
    assert "firm_id" in query
    assert m.FIRM_ID in args
