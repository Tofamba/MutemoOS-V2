"""
Unit tests for clients.created_by — the "My Clients" default-view filter's
join key (backend/main.py). Covers only the new ownership behavior; the
rest of the /api/clients endpoints are already covered by
tests/test_clients_api.py.

Called directly as plain async functions, same convention as
tests/test_clients_api.py — see that file's docstring for why. AUTH_ENABLED
is False by default in this environment, so get_current_user() returns the
synthetic dev user (id=None) unless a test explicitly monkeypatches it to
simulate a real logged-in session.
"""

import asyncio
import uuid
from datetime import datetime, timezone

from backend.main import FIRM_ID, ClientCreate, create_client, list_clients


class FakeConnection:
    def __init__(self, clients, users=None):
        self.clients = clients
        self.users = users if users is not None else []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT initials FROM users WHERE id=$1"):
            for u in self.users:
                if u["id"] == args[0]:
                    return {"initials": u.get("initials")}
            return None

        if q.startswith("INSERT INTO clients"):
            cols = [c.strip() for c in q.split("(", 1)[1].split(")", 1)[0].split(",")]
            row = dict(zip(cols, args))
            self.clients.append(row)
            return dict(row)

        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT * FROM clients WHERE firm_id=$1 ORDER BY full_name"):
            rows = [c for c in self.clients if c["firm_id"] == args[0]]
            rows.sort(key=lambda c: (c["full_name"] or "").lower())
            return [dict(r) for r in rows]

        if q.startswith("SELECT initials FROM users WHERE firm_id=$1 AND initials IS NOT NULL"):
            return [{"initials": u["initials"]} for u in self.users if u.get("initials")]

        if q.startswith("SELECT client_number FROM clients WHERE firm_id=$1 AND client_number LIKE $2"):
            prefix = args[1][:-1]
            return [{"client_number": c["client_number"]} for c in self.clients
                    if c["firm_id"] == args[0] and (c.get("client_number") or "").startswith(prefix)]

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
    def __init__(self, clients=None, users=None):
        self.conn = FakeConnection(clients if clients is not None else [], users if users is not None else [])

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _client_row(client_id, full_name, created_by=None, client_number=None, firm_id=FIRM_ID):
    return {
        "id": client_id, "firm_id": firm_id, "full_name": full_name, "email": None, "phone": None,
        "physical_address": None, "id_or_registration_number": None, "contact_person": None, "notes": None,
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
        "client_number": client_number, "created_by": created_by,
    }


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


# ── create_client stamps created_by from the real session user ──────────

def test_create_client_stamps_created_by_from_real_session_user(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)
    lawyer_id = uuid.uuid4()
    _as_current_user(monkeypatch, m, {
        "id": lawyer_id, "firm_id": FIRM_ID, "role": "partner", "display_name": "Rufaro Rusike",
    })

    result = asyncio.run(create_client(ClientCreate(full_name="John Moyo"), None))

    assert result["created_by"] == str(lawyer_id)


def test_create_client_leaves_created_by_null_for_synthetic_user(monkeypatch):
    """AUTH_ENABLED is False and get_current_user() is left unpatched here —
    the synthetic dev user has id=None, so there's no real lawyer to stamp."""
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(create_client(ClientCreate(full_name="John Moyo"), None))

    assert result.get("created_by") is None


# ── list_clients: not a permission change — full firm list regardless ───

def test_list_clients_returns_full_firm_list_regardless_of_caller_role(monkeypatch):
    """This is a default-VIEW change (frontend-side), not a backend
    permission change — GET /api/clients must keep returning every client
    to any client:read-permitted role, same as before created_by existed."""
    import backend.main as m
    lawyer_a, lawyer_b = uuid.uuid4(), uuid.uuid4()
    existing = [
        _client_row(uuid.uuid4(), "Alice Huang", created_by=lawyer_a),
        _client_row(uuid.uuid4(), "Bongani Ncube", created_by=lawyer_b),
        _client_row(uuid.uuid4(), "Zulu Trading", created_by=None),
    ]
    pool = FakePool(clients=existing)
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, {
        "id": lawyer_a, "firm_id": FIRM_ID, "role": "associate", "display_name": "Blessing Nyathi",
    })

    result = asyncio.run(list_clients(None))

    assert len(result) == 3
    assert {c["full_name"] for c in result} == {"Alice Huang", "Bongani Ncube", "Zulu Trading"}
