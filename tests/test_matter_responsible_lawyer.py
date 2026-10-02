"""
Unit tests for the matter overview "at a glance" fields added to
backend/main.py (2026-09-23, UX audit fix #2): responsible_lawyer_id and
next_action on POST /api/matters, PATCH /api/matters/{id}, and
GET /api/matters.

Deliberately a separate column from the existing assigned_lawyer_id (a
different, unrelated Legal Corner panel-lawyer referral/SLA field) --
see the schema comment in run_migrations() and _create_matter_row()'s
docstring for why. Called directly as plain async functions, same
convention as tests/test_matter_client_linking.py (see that file's
docstring for why).
"""

import asyncio
import re
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from backend.main import FIRM_ID, MatterCreate, MatterUpdate, create_matter, list_matters, update_matter


class FakeConnection:
    def __init__(self, matters=None, users=None, org_roles=None):
        self.matters = matters if matters is not None else []
        self.users = users if users is not None else []
        self.org_roles = org_roles if org_roles is not None else []
        self.executed = []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT role FROM organisation_roles WHERE firm_id=$1 AND user_id=$2"):
            for r in self.org_roles:
                if r["firm_id"] == args[0] and r["user_id"] == args[1]:
                    return {"role": r["role"]}
            return None

        if q.startswith("SELECT id FROM users WHERE id=$1 AND firm_id=$2"):
            # update_matter()'s responsible_lawyer_id validation.
            for u in self.users:
                if u["id"] == args[0] and u["firm_id"] == args[1]:
                    return {"id": u["id"]}
            return None

        if q.startswith("SELECT m.name, m.matter_number, m.responsible_lawyer_id, u.role AS owner_role"):
            # update_matter()'s reassignment authorization lookup.
            for row in self.matters:
                if row["id"] == args[0] and row["firm_id"] == args[1]:
                    owner = next((u for u in self.users if u["id"] == row.get("responsible_lawyer_id")), None)
                    return {
                        "name": row["name"], "matter_number": row.get("matter_number"),
                        "responsible_lawyer_id": row.get("responsible_lawyer_id"),
                        "owner_role": owner.get("role") if owner else None,
                        "owner_active": owner.get("is_active", True) if owner else None,
                        "owner_name": owner["display_name"] if owner else None,
                    }
            return None

        if q.startswith("SELECT display_name FROM users WHERE id=$1"):
            # update_matter()'s post-UPDATE name resolution for the response.
            for u in self.users:
                if u["id"] == args[0]:
                    return {"display_name": u["display_name"]}
            return None

        if q.startswith("INSERT INTO matters"):
            cols = [c.strip() for c in q.split("(", 1)[1].split(")", 1)[0].split(",")]
            row = dict(zip(cols, args))
            self.matters.append(row)
            return dict(row)

        if q.startswith("UPDATE matters SET"):
            m = re.search(r"SET (.+) WHERE id=\$1", q)
            cols = re.findall(r"(\w+)=\$\d+", m.group(1))
            matter_id, firm_id = args[0], args[-1]
            values = args[1:1 + len(cols)]
            for row in self.matters:
                if row["id"] == matter_id and row["firm_id"] == firm_id:
                    for col, val in zip(cols, values):
                        row[col] = val
                    return dict(row)
            return None

        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT * FROM matters WHERE firm_id=$1 AND NOT is_sentinel"):
            firm_id = args[0]
            return [dict(m) for m in self.matters if m["firm_id"] == firm_id]
        if q.startswith("SELECT id, display_name FROM users WHERE firm_id=$1"):
            return [{"id": u["id"], "display_name": u["display_name"]} for u in self.users if u["firm_id"] == args[0]]
        if q.startswith("SELECT * FROM progress_notes"):
            return []
        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")

    async def execute(self, query, *args):
        self.executed.append((" ".join(query.split()), args))
        return "OK"


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, matters=None, users=None, org_roles=None):
        self.conn = FakeConnection(matters, users, org_roles)

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _user_row(user_id, display_name, firm_id=FIRM_ID, role="associate", is_active=True):
    return {"id": user_id, "firm_id": firm_id, "display_name": display_name, "role": role, "is_active": is_active}


