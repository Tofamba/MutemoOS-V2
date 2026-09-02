"""
Unit tests for the Client Compliance Status report (backend/main.py,
2026-09-02):
  - GET /api/reports/client-compliance-status — JSON, every firm client's
    real, computed compliance status, PEP flag, risk rating, and full
    outstanding-requirements list.
  - GET /api/reports/client-compliance-status-export — same data/
    permission, CSV download (same convention as every other report).

The firm-wide roster My Portfolio's own "Compliance/risk snapshot"
(tests/test_my_portfolio.py) never was: that one only ever emits
aggregate counts for one lawyer's own clients. This report is per-client,
firm-wide, and reuses _compute_compliance_status() completely unchanged
(already thoroughly tested in tests/test_client_compliance.py) -- these
tests verify the wiring (batched fetch, row shape, CSV columns,
permission gate), not that function's own internal correctness.

Called directly as plain async functions, same convention as
tests/test_matter_review_status_report.py (whose FakeConnection/FakePool/
_as_current_user/_fake_request/_csv_rows this file mirrors) and
tests/test_my_portfolio.py (whose clients/client_compliance/
beneficial_owners fixture shape this file mirrors).
"""

import asyncio
import csv
import io
import uuid

import pytest
from fastapi import HTTPException

from backend.main import (
    FIRM_ID,
    client_compliance_status_report,
    client_compliance_status_report_export,
)


class FakeConnection:
    def __init__(self, clients=None, compliance=None, owners=None):
        self.clients = clients if clients is not None else []
        self.compliance = compliance if compliance is not None else []
        self.owners = owners if owners is not None else []

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


def _client(name, *, client_type="Individual", client_number=None):
    return {
        "id": uuid.uuid4(), "firm_id": FIRM_ID, "full_name": name,
        "client_type": client_type, "client_number": client_number,
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


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def _fake_request():
    return None


def _csv_rows(response):
    text = response.body.decode("utf-8-sig") if isinstance(response.body, bytes) else response.body.lstrip("﻿")
    return list(csv.reader(io.StringIO(text)))


# ── permission gate ──────────────────────────────────────────────────────

def test_associate_gets_403(monkeypatch):
    import backend.main as m
    associate = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "associate", "display_name": "Assoc"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, associate)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(client_compliance_status_report(_fake_request()))
    assert exc_info.value.status_code == 403


def test_secretary_gets_403(monkeypatch):
    import backend.main as m
    secretary = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "secretary", "display_name": "Sec"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, secretary)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(client_compliance_status_report(_fake_request()))
    assert exc_info.value.status_code == 403


def test_export_associate_gets_403(monkeypatch):
    import backend.main as m
    associate = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "associate", "display_name": "Assoc"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, associate)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(client_compliance_status_report_export(_fake_request()))
    assert exc_info.value.status_code == 403


def test_partner_and_admin_both_succeed(monkeypatch):
    import backend.main as m
    client = _client("Mould Enterprises (Pvt) Ltd")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))

    for role in ("partner", "admin"):
        _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": role, "display_name": "X"})
        rows = asyncio.run(client_compliance_status_report(_fake_request()))
        assert len(rows) == 1


# ── row shape / real compute wiring ──────────────────────────────────────

def test_cleared_individual_client_reports_cleared_with_no_outstanding(monkeypatch):
    """An individual client with everything actually done -- verified
    identity, PEP screening completed (not a PEP), conflict check
    reviewed -- reports Cleared with an empty outstanding list. Beneficial
    ownership is not in scope for an Individual (LEGAL_PERSON_CLIENT_TYPES
    only), so it correctly doesn't appear as missing here."""
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    client = _client("Tendai Moyo", client_type="Individual", client_number="TM-001")
    compliance = _compliance(
        client["id"], identity_verification_status="Verified",
        is_pep=False, conflict_check_reviewed=True, risk_rating="Low",
    )
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance=[compliance]))
    _as_current_user(monkeypatch, m, partner)

    rows = asyncio.run(client_compliance_status_report(_fake_request()))

    assert len(rows) == 1
    row = rows[0]
    assert row["client_id"] == str(client["id"])
    assert row["client_number"] == "TM-001"
    assert row["client_name"] == "Tendai Moyo"
    assert row["client_type"] == "Individual"
    assert row["compliance_status"] == "Cleared"
    assert row["missing"] == []
    assert row["is_pep"] is False
    assert row["risk_rating"] == "Low"


