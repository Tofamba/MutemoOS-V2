"""
Unit tests for "Deadline & Calendar Report" (backend/main.py, 2026-09-18)
-- the fourth of five self-scoped practice reports, same access tier and
scoping contract as My Portfolio.

Merges two already-existing sources into one soonest-first list, no new
deadline/calendar concept:
  - matters.next_deadline/next_deadline_note, scoped created_by=user_id
    the same way every other self-scoped view in this batch is.
  - calendar_events via _calendar_visibility_clause(), the exact same
    visibility rule GET /api/calendar already uses (already thoroughly
    tested in tests/test_calendar_visibility.py) -- these tests verify
    the merge/sort/filter wiring, not that clause's own correctness.

Same FakeConnection convention as tests/test_calendar_visibility.py for
the calendar_events side.
"""

import asyncio
import csv
import io
import json
import uuid
from datetime import date, datetime, timedelta, timezone

import pdfplumber
import pytest

from backend.main import (
    FIRM_ID,
    deadline_calendar_report,
    deadline_calendar_report_export,
    deadline_calendar_report_export_pdf,
)

TODAY = date.today()


class FakeConnection:
    def __init__(self, matters=None, events=None):
        self.matters = matters if matters is not None else []
        self.events = events if events is not None else []

    async def fetch(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT name, matter_number, number, client_name, next_deadline, next_deadline_note"):
            firm_id, created_by = args
            rows = [m for m in self.matters if m["firm_id"] == firm_id and m.get("created_by") == created_by
                    and not m.get("is_sentinel") and m.get("next_deadline") is not None]
            return sorted(rows, key=lambda m: m["next_deadline"])

        if q.startswith("SELECT * FROM calendar_events WHERE"):
            # Real _calendar_visibility_clause() output -- either the
            # no-identity firm-wide fallback ("firm_id=$1") or the real
            # creator-or-accepted-attendee clause. This report never
            # reaches here with no identity (guarded earlier), so only
            # the real-identity shape is exercised.
            if "jsonb_array_elements" in q:
                firm_id, user_id = args
                out = [e for e in self.events if e["firm_id"] == firm_id and e.get("created_by") == user_id]
            else:
                firm_id, = args
                out = [e for e in self.events if e["firm_id"] == firm_id]
            out = [e for e in out if e["date"] >= TODAY]
            return sorted([dict(e) for e in out], key=lambda e: (e["date"], e.get("time") or ""))

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


def _matter(name, *, created_by, firm_id=FIRM_ID, next_deadline=None, next_deadline_note=None,
            matter_number=None, client_name=None, is_sentinel=False):
    return {"id": uuid.uuid4(), "firm_id": firm_id, "name": name, "number": None,
            "matter_number": matter_number, "client_name": client_name, "created_by": created_by,
            "next_deadline": next_deadline, "next_deadline_note": next_deadline_note, "is_sentinel": is_sentinel}


def _event(title, event_date, *, created_by, firm_id=FIRM_ID, event_type="hearing", matter_name=None,
           attendees=None, time=None):
    return {"id": uuid.uuid4(), "firm_id": firm_id, "title": title, "date": event_date, "time": time,
            "event_type": event_type, "matter_name": matter_name, "created_by": created_by,
            "attendees": attendees or [], "matter_id": None, "court": None, "notes": None}


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

def test_only_own_matter_deadlines_returned(monkeypatch):
    import backend.main as m
    me = _lawyer()
    other = uuid.uuid4()
    matters = [
        _matter("Mine", created_by=me["id"], next_deadline=TODAY + timedelta(days=5)),
        _matter("Not mine", created_by=other, next_deadline=TODAY + timedelta(days=2)),
    ]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(deadline_calendar_report(_fake_request()))

    assert [r["matter_name"] for r in result if r["type"] == "Matter Deadline"] == ["(unnumbered) — Mine"]


def test_matters_without_a_deadline_are_excluded(monkeypatch):
    import backend.main as m
    me = _lawyer()
    matters = [_matter("No deadline set", created_by=me["id"], next_deadline=None)]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(deadline_calendar_report(_fake_request()))
    assert result == []


# ── overdue vs upcoming ──────────────────────────────────────────────────

def test_overdue_deadline_flagged_true(monkeypatch):
    import backend.main as m
    me = _lawyer()
    matters = [_matter("Overdue", created_by=me["id"], next_deadline=TODAY - timedelta(days=3))]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(deadline_calendar_report(_fake_request()))
    assert result[0]["overdue"] is True


def test_upcoming_deadline_flagged_false(monkeypatch):
    import backend.main as m
    me = _lawyer()
    matters = [_matter("Upcoming", created_by=me["id"], next_deadline=TODAY + timedelta(days=3))]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(deadline_calendar_report(_fake_request()))
    assert result[0]["overdue"] is False


# ── calendar merge + soonest-first sort ─────────────────────────────────────

def test_calendar_events_merged_and_sorted_soonest_first(monkeypatch):
    import backend.main as m
    me = _lawyer()
    matters = [_matter("Deadline matter", created_by=me["id"], next_deadline=TODAY + timedelta(days=10))]
    events = [_event("Case management hearing", TODAY + timedelta(days=2), created_by=me["id"])]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters, events=events))
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(deadline_calendar_report(_fake_request()))

    assert len(result) == 2
    # Soonest first -- the calendar event (2 days out) before the
    # deadline (10 days out).
    assert result[0]["title"] == "Case management hearing"
    assert result[1]["type"] == "Matter Deadline"