def _matter_row(matter_id, firm_id=FIRM_ID, **overrides):
    row = {
        "id": matter_id, "firm_id": firm_id, "name": "Moyo v Dube", "number": None,
        "internal_ref": None, "external_ref": None, "client_name": None, "client_id": None,
        "case_parties": None, "matter_type": None, "practice_area": None, "status": "Active",
        "custom_status": None, "next_deadline": None, "next_deadline_note": None,
        "created_by": None, "created_at": datetime.now(timezone.utc), "last_activity": datetime.now(timezone.utc),
        "next_review_date": None, "last_reviewed_date": None,
        "responsible_lawyer_id": None, "next_action": None,
    }
    row.update(overrides)
    return row


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def _fake_request():
    return None


# ── create_matter: responsible_lawyer_id defaults to created_by ────────────

def test_create_matter_defaults_responsible_lawyer_to_the_creating_user(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)
    lawyer_id = uuid.uuid4()
    _as_current_user(monkeypatch, m, {"id": lawyer_id, "firm_id": FIRM_ID, "role": "associate", "display_name": "Blessing Nyathi"})

    result = asyncio.run(create_matter(MatterCreate(name="Moyo v Dube"), _fake_request()))

    assert result["responsible_lawyer_id"] == str(lawyer_id)


def test_create_matter_leaves_responsible_lawyer_null_for_synthetic_user(monkeypatch):
    """AUTH_ENABLED False -> the synthetic user has no real id (matching
    created_by's own existing behavior for the same case, see
    tests/test_client_ownership.py::test_create_client_leaves_created_by_null_for_synthetic_user)."""
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, {"id": None, "firm_id": FIRM_ID, "role": "admin", "display_name": "Demo User"})

    result = asyncio.run(create_matter(MatterCreate(name="Moyo v Dube"), _fake_request()))

    assert result["responsible_lawyer_id"] is None
    assert result["created_by"] is None


# ── update_matter: responsible_lawyer_id ────────────────────────────────────

def test_update_matter_sets_responsible_lawyer_and_resolves_its_name(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    lawyer_id = uuid.uuid4()
    pool = FakePool(
        matters=[_matter_row(matter_id)],
        users=[_user_row(lawyer_id, "Tendai Chikafu")],
    )
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_matter(str(matter_id), MatterUpdate(responsible_lawyer_id=str(lawyer_id)), _fake_request()))

    assert result["responsible_lawyer_id"] == str(lawyer_id)
    assert result["responsible_lawyer_name"] == "Tendai Chikafu"


def test_update_matter_with_unknown_responsible_lawyer_id_404s(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id)])
    monkeypatch.setattr(m, "_db_pool", pool)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_matter(str(matter_id), MatterUpdate(responsible_lawyer_id=str(uuid.uuid4())), _fake_request()))
    assert exc_info.value.status_code == 404


def test_update_matter_rejects_a_responsible_lawyer_from_another_firm(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    other_firm_lawyer = uuid.uuid4()
    pool = FakePool(
        matters=[_matter_row(matter_id)],
        users=[_user_row(other_firm_lawyer, "Someone Else", firm_id=uuid.uuid4())],
    )
    monkeypatch.setattr(m, "_db_pool", pool)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_matter(str(matter_id), MatterUpdate(responsible_lawyer_id=str(other_firm_lawyer)), _fake_request()))
    assert exc_info.value.status_code == 404


def test_update_matter_with_malformed_responsible_lawyer_id_400s(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id)])
    monkeypatch.setattr(m, "_db_pool", pool)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_matter(str(matter_id), MatterUpdate(responsible_lawyer_id="not-a-uuid"), _fake_request()))
    assert exc_info.value.status_code == 400


def test_update_matter_without_a_responsible_lawyer_set_returns_null_name_not_an_error(monkeypatch):
    """A matter whose responsible_lawyer_id is still NULL (e.g. bulk-
    imported, no created_by) must not crash resolving a name for a PATCH
    that doesn't touch this field at all."""
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id, responsible_lawyer_id=None)])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_matter(str(matter_id), MatterUpdate(next_action="Chase the deposit"), _fake_request()))

    assert result["responsible_lawyer_id"] is None
    assert result["responsible_lawyer_name"] is None


# ── update_matter: next_action ──────────────────────────────────────────────

def test_update_matter_sets_next_action_as_plain_free_text(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id)])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_matter(str(matter_id), MatterUpdate(next_action="File Heads of Argument by Friday"), _fake_request()))

    assert result["next_action"] == "File Heads of Argument by Friday"


# ── list_matters: responsible_lawyer_name attached per row ──────────────────

