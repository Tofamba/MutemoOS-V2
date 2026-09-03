"""
Unit tests for the Matter AML Status report (backend/main.py, 2026-09-04):
  - GET /api/reports/matter-aml-status — JSON, one row per real matter,
    firm-wide (not one row per client).
  - GET /api/reports/matter-aml-status-export — CSV download.
  - GET /api/reports/matter-aml-status-export-pdf — PDF download.

This is the sibling of the AML/Client Compliance Register at matter
granularity: aml_scope/matter_risk are per-MATTER fields (Part B of the
Individual Client AML/CDD Report), so the same client can have matters
with genuinely different AML classification -- the client-level
Register has no way to show that. Same permission tier as the Register/
Exceptions reports (reports:client_compliance_status), not client:read
-- this is a firm-wide roster.

Same FakeConnection/FakePool/_as_current_user/_fake_request convention
as tests/test_client_compliance_status_report.py, which this file
mirrors closely.
"""

import asyncio
import csv
import io
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from backend.main import (
    FIRM_ID,
    matter_aml_status_report,
    matter_aml_status_report_export,
    matter_aml_status_report_export_pdf,
)


class FakeConnection:
    def __init__(self, matters=None):
        self.matters = matters if matters is not None else []

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT * FROM matters WHERE firm_id=$1 AND NOT is_sentinel"):
            firm_id, = args
            rows = [m for m in self.matters if m["firm_id"] == firm_id and not m.get("is_sentinel", False)]
            rows.sort(key=lambda m: (m.get("client_name") is None, m.get("client_name") or "", m["created_at"]))
            return rows
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


def _matter(client_id=None, client_name=None, **overrides):
    row = {
        "id": uuid.uuid4(), "firm_id": FIRM_ID, "client_id": client_id, "client_name": client_name,
        "name": "Acquisition of commercial property", "number": None, "matter_number": None,
        "status": "Active", "aml_scope": "NotAssessed", "aml_scope_reason": None, "matter_risk": "NotAssessed",
        "is_sentinel": False, "created_at": datetime.now(timezone.utc),
    }
    row.update(overrides)
    return row


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def _fake_request():
    return None


def _csv_rows(response):
    text = response.body.decode("utf-8-sig") if isinstance(response.body, bytes) else response.body.lstrip("﻿")
    return list(csv.reader(io.StringIO(text)))


def _partner():
    return {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}


# ── permission gate ──────────────────────────────────────────────────────

def test_associate_gets_403(monkeypatch):
    import backend.main as m
    associate = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "associate", "display_name": "Assoc"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, associate)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(matter_aml_status_report(_fake_request()))
    assert exc_info.value.status_code == 403


def test_export_associate_gets_403(monkeypatch):
    import backend.main as m
    associate = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "associate", "display_name": "Assoc"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, associate)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(matter_aml_status_report_export(_fake_request()))
    assert exc_info.value.status_code == 403


def test_pdf_export_associate_gets_403(monkeypatch):
    import backend.main as m
    associate = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "associate", "display_name": "Assoc"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, associate)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(matter_aml_status_report_export_pdf(_fake_request()))
    assert exc_info.value.status_code == 403


def test_partner_and_admin_both_succeed(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[_matter(client_name="Anchorflow Holdings")]))

    for role in ("partner", "admin"):
        _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": role, "display_name": "X"})
        rows = asyncio.run(matter_aml_status_report(_fake_request()))
        assert len(rows) == 1


# ── row shape / one row per matter, not per client ────────────────────────

def test_two_matters_for_the_same_client_produce_two_distinct_rows(monkeypatch):
    """The whole point of this report: Anchorflow's two matters with
    different AML scope/risk both appear, distinctly -- the client-level
    Register can only ever show one aml_scope value per client."""
    import backend.main as m
    client_id = uuid.uuid4()
    property_matter = _matter(
        client_id=client_id, client_name="Anchorflow Holdings", name="Acquisition of commercial property",
        matter_number="DU-002-01", aml_scope="InScope", matter_risk="High",
        aml_scope_reason="Transaction involves acquisition of immovable property.",
    )
    divorce_matter = _matter(
        client_id=client_id, client_name="Anchorflow Holdings", name="Divorce proceedings",
        matter_number="DU-002-02", aml_scope="OutOfScope", matter_risk="Low",
    )
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[property_matter, divorce_matter]))
    _as_current_user(monkeypatch, m, _partner())

    rows = asyncio.run(matter_aml_status_report(_fake_request()))

    assert len(rows) == 2
    by_number = {r["matter_number"]: r for r in rows}
    assert by_number["DU-002-01"]["aml_scope"] == "InScope"
    assert by_number["DU-002-01"]["matter_risk"] == "High"
    assert by_number["DU-002-01"]["aml_scope_reason"] == "Transaction involves acquisition of immovable property."
    assert by_number["DU-002-02"]["aml_scope"] == "OutOfScope"
    assert by_number["DU-002-02"]["matter_risk"] == "Low"
    assert all(r["client_name"] == "Anchorflow Holdings" for r in rows)


