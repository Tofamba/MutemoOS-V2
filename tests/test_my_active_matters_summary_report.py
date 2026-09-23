"""
Unit tests for "My Active Matters Summary" (backend/main.py, 2026-09-18) --
the first of five self-scoped practice reports, same access tier and
scoping contract as My Portfolio (matter:read, no lawyer_id parameter,
always derived server-side from the session).

Reuses _compute_my_portfolio()/_resolve_my_portfolio() completely
unchanged -- this report is just that computation's Volume/Status +
Practice Area sections, formalized into its own downloadable CSV/PDF.
Section-composition correctness (status tallying, practice-area grouping)
is already thoroughly tested in tests/test_my_portfolio.py; these tests
verify the wiring (self-scoping, isolation, export, empty state) for the
new report-specific endpoints, not a second copy of that function's own
internal correctness.

Same FakeConnection/FakePool convention as tests/test_my_portfolio.py.
"""

import asyncio
import csv
import io
import uuid

import pdfplumber
import pytest
from fastapi import HTTPException

from backend.main import (
    FIRM_ID,
    my_active_matters_summary_report,
    my_active_matters_summary_export,
    my_active_matters_summary_export_pdf,
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

        if q.startswith("SELECT aq.id, aq.client_id, aq.action_type, aq.escalation_level"):
            return []

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


def _empty_review_mock(monkeypatch, m):
    async def fake_review(conn, *, lawyer_id, client_id, status):
        return []
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
        _matter("Mine 1", created_by=me["id"], status="Active"),
        _matter("Mine 2", created_by=me["id"], status="Closed"),
        _matter("Not mine", created_by=other, status="Active"),
    ]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _empty_review_mock(monkeypatch, m)
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(my_active_matters_summary_report(_fake_request()))

    assert result["matter_count"] == 2
    assert result["matters_by_status"]["Active"] == 1
    assert result["matters_by_status"]["Closed"] == 1


# ── cross-user isolation ──────────────────────────────────────────────────

def test_cross_user_isolation_lawyer_b_never_sees_lawyer_as_matters(monkeypatch):
    import backend.main as m
    lawyer_a = uuid.uuid4()
    lawyer_b = _lawyer()
    matters = [
        _matter("A's matter", created_by=lawyer_a, practice_area="Litigation"),
        _matter("B's matter", created_by=lawyer_b["id"], practice_area="Conveyancing/Property"),
    ]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _empty_review_mock(monkeypatch, m)
    _as_current_user(monkeypatch, m, lawyer_b)

    result = asyncio.run(my_active_matters_summary_report(_fake_request()))

    assert result["matter_count"] == 1
    assert [pa["practice_area"] for pa in result["practice_areas"]] == ["Conveyancing/Property"]


def test_no_lawyer_id_parameter_exists_to_request_another_lawyers_report(monkeypatch):
    """The report function takes only a request -- there is no id anywhere
    in its signature a caller could supply to see someone else's data."""
    import inspect
    sig = inspect.signature(my_active_matters_summary_report)
    assert list(sig.parameters.keys()) == ["request"]


# ── firm/tenant isolation ────────────────────────────────────────────────

def test_firm_isolation_never_sees_another_firms_matters(monkeypatch):
    """Same lawyer id, different firm -- proves scoping isn't purely by
    created_by; firm_id must also match. Mirrors tests/test_two_firm_
    isolation.py's own monkeypatch-FIRM_ID technique."""
    import backend.main as m
    shared_lawyer_id = uuid.uuid4()
    firm_b = "b2c3d4e5-0000-0000-0000-000000000002"
    matters = [
        _matter("Firm A matter", created_by=shared_lawyer_id, firm_id=FIRM_ID),
        _matter("Firm B matter", created_by=shared_lawyer_id, firm_id=firm_b),
    ]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _empty_review_mock(monkeypatch, m)
    _as_current_user(monkeypatch, m, {"id": shared_lawyer_id, "firm_id": FIRM_ID, "role": "partner", "display_name": "P"})
    # Active firm is FIRM_ID (unchanged) -- only Firm A's matter should count.

    result = asyncio.run(my_active_matters_summary_report(_fake_request()))

    assert result["matter_count"] == 1


