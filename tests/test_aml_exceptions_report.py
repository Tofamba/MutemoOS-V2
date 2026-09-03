"""
Unit tests for the AML Exceptions / Action Required report
(backend/main.py, 2026-09-03, partner design review, Part B):
  - GET /api/reports/aml-exceptions — JSON, one row per outstanding
    compliance ITEM (not per client), firm-wide, prioritized.
  - GET /api/reports/aml-exceptions-export — CSV download.
  - GET /api/reports/aml-exceptions-export-pdf — PDF download.

Same permission tier as the AML/Client Compliance Register
(reports:client_compliance_status) -- not a new permission key, per
instruction. Reuses _compute_compliance_status()'s missing[] list
completely unchanged (already thoroughly tested in
tests/test_client_compliance.py and tests/test_client_compliance_status_
report.py) -- these tests verify the wiring (flattening to one row per
item, priority derivation, Responsible Person resolution, sort order,
export shapes), not that function's own internal correctness.

Priority rule (a judgment call, reported to the user before shipping):
PEP-related and beneficial-ownership items are High; everything else
_compute_compliance_status() can put in missing[] (identity, conflict
check, unset client type) is Medium. See _priority_for_missing_item()'s
own docstring in backend/main.py.

Same FakeConnection/FakePool/_as_current_user/_fake_request convention
as tests/test_client_compliance_status_report.py, which this file
mirrors closely -- plus a `users` fixture list for the Responsible
Person lookup (SELECT id, display_name FROM users WHERE id = ANY($1)
AND firm_id=$2).
"""

import asyncio
import uuid

import pytest
from fastapi import HTTPException

from backend.main import (
    FIRM_ID,
    aml_exceptions_report,
    aml_exceptions_report_export,
    aml_exceptions_report_export_pdf,
)


class FakeConnection:
    def __init__(self, clients=None, compliance=None, owners=None, users=None):
        self.clients = clients if clients is not None else []
        self.compliance = compliance if compliance is not None else []
        self.owners = owners if owners is not None else []
        self.users = users if users is not None else []

    async def fetch(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT * FROM clients WHERE firm_id=$1 ORDER BY full_name ASC"):
            firm_id, = args
            rows = [c for c in self.clients if c["firm_id"] == firm_id]
            return sorted(rows, key=lambda c: c["full_name"])

        if q.startswith("SELECT * FROM client_compliance WHERE client_id = ANY($1)"):
            client_ids, firm_id = args
            return [c for c in self.compliance if c["client_id"] in client_ids and c["firm_id"] == firm_id]

        if q.startswith("SELECT client_id, verification_status FROM beneficial_owners"):
            client_ids, firm_id = args
            return [o for o in self.owners if o["client_id"] in client_ids and o["firm_id"] == firm_id]

        if q.startswith("SELECT id, display_name FROM users WHERE id = ANY($1) AND firm_id=$2"):
            user_ids, firm_id = args
            return [u for u in self.users if u["id"] in user_ids and u["firm_id"] == firm_id]

        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, **kwargs):
        self.conn = FakeConnection(**kwargs)

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _client(name, *, client_type="Individual", client_number=None, created_by=None):
    return {
        "id": uuid.uuid4(), "firm_id": FIRM_ID, "full_name": name,
        "client_type": client_type, "client_number": client_number,
        "created_by": created_by,
    }


def _compliance(client_id, **kwargs):
    row = {
        "client_id": client_id, "firm_id": FIRM_ID,
        "identity_verification_status": "Unverified",
        "client_is_beneficial_owner": None,
        "is_pep": None, "senior_management_approved_by": None,
        "risk_rating": "NotAssessed", "conflict_check_reviewed": False,
    }
    row.update(kwargs)
    return row


def _owner(client_id, verification_status):
    return {"client_id": client_id, "firm_id": FIRM_ID, "verification_status": verification_status}


def _user(display_name, user_id=None):
    return {"id": user_id or uuid.uuid4(), "firm_id": FIRM_ID, "display_name": display_name}


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def _fake_request():
    return None


def _csv_rows(response):
    import csv, io
    text = response.body.decode("utf-8-sig") if isinstance(response.body, bytes) else response.body.lstrip("﻿")
    return list(csv.reader(io.StringIO(text)))


# ── permission gate ──────────────────────────────────────────────────────

def test_associate_gets_403(monkeypatch):
    import backend.main as m
    associate = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "associate", "display_name": "Assoc"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, associate)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(aml_exceptions_report(_fake_request()))
    assert exc_info.value.status_code == 403


def test_export_associate_gets_403(monkeypatch):
    import backend.main as m
    associate = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "associate", "display_name": "Assoc"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, associate)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(aml_exceptions_report_export(_fake_request()))
    assert exc_info.value.status_code == 403


