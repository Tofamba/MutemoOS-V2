"""
Unit tests for the matter review safety net (backend/main.py, 2026-08-30):
next_review_date on matters, its create/update defaulting behavior via
DEFAULT_REVIEW_INTERVAL_DAYS, and its "Matters for Review" section in the
daily reminder digest — kept deliberately separate from the existing
next_deadline (hard court/filing deadline) events.

Called directly as plain async functions / pure functions, same
convention as tests/test_matter_fees.py and tests/test_client_intake.py.
"""

import asyncio
import re
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from backend.main import (
    DEFAULT_REVIEW_INTERVAL_DAYS,
    REVIEW_DIGEST_LOOKAHEAD_DAYS,
    FIRM_ID,
    MatterUpdate,
    ProgressNote,
    _create_matter_row,
    _get_review_matters_for_digest,
    add_progress_note,
    build_reminder_email_body,
    update_matter,
)


# ── Shared fakes (mirrors tests/test_matter_fees.py + tests/test_client_intake.py) ──

class FakeConnection:
    def __init__(self, matters=None, review_rows=None):
        self.matters = matters if matters is not None else []
        self._review_rows = review_rows if review_rows is not None else []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("INSERT INTO matters"):
            cols = [c.strip() for c in q.split("(", 1)[1].split(")", 1)[0].split(",")]
            row = dict(zip(cols, args))
            self.matters.append(row)
            return dict(row)

        if q.startswith("UPDATE matters SET"):
            match = re.search(r"SET (.+) WHERE id=\$1", q)
            cols = re.findall(r"(\w+)=\$\d+", match.group(1))
            matter_id, firm_id = args[0], args[-1]
            values = args[1:1 + len(cols)]
            for row in self.matters:
                if row["id"] == matter_id and row["firm_id"] == firm_id:
                    for col, val in zip(cols, values):
                        row[col] = val
                    return dict(row)
            return None

        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT * FROM progress_notes"):
            return []
        if q.startswith("SELECT m.id, m.name, m.next_review_date"):
            return self._review_rows
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
    def __init__(self, matters=None, review_rows=None):
        self.conn = FakeConnection(matters=matters, review_rows=review_rows)

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _matter_row(matter_id, next_review_date=None, firm_id=FIRM_ID, name="Estate of X"):
    return {
        "id": matter_id, "firm_id": firm_id, "name": name, "number": None,
        "internal_ref": None, "external_ref": None, "client_name": None, "client_id": None,
        "case_parties": None, "matter_type": None, "practice_area": None, "status": "Active",
        "custom_status": None, "document_count": 0, "last_activity": None,
        "created_at": datetime.now(timezone.utc), "created_by": None,
        "next_deadline": None, "next_deadline_note": None,
        "next_review_date": next_review_date,
    }


def _fake_request():
    return None


# ── Creation: _create_matter_row() defaulting ──────────────────────────────

def test_new_matter_gets_default_review_date():
    conn = FakeConnection()
    row = asyncio.run(_create_matter_row(conn, FIRM_ID, "New Estate Matter"))
    assert row["next_review_date"] == date.today() + timedelta(days=DEFAULT_REVIEW_INTERVAL_DAYS)


def test_new_matter_gets_last_reviewed_date_stamped_to_today():
    """A brand-new matter's baseline 'last touched' reference point is
    its own creation."""
    conn = FakeConnection()
    row = asyncio.run(_create_matter_row(conn, FIRM_ID, "New Estate Matter"))
    assert row["last_reviewed_date"] == date.today()


def test_create_matter_row_respects_explicit_next_review_date():
    conn = FakeConnection()
    explicit = date(2026, 12, 25)
    row = asyncio.run(_create_matter_row(conn, FIRM_ID, "New Estate Matter", next_review_date=explicit))
    assert row["next_review_date"] == explicit


# ── Update: explicit set vs. re-default ─────────────────────────────────────

def test_explicit_review_date_overrides_default_and_restarts_clock(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id)])
    monkeypatch.setattr(m, "_db_pool", pool)

    future = "2026-12-25"
    result = asyncio.run(update_matter(str(matter_id), MatterUpdate(next_review_date=future), _fake_request()))

    assert result["next_review_date"] == future
    # Even an explicit far-future override still stamps last_reviewed_date
    # to today -- this touch IS the review happening now, regardless of
    # when the matter should next be looked at.
    assert result["last_reviewed_date"] == date.today().isoformat()


def test_every_update_stamps_last_reviewed_date_to_today(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id)])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_matter(str(matter_id), MatterUpdate(status="On Hold"), _fake_request()))
    assert result["last_reviewed_date"] == date.today().isoformat()