def test_sentinel_matter_excluded(monkeypatch):
    import backend.main as m
    real = _matter(client_name="Real Client")
    sentinel = _matter(client_name="Real Client", is_sentinel=True)
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[real, sentinel]))
    _as_current_user(monkeypatch, m, _partner())

    rows = asyncio.run(matter_aml_status_report(_fake_request()))

    assert len(rows) == 1
    assert rows[0]["matter_id"] == str(real["id"])


def test_defaults_to_not_assessed_when_never_set(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[_matter(client_name="Client A")]))
    _as_current_user(monkeypatch, m, _partner())

    rows = asyncio.run(matter_aml_status_report(_fake_request()))

    assert rows[0]["aml_scope"] == "NotAssessed"
    assert rows[0]["matter_risk"] == "NotAssessed"
    assert rows[0]["aml_scope_reason"] == ""


def test_unlinked_matter_shows_unlinked_client(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[_matter(client_id=None, client_name=None)]))
    _as_current_user(monkeypatch, m, _partner())

    rows = asyncio.run(matter_aml_status_report(_fake_request()))

    assert rows[0]["client_id"] is None
    assert rows[0]["client_name"] == "Unlinked"


def test_matter_number_falls_back_to_legacy_number_then_unnumbered(monkeypatch):
    import backend.main as m
    with_matter_number = _matter(client_name="A", matter_number="AA-001-01", number="legacy-1")
    with_legacy_only = _matter(client_name="B", matter_number=None, number="legacy-2")
    with_neither = _matter(client_name="C", matter_number=None, number=None)
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[with_matter_number, with_legacy_only, with_neither]))
    _as_current_user(monkeypatch, m, _partner())

    rows = asyncio.run(matter_aml_status_report(_fake_request()))

    by_client = {r["client_name"]: r for r in rows}
    assert by_client["A"]["matter_number"] == "AA-001-01"
    assert by_client["B"]["matter_number"] == "legacy-2"
    assert by_client["C"]["matter_number"] == "(unnumbered)"


def test_no_matters_returns_empty_list(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, _partner())

    assert asyncio.run(matter_aml_status_report(_fake_request())) == []


# ── CSV export ────────────────────────────────────────────────────────────

def test_csv_export_columns_and_content(monkeypatch):
    import backend.main as m
    matter = _matter(
        client_name="Anchorflow Holdings", name="Acquisition of commercial property",
        matter_number="DU-002-01", aml_scope="InScope", matter_risk="High",
        aml_scope_reason="Transaction involves acquisition of immovable property.", status="Active",
    )
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter]))
    _as_current_user(monkeypatch, m, _partner())

    response = asyncio.run(matter_aml_status_report_export(_fake_request()))
    rows = _csv_rows(response)

    assert rows[0] == ["Client", "Matter", "AML Scope", "Matter Risk", "Reason", "Matter Status"]
    assert rows[1][0] == "Anchorflow Holdings"
    assert rows[1][1] == "DU-002-01 — Acquisition of commercial property"
    assert rows[1][2] == "In Scope"
    assert rows[1][3] == "High"
    assert rows[1][4] == "Transaction involves acquisition of immovable property."
    assert rows[1][5] == "Active"


# ── PDF export ────────────────────────────────────────────────────────────

def test_pdf_export_produces_a_real_pdf_with_data(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[_matter(client_name="Anchorflow Holdings")]))
    _as_current_user(monkeypatch, m, _partner())

    response = asyncio.run(matter_aml_status_report_export_pdf(_fake_request()))

    assert response.media_type == "application/pdf"
    assert "matter_aml_status" in response.headers["content-disposition"]
    assert response.body.startswith(b"%PDF")


def test_pdf_export_handles_no_matters_without_crashing(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, _partner())

    response = asyncio.run(matter_aml_status_report_export_pdf(_fake_request()))

    assert response.media_type == "application/pdf"
    assert response.body.startswith(b"%PDF")
