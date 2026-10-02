"""
Unit tests for Matter Collaboration stage 2 (backend/main.py, 2026-10-02):
invite / list / revoke-or-leave on a matter, plus the colleague picker.

Design under test: only the matter's RESPONSIBLE LAWYER (not a partner/admin
override) may invite or revoke; a collaborator may leave; the list is visible
only to participants; access starts immediately; every change is audit-logged
under a non-compliance target; the invitee is notified by email and Home. It is
a coordination record, not an access grant -- no existing permission changes.

Plain async calls against a hand-built fake DB (same convention as
tests/test_matter_responsible_lawyer.py).
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from fastapi import HTTPException

from backend.main import (
    COLLABORATION_REASON_MAX_LENGTH, FIRM_ID, CollaboratorInvite, invite_collaborator,
    list_colleagues, list_matter_collaborators, revoke_collaborator,
)

NOW = datetime.now(timezone.utc)


class FakeConnection:
    def __init__(self, matters, users, collabs=None):
        self.matters, self.users = matters, users
        self.collabs = collabs if collabs is not None else []
        self.executed = []

    def _active(self, c):
        return c["revoked_at"] is None and (c["expires_at"] is None or c["expires_at"] > NOW)

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT id, name, matter_number, responsible_lawyer_id, is_sentinel FROM matters"):
            return next((m for m in self.matters if m["id"] == args[0] and m["firm_id"] == args[1]), None)
        if q.startswith("SELECT id, responsible_lawyer_id FROM matters"):
            return next((m for m in self.matters if m["id"] == args[0] and m["firm_id"] == args[1]), None)
        if q.startswith("SELECT id, name, responsible_lawyer_id FROM matters"):
            return next((m for m in self.matters if m["id"] == args[0] and m["firm_id"] == args[1]), None)
        if q.startswith("SELECT id, display_name, is_active FROM users"):
            return next((u for u in self.users if u["id"] == args[0] and u["firm_id"] == args[1]), None)
        if q.startswith("INSERT INTO matter_collaborators"):
            firm_id, matter_id, user_id, invited_by, reason = args
            if any(c["matter_id"] == matter_id and c["user_id"] == user_id and c["revoked_at"] is None for c in self.collabs):
                raise asyncpg.UniqueViolationError("duplicate active collaboration")
            row = {"id": uuid.uuid4(), "firm_id": firm_id, "matter_id": matter_id, "user_id": user_id,
                   "invited_by": invited_by, "reason": reason, "created_at": NOW, "expires_at": None,
                   "revoked_at": None, "revoked_by": None}
            self.collabs.append(row)
            return dict(row)
        if q.startswith("SELECT c.id, c.user_id, u.display_name FROM matter_collaborators c"):
            cid, mid, firm = args
            for c in self.collabs:
                if c["id"] == cid and c["matter_id"] == mid and c["firm_id"] == firm and c["revoked_at"] is None:
                    u = next(u for u in self.users if u["id"] == c["user_id"])
                    return {"id": c["id"], "user_id": c["user_id"], "display_name": u["display_name"]}
            return None
        raise NotImplementedError(q)

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT c.id, c.user_id, c.reason, c.created_at, c.expires_at"):
            mid, firm = args
            out = []
            for c in self.collabs:
                if c["matter_id"] == mid and c["firm_id"] == firm and self._active(c):
                    u = next(u for u in self.users if u["id"] == c["user_id"])
                    ib = next((u2 for u2 in self.users if u2["id"] == c["invited_by"]), None)
                    out.append({"id": c["id"], "user_id": c["user_id"], "reason": c["reason"],
                                "created_at": c["created_at"], "expires_at": c["expires_at"],
                                "display_name": u["display_name"],
                                "invited_by_name": ib["display_name"] if ib else None})
            return out
        if q.startswith("SELECT id, display_name FROM users WHERE firm_id=$1 AND is_active=TRUE"):
            firm, me = args
            return sorted([{"id": u["id"], "display_name": u["display_name"]} for u in self.users
                           if u["firm_id"] == firm and u["is_active"] and u["id"] != me],
                          key=lambda r: r["display_name"])
        raise NotImplementedError(q)

    async def execute(self, query, *args):
        q = " ".join(query.split())
        self.executed.append((q, args))
        if q.startswith("UPDATE matter_collaborators SET revoked_at=NOW()"):
            revoked_by, cid = args
            for c in self.collabs:
                if c["id"] == cid and c["revoked_at"] is None:
                    c["revoked_at"], c["revoked_by"] = NOW, revoked_by
        return "OK"


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Ctx(self.conn)


def _user(name, role="associate", active=True, firm_id=FIRM_ID):
    return {"id": uuid.uuid4(), "firm_id": firm_id, "display_name": name, "role": role, "is_active": active}


def _setup(monkeypatch, owner_is_caller=True, caller_role="associate"):
    import backend.main as m
    caller = _user("Caller Lawyer", caller_role)
    ralph = _user("Ralph Labour")
    other = _user("Other Person")
    owner = caller if owner_is_caller else _user("Real Owner", "partner")
    matter = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "name": "Moyo v Dube", "matter_number": "DU-009-01",
              "responsible_lawyer_id": owner["id"], "is_sentinel": False}
    users = [caller, ralph, other] + ([] if owner_is_caller else [owner])
    conn = FakeConnection([matter], users)
    monkeypatch.setattr(m, "_db_pool", FakePool(conn))
    emails = []

    async def fake_send(user_id, subject, body, **kw):
        emails.append((user_id, subject, body))
        return True
    monkeypatch.setattr(m, "send_user_notification", fake_send)
    return m, conn, matter, caller, ralph, other, emails


def _as(monkeypatch, m, user):
    async def fake(request):
        return {"id": user["id"], "firm_id": FIRM_ID, "role": user["role"], "display_name": user["display_name"]}
    monkeypatch.setattr(m, "get_current_user", fake)


def _invite(matter, invitee, reason=None):
    return invite_collaborator(str(matter["id"]), CollaboratorInvite(user_id=str(invitee["id"]), reason=reason), None)


def _inserted(conn, table):
    return [a for q, a in conn.executed if q.startswith(f"INSERT INTO {table}")]


# ── invite ───────────────────────────────────────────────────────────────────

def test_responsible_lawyer_invites_a_colleague_with_audit_and_notifications(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    _as(monkeypatch, m, caller)

    out = asyncio.run(_invite(matter, ralph, reason="Labour law angle on the dismissal"))

    assert out["user_id"] == str(ralph["id"]) and out["display_name"] == "Ralph Labour"
    assert len(conn.collabs) == 1 and conn.collabs[0]["invited_by"] == caller["id"]
    audit = _inserted(conn, "audit_logs")
    assert len(audit) == 1
    assert audit[0][4] == "COLLABORATOR_INVITED" and audit[0][5] == "MATTER_COLLABORATION"
    notes = _inserted(conn, "user_notifications")
    assert [n[1] for n in notes] == [ralph["id"]] and notes[0][2] == "matter_collaboration_invited"
    assert [e[0] for e in emails] == [ralph["id"]]
    assert "Labour law angle" in emails[0][2] and "Caller Lawyer" in emails[0][2]


def test_partner_who_is_not_responsible_cannot_invite(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch, owner_is_caller=False, caller_role="partner")
    _as(monkeypatch, m, caller)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_invite(matter, ralph))
    assert exc.value.status_code == 403
    assert conn.collabs == [] and emails == []


def test_unassigned_matter_cannot_have_collaborators_invited(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    matter["responsible_lawyer_id"] = None
    _as(monkeypatch, m, caller)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_invite(matter, ralph))
    assert exc.value.status_code == 403


def test_cannot_invite_yourself(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    _as(monkeypatch, m, caller)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_invite(matter, caller))
    assert exc.value.status_code == 400


def test_cannot_invite_a_user_from_another_firm_or_unknown(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    stranger = _user("Outsider", firm_id=uuid.uuid4())
    conn.users.append(stranger)
    _as(monkeypatch, m, caller)
    for target in (stranger, {"id": uuid.uuid4()}):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_invite(matter, target))
        assert exc.value.status_code == 404
    assert conn.collabs == []


def test_cannot_invite_a_deactivated_user(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    ralph["is_active"] = False
    _as(monkeypatch, m, caller)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_invite(matter, ralph))
    assert exc.value.status_code == 400


def test_duplicate_active_invite_is_409(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    _as(monkeypatch, m, caller)
    asyncio.run(_invite(matter, ralph))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_invite(matter, ralph))
    assert exc.value.status_code == 409
    assert len(conn.collabs) == 1


def test_malformed_ids_and_overlong_reason_are_400(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    _as(monkeypatch, m, caller)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(invite_collaborator("nope", CollaboratorInvite(user_id=str(ralph["id"])), None))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        asyncio.run(invite_collaborator(str(matter["id"]), CollaboratorInvite(user_id="nope"), None))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_invite(matter, ralph, reason="x" * (COLLABORATION_REASON_MAX_LENGTH + 1)))
    assert exc.value.status_code == 400


def test_unknown_matter_404_and_sentinel_matter_400(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    _as(monkeypatch, m, caller)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(invite_collaborator(str(uuid.uuid4()), CollaboratorInvite(user_id=str(ralph["id"])), None))
    assert exc.value.status_code == 404
    matter["is_sentinel"] = True
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_invite(matter, ralph))
    assert exc.value.status_code == 400


def test_synthetic_user_without_identity_cannot_invite(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)

    async def fake(request):
        return {"id": None, "firm_id": FIRM_ID, "role": "partner", "display_name": "Demo User"}
    monkeypatch.setattr(m, "get_current_user", fake)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_invite(matter, ralph))
    assert exc.value.status_code == 403


def test_notification_failure_never_fails_the_invite(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    _as(monkeypatch, m, caller)

    async def boom(*a, **k):
        raise RuntimeError("resend down")
    monkeypatch.setattr(m, "send_user_notification", boom)
    out = asyncio.run(_invite(matter, ralph))
    assert out["user_id"] == str(ralph["id"]) and len(conn.collabs) == 1


# ── list: visible only to participants ───────────────────────────────────────

def _list(matter):
    return list_matter_collaborators(str(matter["id"]), None)


def test_responsible_lawyer_and_collaborator_see_the_list_others_see_nothing(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    _as(monkeypatch, m, caller)
    asyncio.run(_invite(matter, ralph))

    owner_view = asyncio.run(_list(matter))
    assert owner_view["can_manage"] is True and len(owner_view["collaborators"]) == 1
    assert owner_view["collaborators"][0]["display_name"] == "Ralph Labour"
    assert owner_view["collaborators"][0]["invited_by_name"] == "Caller Lawyer"

    _as(monkeypatch, m, ralph)
    collab_view = asyncio.run(_list(matter))
    assert collab_view["can_manage"] is False and collab_view["my_collaboration_id"] == str(conn.collabs[0]["id"])
    assert len(collab_view["collaborators"]) == 1

    _as(monkeypatch, m, other)
    assert asyncio.run(_list(matter)) == {"can_manage": False, "my_collaboration_id": None, "collaborators": []}


def test_a_partner_who_is_not_a_participant_cannot_see_the_collaborators(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    _as(monkeypatch, m, caller)
    asyncio.run(_invite(matter, ralph))
    partner = _user("Some Partner", "partner")
    _as(monkeypatch, m, partner)
    assert asyncio.run(_list(matter))["collaborators"] == []


def test_expired_collaboration_is_not_listed(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    _as(monkeypatch, m, caller)
    asyncio.run(_invite(matter, ralph))
    conn.collabs[0]["expires_at"] = NOW - timedelta(minutes=1)
    assert asyncio.run(_list(matter))["collaborators"] == []
    _as(monkeypatch, m, ralph)
    assert asyncio.run(_list(matter))["my_collaboration_id"] is None


# ── revoke / leave ───────────────────────────────────────────────────────────

def _revoke(matter, collab_id):
    return revoke_collaborator(str(matter["id"]), str(collab_id), None)


def test_responsible_lawyer_revokes_and_it_takes_effect_immediately(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    _as(monkeypatch, m, caller)
    asyncio.run(_invite(matter, ralph))
    cid = conn.collabs[0]["id"]

    assert asyncio.run(_revoke(matter, cid)) == {"revoked": True, "left": False}

    assert conn.collabs[0]["revoked_at"] is not None and conn.collabs[0]["revoked_by"] == caller["id"]
    assert [a[4] for a in _inserted(conn, "audit_logs")] == ["COLLABORATOR_INVITED", "COLLABORATOR_REVOKED"]
    _as(monkeypatch, m, ralph)
    assert asyncio.run(_list(matter))["my_collaboration_id"] is None  # no longer a participant


def test_collaborator_can_leave(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    _as(monkeypatch, m, caller)
    asyncio.run(_invite(matter, ralph))
    cid = conn.collabs[0]["id"]
    _as(monkeypatch, m, ralph)

    assert asyncio.run(_revoke(matter, cid)) == {"revoked": True, "left": True}
    assert _inserted(conn, "audit_logs")[-1][4] == "COLLABORATOR_LEFT"
    assert conn.collabs[0]["revoked_by"] == ralph["id"]


def test_a_collaborator_cannot_revoke_another_collaborator(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    _as(monkeypatch, m, caller)
    asyncio.run(_invite(matter, ralph))
    asyncio.run(_invite(matter, other))
    others_id = [c for c in conn.collabs if c["user_id"] == other["id"]][0]["id"]
    _as(monkeypatch, m, ralph)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_revoke(matter, others_id))
    assert exc.value.status_code == 403


def test_a_non_responsible_partner_cannot_revoke(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    _as(monkeypatch, m, caller)
    asyncio.run(_invite(matter, ralph))
    cid = conn.collabs[0]["id"]
    partner = _user("Some Partner", "partner")
    _as(monkeypatch, m, partner)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_revoke(matter, cid))
    assert exc.value.status_code == 403
    assert conn.collabs[0]["revoked_at"] is None


def test_revoking_twice_or_with_the_wrong_matter_is_404(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    _as(monkeypatch, m, caller)
    asyncio.run(_invite(matter, ralph))
    cid = conn.collabs[0]["id"]
    asyncio.run(_revoke(matter, cid))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_revoke(matter, cid))
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        asyncio.run(revoke_collaborator(str(uuid.uuid4()), str(cid), None))
    assert exc.value.status_code == 404


def test_a_revoked_colleague_can_be_invited_again(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    _as(monkeypatch, m, caller)
    asyncio.run(_invite(matter, ralph))
    asyncio.run(_revoke(matter, conn.collabs[0]["id"]))
    asyncio.run(_invite(matter, ralph))
    assert len(conn.collabs) == 2 and conn.collabs[1]["revoked_at"] is None


def test_reassigning_the_matter_does_not_end_existing_collaborations(monkeypatch):
    """The new responsible lawyer inherits the collaboration and can revoke it."""
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    _as(monkeypatch, m, caller)
    asyncio.run(_invite(matter, ralph))
    matter["responsible_lawyer_id"] = other["id"]  # reassigned
    _as(monkeypatch, m, other)
    view = asyncio.run(_list(matter))
    assert view["can_manage"] is True and len(view["collaborators"]) == 1
    assert asyncio.run(_revoke(matter, conn.collabs[0]["id"]))["revoked"] is True


# ── colleague picker ─────────────────────────────────────────────────────────

def test_colleagues_lists_active_firm_users_except_the_caller(monkeypatch):
    m, conn, matter, caller, ralph, other, emails = _setup(monkeypatch)
    conn.users.append(_user("Dormant Dan", active=False))
    conn.users.append(_user("Outsider", firm_id=uuid.uuid4()))
    _as(monkeypatch, m, caller)
    result = asyncio.run(list_colleagues(None))
    assert [c["display_name"] for c in result] == ["Other Person", "Ralph Labour"]
    assert all(c["id"] != str(caller["id"]) for c in result)