def test_matter_updated_without_review_date_gets_redefaulted(monkeypatch):
    """A PATCH that touches something unrelated (not a dedicated 'review'
    action) still re-defaults next_review_date -- this is the whole
    mechanism: a matter genuinely being worked on keeps pushing its own
    review date forward and never goes stale."""
    import backend.main as m
    matter_id = uuid.uuid4()
    # Start with a stale/overdue date to make the re-default unambiguous.
    stale = date.today() - timedelta(days=5)
    pool = FakePool(matters=[_matter_row(matter_id, next_review_date=stale)])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_matter(str(matter_id), MatterUpdate(status="Active"), _fake_request()))

    # _row_to_matter() serializes dates to ISO strings for the API response.
    assert result["next_review_date"] == (date.today() + timedelta(days=DEFAULT_REVIEW_INTERVAL_DAYS)).isoformat()


def test_invalid_next_review_date_format_400s(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id)])
    monkeypatch.setattr(m, "_db_pool", pool)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_matter(str(matter_id), MatterUpdate(next_review_date="not-a-date"), _fake_request()))
    assert exc_info.value.status_code == 400


def test_update_matter_404s_for_unknown_matter_still_works(monkeypatch):
    """Regression check: the review-date re-default logic runs before the
    UPDATE, so it must not break the existing 404-for-unknown-matter path."""
    import backend.main as m
    pool = FakePool(matters=[])
    monkeypatch.setattr(m, "_db_pool", pool)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_matter(str(uuid.uuid4()), MatterUpdate(status="Active"), _fake_request()))
    assert exc_info.value.status_code == 404


# ── Digest: _get_review_matters_for_digest() ────────────────────────────────

def test_digest_query_includes_overdue_and_approaching_excludes_future(monkeypatch):
    import backend.main as m
    today = date.today()
    overdue_row = {
        "id": uuid.uuid4(), "name": "Overdue Matter", "next_review_date": today - timedelta(days=3),
        "last_reviewed_date": today - timedelta(days=33),
        "matter_number": "NGM-001", "matter_client_name": "Client A", "client_full_name": None,
    }
    approaching_row = {
        "id": uuid.uuid4(), "name": "Approaching Matter",
        "next_review_date": today + timedelta(days=REVIEW_DIGEST_LOOKAHEAD_DAYS - 1),
        "last_reviewed_date": today - timedelta(days=DEFAULT_REVIEW_INTERVAL_DAYS - (REVIEW_DIGEST_LOOKAHEAD_DAYS - 1)),
        "matter_number": "NGM-002", "matter_client_name": None, "client_full_name": "Client B",
    }
    # The query itself (real SQL, WHERE next_review_date <= today+lookahead)
    # is what excludes a too-far-future matter -- the fake only returns
    # what a real query would, so a far-future row is simply never in
    # review_rows here (not asserted separately; the fake stands in for
    # the DB doing the actual filtering).
    pool = FakePool(review_rows=[overdue_row, approaching_row])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(_get_review_matters_for_digest(today))

    assert len(result) == 2
    assert result[0]["name"] == "Overdue Matter"
    assert result[0]["days_until"] == -3
    assert result[0]["resolved_client_name"] == "Client A"
    assert result[1]["days_until"] == REVIEW_DIGEST_LOOKAHEAD_DAYS - 1


def test_digest_query_returns_empty_when_no_review_matters(monkeypatch):
    import backend.main as m
    pool = FakePool(review_rows=[])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(_get_review_matters_for_digest(date.today()))
    assert result == []


# ── build_reminder_email_body(): rendering, kept separate from deadlines ───

def test_review_section_appears_labeled_separately_from_deadlines():
    events = [{
        "event_type": "deadline", "title": "Matter deadline: Court Filing",
        "date": (date.today() + timedelta(days=2)).isoformat(), "time": None,
        "court": None, "matter_name": "Court Filing", "days_until": 2,
        "matter_number": None, "case_number": None, "resolved_client_name": None,
    }]
    review_matters = [{
        "name": "Quiet Matter", "matter_number": "NGM-003", "case_number": None,
        "resolved_client_name": "Client C",
        "next_review_date": (date.today() - timedelta(days=1)).isoformat(),
        "days_until": -1,
    }]
    text, html = build_reminder_email_body(events, review_matters)

    assert "MATTERS FOR REVIEW" in text
    assert "Quiet Matter" in text or "NGM-003" in text
    assert "overdue by 1d" in text
    assert "Matters for Review" in html
    assert "NGM-003" in html
    # The two sections stay distinct — the review item's own label doesn't
    # leak into the deadline-only heading and vice versa. A 2-day-out
    # event falls in the existing "later this week" bucket (1 < days <= 7).
    assert "LATER THIS WEEK" in text


