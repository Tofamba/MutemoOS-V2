"""
Unit tests for the Calendar event compact-card/detail-view split
(2026-09-27): calendar_events.document_id (the one genuinely new piece --
notes and attendees already existed), CALENDAR_NOTES_MAX_LENGTH, and the
dedicated PATCH /api/calendar/{event_id}/document endpoint.

Confirmed with the user before building: the document link deliberately
does NOT go through update_calendar_event()'s general PATCH -- that path
bumps `sequence` and re-notifies every attendee ("event updated"), correct
for an actual reschedule, wrong for simply attaching evidence. These
tests lock that separation down explicitly, not just the happy path.

No prior test file exercises add_calendar_event()/update_calendar_event()
against a fake DB at all (confirmed: the full suite passes unchanged with
these two functions' SQL text now different) -- this file is the first,
scoped to what's new here rather than retro-covering the pre-existing
surface.

Called directly as plain async functions with fake conns/requests, same
convention as tests/test_title_deeds_checklist.py.
"""

import asyncio
import uuid
from datetime import datetime

import pytest
from fastapi import HTTPException

from backend.main import (
    FIRM_ID,
    CALENDAR_NOTES_MAX_LENGTH,
    CalendarEvent,
    CalendarEventDocumentUpdate,
    CalendarEventUpdate,
    add_calendar_event,
    update_calendar_event,
    update_calendar_event_document,
)


class FakeConnection:
    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.inserted = []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("INSERT INTO calendar_events"):
            cols_str = q.split("(", 1)[1].split(")", 1)[0]
            cols = [c.strip() for c in cols_str.split(",")]
            row = dict(zip(cols, args))
            row.setdefault("id", uuid.uuid4())
            row.setdefault("sequence", 0)
            row.setdefault("created_at", datetime.utcnow())
            self.events.append(row)
            self.inserted.append(row)
            return dict(row)

        if q.startswith("SELECT * FROM calendar_events WHERE id=$1 AND firm_id=$2"):
            event_id, firm_id = args
            for e in self.events:
                if e["id"] == event_id and e["firm_id"] == firm_id:
                    return dict(e)
            return None

        if q.startswith("UPDATE calendar_events SET document_id=$1 WHERE id=$2 AND firm_id=$3"):
            document_id, event_id, firm_id = args
            for e in self.events:
                if e["id"] == event_id and e["firm_id"] == firm_id:
                    e["document_id"] = document_id
                    return dict(e)
            return None

        if q.startswith("UPDATE calendar_events SET"):
            # update_calendar_event()'s dynamic SET builder -- the trailing
            # WHERE id=$N always comes last in `values`.
            import re
            m = re.search(r"SET (.+) WHERE id=\$(\d+)", q)
            set_clause = m.group(1)
            cols = [c.split("=")[0].strip() for c in set_clause.split(",") if "=" in c]
            event_id = args[-1]
            for e in self.events:
                if e["id"] == event_id:
                    for col, val in zip(cols, args):
                        if col == "sequence=sequence+1" or col == "sequence":
                            continue
                        e[col] = val
                    if "sequence=sequence+1" in set_clause:
                        e["sequence"] = e.get("sequence", 0) + 1
                    return dict(e)
            return None

        raise NotImplementedError(f"fetchrow: unhandled query: {q}")


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


class FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.cookies = {}


class FakeBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))


def _partner_user():
    return {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "Farai"}


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def _no_attendee_resolution(monkeypatch, m):
    async def fake_resolve(conn, firm_id, attendees):
        return attendees
    monkeypatch.setattr(m, "_resolve_attendee_users", fake_resolve)


def _existing_event(*, firm_id=FIRM_ID, document_id=None, sequence=0, attendees=None):
    return {
        "id": uuid.uuid4(), "firm_id": firm_id, "matter_id": None, "title": "Hearing",
        "date": "2026-10-01", "time": None, "event_type": "hearing", "court": None,
        "matter_name": None, "notes": None, "source": "manual", "document_id": document_id,
        "sequence": sequence, "created_at": datetime.utcnow(), "created_by": None,
        "attendees": attendees or [],
    }


# ── add_calendar_event(): document_id + notes length ────────────────────────

def test_create_event_stores_document_id(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())
    _no_attendee_resolution(monkeypatch, m)
    doc_id = uuid.uuid4()

    result = asyncio.run(add_calendar_event(
        CalendarEvent(title="Hearing", event_type="hearing", date="2026-10-01", document_id=str(doc_id)),
        FakeBackgroundTasks(), FakeRequest(),
    ))

    assert result["document_id"] == str(doc_id)


def test_create_event_without_document_id_stores_none(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())
    _no_attendee_resolution(monkeypatch, m)

    result = asyncio.run(add_calendar_event(
        CalendarEvent(title="Hearing", event_type="hearing", date="2026-10-01"),
        FakeBackgroundTasks(), FakeRequest(),
    ))

    assert result.get("document_id") is None


