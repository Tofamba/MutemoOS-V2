"""
Unit tests for the RBZ compliance export feature (backend/main.py):
  - GET /api/reports/rbz-compliance-export — CSV of all clients+matters,
    firm-wide, restricted to partner-tier roles (admin/partner).
  - GET /api/reports/rbz-compliance-export-pdf — the same data/restriction,
    a second output format rather than a separate feature. Verified via
    pdfplumber (already a dependency elsewhere in this codebase for OCR/
    extraction) rather than adding a PDF-parsing test-only dependency.
  - Every generation is logged to report_history (who, when, counts) —
    a genuine audit trail, not just a one-off produced on demand.
  - GET /api/reports/history — the log itself.

Called directly as plain async functions, same convention as this repo's
other backend tests (see tests/test_docx_export.py's docstring for why).
AUTH_ENABLED is False by default, so get_current_user() normally returns a
synthetic partner user with id=None — that's enough for the "partner
succeeds" case, but the "associate gets 403" case needs a real fake user
with role=associate, so get_current_user() is monkeypatched directly for
that one (same pattern as tests/test_calendar_visibility.py).
"""

import asyncio
import csv
import io
import uuid
from datetime import datetime, timezone

import pdfplumber
import pytest
from fastapi import HTTPException

from backend.main import (
    FIRM_ID,
    export_rbz_compliance_report,
    export_rbz_compliance_report_pdf,
    list_report_history,
)


class FakeConnection:
    def __init__(self, clients=None, matters=None, report_history=None):
        self.clients = clients if clients is not None else []
        self.matters = matters if matters is not None else []
        self.report_history = report_history if report_history is not None else []

    async def fetch(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT c.id AS client_id"):
            firm_id, = args
            rows = []
            for c in self.clients:
                if c["firm_id"] != firm_id:
                    continue
                client_matters = [m for m in self.matters if m.get("client_id") == c["id"]]
                if not client_matters:
                    rows.append({
                        "client_id": c["id"], "client_number": c.get("client_number"),
                        "client_name": c["full_name"], "contact_person": c.get("contact_person"),
                        "phone": c.get("phone"), "email": c.get("email"),
                        "matter_number": None, "matter_status": None,
                    })
                else:
                    for m in sorted(client_matters, key=lambda x: x.get("created_at") or datetime.min):
                        rows.append({
                            "client_id": c["id"], "client_number": c.get("client_number"),
                            "client_name": c["full_name"], "contact_person": c.get("contact_person"),
                            "phone": c.get("phone"), "email": c.get("email"),
                            "matter_number": m.get("matter_number"), "matter_status": m.get("status"),
                        })
            rows.sort(key=lambda r: r["client_name"] or "")
            return rows

        if q.startswith("SELECT * FROM report_history WHERE firm_id=$1"):
            firm_id, = args
            rows = [r for r in self.report_history if r["firm_id"] == firm_id]
            rows.sort(key=lambda r: r["generated_at"], reverse=True)
            return [dict(r) for r in rows]

        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO report_history"):
            cols = [c.strip() for c in q.split("(", 1)[1].split(")", 1)[0].split(",")]
            row = dict(zip(cols, args))
            row.setdefault("id", uuid.uuid4())
            row.setdefault("generated_at", datetime.now(timezone.utc))
            self.report_history.append(row)
            return "OK"
        raise NotImplementedError(f"FakeConnection.execute: unhandled query: {q}")


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, clients=None, matters=None, report_history=None):
        self.conn = FakeConnection(clients, matters, report_history)

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _client(name, client_number=None, contact_person=None, phone=None, email=None):
    return {
        "id": uuid.uuid4(), "firm_id": FIRM_ID, "full_name": name, "client_number": client_number,
        "contact_person": contact_person, "phone": phone, "email": email,
    }


def _matter(client_id, matter_number, status="Active", created_at=None):
    return {
        "id": uuid.uuid4(), "firm_id": FIRM_ID, "client_id": client_id,
        "matter_number": matter_number, "status": status,
        "created_at": created_at or datetime.now(timezone.utc),
    }


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def _fake_request():
    return None


def _csv_rows(response):
    text = response.body.decode("utf-8") if isinstance(response.body, bytes) else response.body
    return list(csv.reader(io.StringIO(text)))


def _pdf_text(response):
    with pdfplumber.open(io.BytesIO(response.body)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


# ── permission gate ──────────────────────────────────────────────────────

def test_associate_gets_403(monkeypatch):
    import backend.main as m
    associate = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "email": "a@sm.co.zw",
                 "role": "associate", "display_name": "Assoc Person"}
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, associate)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(export_rbz_compliance_report(_fake_request()))
    assert exc_info.value.status_code == 403