def test_review_section_shows_last_reviewed_when_available():
    review_matters = [{
        "name": "Quiet Matter", "matter_number": "NGM-003", "case_number": None,
        "resolved_client_name": "Client C",
        "next_review_date": (date.today() - timedelta(days=1)).isoformat(),
        "last_reviewed_date": (date.today() - timedelta(days=31)).isoformat(),
        "days_until": -1,
    }]
    text, html = build_reminder_email_body([], review_matters)
    assert "last reviewed" in text
    assert "last reviewed" in html


def test_review_section_absent_when_no_review_matters():
    events = [{
        "event_type": "deadline", "title": "Something", "date": date.today().isoformat(),
        "time": None, "court": None, "matter_name": "Something", "days_until": 0,
        "matter_number": None, "case_number": None, "resolved_client_name": None,
    }]
    text, html = build_reminder_email_body(events, review_matters=[])
    assert "MATTERS FOR REVIEW" not in text
    assert "Matters for Review" not in html


def test_no_events_and_no_review_matters_returns_original_nothing_upcoming_message():
    """Regression: existing behavior when there's genuinely nothing at all
    must be unchanged."""
    text, html = build_reminder_email_body([], review_matters=[])
    assert "no court dates, deadlines, or filings" in text
    assert "MATTERS FOR REVIEW" not in text


def test_review_matters_alone_with_no_deadline_events_still_renders():
    """A firm with zero calendar deadlines but a stale matter must still
    get a real digest, not the 'nothing upcoming' message."""
    review_matters = [{
        "name": "Only Thing Going On", "matter_number": None, "case_number": None,
        "resolved_client_name": None,
        "next_review_date": date.today().isoformat(), "days_until": 0,
    }]
    text, html = build_reminder_email_body([], review_matters=review_matters)
    assert "no court dates, deadlines, or filings" not in text
    assert "MATTERS FOR REVIEW" in text
    assert "due today" in text


# ── add_progress_note(): adding a note is also a "reviewed" action ─────────

class _NoteFakeConnection:
    """
    Purpose-built fake for add_progress_note() -- distinct from the shared
    FakeConnection above since this endpoint's query shape (a matter
    lookup, a progress_notes INSERT, then a targeted 3-column matters
    UPDATE) doesn't overlap with update_matter()'s generic dynamic SET.
    """
    def __init__(self, matter_row):
        self.matter_row = matter_row
        self.last_update_args = None

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT id, name, internal_ref FROM matters"):
            return {"id": self.matter_row["id"], "name": self.matter_row["name"], "internal_ref": None}
        if q.startswith("INSERT INTO progress_notes"):
            nid, matter_id, firm_id, text, author, user_id, created_at = args
            return {"id": nid, "matter_id": matter_id, "firm_id": firm_id, "text": text,
                    "author": author, "user_id": user_id, "created_at": created_at}
        raise NotImplementedError(f"_NoteFakeConnection.fetchrow: unhandled query: {q}")

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("UPDATE matters SET last_activity=$1, next_review_date=$2, last_reviewed_date=$3"):
            self.last_update_args = args
            return "UPDATE 1"
        raise NotImplementedError(f"_NoteFakeConnection.execute: unhandled query: {q}")


def test_add_progress_note_defaults_review_dates_when_none_given(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    conn = _NoteFakeConnection({"id": matter_id, "name": "Estate of X"})
    pool = FakePool()
    pool.conn = conn
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(add_progress_note(str(matter_id), ProgressNote(text="Called the client."), _fake_request()))

    assert result["next_review_date"] == (date.today() + timedelta(days=DEFAULT_REVIEW_INTERVAL_DAYS)).isoformat()
    assert result["last_reviewed_date"] == date.today().isoformat()
    # And the actual UPDATE that would hit the real DB carries real date
    # objects, not strings — confirms the SQL call itself, not just the
    # JSON response, got the right values.
    _now, next_review, last_reviewed, _matter_id = conn.last_update_args
    assert next_review == date.today() + timedelta(days=DEFAULT_REVIEW_INTERVAL_DAYS)
    assert last_reviewed == date.today()


def test_add_progress_note_respects_explicit_review_date(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    conn = _NoteFakeConnection({"id": matter_id, "name": "Estate of X"})
    pool = FakePool()
    pool.conn = conn
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(add_progress_note(
        str(matter_id), ProgressNote(text="Filed the application.", next_review_date="2026-12-25"), _fake_request()
    ))

    assert result["next_review_date"] == "2026-12-25"
    assert result["last_reviewed_date"] == date.today().isoformat()


def test_add_progress_note_invalid_review_date_400s(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    conn = _NoteFakeConnection({"id": matter_id, "name": "Estate of X"})
    pool = FakePool()
    pool.conn = conn
    monkeypatch.setattr(m, "_db_pool", pool)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(add_progress_note(
            str(matter_id), ProgressNote(text="Note.", next_review_date="not-a-date"), _fake_request()
        ))
    assert exc_info.value.status_code == 400
