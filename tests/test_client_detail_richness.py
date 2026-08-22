"""
Unit tests for GET /api/clients/{id}'s richness additions in backend/main.py
(get_client): progress notes and documents batched per matter, and
calendar events scoped to the client's own matters — the data backing the
client detail view's Recent Activity, Upcoming Deadlines, and Practice
Area Breakdown sections (mostly computed client-side in frontend/index.html
from what this endpoint returns; see renderClientDetailContent()).

Called directly as plain async functions, same convention as
tests/test_clients_api.py.
"""

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

from backend.main import FIRM_ID, get_client


class FakeConnection:
    def __init__(self, clients, matters=None, notes=None, documents=None, calendar_events=None):
        self.clients = clients
        self.matters = matters if matters is not None else []
        self.notes = notes if notes is not None else []
        self.documents = documents if documents is not None else []
        self.calendar_events = calendar_events if calendar_events is not None else []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT * FROM clients WHERE id=$1 AND firm_id=$2"):
            for c in self.clients:
                if c["id"] == args[0] and c["firm_id"] == args[1]:
                    return dict(c)
            return None
        # get_client()'s compliance-badge addition (AML/KYC module) — not
        # exercised here; see tests/test_client_compliance.py.
        if q.startswith("SELECT * FROM client_compliance WHERE client_id=$1 AND firm_id=$2"):
            return None
        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT * FROM matters WHERE client_id=$1 AND firm_id=$2"):
            rows = [m for m in self.matters if m.get("client_id") == args[0] and m["firm_id"] == args[1]]
            return [dict(r) for r in rows]

        if q.startswith("SELECT * FROM progress_notes WHERE matter_id = ANY($1)"):
            matter_ids = set(args[0])
            return [dict(n) for n in self.notes if n["matter_id"] in matter_ids]

        if q.startswith("SELECT * FROM documents WHERE matter_id = ANY($1) AND status='complete'"):
            matter_ids = set(args[0])
            return [dict(d) for d in self.documents if d["matter_id"] in matter_ids and d["status"] == "complete"]

        if q.startswith("SELECT * FROM calendar_events WHERE matter_id = ANY($1) AND date >= CURRENT_DATE"):
            matter_ids = set(args[0])
            today = date.today()
            return [dict(e) for e in self.calendar_events if e["matter_id"] in matter_ids and e["date"] >= today]

        if q.startswith("SELECT verification_status FROM beneficial_owners WHERE client_id=$1 AND firm_id=$2"):
            return []

        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")

    async def execute(self, query, *args):
        return "OK"


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


def _client_row(client_id, full_name="John Moyo", firm_id=FIRM_ID):
    return {
        "id": client_id, "firm_id": firm_id, "full_name": full_name, "email": None, "phone": None,
        "physical_address": None, "id_or_registration_number": None, "contact_person": None, "notes": None,
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
        "client_number": None, "created_by": None,
    }


def _matter_row(matter_id, client_id, name="Test Matter", firm_id=FIRM_ID):
    return {
        "id": matter_id, "firm_id": firm_id, "name": name, "number": None,
        "internal_ref": None, "external_ref": None, "client_name": None, "client_id": client_id,
        "case_parties": None, "matter_type": None, "practice_area": None, "status": "Active",
        "custom_status": None, "document_count": 0, "last_activity": None,
        "created_at": datetime.now(timezone.utc), "created_by": None,
        "next_deadline": None, "next_deadline_note": None,
        "amount_billed": None, "amount_received": None,
        "conveyancing_milestone": None, "conveyancing_property_address": None,
        "conveyancing_title_deed_number": None, "conveyancing_purchase_price": None,
        "conveyancing_other_conveyancer_contact": None, "conveyancing_transfer_date": None,
        "conveyancing_rates_clearance_expiry": None, "conveyancing_bond_registration_deadline": None,
    }


def _note(matter_id, text, firm_id=FIRM_ID, created_at=None):
    return {
        "id": uuid.uuid4(), "matter_id": matter_id, "firm_id": firm_id, "text": text,
        "author": "Test Lawyer", "user_id": None, "created_at": created_at or datetime.now(timezone.utc),
    }


