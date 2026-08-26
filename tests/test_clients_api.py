"""
Unit tests for the /api/clients endpoints in backend/main.py: create, list,
detail (including the linked-matters list), and update.

Called directly as plain async functions rather than through FastAPI's
TestClient, matching this repo's existing test convention (see
tests/test_docx_export.py) — AUTH_ENABLED is False by default with no OTP
env vars configured, so get_current_user() never touches the `request`
argument or the DB.

There's no live Postgres in this environment, so _db_pool is swapped for a
small in-memory fake that understands exactly the queries these four
endpoints issue (and nothing more) — enough to exercise the real endpoint
code (SQL construction, row shaping, firm-scoping, 404s) without a real
database.
"""

import asyncio
import re
import uuid
from datetime import datetime, timezone

import pytest

from backend.main import (
    FIRM_ID,
    ClientCreate,
    ClientUpdate,
    create_client,
    get_client,
    list_clients,
    update_client,
)
from fastapi import HTTPException


class FakeConnection:
    def __init__(self, clients, matters, users=None, numbering_counters=None, client_compliance=None):
        self.clients = clients
        self.matters = matters
        self.users = users if users is not None else []
        self.numbering_counters = numbering_counters if numbering_counters is not None else []
        # Compliance-gap fix (2026-08-26): _create_client_row() now also
        # inserts a client_compliance row for every client it creates.
        self.client_compliance = client_compliance if client_compliance is not None else []

    async def fetchval(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT 1 FROM numbering_counters WHERE firm_id=$1 AND prefix=$2"):
            for c in self.numbering_counters:
                if c["firm_id"] == args[0] and c["prefix"] == args[1]:
                    return 1
            return None
        raise NotImplementedError(f"FakeConnection.fetchval: unhandled query: {q}")

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("UPDATE numbering_counters SET next_seq = next_seq + 1"):
            firm_id, prefix = args
            for c in self.numbering_counters:
                if c["firm_id"] == firm_id and c["prefix"] == prefix:
                    allocated = c["next_seq"]
                    c["next_seq"] += 1
                    return {"allocated": allocated}
            return None

        if q.startswith("SELECT initials FROM users WHERE id=$1"):
            for u in self.users:
                if u["id"] == args[0]:
                    return {"initials": u.get("initials")}
            return None

        if q.startswith("INSERT INTO clients"):
            # Column list read straight out of the query rather than
            # hardcoded — keeps this fake correct automatically as columns
            # are added to create_client's INSERT (e.g. contact_person).
            cols = [c.strip() for c in q.split("(", 1)[1].split(")", 1)[0].split(",")]
            row = dict(zip(cols, args))
            self.clients.append(row)
            return dict(row)

        if q.startswith("SELECT full_name FROM clients WHERE id=$1 AND firm_id=$2"):
            for c in self.clients:
                if c["id"] == args[0] and c["firm_id"] == args[1]:
                    return {"full_name": c["full_name"]}
            return None

        if q.startswith("SELECT * FROM clients WHERE id=$1 AND firm_id=$2"):
            for c in self.clients:
                if c["id"] == args[0] and c["firm_id"] == args[1]:
                    return dict(c)
            return None

        if q.startswith("UPDATE clients SET"):
            m = re.search(r"SET (.+) WHERE id=\$1", q)
            cols = re.findall(r"(\w+)=\$\d+", m.group(1))
            cid, firm_id = args[0], args[-1]
            values = args[1:1 + len(cols)]
            for c in self.clients:
                if c["id"] == cid and c["firm_id"] == firm_id:
                    for col, val in zip(cols, values):
                        c[col] = val
                    return dict(c)
            return None

        # get_client()'s compliance-badge addition (AML/KYC module) — this
        # file doesn't exercise compliance data, only that get_client()
        # still returns the right matters; see tests/test_client_compliance.py.
        if q.startswith("SELECT * FROM client_compliance WHERE client_id=$1 AND firm_id=$2"):
            return None

        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT * FROM clients WHERE firm_id=$1 ORDER BY full_name"):
            rows = [c for c in self.clients if c["firm_id"] == args[0]]
            rows.sort(key=lambda c: (c["full_name"] or "").lower())
            return [dict(r) for r in rows]

        if q.startswith("SELECT * FROM matters WHERE client_id=$1 AND firm_id=$2"):
            rows = [m for m in self.matters if m.get("client_id") == args[0] and m["firm_id"] == args[1]]
            rows.sort(key=lambda m: (m.get("last_activity") is None, m.get("last_activity"), m.get("created_at")), reverse=True)
            return [dict(r) for r in rows]

        if q.startswith("SELECT initials FROM users WHERE firm_id=$1 AND initials IS NOT NULL"):
            return [{"initials": u["initials"]} for u in self.users if u.get("initials")]

        if q.startswith("SELECT client_number FROM clients WHERE firm_id=$1 AND client_number LIKE $2"):
            prefix = args[1][:-1]  # strip trailing '%'
            return [{"client_number": c["client_number"]} for c in self.clients
                    if c["firm_id"] == args[0] and (c.get("client_number") or "").startswith(prefix)]

        # get_client()'s richness additions (notes/documents/calendar events
        # per matter) — this file doesn't exercise that data, only that
        # get_client() still returns the right matters; see
        # tests/test_client_detail_richness.py for real coverage of these.
        if q.startswith("SELECT * FROM progress_notes WHERE matter_id = ANY($1)"):
            return []
        if q.startswith("SELECT * FROM documents WHERE matter_id = ANY($1)"):
            return []
        if q.startswith("SELECT * FROM calendar_events WHERE matter_id = ANY($1)"):
            return []
        if q.startswith("SELECT verification_status FROM beneficial_owners WHERE client_id=$1 AND firm_id=$2"):
            return []

        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO numbering_counters"):
            firm_id, prefix, seed = args
            if not any(c["firm_id"] == firm_id and c["prefix"] == prefix for c in self.numbering_counters):
                self.numbering_counters.append({"firm_id": firm_id, "prefix": prefix, "next_seq": seed})
            return "INSERT 0 1"
        if q.startswith("INSERT INTO client_compliance"):
            client_id, firm_id, client_is_beneficial_owner = args
            self.client_compliance.append({
                "client_id": client_id, "firm_id": firm_id,
                "client_is_beneficial_owner": client_is_beneficial_owner,
            })
            return "INSERT 0 1"
        return "OK"


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, clients=None, matters=None, users=None, numbering_counters=None, client_compliance=None):
        self.conn = FakeConnection(
            clients if clients is not None else [],
            matters if matters is not None else [],
            users if users is not None else [],
            numbering_counters if numbering_counters is not None else [],
            client_compliance if client_compliance is not None else [],
        )

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _matter_row(id_, client_id=None, name="Test Matter", last_activity=None, created_at=None):
    return {
        "id": id_, "firm_id": FIRM_ID, "name": name, "number": None,
        "internal_ref": None, "external_ref": None, "client_name": None,
        "client_id": client_id, "case_parties": None, "matter_type": None,
        "status": "Active", "custom_status": None, "document_count": 0,
        "last_activity": last_activity, "created_at": created_at or datetime.now(timezone.utc),
        "created_by": None, "next_deadline": None, "next_deadline_note": None,
        "assigned_lawyer_id": None, "coverage_tier": None, "sla_deadline": None,
        "assigned_by_id": None, "service_type": None,
    }


