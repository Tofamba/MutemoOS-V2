"""
Unit tests for "Matter Status & Progress Report" (backend/main.py,
2026-09-18) -- the second of five self-scoped practice reports, same
access tier and scoping contract as My Portfolio.

Merges two already-existing per-matter views by matter id, no new health/
stage/activity logic:
  - matter_health + last real activity: _fetch_matter_review_status_rows(),
    the exact function Matter Review Status/My Portfolio already call
    (already thoroughly tested in tests/test_matter_review_status_report.py
    and tests/test_matter_health.py) -- these tests verify the merge
    wiring, not that function's own internal correctness.
  - current stage: _row_to_matter()'s own Matter Progress Tracker
    computation (backend/matter_stages.py), reused via a raw
    `SELECT * FROM matters` fetch.

Same FakeConnection/FakePool convention as
tests/test_matter_review_status_report.py, extended with the raw matters
fetch _row_to_matter() needs for stage_info.
"""

import asyncio
import csv
import io
import uuid
from datetime import date, datetime, timedelta, timezone

import pdfplumber
import pytest

from backend.main import (
    FIRM_ID,
    matter_status_progress_report,
    matter_status_progress_report_export,
    matter_status_progress_report_export_pdf,
)


class FakeConnection:
    def __init__(self, matters=None, notes=None, documents=None, clients=None, users=None):
        self.matters = matters if matters is not None else []
        self.notes = notes if notes is not None else []
        self.documents = documents if documents is not None else []
        self.clients = clients if clients is not None else []
        self.users = users if users is not None else []

    async def fetch(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT m.id, m.name, m.number, m.matter_number"):
            firm_id = args[0]
            filters = list(args[1:])
            rows = [m for m in self.matters if m["firm_id"] == firm_id and not m.get("is_sentinel")]
            if "m.created_by=$" in q:
                lawyer_id = filters.pop(0)
                rows = [m for m in rows if m.get("created_by") == lawyer_id]
            if "m.client_id=$" in q:
                client_id = filters.pop(0)
                rows = [m for m in rows if m.get("client_id") == client_id]
            if "m.status=$" in q:
                status = filters.pop(0)
                rows = [m for m in rows if m.get("status") == status]

            clients_by_id = {c["id"]: c for c in self.clients}
            users_by_id = {u["id"]: u for u in self.users}
            out = []
            for m in rows:
                client = clients_by_id.get(m.get("client_id"))
                creator = users_by_id.get(m.get("created_by"))
                out.append({
                    "id": m["id"], "name": m["name"], "number": m.get("number"),
                    "matter_number": m.get("matter_number"), "client_id": m.get("client_id"),
                    "client_name": m.get("client_name"), "status": m.get("status"),
                    "next_review_date": m.get("next_review_date"),
                    "last_reviewed_date": m.get("last_reviewed_date"),
                    "next_deadline": m.get("next_deadline"), "next_deadline_note": m.get("next_deadline_note"),
                    "aml_scope": m.get("aml_scope"), "matter_risk": m.get("matter_risk"),
                    "aml_scope_reason": m.get("aml_scope_reason"),
                    "last_activity": m.get("last_activity"), "created_at": m.get("created_at"),
                    "created_by": m.get("created_by"),
                    "client_full_name": client["full_name"] if client else None,
                    "created_by_name": creator["display_name"] if creator else None,
                })
            out.sort(key=lambda r: (r["next_review_date"] is not None, r["next_review_date"] or date.min, r["name"]))
            return out

        if q.startswith("SELECT matter_id, text, created_at FROM progress_notes"):
            matter_ids, = args
            rows = [n for n in self.notes if n["matter_id"] in matter_ids]
            rows.sort(key=lambda n: (n["matter_id"], n["created_at"]), reverse=True)
            return rows

        if q.startswith("SELECT matter_id, filename, uploaded_at FROM documents"):
            matter_ids, = args
            rows = [d for d in self.documents if d["matter_id"] in matter_ids and d.get("status", "complete") == "complete"]
            rows.sort(key=lambda d: (d["matter_id"], d["uploaded_at"]), reverse=True)
            return rows

        if q.startswith("SELECT * FROM matters WHERE firm_id=$1 AND created_by=$2 AND NOT is_sentinel"):
            firm_id, created_by = args
            return [dict(m) for m in self.matters
                    if m["firm_id"] == firm_id and m.get("created_by") == created_by and not m.get("is_sentinel")]

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


def _matter(name, *, status="Active", created_by=None, firm_id=FIRM_ID, matter_number=None,
            is_sentinel=False, next_deadline=None, next_review_date=None, last_reviewed_date=None,
            last_activity=None, created_at=None, client_id=None, client_name=None,
            matter_type=None, practice_area=None, stage=None, stage_updated_at=None,
            aml_scope=None, matter_risk=None, aml_scope_reason=None):
    return {
        "id": uuid.uuid4(), "firm_id": firm_id, "name": name, "number": None,
        "matter_number": matter_number, "status": status, "created_by": created_by,
        "is_sentinel": is_sentinel, "next_deadline": next_deadline, "next_deadline_note": None,
        "next_review_date": next_review_date, "last_reviewed_date": last_reviewed_date,
        "last_activity": last_activity, "created_at": created_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
        "client_id": client_id, "client_name": client_name,
        "aml_scope": aml_scope, "matter_risk": matter_risk, "aml_scope_reason": aml_scope_reason,
        # Full-row fields _row_to_matter() reads for stage_info/fee_balance --
        # every other field it touches degrades safely via .get() when absent.
        "matter_type": matter_type, "practice_area": practice_area,
        "stage": stage, "conveyancing_milestone": None, "stage_updated_at": stage_updated_at,
        "amount_billed": None, "amount_received": None, "conveyancing_purchase_price": None,
        "conveyancing_transfer_date": None, "conveyancing_rates_clearance_expiry": None,
        "conveyancing_bond_registration_deadline": None,
    }


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


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

def test_only_own_matters_returned(monkeypatch):
    import backend.main as m
    me = _lawyer()
    other = uuid.uuid4()
    matters = [_matter("Mine", created_by=me["id"]), _matter("Not mine", created_by=other)]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(matter_status_progress_report(_fake_request()))

    assert len(result) == 1
    assert result[0]["matter_name"] == "Mine"


# ── report-specific calculation reuse: matter_health ───────────────────────

def test_matter_health_matches_compute_matter_health_directly(monkeypatch):
    """No second health calculation -- confirms the row's matter_health
    is exactly what compute_matter_health() itself would return for the
    same matter (a Closed matter is always Grey)."""
    import backend.main as m
    from backend.matter_health import compute_matter_health
    me = _lawyer()
    matters = [_matter("Closed matter", created_by=me["id"], status="Closed")]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(matter_status_progress_report(_fake_request()))

    expected = compute_matter_health({"status": "Closed", "next_deadline": None, "next_review_date": None,
                                       "last_activity": None, "created_at": matters[0]["created_at"],
                                       "aml_scope": None, "matter_risk": None, "aml_scope_reason": None})
    assert result[0]["matter_health"]["status"] == expected["status"] == "grey"


# ── report-specific calculation reuse: current stage ───────────────────────

def test_current_stage_reflects_matter_progress_tracker(monkeypatch):
    """No second stage calculation -- reuses backend.matter_stages via
    _row_to_matter()'s own stage_info computation."""
    import backend.main as m
    me = _lawyer()
    matters = [_matter(
        "Debt matter", created_by=me["id"], matter_type="debt_collection",
        stage="Summons Issued", stage_updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(matter_status_progress_report(_fake_request()))

    assert result[0]["stage_info"]["current_stage"] == "Summons Issued"
    assert result[0]["stage_info"]["sequence"][0] == "Letter of Demand Sent"


def test_matter_type_with_no_defined_sequence_has_no_stage_info(monkeypatch):
    import backend.main as m
    me = _lawyer()
    matters = [_matter("Generic matter", created_by=me["id"], matter_type="something_undefined")]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(matter_status_progress_report(_fake_request()))

    assert result[0]["stage_info"] is None


# ── last activity reuse ──────────────────────────────────────────────────

def test_last_activity_prefers_most_recent_note(monkeypatch):
    import backend.main as m
    me = _lawyer()
    matter = _matter("M1", created_by=me["id"])
    notes = [{"matter_id": matter["id"], "text": "Filed with the court", "created_at": datetime(2026, 9, 10, tzinfo=timezone.utc)}]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter], notes=notes))
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(matter_status_progress_report(_fake_request()))

    assert result[0]["last_activity_kind"] == "note"
    assert "Filed with the court" in result[0]["last_activity_text"]