def test_create_event_rejects_malformed_document_id(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, _partner_user())
    _no_attendee_resolution(monkeypatch, m)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(add_calendar_event(
            CalendarEvent(title="Hearing", event_type="hearing", date="2026-10-01", document_id="not-a-uuid"),
            FakeBackgroundTasks(), FakeRequest(),
        ))
    assert exc.value.status_code == 400


def test_create_event_rejects_notes_over_the_length_ceiling(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, _partner_user())
    _no_attendee_resolution(monkeypatch, m)
    long_notes = "x" * (CALENDAR_NOTES_MAX_LENGTH + 1)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(add_calendar_event(
            CalendarEvent(title="Hearing", event_type="hearing", date="2026-10-01", notes=long_notes),
            FakeBackgroundTasks(), FakeRequest(),
        ))
    assert exc.value.status_code == 400


def test_create_event_accepts_notes_right_at_the_ceiling(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())
    _no_attendee_resolution(monkeypatch, m)
    notes = "x" * CALENDAR_NOTES_MAX_LENGTH

    result = asyncio.run(add_calendar_event(
        CalendarEvent(title="Hearing", event_type="hearing", date="2026-10-01", notes=notes),
        FakeBackgroundTasks(), FakeRequest(),
    ))
    assert result["notes"] == notes


# ── update_calendar_event(): notes length ceiling ────────────────────────────

def test_update_event_rejects_notes_over_the_length_ceiling(monkeypatch):
    import backend.main as m
    event = _existing_event()
    pool = FakePool(events=[event])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())
    long_notes = "x" * (CALENDAR_NOTES_MAX_LENGTH + 1)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(update_calendar_event(
            str(event["id"]), CalendarEventUpdate(notes=long_notes), FakeBackgroundTasks(), FakeRequest(),
        ))
    assert exc.value.status_code == 400


# ── PATCH /api/calendar/{event_id}/document: the dedicated endpoint ────────

def test_update_event_document_sets_the_link(monkeypatch):
    import backend.main as m
    event = _existing_event()
    pool = FakePool(events=[event])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())
    doc_id = uuid.uuid4()

    result = asyncio.run(update_calendar_event_document(
        str(event["id"]), CalendarEventDocumentUpdate(document_id=str(doc_id)), FakeRequest(),
    ))

    assert result["document_id"] == str(doc_id)


def test_update_event_document_can_genuinely_clear_the_link(monkeypatch):
    """Unlike the dict()-filter PATCH convention elsewhere in this app
    (authority_document_id, matter_checklist_items.document_id -- neither
    can ever be explicitly cleared), this single-field endpoint applies
    exactly what's given."""
    import backend.main as m
    doc_id = uuid.uuid4()
    event = _existing_event(document_id=doc_id)
    pool = FakePool(events=[event])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())

    result = asyncio.run(update_calendar_event_document(
        str(event["id"]), CalendarEventDocumentUpdate(document_id=None), FakeRequest(),
    ))

    assert result.get("document_id") is None


def test_update_event_document_does_not_bump_sequence(monkeypatch):
    """The core behavioral guarantee: linking evidence is not a
    reschedule, so it must never touch `sequence` (which drives whether
    attendees get a re-sent 'event updated' calendar invite)."""
    import backend.main as m
    event = _existing_event(sequence=3)
    pool = FakePool(events=[event])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())

    asyncio.run(update_calendar_event_document(
        str(event["id"]), CalendarEventDocumentUpdate(document_id=str(uuid.uuid4())), FakeRequest(),
    ))

    assert pool.conn.events[0]["sequence"] == 3


def test_update_event_document_rejects_malformed_uuid(monkeypatch):
    import backend.main as m
    event = _existing_event()
    monkeypatch.setattr(m, "_db_pool", FakePool(events=[event]))
    _as_current_user(monkeypatch, m, _partner_user())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(update_calendar_event_document(
            str(event["id"]), CalendarEventDocumentUpdate(document_id="not-a-uuid"), FakeRequest(),
        ))
    assert exc.value.status_code == 400


def test_update_event_document_404s_on_unknown_event(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, _partner_user())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(update_calendar_event_document(
            str(uuid.uuid4()), CalendarEventDocumentUpdate(document_id=str(uuid.uuid4())), FakeRequest(),
        ))
    assert exc.value.status_code == 404


def test_update_event_document_404s_on_event_from_another_firm(monkeypatch):
    import backend.main as m
    other_firm_event = _existing_event(firm_id=uuid.uuid4())
    monkeypatch.setattr(m, "_db_pool", FakePool(events=[other_firm_event]))
    _as_current_user(monkeypatch, m, _partner_user())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(update_calendar_event_document(
            str(other_firm_event["id"]), CalendarEventDocumentUpdate(document_id=str(uuid.uuid4())), FakeRequest(),
        ))
    assert exc.value.status_code == 404
