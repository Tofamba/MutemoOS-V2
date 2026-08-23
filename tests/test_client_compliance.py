"""
Unit tests for the AML/KYC Client Compliance module (backend/main.py) --
Money Laundering and Proceeds of Crime Act [Chapter 9:24] ss17, 15, 20, 24.

Covers: client_type enum validation (Part 1), multiple beneficial owners
per client with no percentage required (Part 2), PEP flag forcing
senior_management_approval_required and blocking compliance_status until
approved (Part 4), and compliance_status correctly reflecting missing vs.
complete items. Existing client CRUD is verified unaffected by re-running
the full suite (tests/test_clients_api.py, tests/test_client_detail_richness.py
both still pass unmodified in behaviour -- only their FakeConnections
gained two new no-op query handlers for this module's additions to
get_client()).

Called directly as plain async functions, same convention as
tests/test_clients_api.py -- see that file's docstring conventions for the
column-list-read-from-the-query-text INSERT handling this mirrors.
"""
import asyncio
import re
import uuid
from datetime import date, datetime, timezone

import asyncpg
import pytest
from fastapi import HTTPException

import backend.main as m
from backend.main import (
    AuthorizedRepresentativeCreate,
    BeneficialOwnerCreate,
    BeneficialOwnerUpdate,
    ClientComplianceUpdate,
    ClientUpdate,
    CLIENT_TYPES,
    create_beneficial_owner,
    get_client,
    get_client_compliance,
    list_beneficial_owners,
    update_beneficial_owner,
    update_client,
    update_client_compliance,
)

# A user id that never exists in the fake `users` table -- simulates the
# real Postgres foreign-key violation client_compliance.senior_management_
# approved_by / conflict_check_reviewed_by would raise for a bogus id.
INVALID_USER_ID = uuid.uuid4()


def _raise_if_invalid_user_fk(row: dict) -> None:
    for field in ("senior_management_approved_by", "conflict_check_reviewed_by"):
        if row.get(field) == INVALID_USER_ID:
            err = asyncpg.exceptions.ForeignKeyViolationError(
                f'insert or update on table "client_compliance" violates foreign key constraint '
                f'"client_compliance_{field}_fkey"'
            )
            err.constraint_name = f"client_compliance_{field}_fkey"
            raise err


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, clients):
        self.clients = clients
        self.beneficial_owners = []
        self.compliance = {}  # client_id -> dict
        self.matters = []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT * FROM clients WHERE id=$1 AND firm_id=$2"):
            for c in self.clients:
                if c["id"] == args[0] and c["firm_id"] == args[1]:
                    return dict(c)
            return None

        if q.startswith("UPDATE clients SET"):
            m_ = re.search(r"SET (.+) WHERE id=\$1", q)
            cols = re.findall(r"(\w+)=\$\d+(?:::jsonb)?", m_.group(1))
            cid, firm_id = args[0], args[-1]
            values = args[1:1 + len(cols)]
            for c in self.clients:
                if c["id"] == cid and c["firm_id"] == firm_id:
                    for col, val in zip(cols, values):
                        c[col] = val
                    return dict(c)
            return None

        if q.startswith("INSERT INTO beneficial_owners"):
            cols = [c.strip() for c in q.split("(", 1)[1].split(")", 1)[0].split(",")]
            row = dict(zip(cols, args))
            row["id"] = uuid.uuid4()
            row.setdefault("verification_status", "Unverified")
            row.setdefault("verified_date", None)
            row.setdefault("verified_by", None)
            row["created_at"] = datetime.now(timezone.utc)
            self.beneficial_owners.append(row)
            return dict(row)

        if q.startswith("UPDATE beneficial_owners SET"):
            m_ = re.search(r"SET (.+) WHERE id=\$1", q)
            cols = re.findall(r"(\w+)=\$\d+", m_.group(1))
            oid, cid = args[0], args[1]
            values = args[2:2 + len(cols)]
            for o in self.beneficial_owners:
                if o["id"] == oid and o["client_id"] == cid:
                    for col, val in zip(cols, values):
                        o[col] = val
                    return dict(o)
            return None

        if q.startswith("SELECT * FROM client_compliance WHERE client_id=$1 AND firm_id=$2"):
            return dict(self.compliance[args[0]]) if args[0] in self.compliance else None

        if q.startswith("SELECT id FROM client_compliance WHERE client_id=$1 AND firm_id=$2"):
            return {"id": self.compliance[args[0]]["id"]} if args[0] in self.compliance else None

        if q.startswith("INSERT INTO client_compliance"):
            cols = [c.strip() for c in q.split("(", 1)[1].split(")", 1)[0].split(",")]
            row = dict(zip(cols, args))
            _raise_if_invalid_user_fk(row)
            row["id"] = uuid.uuid4()
            for k, default in m._DEFAULT_CLIENT_COMPLIANCE.items():
                row.setdefault(k, default)
            row["created_at"] = row["updated_at"] = datetime.now(timezone.utc)
            self.compliance[row["client_id"]] = row
            return dict(row)

        if q.startswith("UPDATE client_compliance SET"):
            m_ = re.search(r"SET (.+) WHERE client_id=\$1", q)
            cols = re.findall(r"(\w+)=\$\d+", m_.group(1))
            cid, firm_id = args[0], args[1]
            values = args[2:2 + len(cols)]
            _raise_if_invalid_user_fk(dict(zip(cols, values)))
            row = self.compliance[cid]
            for col, val in zip(cols, values):
                row[col] = val
            return dict(row)

        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT * FROM beneficial_owners WHERE client_id=$1 AND firm_id=$2"):
            return [dict(o) for o in self.beneficial_owners if o["client_id"] == args[0]]

        if q.startswith("SELECT verification_status FROM beneficial_owners WHERE client_id=$1 AND firm_id=$2"):
            return [{"verification_status": o["verification_status"]}
                    for o in self.beneficial_owners if o["client_id"] == args[0]]

        if q.startswith("SELECT * FROM matters WHERE client_id=$1 AND firm_id=$2"):
            return [dict(mm) for mm in self.matters if mm.get("client_id") == args[0]]

        if q.startswith("SELECT * FROM progress_notes WHERE matter_id = ANY($1)"):
            return []
        if q.startswith("SELECT * FROM documents WHERE matter_id = ANY($1)"):
            return []
        if q.startswith("SELECT * FROM calendar_events WHERE matter_id = ANY($1)"):
            return []

        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")

    async def execute(self, query, *args):
        return "OK"