def test_list_matters_attaches_responsible_lawyer_name_to_each_row(monkeypatch):
    import backend.main as m
    lawyer_id = uuid.uuid4()
    unassigned_id = uuid.uuid4()
    pool = FakePool(
        matters=[
            _matter_row(uuid.uuid4(), responsible_lawyer_id=str(lawyer_id)),
            _matter_row(unassigned_id, responsible_lawyer_id=None),
        ],
        users=[_user_row(lawyer_id, "Farai Gumbo")],
    )
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, {"id": None, "firm_id": FIRM_ID, "role": "admin", "display_name": "Demo User"})

    result = asyncio.run(list_matters(_fake_request()))
    by_id = {r["id"]: r for r in result}

    assert by_id[str(unassigned_id)]["responsible_lawyer_name"] is None
    assigned = [r for r in result if r["id"] != str(unassigned_id)][0]
    assert assigned["responsible_lawyer_name"] == "Farai Gumbo"


# ── Reassignment authorization (2026-10-02) ─────────────────────────────────
# Real concern: any role with matter:edit could change responsible_lawyer_id,
# so anyone could self-assign onto a colleague's matter. Rule: the current
# responsible lawyer may hand their matter on; a partner/admin may move a
# NON-partner's matter but never another partner's; a deactivated owner's
# matters are movable by a partner/admin; an unassigned matter may be
# claimed by anyone for themselves.

from backend.main import can_reassign_responsible_lawyer as _can

A, B, C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


@pytest.mark.parametrize("actor_id,actor_role,owner_id,owner_role,owner_active,new_id,expected", [
    # the owner themselves, any role
    (A, "associate", A, "associate", True, B, True),
    (A, "secretary", A, "secretary", True, B, True),
    (A, "partner", A, "partner", True, B, True),
    # partner / admin over a non-partner's matter
    (A, "partner", B, "associate", True, C, True),
    (A, "partner", B, "secretary", True, C, True),
    (A, "admin", B, "associate", True, C, True),
    # never another partner's matter, even self-assigning
    (A, "partner", B, "partner", True, C, False),
    (A, "partner", B, "partner", True, A, False),
    (A, "admin", B, "partner", True, C, False),
    # non-owner associates / secretaries
    (A, "associate", B, "associate", True, A, False),
    (A, "secretary", B, "associate", True, C, False),
    # deactivated owner: partner/admin may move it, others may not
    (A, "partner", B, "partner", False, C, True),
    (A, "admin", B, "partner", False, A, True),
    (A, "associate", B, "partner", False, A, False),
    # unassigned
    (A, "partner", None, None, False, C, True),
    (A, "admin", None, None, False, C, True),
    (A, "associate", None, None, False, A, True),
    (A, "associate", None, None, False, C, False),
    (A, "secretary", None, None, False, C, False),
    # synthetic dev user (no identity) is never "the owner"
    (None, "partner", B, "partner", True, C, False),
    (None, "partner", B, "associate", True, C, True),
])
def test_can_reassign_responsible_lawyer_matrix(actor_id, actor_role, owner_id, owner_role, owner_active, new_id, expected):
    assert _can(actor_id, actor_role, owner_id, owner_role, owner_active, new_id) is expected


class _Recorder:
    """Captures what the reassignment flow would send, without any email."""
    def __init__(self, monkeypatch, m):
        self.emails = []

        async def fake_send(user_id, subject, body, **kw):
            self.emails.append((user_id, subject, body))
            return True
        monkeypatch.setattr(m, "send_user_notification", fake_send)


def _setup(monkeypatch, owner_role="associate", actor_role="partner", owner_is_owner=False, owner_active=True):
    import backend.main as m
    matter_id, owner_id, actor_id, new_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    users = [
        _user_row(owner_id, "Owner Person", role=owner_role, is_active=owner_active),
        _user_row(actor_id, "Actor Person", role=actor_role),
        _user_row(new_id, "New Person"),
    ]
    effective_owner = actor_id if owner_is_owner else owner_id
    if owner_is_owner:
        users[1]["role"] = owner_role  # actor == owner, so they share one role
    pool = FakePool(matters=[_matter_row(matter_id, responsible_lawyer_id=effective_owner)], users=users)
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, {"id": actor_id, "firm_id": FIRM_ID, "role": actor_role, "display_name": "Actor Person"})
    rec = _Recorder(monkeypatch, m)
    return m, pool, rec, matter_id, effective_owner, actor_id, new_id


def _inserted_notifications(pool):
    return [args for q, args in getattr(pool.conn, "executed", []) if q.startswith("INSERT INTO user_notifications")]