# ── create_client ─────────────────────────────────────────────────────────

def test_create_client_returns_stringified_ids_and_all_fields(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    req = ClientCreate(full_name="John Moyo", email="john@example.com", phone="+263771234567")
    result = asyncio.run(create_client(req, None))

    assert result["full_name"] == "John Moyo"
    assert result["email"] == "john@example.com"
    assert isinstance(result["id"], str)
    assert result["firm_id"] == str(FIRM_ID)
    assert result["created_at"] is not None
    assert len(pool.conn.clients) == 1


def test_create_client_also_creates_a_not_yet_assessed_compliance_row(monkeypatch):
    """Compliance-gap fix (2026-08-26): every client creation path now
    gets a client_compliance row via _create_client_row() — verified
    here for the canonical single-client path too, alongside the
    equivalent tests for client_intake and bulk_onboard_from_excel.
    create_client() collects no client_type/beneficial-owner fields, so
    this must be the plain default "not yet assessed" state."""
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    req = ClientCreate(full_name="John Moyo", email="john@example.com", phone="+263771234567")
    result = asyncio.run(create_client(req, None))

    assert len(pool.conn.client_compliance) == 1
    row = pool.conn.client_compliance[0]
    assert row["client_id"] == uuid.UUID(result["id"])
    assert row["client_is_beneficial_owner"] is None


def test_create_client_assigns_first_client_number_under_creating_users_initials(monkeypatch):
    """The creating user's display_name is run through generate_initials()
    to derive the new client's number prefix — verified here against an
    explicit test user rather than depending on whatever the real
    AUTH_ENABLED=False dev-mode fallback's display_name happens to be
    (generate_initials() itself is unit-tested independently in
    tests/test_numbering.py)."""
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    async def fake_get_current_user(request):
        return {"id": None, "firm_id": FIRM_ID, "phone": None, "email": None,
                "role": "partner", "display_name": "Test User"}
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)

    result = asyncio.run(create_client(ClientCreate(full_name="John Moyo"), None))

    assert result["client_number"] == "TU-001"


