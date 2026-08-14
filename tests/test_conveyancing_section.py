"""
Unit tests for the conveyancing-specific matter fields (backend/main.py,
backend/conveyancing.py): conveyancing_milestone validation, and the
_sync_conveyancing_calendar_events() helper that keeps calendar_events in
sync with a matter's three conveyancing key dates — this is what "feeding
into the existing deadline/calendar system" means concretely.

Also confirms conveyancing fields are accepted/stored on a matter
regardless of its current practice_area — the practice_area ===
'Conveyancing/Property' check that gates whether the section is *shown*
is a frontend display condition only (see frontend/index.html's
renderMatterPanel()), deliberately not enforced as a write-time
restriction here (a matter's classification can be corrected later, and
we don't want to reject or silently drop already-entered data).

Called directly as plain async functions, same convention as
tests/test_matter_client_linking.py.
"""

import asyncio
import re
import uuid
from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException

from backend.main import FIRM_ID, MatterUpdate, _sync_conveyancing_calendar_events, update_matter


class FakeConnection:
    def __init__(self, matters, calendar_events=None):
        self.matters = matters
        self.calendar_events = calendar_events if calendar_events is not None else []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

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

        if q.startswith("SELECT id FROM calendar_events WHERE matter_id=$1 AND source='conveyancing_sync' AND title=$2"):
            matter_id, title = args
            for e in self.calendar_events:
                if e["matter_id"] == matter_id and e.get("source") == "conveyancing_sync" and e["title"] == title:
                    return {"id": e["id"]}
            return None

        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT * FROM progress_notes"):
            return []
        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")

    async def execute(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("INSERT INTO calendar_events"):
            cols = [c.strip() for c in q.split("(", 1)[1].split(")", 1)[0].split(",")]
            row = dict(zip(cols, args))
            self.calendar_events.append(row)
            return "INSERT 0 1"

        if q.startswith("UPDATE calendar_events SET date=$1 WHERE id=$2"):
            date_val, event_id = args
            for e in self.calendar_events:
                if e["id"] == event_id:
                    e["date"] = date_val
            return "UPDATE 1"

        if q.startswith("DELETE FROM calendar_events WHERE id=$1"):
            event_id, = args
            self.calendar_events[:] = [e for e in self.calendar_events if e["id"] != event_id]
            return "DELETE 1"

        return "OK"


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, matters=None, calendar_events=None):
        self.conn = FakeConnection(matters if matters is not None else [], calendar_events)

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _matter_row(matter_id, practice_area=None, firm_id=FIRM_ID):
    return {
        "id": matter_id, "firm_id": firm_id, "name": "Moyo Stand 245 Borrowdale", "number": None,
        "internal_ref": None, "external_ref": None, "client_name": None, "client_id": None,
        "case_parties": None, "matter_type": None, "practice_area": practice_area, "status": "Active",
        "custom_status": None, "document_count": 0, "last_activity": None,
        "created_at": datetime.now(timezone.utc), "created_by": None,
        "next_deadline": None, "next_deadline_note": None,
        "conveyancing_milestone": None, "conveyancing_property_address": None,
        "conveyancing_title_deed_number": None, "conveyancing_purchase_price": None,
        "conveyancing_other_conveyancer_contact": None, "conveyancing_transfer_date": None,
        "conveyancing_rates_clearance_expiry": None, "conveyancing_bond_registration_deadline": None,
    }


def _fake_request():
    return None


# ── conveyancing_milestone validation ────────────────────────────────────

def test_update_matter_rejects_invalid_conveyancing_milestone(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id, practice_area="Conveyancing/Property")])
    monkeypatch.setattr(m, "_db_pool", pool)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_matter(str(matter_id), MatterUpdate(conveyancing_milestone="Made Up Stage"), _fake_request()))
    assert exc_info.value.status_code == 422


def test_update_matter_accepts_a_valid_conveyancing_milestone(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id, practice_area="Conveyancing/Property")])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_matter(
        str(matter_id), MatterUpdate(conveyancing_milestone="Rates Clearance Obtained"), _fake_request()
    ))

    assert result["conveyancing_milestone"] == "Rates Clearance Obtained"


# ── write-time: conveyancing fields not gated on practice_area ──────────

