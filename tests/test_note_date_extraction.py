"""
Unit tests for calendar-event suggestions from progress notes
(backend/main.py, 2026-08-31).

This feature already existed end-to-end (add_progress_note()'s
detected_dates + the frontend's showNoteDatePrompt()/addNoteDatesToCalendar())
but had zero test coverage, and its date-scanning was a third,
independently-written Claude prompt rather than genuinely reusing "the
existing date-extraction pipeline" already built for document uploads.
Consolidated into one shared _extract_dates_from_text(), used by
extract_dates_from_document(), extract_dates_by_document_id(), and
add_progress_note() alike -- this file tests that consolidation plus
the note-specific suggestion flow.

No backend test exists for "declining/ignoring a suggestion creates
nothing" -- that's inherently a frontend behavior (the browser simply
never calls POST /api/calendar for a dismissed suggestion); there is no
server-side action to unit test for the absence of a request.

Called directly as plain async functions, same convention as
tests/test_matter_review_safety_net.py (whose _NoteFakeConnection this
file's own fake mirrors) and tests/test_calendar_visibility.py (whose
add_calendar_event calling convention section 3 below reuses).
"""

import asyncio
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.main import (
    FIRM_ID,
    Attendee,
    CalendarEvent,
    ProgressNote,
    _extract_dates_from_text,
    add_calendar_event,
    add_progress_note,
    extract_dates_by_document_id,
    extract_dates_from_document,
)


def _fake_claude_message(json_body: dict):
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(json_body))])


def _fake_request():
    return None


# ── _extract_dates_from_text: the shared core ───────────────────────────────

def test_extract_dates_from_text_returns_dates_and_summary(monkeypatch):
    import backend.main as m
    body = {
        "dates": [{"title": "Pre-trial conference", "date": "2026-09-15", "time": "09:00",
                    "event_type": "hearing", "party": None, "notes": None}],
        "document_summary": "A short note about an upcoming hearing.",
    }
    monkeypatch.setattr(m.client.messages, "create", lambda **kwargs: _fake_claude_message(body))

    result = _extract_dates_from_text("Pre-trial conference set for 15 September 2026 at 9am.")

    assert result["dates"] == body["dates"]
    assert result["document_summary"] == body["document_summary"]


def test_extract_dates_from_text_returns_empty_when_no_dates(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m.client.messages, "create",
                         lambda **kwargs: _fake_claude_message({"dates": [], "document_summary": None}))

    result = _extract_dates_from_text("Called the client to discuss general strategy.")

    assert result["dates"] == []
    assert result["document_summary"] is None


def test_extract_dates_from_text_strips_markdown_code_fence(monkeypatch):
    """Claude sometimes wraps its JSON in a ```json fence despite the
    prompt saying not to -- same defensive stripping the original three
    independent implementations each already did."""
    import backend.main as m
    fenced = SimpleNamespace(content=[SimpleNamespace(
        text='```json\n{"dates": [], "document_summary": null}\n```'
    )])
    monkeypatch.setattr(m.client.messages, "create", lambda **kwargs: fenced)

    result = _extract_dates_from_text("No dates here.")
    assert result["dates"] == []


# ── add_progress_note(): the suggestion flow ────────────────────────────────

class _NoteFakeConnection:
    """Mirrors tests/test_matter_review_safety_net.py's _NoteFakeConnection
    exactly -- same query shapes add_progress_note() actually issues."""
    def __init__(self, matter_row):
        self.matter_row = matter_row

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT id, name, internal_ref FROM matters"):
            return {"id": self.matter_row["id"], "name": self.matter_row["name"],
                     "internal_ref": self.matter_row.get("internal_ref")}
        if q.startswith("INSERT INTO progress_notes"):
            nid, matter_id, firm_id, text, author, user_id, created_at = args
            return {"id": nid, "matter_id": matter_id, "firm_id": firm_id, "text": text,
                    "author": author, "user_id": user_id, "created_at": created_at}
        raise NotImplementedError(f"_NoteFakeConnection.fetchrow: unhandled query: {q}")

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("UPDATE matters SET last_activity=$1, next_review_date=$2, last_reviewed_date=$3"):
            return "UPDATE 1"
        raise NotImplementedError(f"_NoteFakeConnection.execute: unhandled query: {q}")


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _pool_for(matter_id, matter_name="Estate of Chikafu", internal_ref=None):
    conn = _NoteFakeConnection({"id": matter_id, "name": matter_name, "internal_ref": internal_ref})
    return FakePool(conn)