def test_create_client_numbers_sequentially_for_the_same_prefix(monkeypatch):
    import backend.main as m
    existing = [{
        "id": uuid.uuid4(), "firm_id": FIRM_ID, "full_name": "Existing Client", "client_number": "TU-006",
    }]
    pool = FakePool(clients=existing)
    monkeypatch.setattr(m, "_db_pool", pool)

    async def fake_get_current_user(request):
        return {"id": None, "firm_id": FIRM_ID, "phone": None, "email": None,
                "role": "partner", "display_name": "Test User"}
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)

    result = asyncio.run(create_client(ClientCreate(full_name="John Moyo"), None))

    assert result["client_number"] == "TU-007"


def test_create_client_number_prefix_ignores_other_firms_numbers(monkeypatch):
    import backend.main as m
    other_firm = uuid.uuid4()
    existing = [{
        "id": uuid.uuid4(), "firm_id": other_firm, "full_name": "Other Firm's Client", "client_number": "TU-099",
    }]
    pool = FakePool(clients=existing)
    monkeypatch.setattr(m, "_db_pool", pool)

    async def fake_get_current_user(request):
        return {"id": None, "firm_id": FIRM_ID, "phone": None, "email": None,
                "role": "partner", "display_name": "Test User"}
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)

    result = asyncio.run(create_client(ClientCreate(full_name="John Moyo"), None))

    assert result["client_number"] == "TU-001"


# ── list_clients ─────────────────────────────────────────────────────────