def test_associate_cannot_take_over_a_colleagues_matter(monkeypatch):
    m, pool, rec, matter_id, owner_id, actor_id, new_id = _setup(monkeypatch, actor_role="associate")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(update_matter(str(matter_id), MatterUpdate(responsible_lawyer_id=str(actor_id)), _fake_request()))
    assert exc.value.status_code == 403
    assert pool.conn.matters[0]["responsible_lawyer_id"] == owner_id  # unchanged
    assert rec.emails == [] and _inserted_notifications(pool) == []


def test_partner_cannot_reassign_another_partners_matter(monkeypatch):
    m, pool, rec, matter_id, owner_id, actor_id, new_id = _setup(monkeypatch, owner_role="partner", actor_role="partner")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(update_matter(str(matter_id), MatterUpdate(responsible_lawyer_id=str(new_id)), _fake_request()))
    assert exc.value.status_code == 403
    assert pool.conn.matters[0]["responsible_lawyer_id"] == owner_id


def test_partner_reassigns_an_associates_matter_and_both_are_notified(monkeypatch):
    m, pool, rec, matter_id, owner_id, actor_id, new_id = _setup(monkeypatch, owner_role="associate", actor_role="partner")
    result = asyncio.run(update_matter(str(matter_id), MatterUpdate(responsible_lawyer_id=str(new_id)), _fake_request()))

    assert result["responsible_lawyer_id"] == str(new_id)
    # In-app (Home) notifications: new owner + previous owner, never the actor.
    notified = {args[1]: args[2] for args in _inserted_notifications(pool)}
    assert notified == {new_id: "matter_assigned", owner_id: "matter_reassigned_away"}
    # Email goes to the same two people through send_user_notification.
    assert {e[0] for e in rec.emails} == {new_id, owner_id}
    assert all("Actor Person" in e[2] for e in rec.emails)
    # Audit row: who moved it, from whom, to whom, under a non-compliance target.
    audit = [a for q, a in pool.conn.executed if q.startswith("INSERT INTO audit_logs")]
    assert len(audit) == 1
    assert audit[0][4] == "RESPONSIBLE_LAWYER_CHANGED" and audit[0][5] == "MATTER_ASSIGNMENT"
    assert str(owner_id) in audit[0][7] and str(new_id) in audit[0][7]


def test_owner_handing_on_their_own_matter_notifies_only_the_new_owner(monkeypatch):
    m, pool, rec, matter_id, owner_id, actor_id, new_id = _setup(
        monkeypatch, owner_role="partner", actor_role="partner", owner_is_owner=True)
    asyncio.run(update_matter(str(matter_id), MatterUpdate(responsible_lawyer_id=str(new_id)), _fake_request()))
    assert [args[1] for args in _inserted_notifications(pool)] == [new_id]
    assert [e[0] for e in rec.emails] == [new_id]


def test_resaving_the_same_responsible_lawyer_is_a_silent_noop(monkeypatch):
    m, pool, rec, matter_id, owner_id, actor_id, new_id = _setup(monkeypatch, actor_role="associate")
    asyncio.run(update_matter(str(matter_id), MatterUpdate(responsible_lawyer_id=str(owner_id)), _fake_request()))
    assert rec.emails == [] and _inserted_notifications(pool) == []
    assert not [q for q, a in pool.conn.executed if q.startswith("INSERT INTO audit_logs")]


def test_associate_may_claim_an_unassigned_matter_for_themselves(monkeypatch):
    import backend.main as m
    matter_id, actor_id = uuid.uuid4(), uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id, responsible_lawyer_id=None)],
                    users=[_user_row(actor_id, "Actor Person")])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, {"id": actor_id, "firm_id": FIRM_ID, "role": "associate", "display_name": "Actor Person"})
    rec = _Recorder(monkeypatch, m)
    result = asyncio.run(update_matter(str(matter_id), MatterUpdate(responsible_lawyer_id=str(actor_id)), _fake_request()))
    assert result["responsible_lawyer_id"] == str(actor_id)
    assert rec.emails == []  # nobody to tell: they assigned it to themselves


def test_notification_failure_never_fails_the_reassignment(monkeypatch):
    m, pool, rec, matter_id, owner_id, actor_id, new_id = _setup(monkeypatch, actor_role="partner")

    async def boom(*a, **k):
        raise RuntimeError("resend down")
    monkeypatch.setattr(m, "send_user_notification", boom)

    result = asyncio.run(update_matter(str(matter_id), MatterUpdate(responsible_lawyer_id=str(new_id)), _fake_request()))
    assert result["responsible_lawyer_id"] == str(new_id)
    assert len(_inserted_notifications(pool)) == 2  # in-app records still written