# ── empty state ──────────────────────────────────────────────────────────

def test_no_matters_returns_zero_counts_not_an_error(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _empty_review_mock(monkeypatch, m)
    _as_current_user(monkeypatch, m, _lawyer())

    result = asyncio.run(my_active_matters_summary_report(_fake_request()))

    assert result["matter_count"] == 0
    assert result["practice_areas"] == []
    assert all(c == 0 for c in result["matters_by_status"].values())


def test_no_real_identity_returns_empty_not_an_error(monkeypatch):
    """AUTH_ENABLED=False's synthetic dev user (id=None) -- honest empty
    result, not firm-wide data under a self-scoped label."""
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, {"id": None, "firm_id": FIRM_ID, "role": "partner", "display_name": "Demo User"})

    result = asyncio.run(my_active_matters_summary_report(_fake_request()))

    assert result["matter_count"] == 0
    assert result["practice_areas"] == []


# ── CSV generation ───────────────────────────────────────────────────────

def test_csv_export_contains_correct_data(monkeypatch):
    import backend.main as m
    me = _lawyer()
    matters = [
        _matter("Matter One", created_by=me["id"], status="Active", practice_area="Litigation"),
        _matter("Matter Two", created_by=me["id"], status="Active", practice_area="Litigation"),
        _matter("Matter Three", created_by=me["id"], status="Closed", practice_area="Conveyancing/Property"),
    ]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _empty_review_mock(monkeypatch, m)
    _as_current_user(monkeypatch, m, me)

    response = asyncio.run(my_active_matters_summary_export(_fake_request()))
    rows = _csv_rows(response)
    flat = [",".join(r) for r in rows]

    assert any("Litigation,2" in line for line in flat)
    assert any("Conveyancing/Property,1" in line for line in flat)
    assert any("Active,2" in line for line in flat)
    assert any("Closed,1" in line for line in flat)


def test_csv_export_starts_with_utf8_bom(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _empty_review_mock(monkeypatch, m)
    _as_current_user(monkeypatch, m, _lawyer())

    response = asyncio.run(my_active_matters_summary_export(_fake_request()))
    assert response.body.startswith("﻿".encode("utf-8"))


# ── PDF generation ───────────────────────────────────────────────────────

def test_pdf_export_generates_without_error_and_contains_correct_data(monkeypatch):
    import backend.main as m
    me = _lawyer()
    matters = [_matter("Matter One", created_by=me["id"], status="Active", practice_area="Family Law")]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _empty_review_mock(monkeypatch, m)
    _as_current_user(monkeypatch, m, me)

    response = asyncio.run(my_active_matters_summary_export_pdf(_fake_request()))
    assert response.media_type == "application/pdf"
    assert response.body.startswith(b"%PDF")
    text = _pdf_text(response)
    assert "Family Law" in text
    assert "Active" in text


def test_pdf_export_handles_empty_summary_without_crashing(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _empty_review_mock(monkeypatch, m)
    _as_current_user(monkeypatch, m, _lawyer())

    response = asyncio.run(my_active_matters_summary_export_pdf(_fake_request()))
    assert response.body.startswith(b"%PDF")
    assert "No matters on file" in _pdf_text(response)


def test_pdf_export_scoped_strictly_to_calling_lawyers_own_data(monkeypatch):
    import backend.main as m
    me = _lawyer()
    other = uuid.uuid4()
    matters = [
        _matter("Mine", created_by=me["id"], practice_area="Litigation"),
        _matter("Not mine", created_by=other, practice_area="Conveyancing/Property"),
    ]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _empty_review_mock(monkeypatch, m)
    _as_current_user(monkeypatch, m, me)

    response = asyncio.run(my_active_matters_summary_export_pdf(_fake_request()))
    text = _pdf_text(response)
    assert "Litigation" in text
    assert "Conveyancing/Property" not in text
