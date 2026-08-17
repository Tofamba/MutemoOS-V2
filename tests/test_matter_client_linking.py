"""
Unit tests for the matters<->clients linking behavior added to
POST /api/matters and PATCH /api/matters/{id} in backend/main.py:
  - passing client_id resolves the client (firm-scoped) and syncs
    matters.client_name from it (client_name becomes a display fallback,
    not the source of truth, once client_id is set)
  - an unknown or cross-firm client_id 404s rather than silently linking
  - the legacy client_name-only path (no client_id) still works unchanged

Called directly as plain async functions, same convention as
tests/test_clients_api.py — see that file's docstring for why.
"""

import asyncio
import re
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from backend.main import (
    FIRM_ID,
    AutoCreateMatterRequest,
    MatterCreate,
    MatterUpdate,
    auto_create_matter,
    create_matter,
    update_matter,
)


class FakeConnection:
    def __init__(self, clients, matters, org_roles=None, numbering_counters=None):
        self.clients = clients
        self.matters = matters
        self.org_roles = org_roles if org_roles is not None else []
        self.numbering_counters = numbering_counters if numbering_counters is not None else []

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

        if q.startswith("SELECT full_name, client_number FROM clients WHERE id=$1 AND firm_id=$2"):
            # create_matter's lookup — includes client_number so it can
            # number the new matter under that client.
            for c in self.clients:
                if c["id"] == args[0] and c["firm_id"] == args[1]:
                    return {"full_name": c["full_name"], "client_number": c.get("client_number")}
            return None

        if q.startswith("SELECT full_name FROM clients WHERE id=$1 AND firm_id=$2"):
            # update_matter's lookup — client_number isn't relevant there
            # (matter_number is only assigned at creation, not on relink).
            for c in self.clients:
                if c["id"] == args[0] and c["firm_id"] == args[1]:
                    return {"full_name": c["full_name"]}
            return None

        if q.startswith("SELECT * FROM matters WHERE firm_id=$1 AND external_ref=$2"):
            for row in self.matters:
                if row["firm_id"] == args[0] and row.get("external_ref") == args[1]:
                    return dict(row)
            return None

        if q.startswith("SELECT role FROM organisation_roles WHERE firm_id=$1 AND user_id=$2"):
            for r in self.org_roles:
                if r["firm_id"] == args[0] and r["user_id"] == args[1]:
                    return {"role": r["role"]}
            return None

        if q.startswith("INSERT INTO matters"):
            # Generic: read the actual column list out of the query rather
            # than hardcoding one — this fake backs both create_matter's and
            # auto_create_matter's INSERTs, which use different column sets.
            cols_str = q.split("(", 1)[1].split(")", 1)[0]
            cols = [c.strip() for c in cols_str.split(",")]
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
        if q.startswith("SELECT * FROM progress_notes"):
            return []
        if q.startswith("SELECT matter_number FROM matters WHERE firm_id=$1 AND matter_number LIKE $2"):
            prefix = args[1][:-1]  # strip trailing '%'
            return [{"matter_number": m["matter_number"]} for m in self.matters
                    if m["firm_id"] == args[0] and (m.get("matter_number") or "").startswith(prefix)]
        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO numbering_counters"):
            firm_id, prefix, seed = args
            if not any(c["firm_id"] == firm_id and c["prefix"] == prefix for c in self.numbering_counters):
                self.numbering_counters.append({"firm_id": firm_id, "prefix": prefix, "next_seq": seed})
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
    def __init__(self, clients=None, matters=None, org_roles=None, numbering_counters=None):
        self.conn = FakeConnection(
            clients if clients is not None else [],
            matters if matters is not None else [],
            org_roles if org_roles is not None else [],
            numbering_counters if numbering_counters is not None else [],
        )

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _client_row(client_id, full_name, firm_id=FIRM_ID, client_number=None):
    return {
        "id": client_id, "firm_id": firm_id, "full_name": full_name, "email": None,
        "phone": None, "physical_address": None, "id_or_registration_number": None,
        "notes": None, "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
        "client_number": client_number,
    }


# ── create_matter ────────────────────────────────────────────────────────