def _doc(matter_id, filename, status="complete", firm_id=FIRM_ID, uploaded_at=None):
    return {
        "id": uuid.uuid4(), "matter_id": matter_id, "firm_id": firm_id, "filename": filename,
        "document_type": None, "matter_type": None, "parties": None, "doc_date": None, "court": None,
        "word_count": 0, "page_count": 1, "chunk_count": 0, "ocr_used": False, "ocr_confidence": None,
        "needs_review": False, "status": status, "error_message": None,
        "uploaded_at": uploaded_at or datetime.now(timezone.utc), "uploaded_by": None,
    }


def _event(matter_id, title, event_date, firm_id=FIRM_ID):
    return {
        "id": uuid.uuid4(), "firm_id": firm_id, "matter_id": matter_id, "title": title, "date": event_date,
        "time": None, "event_type": "deadline", "court": None, "matter_name": None, "notes": None,
        "source": "manual", "attendees": [], "sequence": 0, "created_at": datetime.now(timezone.utc),
        "created_by": None,
    }


def test_get_client_attaches_progress_notes_per_matter(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    matter_id = uuid.uuid4()
    pool = FakePool(
        clients=[_client_row(client_id)],
        matters=[_matter_row(matter_id, client_id)],
        notes=[_note(matter_id, "First note"), _note(matter_id, "Second note")],
    )
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(get_client(str(client_id), None))

    assert len(result["matters"]) == 1
    texts = {n["text"] for n in result["matters"][0]["progress_notes"]}
    assert texts == {"First note", "Second note"}


def test_get_client_attaches_documents_per_matter_completed_only(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    matter_id = uuid.uuid4()
    pool = FakePool(
        clients=[_client_row(client_id)],
        matters=[_matter_row(matter_id, client_id)],
        documents=[_doc(matter_id, "lease.pdf", status="complete"), _doc(matter_id, "still-processing.pdf", status="processing")],
    )
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(get_client(str(client_id), None))

    filenames = {d["filename"] for d in result["matters"][0]["documents"]}
    assert filenames == {"lease.pdf"}  # the processing one is excluded


def test_get_client_notes_and_documents_scoped_to_correct_matter(monkeypatch):
    """Two matters under the same client — each must only see its own
    notes/documents, not the other's."""
    import backend.main as m
    client_id = uuid.uuid4()
    matter_a, matter_b = uuid.uuid4(), uuid.uuid4()
    pool = FakePool(
        clients=[_client_row(client_id)],
        matters=[_matter_row(matter_a, client_id, name="Matter A"), _matter_row(matter_b, client_id, name="Matter B")],
        notes=[_note(matter_a, "Note for A"), _note(matter_b, "Note for B")],
    )
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(get_client(str(client_id), None))

    by_name = {mm["name"]: mm for mm in result["matters"]}
    assert [n["text"] for n in by_name["Matter A"]["progress_notes"]] == ["Note for A"]
    assert [n["text"] for n in by_name["Matter B"]["progress_notes"]] == ["Note for B"]


def test_get_client_calendar_events_scoped_to_this_clients_matters_only(monkeypatch):
    """A firm-wide calendar event tied to some other client's matter must
    not leak into this client's detail view."""
    import backend.main as m
    client_id = uuid.uuid4()
    other_client_id = uuid.uuid4()
    matter_id = uuid.uuid4()
    other_matter_id = uuid.uuid4()
    tomorrow = date.today() + timedelta(days=1)
    pool = FakePool(
        clients=[_client_row(client_id)],
        matters=[_matter_row(matter_id, client_id), _matter_row(other_matter_id, other_client_id)],
        calendar_events=[
            _event(matter_id, "Hearing for this client", tomorrow),
            _event(other_matter_id, "Hearing for a different client", tomorrow),
        ],
    )
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(get_client(str(client_id), None))

    titles = {e["title"] for e in result["calendar_events"]}
    assert titles == {"Hearing for this client"}


def test_get_client_with_no_matters_returns_empty_richness_lists(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    pool = FakePool(clients=[_client_row(client_id)])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(get_client(str(client_id), None))

    assert result["matters"] == []
    assert result["calendar_events"] == []
