"""
Unit tests for "Workload & Capacity Snapshot" (backend/main.py,
2026-09-18) -- the fifth of five self-scoped practice reports, same
access tier and scoping contract as My Portfolio.

Reuses _compute_my_portfolio()/_resolve_my_portfolio() completely
unchanged -- Volume/Status + Practice Area (same slice "My Active
Matters Summary" uses) plus Review Status counts (overdue/due soon/
never reviewed). Section-composition correctness is already thoroughly
tested in tests/test_my_portfolio.py; these tests verify the wiring for
the new report-specific endpoints.

Same FakeConnection/FakePool convention as
tests/test_my_active_matters_summary_report.py.
"""

import asyncio
import csv
import io
import uuid

import pdfplumber
import pytest

from backend.main import (
    FIRM_ID,
    workload_capacity_report,
    workload_capacity_report_export,
    workload_capacity_report_export_pdf,
)


class FakeConnection:
    def __init__(self, clients=None, matters=None, compliance=None, owners=None, fee_matters=None):
        self.clients = clients if clients is not None else []
        self.matters = matters if matters is not None else []
        self.compliance = compliance if compliance is not None else []
        self.owners = owners if owners is not None else []
        self.fee_matters = fee_matters if fee_matters is not None else []

    async def fetchval(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT COUNT(*) FROM clients"):
            firm_id, created_by = args
            return len([c for c in self.clients if c["firm_id"] == firm_id and c.get("created_by") == created_by])
        raise NotImplementedError(f"FakeConnection.fetchval: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT status, COUNT(*) AS matter_count FROM matters"):
            firm_id, created_by = args
            mine = [m for m in self.matters if m["firm_id"] == firm_id and m.get("created_by") == created_by and not m.get("is_sentinel")]
            tally = {}
            for m in mine:
                tally[m["status"]] = tally.get(m["status"], 0) + 1
            return [{"status": s, "matter_count": n} for s, n in tally.items()]

        if q.startswith("SELECT practice_area, COUNT(*) AS matter_count FROM matters"):
            firm_id, created_by = args
            mine = [m for m in self.matters if m["firm_id"] == firm_id and m.get("created_by") == created_by and not m.get("is_sentinel")]
            tally = {}
            for m in mine:
                tally[m.get("practice_area")] = tally.get(m.get("practice_area"), 0) + 1
            rows = [{"practice_area": pa, "matter_count": n} for pa, n in tally.items()]
            rows.sort(key=lambda r: r["matter_count"], reverse=True)
            return rows

        if q.startswith("SELECT * FROM clients WHERE firm_id=$1 AND created_by=$2"):
            firm_id, created_by = args
            return [c for c in self.clients if c["firm_id"] == firm_id and c.get("created_by") == created_by]

        if q.startswith("SELECT * FROM client_compliance WHERE client_id = ANY($1)"):
            client_ids, firm_id = args
            return [c for c in self.compliance if c["client_id"] in client_ids and c["firm_id"] == firm_id]

        if q.startswith("SELECT client_id, verification_status FROM beneficial_owners"):
            client_ids, firm_id = args
            return [o for o in self.owners if o["client_id"] in client_ids and o["firm_id"] == firm_id]

        if q.startswith("SELECT client_id, client_name, amount_billed, amount_received FROM matters"):
            firm_id, created_by = args
            return [
                m for m in self.fee_matters
                if m["firm_id"] == firm_id and m.get("created_by") == created_by
                and (m.get("amount_billed") is not None or m.get("amount_received") is not None)
            ]

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


def _matter(name, *, created_by, firm_id=FIRM_ID, status="Active", practice_area=None, is_sentinel=False):
    return {"id": uuid.uuid4(), "firm_id": firm_id, "name": name, "status": status,
            "practice_area": practice_area, "created_by": created_by, "is_sentinel": is_sentinel}


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def _review_mock(monkeypatch, m, rows):
    async def fake_review(conn, *, lawyer_id, client_id, status):
        return rows
    monkeypatch.setattr(m, "_fetch_matter_review_status_rows", fake_review)


def _fake_request():
    return None


def _lawyer(user_id=None, role="associate"):
    return {"id": user_id or uuid.uuid4(), "firm_id": FIRM_ID, "role": role, "display_name": "L. Test"}


def _csv_rows(response):
    text = response.body.decode("utf-8-sig") if isinstance(response.body, bytes) else response.body.lstrip("﻿")
    return list(csv.reader(io.StringIO(text)))


def _pdf_text(response):
    with pdfplumber.open(io.BytesIO(response.body)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


# ── self-scoping ─────────────────────────────────────────────────────────

def test_only_own_matters_counted(monkeypatch):
    import backend.main as m
    me = _lawyer()
    other = uuid.uuid4()
    matters = [
        _matter("Mine 1", created_by=me["id"]),
        _matter("Not mine", created_by=other),
    ]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _review_mock(monkeypatch, m, [])
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(workload_capacity_report(_fake_request()))

    assert result["matter_count"] == 1


# ── report-specific calculation reuse: review_status counts ────────────────

def test_review_status_counts_reused_from_matter_review_status_rows(monkeypatch):
    """No second review-status calculation -- this report's overdue/due
    soon/never reviewed counts come from whatever
    _fetch_matter_review_status_rows() (already tested on its own)
    returns, same as My Portfolio's own tallying."""
    import backend.main as m
    me = _lawyer()
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[_matter("M1", created_by=me["id"])]))
    review_rows = [
        {"next_review_date": "2020-01-01"},  # overdue (always in the past)
        {"next_review_date": None},          # never reviewed
    ]
    _review_mock(monkeypatch, m, review_rows)
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(workload_capacity_report(_fake_request()))

    assert result["review_status"]["overdue_count"] == 1
    assert result["review_status"]["never_reviewed_count"] == 1
    assert result["review_status"]["due_soon_count"] == 0