def test_create_matter_with_client_id_syncs_client_name_from_client_record(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    pool = FakePool(clients=[_client_row(client_id, "John Moyo")])
    monkeypatch.setattr(m, "_db_pool", pool)

    req = MatterCreate(name="Moyo v Dube", client_id=str(client_id))
    result = asyncio.run(create_matter(req, _fake_request()))

    assert result["client_id"] == str(client_id)
    assert result["client_name"] == "John Moyo"  # synced, not the source of truth


def test_create_matter_client_id_overrides_a_stale_manual_client_name(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    pool = FakePool(clients=[_client_row(client_id, "John Moyo (correct)")])
    monkeypatch.setattr(m, "_db_pool", pool)

    req = MatterCreate(name="Moyo v Dube", client_id=str(client_id), client_name="Jon Moya (typo)")
    result = asyncio.run(create_matter(req, _fake_request()))

    assert result["client_name"] == "John Moyo (correct)"


def test_create_matter_with_unknown_client_id_404s(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    req = MatterCreate(name="Moyo v Dube", client_id=str(uuid.uuid4()))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(create_matter(req, _fake_request()))
    assert exc_info.value.status_code == 404


def test_create_matter_with_cross_firm_client_id_404s(monkeypatch):
    import backend.main as m
    other_firm = uuid.uuid4()
    client_id = uuid.uuid4()
    pool = FakePool(clients=[_client_row(client_id, "Someone Else's Client", firm_id=other_firm)])
    monkeypatch.setattr(m, "_db_pool", pool)

    req = MatterCreate(name="Moyo v Dube", client_id=str(client_id))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(create_matter(req, _fake_request()))
    assert exc_info.value.status_code == 404


def test_create_matter_legacy_client_name_only_still_works(monkeypatch):
    """No client_id at all — the pre-Client-entity path, must be unaffected."""
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    req = MatterCreate(name="Estate of X", client_name="Free Text Client")
    result = asyncio.run(create_matter(req, _fake_request()))

    assert result["client_name"] == "Free Text Client"
    assert result.get("client_id") is None


def test_create_matter_with_numbered_client_assigns_first_matter_number(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    pool = FakePool(clients=[_client_row(client_id, "John Moyo", client_number="NGM-007")])
    monkeypatch.setattr(m, "_db_pool", pool)

    req = MatterCreate(name="Moyo v Dube", client_id=str(client_id))
    result = asyncio.run(create_matter(req, _fake_request()))

    assert result["matter_number"] == "NGM-007-01"


def test_create_matter_numbers_sequentially_within_the_same_client(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    existing_matter = {
        "id": uuid.uuid4(), "firm_id": FIRM_ID, "client_id": client_id, "matter_number": "NGM-007-01",
    }
    pool = FakePool(clients=[_client_row(client_id, "John Moyo", client_number="NGM-007")],
                     matters=[existing_matter])
    monkeypatch.setattr(m, "_db_pool", pool)

    req = MatterCreate(name="Moyo Estate Matter", client_id=str(client_id))
    result = asyncio.run(create_matter(req, _fake_request()))

    assert result["matter_number"] == "NGM-007-02"


def test_create_matter_without_client_number_leaves_matter_number_blank(monkeypatch):
    """Linked to a real client, but that client predates numbering (or
    hasn't been backfilled yet) — matter_number stays NULL rather than
    guessing a prefix."""
    import backend.main as m
    client_id = uuid.uuid4()
    pool = FakePool(clients=[_client_row(client_id, "John Moyo")])  # client_number=None
    monkeypatch.setattr(m, "_db_pool", pool)

    req = MatterCreate(name="Moyo v Dube", client_id=str(client_id))
    result = asyncio.run(create_matter(req, _fake_request()))

    assert result.get("matter_number") is None


def test_create_matter_without_client_id_leaves_matter_number_blank(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    req = MatterCreate(name="Estate of X", client_name="Free Text Client")
    result = asyncio.run(create_matter(req, _fake_request()))

    assert result.get("matter_number") is None


def test_create_matter_also_stores_case_parties(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    req = MatterCreate(name="Chikwanha v Ministry", case_parties="Chikwanha's company (Zenith Pvt Ltd)")
    result = asyncio.run(create_matter(req, _fake_request()))

    assert result["case_parties"] == "Chikwanha's company (Zenith Pvt Ltd)"


# ── practice_area (fixed category set) ───────────────────────────────────

def test_create_matter_accepts_a_valid_practice_area(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    req = MatterCreate(name="Moyo v Dube", practice_area="Family/Matrimonial")
    result = asyncio.run(create_matter(req, _fake_request()))

    assert result["practice_area"] == "Family/Matrimonial"


def test_create_matter_without_practice_area_leaves_it_null(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    req = MatterCreate(name="Moyo v Dube")
    result = asyncio.run(create_matter(req, _fake_request()))

    assert result.get("practice_area") is None


def test_create_matter_rejects_a_practice_area_outside_the_fixed_list(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    req = MatterCreate(name="Moyo v Dube", practice_area="Not A Real Category")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(create_matter(req, _fake_request()))
    assert exc_info.value.status_code == 422


def test_update_matter_accepts_a_valid_practice_area(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    existing_matter = {
        "id": matter_id, "firm_id": FIRM_ID, "name": "Estate of X", "number": None,
        "internal_ref": None, "external_ref": None, "client_name": None, "client_id": None,
        "case_parties": None, "matter_type": None, "practice_area": None, "status": "Active",
        "custom_status": None, "document_count": 0, "last_activity": None,
        "created_at": datetime.now(timezone.utc), "created_by": None,
        "next_deadline": None, "next_deadline_note": None,
    }
    pool = FakePool(matters=[existing_matter])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_matter(str(matter_id), MatterUpdate(practice_area="Trust"), _fake_request()))

    assert result["practice_area"] == "Trust"


def test_update_matter_rejects_a_practice_area_outside_the_fixed_list(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    existing_matter = {
        "id": matter_id, "firm_id": FIRM_ID, "name": "Estate of X", "number": None,
        "internal_ref": None, "external_ref": None, "client_name": None, "client_id": None,
        "case_parties": None, "matter_type": None, "practice_area": None, "status": "Active",
        "custom_status": None, "document_count": 0, "last_activity": None,
        "created_at": datetime.now(timezone.utc), "created_by": None,
        "next_deadline": None, "next_deadline_note": None,
    }
    pool = FakePool(matters=[existing_matter])
    monkeypatch.setattr(m, "_db_pool", pool)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_matter(str(matter_id), MatterUpdate(practice_area="Made Up"), _fake_request()))
    assert exc_info.value.status_code == 422


# ── update_matter ────────────────────────────────────────────────────────

def test_update_matter_with_client_id_syncs_client_name(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    client_id = uuid.uuid4()
    existing_matter = {
        "id": matter_id, "firm_id": FIRM_ID, "name": "Estate of X", "number": None,
        "internal_ref": None, "external_ref": None, "client_name": None, "client_id": None,
        "case_parties": None, "matter_type": None, "status": "Active", "custom_status": None,
        "document_count": 0, "last_activity": None, "created_at": datetime.now(timezone.utc),
        "created_by": None, "next_deadline": None, "next_deadline_note": None,
    }
    pool = FakePool(clients=[_client_row(client_id, "John Moyo")], matters=[existing_matter])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_matter(str(matter_id), MatterUpdate(client_id=str(client_id)), _fake_request()))

    assert result["client_id"] == str(client_id)
    assert result["client_name"] == "John Moyo"


def test_update_matter_with_unknown_client_id_404s(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    existing_matter = {
        "id": matter_id, "firm_id": FIRM_ID, "name": "Estate of X", "number": None,
        "internal_ref": None, "external_ref": None, "client_name": None, "client_id": None,
        "case_parties": None, "matter_type": None, "status": "Active", "custom_status": None,
        "document_count": 0, "last_activity": None, "created_at": datetime.now(timezone.utc),
        "created_by": None, "next_deadline": None, "next_deadline_note": None,
    }
    pool = FakePool(matters=[existing_matter])
    monkeypatch.setattr(m, "_db_pool", pool)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_matter(str(matter_id), MatterUpdate(client_id=str(uuid.uuid4())), _fake_request()))
    assert exc_info.value.status_code == 404


def _fake_request():
    """AUTH_ENABLED is False in this test environment (no OTP env vars), so
    get_current_user() never touches this — see tests/test_docx_export.py."""
    return None


# ── auto_create_matter (Legal Corner spec endpoint) ─────────────────────────
# verify_firm_api_key() does its own Authorization-header + firm_api_keys
# DB lookup, unrelated to what's under test here, so it's monkeypatched to a
# fixed firm_id directly rather than faked through the DB layer too.

def _patch_api_key(monkeypatch, firm_id=FIRM_ID):
    import backend.main as m

    async def _fake_verify(request):
        return str(firm_id)

    monkeypatch.setattr(m, "verify_firm_api_key", _fake_verify)


def test_auto_create_matter_with_client_id_syncs_client_name_and_derived_name(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    lawyer_id = uuid.uuid4()
    pool = FakePool(
        clients=[_client_row(client_id, "John Moyo")],
        org_roles=[{"firm_id": FIRM_ID, "user_id": lawyer_id, "role": "panel_lawyer"}],
    )
    monkeypatch.setattr(m, "_db_pool", pool)
    _patch_api_key(monkeypatch)

    req = AutoCreateMatterRequest(
        external_ref="LC-001", client_name="Jon Moya (typo from subscriber)",
        assigned_lawyer_id=str(lawyer_id), coverage_tier="tier_1", service_type="Consultation",
        client_id=str(client_id),
    )
    result = asyncio.run(auto_create_matter(req, _fake_request()))

    assert result["created"] is True
    assert str(result["client_id"]) == str(client_id)
    assert result["client_name"] == "John Moyo"  # resolved, not the typo'd input
    assert result["name"] == "John Moyo — Consultation"  # derived name also uses resolved client


def test_auto_create_matter_stores_case_parties(monkeypatch):
    import backend.main as m
    lawyer_id = uuid.uuid4()
    pool = FakePool(org_roles=[{"firm_id": FIRM_ID, "user_id": lawyer_id, "role": "panel_lawyer"}])
    monkeypatch.setattr(m, "_db_pool", pool)
    _patch_api_key(monkeypatch)

    req = AutoCreateMatterRequest(
        external_ref="LC-002", client_name="Jane Sithole",
        assigned_lawyer_id=str(lawyer_id), coverage_tier="tier_2", service_type="Drafting",
        case_parties="Sithole Transport (Pvt) Ltd, cited separately",
    )
    result = asyncio.run(auto_create_matter(req, _fake_request()))

    assert result["case_parties"] == "Sithole Transport (Pvt) Ltd, cited separately"


def test_auto_create_matter_with_unknown_client_id_404s(monkeypatch):
    import backend.main as m
    lawyer_id = uuid.uuid4()
    pool = FakePool(org_roles=[{"firm_id": FIRM_ID, "user_id": lawyer_id, "role": "panel_lawyer"}])
    monkeypatch.setattr(m, "_db_pool", pool)
    _patch_api_key(monkeypatch)

    req = AutoCreateMatterRequest(
        external_ref="LC-003", client_name="Jane Sithole",
        assigned_lawyer_id=str(lawyer_id), coverage_tier="tier_2", service_type="Drafting",
        client_id=str(uuid.uuid4()),
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(auto_create_matter(req, _fake_request()))
    assert exc_info.value.status_code == 404


def test_auto_create_matter_with_cross_firm_client_id_404s(monkeypatch):
    import backend.main as m
    other_firm = uuid.uuid4()
    client_id = uuid.uuid4()
    lawyer_id = uuid.uuid4()
    pool = FakePool(
        clients=[_client_row(client_id, "Someone Else's Client", firm_id=other_firm)],
        org_roles=[{"firm_id": FIRM_ID, "user_id": lawyer_id, "role": "panel_lawyer"}],
    )
    monkeypatch.setattr(m, "_db_pool", pool)
    _patch_api_key(monkeypatch)

    req = AutoCreateMatterRequest(
        external_ref="LC-004", client_name="Jane Sithole",
        assigned_lawyer_id=str(lawyer_id), coverage_tier="tier_2", service_type="Drafting",
        client_id=str(client_id),
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(auto_create_matter(req, _fake_request()))
    assert exc_info.value.status_code == 404


def test_auto_create_matter_without_client_id_still_works(monkeypatch):
    """Legacy path — no client_id at all, must be unaffected by the new fields."""
    import backend.main as m
    lawyer_id = uuid.uuid4()
    pool = FakePool(org_roles=[{"firm_id": FIRM_ID, "user_id": lawyer_id, "role": "panel_lawyer"}])
    monkeypatch.setattr(m, "_db_pool", pool)
    _patch_api_key(monkeypatch)

    req = AutoCreateMatterRequest(
        external_ref="LC-005", client_name="Jane Sithole",
        assigned_lawyer_id=str(lawyer_id), coverage_tier="tier_2", service_type="Drafting",
    )
    result = asyncio.run(auto_create_matter(req, _fake_request()))

    assert result["created"] is True
    assert result["client_name"] == "Jane Sithole"
    assert result.get("client_id") is None
    assert result.get("case_parties") is None