def test_note_with_clear_date_surfaces_one_suggestion(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", _pool_for(matter_id))
    body = {"dates": [{"title": "File heads of argument", "date": "2026-09-20", "time": None,
                        "event_type": "filing", "party": None, "notes": None}], "document_summary": None}
    monkeypatch.setattr(m.client.messages, "create", lambda **kwargs: _fake_claude_message(body))

    result = asyncio.run(add_progress_note(
        str(matter_id), ProgressNote(text="Must file heads of argument by 20 September 2026."), _fake_request()
    ))

    assert len(result["detected_dates"]) == 1
    d = result["detected_dates"][0]
    assert d["date"] == "2026-09-20"
    assert d["title"] == "File heads of argument"
    # Stamped so the frontend/create-event flow has what it needs without
    # a second round-trip.
    assert d["matter_id"] == str(matter_id)
    assert d["matter_name"] == "Estate of Chikafu"
    assert d["source"] == "progress_note"


def test_note_with_no_date_surfaces_nothing(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", _pool_for(matter_id))
    monkeypatch.setattr(m.client.messages, "create",
                         lambda **kwargs: _fake_claude_message({"dates": [], "document_summary": None}))

    result = asyncio.run(add_progress_note(
        str(matter_id), ProgressNote(text="Spoke to the client, no action needed yet."), _fake_request()
    ))

    assert result["detected_dates"] == []


def test_note_with_multiple_dates_surfaces_all_of_them(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", _pool_for(matter_id))
    body = {
        "dates": [
            {"title": "Rates clearance expiry", "date": "2026-09-01", "time": None, "event_type": "deadline"},
            {"title": "Transfer date", "date": "2026-10-15", "time": None, "event_type": "other"},
        ],
        "document_summary": None,
    }
    monkeypatch.setattr(m.client.messages, "create", lambda **kwargs: _fake_claude_message(body))

    result = asyncio.run(add_progress_note(
        str(matter_id),
        ProgressNote(text="Rates clearance expires 1 September 2026; transfer set for 15 October 2026."),
        _fake_request(),
    ))

    assert len(result["detected_dates"]) == 2
    assert {d["date"] for d in result["detected_dates"]} == {"2026-09-01", "2026-10-15"}


def test_note_saved_successfully_even_when_date_scan_fails(monkeypatch):
    """The date scan is best-effort -- a Claude/parsing failure must not
    stop the note itself from saving."""
    import backend.main as m
    matter_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", _pool_for(matter_id))
    def raise_error(**kwargs):
        raise RuntimeError("Claude API unavailable")
    monkeypatch.setattr(m.client.messages, "create", raise_error)

    result = asyncio.run(add_progress_note(
        str(matter_id), ProgressNote(text="Filed the application on 1 September 2026."), _fake_request()
    ))

    assert result["detected_dates"] == []
    assert result["text"] == "Filed the application on 1 September 2026."  # the note itself still saved


# ── confirming a suggestion creates a real, correctly-linked event ─────────

class _NullTasks:
    def add_task(self, *args, **kwargs):
        pass


class _CalendarFakeConnection:
    """Mirrors tests/test_calendar_visibility.py's FakeConnection's
    add_calendar_event-relevant slice."""
    def __init__(self):
        self.events = []

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT id, email FROM users WHERE firm_id=$1"):
            return []
        raise NotImplementedError(f"_CalendarFakeConnection.fetch: unhandled query: {q}")

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO calendar_events"):
            cols = [c.strip() for c in q.split("(", 1)[1].split(")", 1)[0].split(",")]
            row = dict(zip(cols, args))
            row.setdefault("id", uuid.uuid4())
            row.setdefault("sequence", 0)
            row.setdefault("created_at", datetime.now(timezone.utc))
            self.events.append(row)
            return dict(row)
        raise NotImplementedError(f"_CalendarFakeConnection.fetchrow: unhandled query: {q}")


class _CalendarFakePool:
    def __init__(self):
        self.conn = _CalendarFakeConnection()

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def test_confirming_a_detected_date_creates_a_real_linked_calendar_event(monkeypatch):
    """The actual point of requirement 5: a detected date, once confirmed,
    goes through the SAME add_calendar_event() the normal Calendar tab
    uses -- not a second creation path. Simulates exactly what the
    frontend's addNoteDatesToCalendar() sends: the detected-date dict's
    fields mapped straight into a CalendarEvent."""
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = _CalendarFakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    detected = {
        "title": "File heads of argument", "date": "2026-09-20", "time": None,
        "event_type": "filing", "matter_id": str(matter_id), "matter_name": "Estate of Chikafu",
        "internal_ref": None, "source": "progress_note",
    }

    created = asyncio.run(add_calendar_event(
        CalendarEvent(
            title=detected["title"], date=detected["date"], time=detected["time"],
            event_type=detected["event_type"], matter_id=detected["matter_id"],
            matter_name=detected["matter_name"], notes="From progress note",
        ),
        background_tasks=_NullTasks(), request=_fake_request(),
    ))

    assert created["title"] == "File heads of argument"
    assert created["date"] == "2026-09-20"
    assert created["matter_id"] == str(matter_id)
    assert created["matter_name"] == "Estate of Chikafu"
    assert len(pool.conn.events) == 1  # one real row, not a second/duplicate creation path


# ── regression coverage for the two document-based endpoints touched ───────
# by the same refactor (previously zero coverage on either).

class _FakeUploadFile:
    def __init__(self, filename: str, content: bytes):
        self.filename = filename
        self._content = content

    async def read(self):
        return self._content


def test_extract_dates_from_document_uses_shared_pipeline(monkeypatch):
    import backend.main as m
    body = {"dates": [{"title": "Hearing", "date": "2026-09-01", "time": None, "event_type": "hearing"}],
            "document_summary": "A hearing notice."}
    monkeypatch.setattr(m.client.messages, "create", lambda **kwargs: _fake_claude_message(body))

    result = asyncio.run(extract_dates_from_document(
        file=_FakeUploadFile("notice.txt", b"Hearing set for 1 September 2026."),
        matter_id="m-1", matter_name="Test Matter", request=None,
    ))

    assert result["count"] == 1
    assert result["dates"][0]["matter_id"] == "m-1"
    assert result["document_summary"] == "A hearing notice."


def test_extract_dates_by_document_id_uses_shared_pipeline(monkeypatch):
    import backend.main as m

    class _DocConn:
        async def fetchrow(self, query, *args):
            q = " ".join(query.split())
            if q.startswith("SELECT * FROM documents"):
                return {"id": args[0], "firm_id": args[1], "filename": "notice.pdf", "matter_id": None}
            if q.startswith("SELECT name FROM matters"):
                return None
            raise NotImplementedError(q)

        async def fetch(self, query, *args):
            q = " ".join(query.split())
            if q.startswith("SELECT text FROM chunks"):
                return [{"text": "Hearing set for 1 September 2026."}]
            raise NotImplementedError(q)

    class _DocPool:
        def __init__(self):
            self.conn = _DocConn()

        def acquire(self):
            return _FakeAcquireCtx(self.conn)

    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    monkeypatch.setattr(m, "_db_pool", _DocPool())
    async def fake_get_current_user(request):
        return partner
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)
    body = {"dates": [{"title": "Hearing", "date": "2026-09-01", "time": None, "event_type": "hearing"}],
            "document_summary": "A hearing notice."}
    monkeypatch.setattr(m.client.messages, "create", lambda **kwargs: _fake_claude_message(body))

    result = asyncio.run(extract_dates_by_document_id(request=_fake_request(), document_id=str(uuid.uuid4())))

    assert result["count"] == 1
    assert result["filename"] == "notice.pdf"