def test_conveyancing_fields_stored_even_when_practice_area_is_not_conveyancing(monkeypatch):
    """The practice_area === 'Conveyancing/Property' check is a frontend
    display condition only — writes must not be rejected or silently
    dropped just because the matter's classification is something else
    (or was changed after conveyancing data was already entered)."""
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id, practice_area="Estate/Inheritance")])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_matter(
        str(matter_id), MatterUpdate(conveyancing_property_address="12 Baines Avenue"), _fake_request()
    ))

    assert result["conveyancing_property_address"] == "12 Baines Avenue"


# ── calendar sync: create/update on the 3 key dates ──────────────────────

def test_setting_transfer_date_creates_a_calendar_event(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id, practice_area="Conveyancing/Property")])
    monkeypatch.setattr(m, "_db_pool", pool)

    asyncio.run(update_matter(
        str(matter_id), MatterUpdate(conveyancing_transfer_date="2026-12-01"), _fake_request()
    ))

    events = pool.conn.calendar_events
    assert len(events) == 1
    assert events[0]["title"] == "Conveyancing: Transfer Date"
    assert events[0]["date"] == date(2026, 12, 1)
    assert events[0]["source"] == "conveyancing_sync"
    assert events[0]["matter_id"] == matter_id


def test_setting_all_three_key_dates_creates_three_separate_events(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id, practice_area="Conveyancing/Property")])
    monkeypatch.setattr(m, "_db_pool", pool)

    asyncio.run(update_matter(str(matter_id), MatterUpdate(
        conveyancing_transfer_date="2026-12-01",
        conveyancing_rates_clearance_expiry="2026-11-01",
        conveyancing_bond_registration_deadline="2026-11-15",
    ), _fake_request()))

    titles = {e["title"] for e in pool.conn.calendar_events}
    assert titles == {
        "Conveyancing: Transfer Date",
        "Conveyancing: Rates Clearance Expiry",
        "Conveyancing: Bond Registration Deadline",
    }


def test_resaving_the_same_key_date_updates_the_existing_event_not_a_duplicate(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id, practice_area="Conveyancing/Property")])
    monkeypatch.setattr(m, "_db_pool", pool)

    asyncio.run(update_matter(str(matter_id), MatterUpdate(conveyancing_transfer_date="2026-12-01"), _fake_request()))
    asyncio.run(update_matter(str(matter_id), MatterUpdate(conveyancing_transfer_date="2026-12-15"), _fake_request()))

    events = pool.conn.calendar_events
    assert len(events) == 1  # updated in place, not duplicated
    assert events[0]["date"] == date(2026, 12, 15)


def test_updating_unrelated_field_does_not_touch_calendar_events(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id, practice_area="Conveyancing/Property")])
    monkeypatch.setattr(m, "_db_pool", pool)

    asyncio.run(update_matter(str(matter_id), MatterUpdate(conveyancing_transfer_date="2026-12-01"), _fake_request()))
    asyncio.run(update_matter(str(matter_id), MatterUpdate(conveyancing_property_address="New Address"), _fake_request()))

    assert len(pool.conn.calendar_events) == 1  # untouched by the unrelated field update


# ── _sync_conveyancing_calendar_events(): delete-on-clear (direct helper
# test — not reachable via update_matter's PATCH today, since it filters
# out None values before this is ever called, matching every other
# Optional field on MatterUpdate. Tested directly for correctness in case
# a future caller ever does send an explicit clear.) ─────────────────────

def test_sync_helper_deletes_event_when_date_is_cleared_to_none():
    matter_id = uuid.uuid4()
    existing_event_id = uuid.uuid4()
    calendar_events = [{
        "id": existing_event_id, "matter_id": matter_id, "title": "Conveyancing: Transfer Date",
        "source": "conveyancing_sync", "date": date(2026, 12, 1),
    }]
    conn = FakeConnection(matters=[], calendar_events=calendar_events)
    matter = {"id": matter_id, "firm_id": FIRM_ID, "name": "Test Matter",
              "conveyancing_transfer_date": None}

    asyncio.run(_sync_conveyancing_calendar_events(conn, matter, ["conveyancing_transfer_date"]))

    assert conn.calendar_events == []