def test_list_clients_is_firm_scoped_and_alphabetical(monkeypatch):
    import backend.main as m
    other_firm = uuid.uuid4()
    existing = [
        {"id": uuid.uuid4(), "firm_id": FIRM_ID, "full_name": "Zulu Trading", "email": None, "phone": None,
         "physical_address": None, "id_or_registration_number": None, "notes": None,
         "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)},
        {"id": uuid.uuid4(), "firm_id": FIRM_ID, "full_name": "Alice Huang", "email": None, "phone": None,
         "physical_address": None, "id_or_registration_number": None, "notes": None,
         "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)},
        {"id": uuid.uuid4(), "firm_id": other_firm, "full_name": "Other Firm Client", "email": None, "phone": None,
         "physical_address": None, "id_or_registration_number": None, "notes": None,
         "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)},
    ]
    pool = FakePool(clients=existing)
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(list_clients(None))

    names = [c["full_name"] for c in result]
    assert names == ["Alice Huang", "Zulu Trading"]  # alphabetical, other firm excluded


# ── get_client (detail, incl. linked matters) ───────────────────────────────

def test_get_client_includes_all_linked_matters(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    other_client_id = uuid.uuid4()
    client_row = {
        "id": client_id, "firm_id": FIRM_ID, "full_name": "John Moyo", "email": None, "phone": None,
        "physical_address": None, "id_or_registration_number": None, "notes": None,
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
    }
    matters = [
        _matter_row(uuid.uuid4(), client_id=client_id, name="Moyo v Dube"),
        _matter_row(uuid.uuid4(), client_id=client_id, name="Moyo Estate Matter"),
        _matter_row(uuid.uuid4(), client_id=other_client_id, name="Unrelated Matter"),
        _matter_row(uuid.uuid4(), client_id=None, name="No Client Matter"),
    ]
    pool = FakePool(clients=[client_row], matters=matters)
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(get_client(str(client_id), None))

    assert result["full_name"] == "John Moyo"
    assert len(result["matters"]) == 2
    matter_names = {mm["name"] for mm in result["matters"]}
    assert matter_names == {"Moyo v Dube", "Moyo Estate Matter"}
    for mm in result["matters"]:
        assert mm["client_id"] == str(client_id)


def test_get_client_404s_for_unknown_id(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_client(str(uuid.uuid4()), None))
    assert exc_info.value.status_code == 404


def test_get_client_404s_for_client_belonging_to_another_firm(monkeypatch):
    import backend.main as m
    other_firm = uuid.uuid4()
    client_id = uuid.uuid4()
    client_row = {
        "id": client_id, "firm_id": other_firm, "full_name": "Someone Else's Client",
        "email": None, "phone": None, "physical_address": None,
        "id_or_registration_number": None, "notes": None,
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
    }
    pool = FakePool(clients=[client_row])
    monkeypatch.setattr(m, "_db_pool", pool)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_client(str(client_id), None))
    assert exc_info.value.status_code == 404


# ── update_client ────────────────────────────────────────────────────────

def test_update_client_updates_only_provided_fields(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client_row = {
        "id": client_id, "firm_id": FIRM_ID, "full_name": "John Moyo", "email": "old@example.com",
        "phone": None, "physical_address": None, "id_or_registration_number": None, "notes": None,
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
    }
    pool = FakePool(clients=[client_row])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_client(str(client_id), ClientUpdate(email="new@example.com"), None))

    assert result["email"] == "new@example.com"
    assert result["full_name"] == "John Moyo"  # untouched


def test_update_client_404s_for_unknown_id(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_client(str(uuid.uuid4()), ClientUpdate(email="x@example.com"), None))
    assert exc_info.value.status_code == 404


def test_update_client_rejects_empty_update(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_client(str(uuid.uuid4()), ClientUpdate(), None))
    assert exc_info.value.status_code == 400


# ── contact_person (corporate/entity clients only) ──────────────────────────

def test_create_client_with_contact_person_for_a_company(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    req = ClientCreate(full_name="Vengesai Enterprises", contact_person="Jane Muzenda (Company Secretary)")
    result = asyncio.run(create_client(req, None))

    assert result["contact_person"] == "Jane Muzenda (Company Secretary)"
    assert pool.conn.clients[0]["contact_person"] == "Jane Muzenda (Company Secretary)"


def test_create_client_without_contact_person_for_an_individual(monkeypatch):
    """The common case — individuals leave this blank."""
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    req = ClientCreate(full_name="John Moyo")
    result = asyncio.run(create_client(req, None))

    assert result["contact_person"] is None


def test_update_client_can_set_contact_person(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client_row = {
        "id": client_id, "firm_id": FIRM_ID, "full_name": "Vengesai Enterprises", "email": None,
        "phone": None, "physical_address": None, "id_or_registration_number": None,
        "contact_person": None, "notes": None,
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
    }
    pool = FakePool(clients=[client_row])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_client(str(client_id), ClientUpdate(contact_person="New Contact"), None))

    assert result["contact_person"] == "New Contact"