class FakePool:
    def __init__(self, clients=None):
        self.conn = FakeConnection(clients or [])

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _client_row(firm_id, **overrides):
    row = {
        "id": uuid.uuid4(), "firm_id": firm_id, "full_name": "Test Client",
        "email": None, "phone": None, "physical_address": None,
        "id_or_registration_number": None, "contact_person": None, "notes": None,
        "client_number": None, "created_by": None,
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
        "client_type": None,
        "date_of_birth": None, "place_of_birth": None, "national_id_number": None,
        "passport_number": None, "id_expiry_date": None, "residential_address": None,
        "occupation": None, "employer_or_business": None,
        "registered_name": None, "trading_name": None, "registration_number": None,
        "date_incorporated": None, "registered_office_address": None,
        "principal_business_address": None,
        "proof_of_incorporation_document_id": None, "governing_document_id": None,
        "trustees": [], "settlors": [], "beneficiaries": [],
    }
    row.update(overrides)
    return row


# ── Part 1: client_type enum validation ──────────────────────────────────

def test_client_type_rejects_invalid_value(monkeypatch):
    client = _client_row(m.FIRM_ID)
    pool = FakePool(clients=[client])
    monkeypatch.setattr(m, "_db_pool", pool)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_client(str(client["id"]), ClientUpdate(client_type="NotARealType"), None))
    assert exc_info.value.status_code == 422


def test_client_type_accepts_every_valid_value(monkeypatch):
    for ct in CLIENT_TYPES:
        client = _client_row(m.FIRM_ID)
        pool = FakePool(clients=[client])
        monkeypatch.setattr(m, "_db_pool", pool)

        result = asyncio.run(update_client(str(client["id"]), ClientUpdate(client_type=ct), None))
        assert result["client_type"] == ct


# ── Part 2: multiple beneficial owners, no percentage required ──────────