def test_secretary_gets_403(monkeypatch):
    import backend.main as m
    secretary = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "email": "s@sm.co.zw",
                 "role": "secretary", "display_name": "Secretary Person"}
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, secretary)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(export_rbz_compliance_report(_fake_request()))
    assert exc_info.value.status_code == 403


# ── partner succeeds, correct CSV content ───────────────────────────────

def test_partner_succeeds_with_expected_csv_content(monkeypatch):
    """AUTH_ENABLED=False's synthetic dev user is role=partner by default —
    no monkeypatch needed for the success path itself."""
    import backend.main as m
    huang = _client("Huang Li Qiang", client_number="NGM-001", phone="+263771234567")
    vengesai = _client("Vengesai Enterprises", client_number="NGM-002",
                        contact_person="Jane Muzenda", email="info@vengesai.co.zw")
    matters = [
        _matter(huang["id"], "NGM-001-01", status="Active"),
        _matter(huang["id"], "NGM-001-02", status="Closed"),
    ]
    pool = FakePool(clients=[huang, vengesai], matters=matters)
    monkeypatch.setattr(m, "_db_pool", pool)

    response = asyncio.run(export_rbz_compliance_report(_fake_request()))

    assert response.media_type == "text/csv"
    assert "attachment" in response.headers["Content-Disposition"]

    rows = _csv_rows(response)
    assert rows[0] == ["Client Number", "Client Name", "Contact Person", "Phone", "Email",
                        "Matter Number", "Matter Status"]
    data_rows = rows[1:]
    assert len(data_rows) == 3  # 2 Huang matters + 1 Vengesai row (no matters)
    assert ["NGM-001", "Huang Li Qiang", "", "+263771234567", "", "NGM-001-01", "Active"] in data_rows
    assert ["NGM-001", "Huang Li Qiang", "", "+263771234567", "", "NGM-001-02", "Closed"] in data_rows
    assert ["NGM-002", "Vengesai Enterprises", "Jane Muzenda", "", "info@vengesai.co.zw", "", ""] in data_rows


# ── report_history logging ───────────────────────────────────────────────

def test_report_history_records_correct_user_and_counts(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "email": "p@sm.co.zw",
               "role": "partner", "display_name": "Ostern Mutero"}
    huang = _client("Huang Li Qiang", client_number="NGM-001")
    matters = [_matter(huang["id"], "NGM-001-01"), _matter(huang["id"], "NGM-001-02")]
    pool = FakePool(clients=[huang], matters=matters)
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, partner)

    asyncio.run(export_rbz_compliance_report(_fake_request()))

    assert len(pool.conn.report_history) == 1
    entry = pool.conn.report_history[0]
    assert entry["generated_by_name"] == "Ostern Mutero"
    assert entry["generated_by"] == partner["id"]
    assert entry["client_count"] == 1
    assert entry["matter_count"] == 2
    assert entry["report_type"] == "rbz_compliance_export"


def test_list_report_history_returns_logged_entries(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "email": "p@sm.co.zw",
               "role": "partner", "display_name": "Ostern Mutero"}
    pool = FakePool(clients=[_client("Huang Li Qiang", client_number="NGM-001")])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, partner)

    asyncio.run(export_rbz_compliance_report(_fake_request()))
    history = asyncio.run(list_report_history(_fake_request()))

    assert len(history) == 1
    assert history[0]["generated_by_name"] == "Ostern Mutero"
    assert history[0]["client_count"] == 1
    assert history[0]["matter_count"] == 0
    assert isinstance(history[0]["id"], str)
    assert isinstance(history[0]["generated_by"], str)


def test_list_report_history_403s_for_associate(monkeypatch):
    import backend.main as m
    associate = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "email": "a@sm.co.zw",
                 "role": "associate", "display_name": "Assoc Person"}
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, associate)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(list_report_history(_fake_request()))
    assert exc_info.value.status_code == 403


# ── zero clients doesn't error ───────────────────────────────────────────

def test_zero_clients_export_does_not_error(monkeypatch):
    import backend.main as m
    pool = FakePool()  # no clients, no matters
    monkeypatch.setattr(m, "_db_pool", pool)

    response = asyncio.run(export_rbz_compliance_report(_fake_request()))

    rows = _csv_rows(response)
    assert len(rows) == 1  # header only, no data rows
    assert rows[0] == ["Client Number", "Client Name", "Contact Person", "Phone", "Email",
                        "Matter Number", "Matter Status"]

    assert len(pool.conn.report_history) == 1
    assert pool.conn.report_history[0]["client_count"] == 0
    assert pool.conn.report_history[0]["matter_count"] == 0