# ── cross-user isolation ──────────────────────────────────────────────────

def test_cross_user_isolation(monkeypatch):
    import backend.main as m
    lawyer_a = uuid.uuid4()
    lawyer_b = _lawyer()
    matters = [
        _matter("A's matter", created_by=lawyer_a, practice_area="Litigation"),
        _matter("B's matter", created_by=lawyer_b["id"], practice_area="Family Law"),
    ]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _review_mock(monkeypatch, m, [])
    _as_current_user(monkeypatch, m, lawyer_b)

    result = asyncio.run(workload_capacity_report(_fake_request()))

    assert result["matter_count"] == 1
    assert [pa["practice_area"] for pa in result["practice_areas"]] == ["Family Law"]


def test_no_lawyer_id_parameter_exists(monkeypatch):
    import inspect
    sig = inspect.signature(workload_capacity_report)
    assert list(sig.parameters.keys()) == ["request"]


# ── firm/tenant isolation ────────────────────────────────────────────────

def test_firm_isolation_never_sees_another_firms_matters(monkeypatch):
    import backend.main as m
    shared_lawyer_id = uuid.uuid4()
    firm_b = "b2c3d4e5-0000-0000-0000-000000000002"
    matters = [
        _matter("Firm A matter", created_by=shared_lawyer_id, firm_id=FIRM_ID),
        _matter("Firm B matter", created_by=shared_lawyer_id, firm_id=firm_b),
    ]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _review_mock(monkeypatch, m, [])
    _as_current_user(monkeypatch, m, {"id": shared_lawyer_id, "firm_id": FIRM_ID, "role": "partner", "display_name": "P"})

    result = asyncio.run(workload_capacity_report(_fake_request()))

    assert result["matter_count"] == 1


# ── empty state ──────────────────────────────────────────────────────────

def test_no_matters_returns_zero_counts_not_an_error(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _review_mock(monkeypatch, m, [])
    _as_current_user(monkeypatch, m, _lawyer())

    result = asyncio.run(workload_capacity_report(_fake_request()))

    assert result["matter_count"] == 0
    assert result["practice_areas"] == []
    assert result["review_status"] == {"overdue_count": 0, "due_soon_count": 0, "never_reviewed_count": 0}


def test_no_real_identity_returns_empty_not_an_error(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, {"id": None, "firm_id": FIRM_ID, "role": "partner", "display_name": "Demo User"})

    result = asyncio.run(workload_capacity_report(_fake_request()))

    assert result["matter_count"] == 0


# ── CSV generation ───────────────────────────────────────────────────────

def test_csv_export_contains_correct_data(monkeypatch):
    import backend.main as m
    me = _lawyer()
    matters = [_matter("M1", created_by=me["id"], practice_area="Litigation")]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _review_mock(monkeypatch, m, [{"next_review_date": "2020-01-01"}])
    _as_current_user(monkeypatch, m, me)

    response = asyncio.run(workload_capacity_report_export(_fake_request()))
    rows = _csv_rows(response)
    flat = [",".join(r) for r in rows]

    assert any("Litigation,1" in line for line in flat)
    assert any("Overdue,1" in line for line in flat)


def test_csv_export_starts_with_utf8_bom(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _review_mock(monkeypatch, m, [])
    _as_current_user(monkeypatch, m, _lawyer())

    response = asyncio.run(workload_capacity_report_export(_fake_request()))
    assert response.body.startswith("﻿".encode("utf-8"))


# ── PDF generation ───────────────────────────────────────────────────────

def test_pdf_export_generates_without_error_and_contains_correct_data(monkeypatch):
    import backend.main as m
    me = _lawyer()
    matters = [_matter("M1", created_by=me["id"], practice_area="Family Law")]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _review_mock(monkeypatch, m, [{"next_review_date": None}])
    _as_current_user(monkeypatch, m, me)

    response = asyncio.run(workload_capacity_report_export_pdf(_fake_request()))
    assert response.media_type == "application/pdf"
    assert response.body.startswith(b"%PDF")
    text = _pdf_text(response)
    assert "Family Law" in text
    assert "Never Reviewed" in text


def test_pdf_export_handles_empty_snapshot_without_crashing(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _review_mock(monkeypatch, m, [])
    _as_current_user(monkeypatch, m, _lawyer())

    response = asyncio.run(workload_capacity_report_export_pdf(_fake_request()))
    assert response.body.startswith(b"%PDF")
    assert "No matters on file" in _pdf_text(response)