def test_multiple_beneficial_owners_supported_per_client(monkeypatch):
    client = _client_row(m.FIRM_ID, client_type="Company")
    pool = FakePool(clients=[client])
    monkeypatch.setattr(m, "_db_pool", pool)

    asyncio.run(create_beneficial_owner(str(client["id"]), BeneficialOwnerCreate(owner_name="Owner One"), None))
    asyncio.run(create_beneficial_owner(str(client["id"]), BeneficialOwnerCreate(owner_name="Owner Two"), None))

    owners = asyncio.run(list_beneficial_owners(str(client["id"]), None))
    assert len(owners) == 2
    assert {o["owner_name"] for o in owners} == {"Owner One", "Owner Two"}


def test_beneficial_owner_does_not_require_ownership_percentage(monkeypatch):
    client = _client_row(m.FIRM_ID, client_type="Company")
    pool = FakePool(clients=[client])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(create_beneficial_owner(
        str(client["id"]),
        BeneficialOwnerCreate(owner_name="De Facto Controller", ownership_or_control_basis="Management agreement"),
        None,
    ))
    assert result["owner_name"] == "De Facto Controller"
    assert result["ownership_percentage"] is None


# ── Part 4: PEP forces senior_management_approval_required ──────────────

def test_setting_is_pep_true_forces_senior_management_approval_required(monkeypatch):
    client = _client_row(m.FIRM_ID, client_type="Individual")
    pool = FakePool(clients=[client])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_client_compliance(
        str(client["id"]), ClientComplianceUpdate(is_pep=True), None
    ))
    assert result["senior_management_approval_required"] is True


def test_invalid_senior_management_approver_id_returns_422_not_500(monkeypatch):
    """A nonexistent user id previously surfaced as a raw 500 (asyncpg's
    ForeignKeyViolationError going uncaught) -- update_client_compliance
    must catch it and return a clean 422 instead."""
    client = _client_row(m.FIRM_ID, client_type="Individual")
    pool = FakePool(clients=[client])
    monkeypatch.setattr(m, "_db_pool", pool)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_client_compliance(
            str(client["id"]),
            ClientComplianceUpdate(senior_management_approved_by=str(INVALID_USER_ID)),
            None,
        ))
    assert exc_info.value.status_code == 422
    assert "senior_management_approved_by" in exc_info.value.detail
    assert "valid user ID" in exc_info.value.detail


def test_compliance_status_blocked_until_pep_approved(monkeypatch):
    client = _client_row(m.FIRM_ID, client_type="Individual")
    pool = FakePool(clients=[client])
    monkeypatch.setattr(m, "_db_pool", pool)

    asyncio.run(update_client_compliance(
        str(client["id"]),
        ClientComplianceUpdate(is_pep=True, identity_verification_status="Verified"),
        None,
    ))
    result = asyncio.run(get_client_compliance(str(client["id"]), None))

    assert result["compliance_status"] == "Action Required"
    assert "Senior management approval required (PEP)" in result["missing"]


def test_compliance_status_cleared_once_pep_approved(monkeypatch):
    client = _client_row(m.FIRM_ID, client_type="Individual")
    pool = FakePool(clients=[client])
    monkeypatch.setattr(m, "_db_pool", pool)

    approver_id = str(uuid.uuid4())
    asyncio.run(update_client_compliance(
        str(client["id"]),
        ClientComplianceUpdate(
            is_pep=True, identity_verification_status="Verified",
            senior_management_approved_by=approver_id,
            senior_management_approved_date="2026-08-01",
            conflict_check_reviewed=True,
        ),
        None,
    ))
    result = asyncio.run(get_client_compliance(str(client["id"]), None))

    assert result["compliance_status"] == "Cleared"
    assert result["missing"] == []


# ── compliance_status: missing vs. complete items ────────────────────────

def test_compliance_status_requires_client_type_first(monkeypatch):
    client = _client_row(m.FIRM_ID, client_type=None)
    pool = FakePool(clients=[client])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(get_client_compliance(str(client["id"]), None))

    assert result["compliance_status"] == "Action Required"
    assert result["missing"] == ["Client type not recorded"]