# ── PDF format: same permission gate, same data, second output format ────

def test_pdf_associate_gets_403(monkeypatch):
    import backend.main as m
    associate = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "email": "a@sm.co.zw",
                 "role": "associate", "display_name": "Assoc Person"}
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, associate)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(export_rbz_compliance_report_pdf(_fake_request()))
    assert exc_info.value.status_code == 403


def test_pdf_partner_succeeds_with_expected_content(monkeypatch):
    import backend.main as m
    huang = _client("Huang Li Qiang", client_number="NGM-001", phone="+263771234567")
    vengesai = _client("Vengesai Enterprises", client_number="NGM-002",
                        contact_person="Jane Muzenda", email="info@vengesai.co.zw")
    matters = [
        _matter(huang["id"], "NGM-001-01", status="Active"),
        _matter(huang["id"], "NGM-001-02", status="Closed"),
    ]
    pool = FakePool(clients=[huang, vengesai], matters=matters)
    monkeypatch.setattr(m, "_db_pool", pool)
    # Set explicitly rather than relying on whatever FIRM_NAME defaults to —
    # the default is deliberately a generic empty string (MUTEMO_FIRM_NAME
    # unset), and asserting against "" would trivially pass no matter what
    # the PDF actually contains.
    monkeypatch.setattr(m, "FIRM_NAME", "Test Firm Legal Practitioners")

    response = asyncio.run(export_rbz_compliance_report_pdf(_fake_request()))

    assert response.media_type == "application/pdf"
    assert "attachment" in response.headers["Content-Disposition"]
    assert response.headers["Content-Disposition"].endswith('.pdf"')

    text = _pdf_text(response)
    assert "Test Firm Legal Practitioners" in text  # firm name header
    assert "RBZ Compliance Export" in text
    assert "NGM-001" in text and "Huang Li Qiang" in text
    assert "NGM-001-01" in text and "Active" in text
    assert "NGM-001-02" in text and "Closed" in text
    assert "NGM-002" in text and "Vengesai Enterprises" in text
    assert "Jane Muzenda" in text


def test_pdf_report_history_records_correct_user_and_counts_and_format(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "email": "p@sm.co.zw",
               "role": "partner", "display_name": "Ostern Mutero"}
    huang = _client("Huang Li Qiang", client_number="NGM-001")
    matters = [_matter(huang["id"], "NGM-001-01"), _matter(huang["id"], "NGM-001-02")]
    pool = FakePool(clients=[huang], matters=matters)
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, partner)

    asyncio.run(export_rbz_compliance_report_pdf(_fake_request()))

    assert len(pool.conn.report_history) == 1
    entry = pool.conn.report_history[0]
    assert entry["generated_by_name"] == "Ostern Mutero"
    assert entry["client_count"] == 1
    assert entry["matter_count"] == 2
    assert entry["report_type"] == "rbz_compliance_export_pdf"  # distinct from the CSV's report_type

    history = asyncio.run(list_report_history(_fake_request()))
    assert history[0]["report_type"] == "rbz_compliance_export_pdf"


def test_pdf_zero_clients_export_does_not_error(monkeypatch):
    import backend.main as m
    pool = FakePool()  # no clients, no matters
    monkeypatch.setattr(m, "_db_pool", pool)

    response = asyncio.run(export_rbz_compliance_report_pdf(_fake_request()))

    text = _pdf_text(response)
    assert "No clients on file." in text

    assert len(pool.conn.report_history) == 1
    assert pool.conn.report_history[0]["client_count"] == 0
    assert pool.conn.report_history[0]["matter_count"] == 0


def test_pdf_handles_em_dashes_and_non_ascii_without_crashing(monkeypatch):
    """Matter free text commonly contains em-dashes (the onboarding
    template's own convention is "Reference — description") — fpdf2's core
    font is latin-1 only, so this must not crash on that or on other
    non-ASCII text (e.g. an accented client name)."""
    import backend.main as m
    client = _client("Müller Trading — Zimbabwe", client_number="NGM-001")
    matters = [_matter(client["id"], "NGM-001-01", status="Active — under review")]
    pool = FakePool(clients=[client], matters=matters)
    monkeypatch.setattr(m, "_db_pool", pool)

    response = asyncio.run(export_rbz_compliance_report_pdf(_fake_request()))
    text = _pdf_text(response)
    assert "NGM-001" in text  # didn't crash; something rendered