def test_pdf_export_associate_gets_403(monkeypatch):
    import backend.main as m
    associate = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "associate", "display_name": "Assoc"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, associate)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(aml_exceptions_report_export_pdf(_fake_request()))
    assert exc_info.value.status_code == 403


def test_partner_and_admin_both_succeed(monkeypatch):
    import backend.main as m
    client = _client("Munyaradzi Gwenzi")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))

    for role in ("partner", "admin"):
        _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": role, "display_name": "X"})
        rows = asyncio.run(aml_exceptions_report(_fake_request()))
        assert len(rows) >= 1


# ── one row per outstanding ITEM, not per client ──────────────────────────

def test_a_fully_cleared_client_produces_zero_rows(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    client = _client("Tendai Moyo", client_type="Individual")
    compliance = _compliance(
        client["id"], identity_verification_status="Verified",
        is_pep=False, conflict_check_reviewed=True,
    )
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance=[compliance]))
    _as_current_user(monkeypatch, m, partner)

    rows = asyncio.run(aml_exceptions_report(_fake_request()))

    assert rows == []


def test_a_client_with_two_missing_items_gets_two_rows(monkeypatch):
    """The core shape of this report: flat, one row per item, not one
    row per client."""
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    client = _client("Panashe Madziva", client_type="Individual")
    # No compliance row at all -> defaults apply: Identity not verified,
    # PEP screening not completed, Conflict check not reviewed (3 items
    # for an Individual -- BO doesn't apply). Use a real compliance row
    # instead to pin it down to exactly 2 known items.
    compliance = _compliance(client["id"], identity_verification_status="Verified", is_pep=None)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance=[compliance]))
    _as_current_user(monkeypatch, m, partner)

    rows = asyncio.run(aml_exceptions_report(_fake_request()))

    assert len(rows) == 2
    issues = {r["issue"] for r in rows}
    assert issues == {"PEP screening not completed", "Conflict check not reviewed"}
    assert all(r["client_name"] == "Panashe Madziva" for r in rows)


# ── Priority derivation ────────────────────────────────────────────────

def test_pep_item_is_high_priority(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    client = _client("PEP Screening Client", client_type="Individual")
    compliance = _compliance(client["id"], identity_verification_status="Verified", conflict_check_reviewed=True)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance=[compliance]))
    _as_current_user(monkeypatch, m, partner)

    rows = asyncio.run(aml_exceptions_report(_fake_request()))

    assert len(rows) == 1
    assert rows[0]["issue"] == "PEP screening not completed"
    assert rows[0]["priority"] == "High"


def test_beneficial_ownership_item_is_high_priority(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    client = _client("Mafuta Family Trust", client_type="Trust")
    compliance = _compliance(
        client["id"], identity_verification_status="Verified",
        is_pep=False, conflict_check_reviewed=True,
    )
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance=[compliance]))
    _as_current_user(monkeypatch, m, partner)

    rows = asyncio.run(aml_exceptions_report(_fake_request()))

    assert len(rows) == 1
    assert rows[0]["issue"] == "Beneficial ownership not assessed"
    assert rows[0]["priority"] == "High"


def test_senior_management_approval_pep_item_is_high_priority(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    client = _client("Mould Enterprises (Pvt) Ltd", client_type="Company")
    compliance = _compliance(
        client["id"], identity_verification_status="Verified",
        client_is_beneficial_owner="Yes", is_pep=True, conflict_check_reviewed=True,
        risk_rating="High",  # isolate the senior-management-approval item alone
    )
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance=[compliance]))
    _as_current_user(monkeypatch, m, partner)

    rows = asyncio.run(aml_exceptions_report(_fake_request()))

    assert len(rows) == 1
    assert rows[0]["issue"] == "Senior management approval required (PEP)"
    assert rows[0]["priority"] == "High"


def test_identity_and_conflict_check_items_are_medium_priority(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    client = _client("Munyaradzi Gwenzi", client_type="Individual")
    compliance = _compliance(client["id"], is_pep=False)  # identity + conflict check missing
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance=[compliance]))
    _as_current_user(monkeypatch, m, partner)

    rows = asyncio.run(aml_exceptions_report(_fake_request()))

    assert len(rows) == 2
    assert {r["issue"] for r in rows} == {"Identity not verified", "Conflict check not reviewed"}
    assert all(r["priority"] == "Medium" for r in rows)


# ── Matter always blank ──────────────────────────────────────────────────

def test_matter_is_always_blank_client_level_only(monkeypatch):
    """Matter-level AML tracking doesn't exist yet -- every current
    outstanding item is client-level."""
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    client = _client("Munyaradzi Gwenzi", client_type="Individual")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, partner)

    rows = asyncio.run(aml_exceptions_report(_fake_request()))

    assert rows
    assert all(r["matter"] == "" for r in rows)


# ── Responsible Person ────────────────────────────────────────────────────

