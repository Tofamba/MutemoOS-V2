"""
Unit tests for Progress Notes Phase 1 (backend/main.py, 2026-09-24):
an optional free-text heading alongside the note body, and removing the
UX-imposed short character limit on the body in favour of a generous
defensive maximum enforced only at the API level.

Explicitly NOT covered here, per instruction -- deferred to a later
phase: mandatory/predefined heading categories, rich text formatting,
note editing.

Same calling convention as tests/test_note_date_extraction.py (plain
async function calls, same _NoteFakeConnection shape mirrored here with
the new `heading` column threaded through the INSERT).
"""

import asyncio
import uuid

import pytest
from fastapi import HTTPException

from backend.main import (
    PROGRESS_NOTE_HEADING_MAX_LENGTH,
    PROGRESS_NOTE_TEXT_MAX_LENGTH,
    ProgressNote,
    add_progress_note,
)


def _fake_request():
    return None


class _NoteFakeConnection:
    def __init__(self, matter_row):
        self.matter_row = matter_row

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT id, name, internal_ref FROM matters"):
            return {"id": self.matter_row["id"], "name": self.matter_row["name"],
                    "internal_ref": self.matter_row.get("internal_ref")}
        if q.startswith("INSERT INTO progress_notes"):
            nid, matter_id, firm_id, text, heading, author, user_id, created_at = args
            return {"id": nid, "matter_id": matter_id, "firm_id": firm_id, "text": text,
                    "heading": heading, "author": author, "user_id": user_id, "created_at": created_at}
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


def _pool_for(matter_id, matter_name="Estate of Chikafu"):
    return FakePool(_NoteFakeConnection({"id": matter_id, "name": matter_name, "internal_ref": None}))


# The date-scan is unrelated to this feature and best-effort by design
# (see test_note_date_extraction.py) -- silence it here so a Claude call
# isn't needed for these tests to exercise the heading/length behavior.
async def _fake_no_dates(text):
    return {"dates": [], "document_summary": None}


# ── Heading round-trips ──────────────────────────────────────────────────────

def test_note_with_heading_round_trips(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", _pool_for(matter_id))
    monkeypatch.setattr(m, "_extract_dates_from_text", lambda text: {"dates": [], "document_summary": None})

    result = asyncio.run(add_progress_note(
        str(matter_id), ProgressNote(text="Called opposing counsel.", heading="Client call"), _fake_request()
    ))

    assert result["heading"] == "Client call"
    assert result["text"] == "Called opposing counsel."


def test_note_without_heading_stores_none(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", _pool_for(matter_id))
    monkeypatch.setattr(m, "_extract_dates_from_text", lambda text: {"dates": [], "document_summary": None})

    result = asyncio.run(add_progress_note(
        str(matter_id), ProgressNote(text="No heading on this one."), _fake_request()
    ))

    assert result["heading"] is None


def test_blank_heading_normalized_to_none(monkeypatch):
    """A heading of only whitespace is the same as no heading at all --
    matches the frontend's own trim-before-send behavior, enforced again
    server-side since the API is the real contract, not just the UI."""
    import backend.main as m
    matter_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", _pool_for(matter_id))
    monkeypatch.setattr(m, "_extract_dates_from_text", lambda text: {"dates": [], "document_summary": None})

    result = asyncio.run(add_progress_note(
        str(matter_id), ProgressNote(text="Whitespace heading.", heading="   "), _fake_request()
    ))

    assert result["heading"] is None


# ── Body length: no short UX limit, a generous defensive ceiling only ───────

def test_long_multi_paragraph_body_saved_in_full(monkeypatch):
    """No UX-imposed short limit -- a real long note (well under the
    defensive ceiling) must be stored and returned verbatim, not
    truncated at the API level."""
    import backend.main as m
    matter_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", _pool_for(matter_id))
    monkeypatch.setattr(m, "_extract_dates_from_text", lambda text: {"dates": [], "document_summary": None})
    long_body = "Paragraph one.\n\n" + ("Detailed background. " * 500) + "\n\nFinal paragraph."
    assert len(long_body) > 5000  # a genuinely long note, well beyond any old short-field limit

    result = asyncio.run(add_progress_note(
        str(matter_id), ProgressNote(text=long_body, heading="Long file note"), _fake_request()
    ))

    assert result["text"] == long_body
    assert len(result["text"]) == len(long_body)


def test_body_exactly_at_max_length_succeeds(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", _pool_for(matter_id))
    monkeypatch.setattr(m, "_extract_dates_from_text", lambda text: {"dates": [], "document_summary": None})
    body = "x" * PROGRESS_NOTE_TEXT_MAX_LENGTH


    result = asyncio.run(add_progress_note(str(matter_id), ProgressNote(text=body), _fake_request()))

    assert len(result["text"]) == PROGRESS_NOTE_TEXT_MAX_LENGTH


def test_body_over_max_length_rejected_with_400(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", _pool_for(matter_id))
    body = "x" * (PROGRESS_NOTE_TEXT_MAX_LENGTH + 1)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(add_progress_note(str(matter_id), ProgressNote(text=body), _fake_request()))

    assert exc_info.value.status_code == 400
    assert str(PROGRESS_NOTE_TEXT_MAX_LENGTH) in exc_info.value.detail


def test_heading_over_max_length_rejected_with_400(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", _pool_for(matter_id))
    heading = "x" * (PROGRESS_NOTE_HEADING_MAX_LENGTH + 1)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(add_progress_note(
            str(matter_id), ProgressNote(text="Body text.", heading=heading), _fake_request()
        ))

    assert exc_info.value.status_code == 400
    assert str(PROGRESS_NOTE_HEADING_MAX_LENGTH) in exc_info.value.detail


def test_heading_exactly_at_max_length_succeeds(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", _pool_for(matter_id))
    monkeypatch.setattr(m, "_extract_dates_from_text", lambda text: {"dates": [], "document_summary": None})
    heading = "x" * PROGRESS_NOTE_HEADING_MAX_LENGTH

    result = asyncio.run(add_progress_note(
        str(matter_id), ProgressNote(text="Body text.", heading=heading), _fake_request()
    ))

    assert result["heading"] == heading