def test_past_calendar_events_excluded_not_shown_as_overdue(monkeypatch):
    """A past calendar entry (a hearing that already happened) isn't
    'upcoming or overdue' the way a missed deadline is -- excluded
    entirely, not flagged overdue=True."""
    import backend.main as m
    me = _lawyer()
    events = [_event("Past hearing", TODAY - timedelta(days=5), created_by=me["id"])]
    monkeypatch.setattr(m, "_db_pool", FakePool(events=events))
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(deadline_calendar_report(_fake_request()))
    assert result == []


def test_calendar_events_never_flagged_overdue(monkeypatch):
    import backend.main as m
    me = _lawyer()
    events = [_event("Upcoming hearing", TODAY + timedelta(days=1), created_by=me["id"])]
    monkeypatch.setattr(m, "_db_pool", FakePool(events=events))
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(deadline_calendar_report(_fake_request()))
    assert result[0]["overdue"] is False


# ── cross-user isolation ──────────────────────────────────────────────────

def test_cross_user_isolation(monkeypatch):
    import backend.main as m
    lawyer_a = uuid.uuid4()
    lawyer_b = _lawyer()
    matters = [
        _matter("A's deadline", created_by=lawyer_a, next_deadline=TODAY + timedelta(days=1)),
        _matter("B's deadline", created_by=lawyer_b["id"], next_deadline=TODAY + timedelta(days=1)),
    ]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _as_current_user(monkeypatch, m, lawyer_b)

    result = asyncio.run(deadline_calendar_report(_fake_request()))
    assert len(result) == 1
    assert "B's deadline" in result[0]["matter_name"]


def test_no_lawyer_id_parameter_exists(monkeypatch):
    import inspect
    sig = inspect.signature(deadline_calendar_report)
    assert list(sig.parameters.keys()) == ["request"]


# ── firm/tenant isolation ────────────────────────────────────────────────

def test_firm_isolation_never_sees_another_firms_deadlines(monkeypatch):
    import backend.main as m
    shared_lawyer_id = uuid.uuid4()
    firm_b = "b2c3d4e5-0000-0000-0000-000000000002"
    matters = [
        _matter("Firm A deadline", created_by=shared_lawyer_id, firm_id=FIRM_ID, next_deadline=TODAY + timedelta(days=1)),
        _matter("Firm B deadline", created_by=shared_lawyer_id, firm_id=firm_b, next_deadline=TODAY + timedelta(days=1)),
    ]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _as_current_user(monkeypatch, m, {"id": shared_lawyer_id, "firm_id": FIRM_ID, "role": "partner", "display_name": "P"})

    result = asyncio.run(deadline_calendar_report(_fake_request()))
    assert len(result) == 1
    assert "Firm A deadline" in result[0]["matter_name"]


# ── empty state ──────────────────────────────────────────────────────────

def test_no_deadlines_or_events_returns_empty_list_not_an_error(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, _lawyer())

    result = asyncio.run(deadline_calendar_report(_fake_request()))
    assert result == []


def test_no_real_identity_returns_empty_not_an_error(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, {"id": None, "firm_id": FIRM_ID, "role": "partner", "display_name": "Demo User"})

    result = asyncio.run(deadline_calendar_report(_fake_request()))
    assert result == []


# ── CSV generation ───────────────────────────────────────────────────────

def test_csv_export_contains_correct_data(monkeypatch):
    import backend.main as m
    me = _lawyer()
    matters = [_matter("Filing deadline", created_by=me["id"], next_deadline=TODAY - timedelta(days=1),
                        next_deadline_note="File heads of argument", matter_number="HC123")]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _as_current_user(monkeypatch, m, me)

    response = asyncio.run(deadline_calendar_report_export(_fake_request()))
    rows = _csv_rows(response)

    assert rows[0] == ["Date", "Type", "Title", "Matter", "Client", "Overdue"]
    assert rows[1][1] == "Matter Deadline"
    assert rows[1][2] == "File heads of argument"
    assert rows[1][5] == "Yes"


def test_csv_export_starts_with_utf8_bom(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, _lawyer())

    response = asyncio.run(deadline_calendar_report_export(_fake_request()))
    assert response.body.startswith("﻿".encode("utf-8"))


# ── PDF generation ───────────────────────────────────────────────────────

def test_pdf_export_generates_without_error_and_contains_correct_data(monkeypatch):
    import backend.main as m
    me = _lawyer()
    matters = [_matter("Filing deadline", created_by=me["id"], next_deadline=TODAY + timedelta(days=3),
                        next_deadline_note="File heads of argument")]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _as_current_user(monkeypatch, m, me)

    response = asyncio.run(deadline_calendar_report_export_pdf(_fake_request()))
    assert response.media_type == "application/pdf"
    assert response.body.startswith(b"%PDF")
    text = _pdf_text(response)
    assert "File heads of argument" in text


def test_pdf_export_handles_no_items_without_crashing(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, _lawyer())

    response = asyncio.run(deadline_calendar_report_export_pdf(_fake_request()))
    assert response.body.startswith(b"%PDF")
    assert "No upcoming deadlines" in _pdf_text(response)