def test_responsible_person_defaults_to_the_clients_created_by_lawyer(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    lawyer_id = uuid.uuid4()
    client = _client("Munyaradzi Gwenzi", client_type="Individual", created_by=lawyer_id)
    lawyer = _user("J. Moyo", user_id=lawyer_id)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], users=[lawyer]))
    _as_current_user(monkeypatch, m, partner)

    rows = asyncio.run(aml_exceptions_report(_fake_request()))

    assert rows
    assert all(r["responsible_person"] == "J. Moyo" for r in rows)


def test_responsible_person_falls_back_to_compliance_officer_when_no_created_by(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    client = _client("Munyaradzi Gwenzi", client_type="Individual", created_by=None)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, partner)

    rows = asyncio.run(aml_exceptions_report(_fake_request()))

    assert rows
    assert all(r["responsible_person"] == "Compliance Officer" for r in rows)


def test_responsible_person_falls_back_to_compliance_officer_when_creator_user_row_is_gone(monkeypatch):
    """created_by points at a real-looking id, but no matching users row
    is found (e.g. the account was removed) -- degrades to the role
    fallback rather than showing a blank or crashing."""
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    client = _client("Munyaradzi Gwenzi", client_type="Individual", created_by=uuid.uuid4())
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], users=[]))
    _as_current_user(monkeypatch, m, partner)

    rows = asyncio.run(aml_exceptions_report(_fake_request()))

    assert rows
    assert all(r["responsible_person"] == "Compliance Officer" for r in rows)


# ── Status always "Open" ──────────────────────────────────────────────────

def test_status_is_always_open(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    client = _client("Munyaradzi Gwenzi", client_type="Individual")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, partner)

    rows = asyncio.run(aml_exceptions_report(_fake_request()))

    assert rows
    assert all(r["status"] == "Open" for r in rows)


# ── Sort order ─────────────────────────────────────────────────────────

def test_high_priority_items_sort_before_medium(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    high_client = _client("Mafuta Family Trust", client_type="Trust")  # BO -> High
    medium_client = _client("Munyaradzi Gwenzi", client_type="Individual")  # identity -> Medium
    high_compliance = _compliance(
        high_client["id"], identity_verification_status="Verified",
        is_pep=False, conflict_check_reviewed=True,
    )
    medium_compliance = _compliance(medium_client["id"], is_pep=False, conflict_check_reviewed=True)
    monkeypatch.setattr(
        m, "_db_pool",
        FakePool(clients=[medium_client, high_client], compliance=[high_compliance, medium_compliance]),
    )
    _as_current_user(monkeypatch, m, partner)

    rows = asyncio.run(aml_exceptions_report(_fake_request()))

    assert [r["priority"] for r in rows] == ["High", "Medium"]


# ── CSV export ────────────────────────────────────────────────────────────

def test_csv_export_columns_and_content(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    lawyer_id = uuid.uuid4()
    client = _client("Munyaradzi Gwenzi", client_type="Individual", created_by=lawyer_id)
    lawyer = _user("J. Moyo", user_id=lawyer_id)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], users=[lawyer]))
    _as_current_user(monkeypatch, m, partner)

    response = asyncio.run(aml_exceptions_report_export(_fake_request()))
    rows = _csv_rows(response)

    assert rows[0] == ["Priority", "Client", "Matter", "Issue", "Responsible Person", "Status"]
    data_rows = {r[3]: r for r in rows[1:]}
    assert "Identity not verified" in data_rows
    row = data_rows["Identity not verified"]
    assert row[0] == "Medium"
    assert row[1] == "Munyaradzi Gwenzi"
    assert row[2] == ""
    assert row[4] == "J. Moyo"
    assert row[5] == "Open"


# ── PDF export ────────────────────────────────────────────────────────────

def test_pdf_export_produces_a_real_pdf_with_data(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    client = _client("Munyaradzi Gwenzi", client_type="Individual")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, partner)

    response = asyncio.run(aml_exceptions_report_export_pdf(_fake_request()))

    assert response.media_type == "application/pdf"
    assert "aml_exceptions" in response.headers["content-disposition"]
    assert response.body.startswith(b"%PDF")


def test_pdf_export_handles_no_exceptions_without_crashing(monkeypatch):
    """Every client Cleared -- the report legitimately has zero rows;
    the PDF must still render (a friendly message) rather than crash."""
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    client = _client("Tendai Moyo", client_type="Individual")
    compliance = _compliance(
        client["id"], identity_verification_status="Verified",
        is_pep=False, conflict_check_reviewed=True,
    )
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance=[compliance]))
    _as_current_user(monkeypatch, m, partner)

    response = asyncio.run(aml_exceptions_report_export_pdf(_fake_request()))

    assert response.media_type == "application/pdf"
    assert response.body.startswith(b"%PDF")