def test_never_assessed_client_reports_action_required_with_full_outstanding_list(monkeypatch):
    """A client with no client_compliance row at all yet (the real,
    common case for an old or just-created client) -- defaults apply,
    and the outstanding list names every real gap, not just a count."""
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    client = _client("Riverbed Civil Works (Pvt) Ltd", client_type="Company")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, partner)

    rows = asyncio.run(client_compliance_status_report(_fake_request()))

    row = rows[0]
    assert row["compliance_status"] == "Action Required"
    assert row["missing"] == [
        "Identity not verified",
        "Beneficial ownership not assessed",
        "PEP screening not completed",
        "Conflict check not reviewed",
    ]
    assert row["is_pep"] is None
    assert row["risk_rating"] == "NotAssessed"


def test_pep_client_missing_senior_management_approval_is_flagged(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    client = _client("Mould Enterprises (Pvt) Ltd", client_type="Company")
    compliance = _compliance(
        client["id"], identity_verification_status="Verified",
        client_is_beneficial_owner="Yes", is_pep=True,
        conflict_check_reviewed=True, risk_rating="High",
    )
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance=[compliance]))
    _as_current_user(monkeypatch, m, partner)

    rows = asyncio.run(client_compliance_status_report(_fake_request()))

    row = rows[0]
    assert row["compliance_status"] == "Action Required"
    assert row["missing"] == ["Senior management approval required (PEP)"]
    assert row["is_pep"] is True
    assert row["risk_rating"] == "High"


def test_legal_person_beneficial_ownership_satisfied_by_a_verified_owner(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    client = _client("Sunshine Properties (Pvt) Ltd", client_type="Company")
    compliance = _compliance(
        client["id"], identity_verification_status="Verified",
        client_is_beneficial_owner="No", is_pep=False, conflict_check_reviewed=True,
    )
    owner = _owner(client["id"], "Verified")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance=[compliance], owners=[owner]))
    _as_current_user(monkeypatch, m, partner)

    rows = asyncio.run(client_compliance_status_report(_fake_request()))

    assert rows[0]["compliance_status"] == "Cleared"
    assert rows[0]["missing"] == []


def test_firm_wide_not_lawyer_scoped(monkeypatch):
    """No lawyer/created_by filter of any kind -- every client in the
    firm appears, unlike My Portfolio's own compliance snapshot."""
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    clients = [_client("Client A"), _client("Client B"), _client("Client C")]
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=clients))
    _as_current_user(monkeypatch, m, partner)

    rows = asyncio.run(client_compliance_status_report(_fake_request()))

    assert {r["client_name"] for r in rows} == {"Client A", "Client B", "Client C"}


def test_sorted_by_client_name(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    clients = [_client("Zebra Holdings"), _client("Anchor Trading")]
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=clients))
    _as_current_user(monkeypatch, m, partner)

    rows = asyncio.run(client_compliance_status_report(_fake_request()))

    assert [r["client_name"] for r in rows] == ["Anchor Trading", "Zebra Holdings"]


def test_no_clients_returns_empty_list(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, partner)

    assert asyncio.run(client_compliance_status_report(_fake_request())) == []


# ── CSV export ────────────────────────────────────────────────────────────

def test_csv_export_includes_full_outstanding_list_not_just_a_count(monkeypatch):
    """The whole point of this report is a real remediation sweep -- the
    CSV's Outstanding column must carry every missing item, semicolon-
    joined, not a bare count."""
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    client = _client("Estate Late Bvumbe", client_type="Estate", client_number="EB-004")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, partner)

    response = asyncio.run(client_compliance_status_report_export(_fake_request()))
    rows = _csv_rows(response)

    assert rows[0] == ["Client Number", "Client Name", "Client Type", "Compliance Status",
                        "Outstanding", "PEP", "Risk Rating"]
    assert rows[1][0] == "EB-004"
    assert rows[1][1] == "Estate Late Bvumbe"
    assert rows[1][3] == "Action Required"
    assert "Identity not verified" in rows[1][4]
    assert "Beneficial ownership not assessed" in rows[1][4]
    assert rows[1][5] == "Not assessed"
    assert rows[1][6] == "NotAssessed"


def test_csv_export_pep_column_reflects_true_false_and_unassessed(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    pep_client = _client("PEP Client")
    clear_client = _client("Clear Client")
    unassessed_client = _client("Unassessed Client")
    compliance = [
        _compliance(pep_client["id"], is_pep=True),
        _compliance(clear_client["id"], is_pep=False),
    ]
    monkeypatch.setattr(
        m, "_db_pool",
        FakePool(clients=[pep_client, clear_client, unassessed_client], compliance=compliance),
    )
    _as_current_user(monkeypatch, m, partner)

    response = asyncio.run(client_compliance_status_report_export(_fake_request()))
    rows = {r[1]: r for r in _csv_rows(response)[1:]}

    assert rows["PEP Client"][5] == "Yes"
    assert rows["Clear Client"][5] == "No"
    assert rows["Unassessed Client"][5] == "Not assessed"