# ── cross-user isolation ──────────────────────────────────────────────────

def test_cross_user_isolation(monkeypatch):
    import backend.main as m
    lawyer_a = uuid.uuid4()
    lawyer_b = _lawyer()
    matters = [_matter("A's matter", created_by=lawyer_a), _matter("B's matter", created_by=lawyer_b["id"])]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _as_current_user(monkeypatch, m, lawyer_b)

    result = asyncio.run(matter_status_progress_report(_fake_request()))

    assert [r["matter_name"] for r in result] == ["B's matter"]


def test_no_lawyer_id_parameter_exists(monkeypatch):
    import inspect
    sig = inspect.signature(matter_status_progress_report)
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
    _as_current_user(monkeypatch, m, {"id": shared_lawyer_id, "firm_id": FIRM_ID, "role": "partner", "display_name": "P"})

    result = asyncio.run(matter_status_progress_report(_fake_request()))

    assert [r["matter_name"] for r in result] == ["Firm A matter"]


# ── empty state ──────────────────────────────────────────────────────────

def test_no_matters_returns_empty_list_not_an_error(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, _lawyer())

    result = asyncio.run(matter_status_progress_report(_fake_request()))
    assert result == []


def test_no_real_identity_returns_empty_not_an_error(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, {"id": None, "firm_id": FIRM_ID, "role": "partner", "display_name": "Demo User"})

    result = asyncio.run(matter_status_progress_report(_fake_request()))
    assert result == []


