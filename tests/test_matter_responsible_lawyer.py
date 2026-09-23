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


def _user_row(user_id, display_name, firm_id=FIRM_ID):
    return {"id": user_id, "firm_id": firm_id, "display_name": display_name}


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