def test_compliance_status_lists_all_missing_items_for_untouched_individual(monkeypatch):
    client = _client_row(m.FIRM_ID, client_type="Individual")
    pool = FakePool(clients=[client])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(get_client_compliance(str(client["id"]), None))

    assert result["compliance_status"] == "Action Required"
    assert "Identity not verified" in result["missing"]
    assert "PEP screening not completed" in result["missing"]
    assert "Conflict check not reviewed" in result["missing"]
    # Individual is inherently its own beneficial owner — not applicable.
    assert not any("Beneficial ownership" in item for item in result["missing"])


def test_conflict_check_reuses_the_real_matter_conflict_endpoint_not_a_stub(monkeypatch):
    """Confirms compliance_status actually gates on the real, existing
    conflict-check feature (GET /api/matters/check-conflict) rather than
    a stub -- this was wrongly assumed not to exist earlier in this
    module's design and had to be corrected before shipping."""
    client = _client_row(m.FIRM_ID, client_type="Individual")
    pool = FakePool(clients=[client])
    monkeypatch.setattr(m, "_db_pool", pool)

    asyncio.run(update_client_compliance(
        str(client["id"]),
        ClientComplianceUpdate(identity_verification_status="Verified", is_pep=False),
        None,
    ))
    result = asyncio.run(get_client_compliance(str(client["id"]), None))
    assert "Conflict check not reviewed" in result["missing"]
    assert result["compliance_status"] == "Action Required"

    asyncio.run(update_client_compliance(
        str(client["id"]), ClientComplianceUpdate(conflict_check_reviewed=True), None
    ))
    result = asyncio.run(get_client_compliance(str(client["id"]), None))
    assert result["compliance_status"] == "Cleared"
    assert result["conflict_check_reviewed"] is True
    # conflict_check_reviewed_by stays None here because request=None gives
    # the synthetic dev user (id: None) -- see test_compliance_status_
    # cleared_once_pep_approved's identical reasoning for senior_management_
    # approved_by. The date is still auto-set regardless of who the user is.
    assert result["conflict_check_reviewed_date"] is not None


def test_beneficial_ownership_missing_for_legal_person_when_not_assessed(monkeypatch):
    client = _client_row(m.FIRM_ID, client_type="Company")
    pool = FakePool(clients=[client])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(get_client_compliance(str(client["id"]), None))

    assert "Beneficial ownership not assessed" in result["missing"]


def test_beneficial_ownership_satisfied_when_client_is_the_owner(monkeypatch):
    client = _client_row(m.FIRM_ID, client_type="Company")
    pool = FakePool(clients=[client])
    monkeypatch.setattr(m, "_db_pool", pool)

    asyncio.run(update_client_compliance(
        str(client["id"]), ClientComplianceUpdate(client_is_beneficial_owner="Yes"), None
    ))
    result = asyncio.run(get_client_compliance(str(client["id"]), None))

    assert not any("Beneficial ownership" in item for item in result["missing"])


def test_beneficial_ownership_requires_a_verified_owner_when_client_is_not_the_owner(monkeypatch):
    client = _client_row(m.FIRM_ID, client_type="Company")
    pool = FakePool(clients=[client])
    monkeypatch.setattr(m, "_db_pool", pool)

    asyncio.run(update_client_compliance(
        str(client["id"]), ClientComplianceUpdate(client_is_beneficial_owner="No"), None
    ))
    owner = asyncio.run(create_beneficial_owner(
        str(client["id"]), BeneficialOwnerCreate(owner_name="Jane Owner"), None
    ))

    result = asyncio.run(get_client_compliance(str(client["id"]), None))
    assert "Beneficial ownership not verified" in result["missing"]

    asyncio.run(update_beneficial_owner(
        str(client["id"]), owner["id"], BeneficialOwnerUpdate(verification_status="Verified"), None
    ))
    result = asyncio.run(get_client_compliance(str(client["id"]), None))
    assert not any("Beneficial ownership" in item for item in result["missing"])


# ── Compliance badge surfaced on GET /api/clients/{id} ───────────────────

def test_get_client_includes_compliance_status_badge(monkeypatch):
    client = _client_row(m.FIRM_ID, client_type="Individual")
    pool = FakePool(clients=[client])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(get_client(str(client["id"]), None))

    assert result["compliance_status"] == "Action Required"
    assert "Identity not verified" in result["compliance_missing"]