# ── CSV generation ───────────────────────────────────────────────────────

def test_csv_export_contains_correct_data(monkeypatch):
    import backend.main as m
    me = _lawyer()
    matters = [_matter("M1", created_by=me["id"], matter_number="TC-001", status="Active",
                        matter_type="debt_collection", stage="Judgment Obtained")]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _as_current_user(monkeypatch, m, me)

    response = asyncio.run(matter_status_progress_report_export(_fake_request()))
    rows = _csv_rows(response)

    assert rows[0] == ["Matter", "Status", "Health", "Health Reasons", "Current Stage", "Last Activity"]
    assert rows[1][0] == "TC-001 — M1"
    assert rows[1][4] == "Judgment Obtained"


def test_csv_export_starts_with_utf8_bom(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, _lawyer())

    response = asyncio.run(matter_status_progress_report_export(_fake_request()))
    assert response.body.startswith("﻿".encode("utf-8"))


# ── PDF generation ───────────────────────────────────────────────────────

def test_pdf_export_generates_without_error_and_contains_correct_data(monkeypatch):
    import backend.main as m
    me = _lawyer()
    matters = [_matter("Progress matter", created_by=me["id"], matter_type="litigation_general", stage="Set Down")]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _as_current_user(monkeypatch, m, me)

    response = asyncio.run(matter_status_progress_report_export_pdf(_fake_request()))
    assert response.media_type == "application/pdf"
    assert response.body.startswith(b"%PDF")
    text = _pdf_text(response)
    assert "Progress matter" in text
    assert "Set Down" in text


def test_pdf_export_handles_no_matters_without_crashing(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, _lawyer())

    response = asyncio.run(matter_status_progress_report_export_pdf(_fake_request()))
    assert response.body.startswith(b"%PDF")
    assert "No matters on file" in _pdf_text(response)
