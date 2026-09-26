"""
Unit tests for PATCH /api/users/{user_id}'s deactivation cascade
(2026-09-26 standing-access audit).

is_active was already a writable field on this endpoint, but flipping it did
NOT actually cut off access: get_current_user()/session_auth_middleware key
off the sessions table alone and never checked users.is_active, and the
per-user reminder/digest scheduler (_maybe_send_reminder/_maybe_send_digest)
joins user_reminder_settings to users with no is_active filter either. A
"deactivated" user with a still-live session, or an opted-in reminder/digest
subscription, kept working/receiving mail regardless. This file proves the
fix: deactivating now kills the session, disables reminder/digest delivery,
and revokes/removes Cloudflare Access; reactivating restores Cloudflare
Access; and a user can never deactivate themselves.

Called directly as plain async functions, same convention as
tests/test_session_hardening.py.
"""
import asyncio
import uuid

import pytest
from fastapi import HTTPException

import backend.main as m
from backend.main import update_user


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
    pass


class _UpdateUserConn:
    def __init__(self, updated_row):
        self.updated_row = updated_row
        self.executed = []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        assert q.startswith("UPDATE users SET"), f"unexpected fetchrow: {q}"
        return self.updated_row

    async def execute(self, query, *args):
        self.executed.append((" ".join(query.split()), args))
        return "OK"


def _acting_admin_id():
    return uuid.uuid4()


def _patch_actor(monkeypatch, actor_id):
    async def fake_get_current_user(request):
        return {"id": actor_id, "role": "admin", "firm_id": m.FIRM_ID}
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def _patch_cf(monkeypatch):
    calls = {"revoke_session": [], "remove_access": [], "add_access": []}

    async def fake_revoke(email):
        calls["revoke_session"].append(email)
        return True

    async def fake_remove(email):
        calls["remove_access"].append(email)
        return True

    async def fake_add(email):
        calls["add_access"].append(email)
        return "rule-id"

    monkeypatch.setattr(m, "_revoke_cloudflare_access_session", fake_revoke)
    monkeypatch.setattr(m, "_remove_email_from_cloudflare_access", fake_remove)
    monkeypatch.setattr(m, "_add_email_to_cloudflare_access", fake_add)
    return calls


def test_deactivating_kills_session_disables_reminders_and_revokes_cloudflare(monkeypatch):
    target_id = uuid.uuid4()
    row = {"id": target_id, "email": "departing@thefirm.co.zw", "phone": "+263771111111",
           "display_name": "Departing Staff", "role": "associate", "is_active": False,
           "firm_id": m.FIRM_ID}
    conn = _UpdateUserConn(row)
    monkeypatch.setattr(m, "_db_pool", _FakePool(conn))
    _patch_actor(monkeypatch, _acting_admin_id())
    cf_calls = _patch_cf(monkeypatch)

    result = asyncio.run(update_user(str(target_id), {"is_active": False}, _FakeRequest()))

    assert result["is_active"] is False

    session_deletes = [c for c in conn.executed if c[0] == "DELETE FROM sessions WHERE user_id=$1"]
    assert len(session_deletes) == 1
    assert session_deletes[0][1] == (target_id,)

    reminder_disables = [c for c in conn.executed if c[0].startswith("UPDATE user_reminder_settings")]
    assert len(reminder_disables) == 1
    assert "enabled=FALSE" in reminder_disables[0][0]
    assert "digest_enabled=FALSE" in reminder_disables[0][0]

    assert cf_calls["revoke_session"] == ["departing@thefirm.co.zw"]
    assert cf_calls["remove_access"] == ["departing@thefirm.co.zw"]
    assert cf_calls["add_access"] == []


def test_reactivating_re_adds_cloudflare_access_and_does_not_touch_sessions(monkeypatch):
    target_id = uuid.uuid4()
    row = {"id": target_id, "email": "returning@thefirm.co.zw", "phone": "+263772222222",
           "display_name": "Returning Staff", "role": "associate", "is_active": True,
           "firm_id": m.FIRM_ID}
    conn = _UpdateUserConn(row)
    monkeypatch.setattr(m, "_db_pool", _FakePool(conn))
    _patch_actor(monkeypatch, _acting_admin_id())
    cf_calls = _patch_cf(monkeypatch)

    result = asyncio.run(update_user(str(target_id), {"is_active": True}, _FakeRequest()))

    assert result["is_active"] is True
    assert cf_calls["add_access"] == ["returning@thefirm.co.zw"]
    assert cf_calls["revoke_session"] == []
    assert cf_calls["remove_access"] == []
    assert not any(c[0].startswith("DELETE FROM sessions") for c in conn.executed)
    assert not any(c[0].startswith("UPDATE user_reminder_settings") for c in conn.executed)


def test_role_only_update_never_triggers_the_deactivation_cascade(monkeypatch):
    target_id = uuid.uuid4()
    row = {"id": target_id, "email": "lawyer@thefirm.co.zw", "phone": "+263773333333",
           "display_name": "Lawyer", "role": "partner", "is_active": True, "firm_id": m.FIRM_ID}
    conn = _UpdateUserConn(row)
    monkeypatch.setattr(m, "_db_pool", _FakePool(conn))
    _patch_actor(monkeypatch, _acting_admin_id())
    cf_calls = _patch_cf(monkeypatch)

    asyncio.run(update_user(str(target_id), {"role": "partner"}, _FakeRequest()))

    assert cf_calls["revoke_session"] == cf_calls["remove_access"] == cf_calls["add_access"] == []
    assert not any(c[0].startswith("DELETE FROM sessions") for c in conn.executed)


def test_cannot_deactivate_own_account(monkeypatch):
    self_id = _acting_admin_id()
    conn = _UpdateUserConn(updated_row={})  # never reached
    monkeypatch.setattr(m, "_db_pool", _FakePool(conn))
    _patch_actor(monkeypatch, self_id)
    _patch_cf(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_user(str(self_id), {"is_active": False}, _FakeRequest()))

    assert exc_info.value.status_code == 400
    assert conn.executed == []  # refused before ever touching the DB


def test_deactivating_a_user_with_no_email_on_file_skips_cloudflare_gracefully(monkeypatch):
    target_id = uuid.uuid4()
    row = {"id": target_id, "email": None, "phone": "+263774444444",
           "display_name": "No Email User", "role": "secretary", "is_active": False,
           "firm_id": m.FIRM_ID}
    conn = _UpdateUserConn(row)
    monkeypatch.setattr(m, "_db_pool", _FakePool(conn))
    _patch_actor(monkeypatch, _acting_admin_id())
    cf_calls = _patch_cf(monkeypatch)

    result = asyncio.run(update_user(str(target_id), {"is_active": False}, _FakeRequest()))

    assert result["is_active"] is False
    assert cf_calls["revoke_session"] == cf_calls["remove_access"] == []
    assert any(c[0] == "DELETE FROM sessions WHERE user_id=$1" for c in conn.executed)
