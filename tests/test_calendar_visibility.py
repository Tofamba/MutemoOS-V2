"""
Unit tests for calendar event visibility (backend/main.py): an event must
be visible only to its creator and to invited attendees who have
accepted — not to the firm at large, and not to a role (e.g. partner) just
by virtue of that role.

Before this feature, list_calendar/export_calendar_ics filtered on
firm_id alone (every user saw every event), and there was no way for an
attendee's invite to ever become "accepted" in the database at all — the
ICS Accept/Decline buttons an attendee's own mail client renders are
cosmetic RSVP headers sent to their mail server, never back to this app.
This file tests the fix: _resolve_attendee_users() stamping a user_id onto
an attendee entry that matches an existing firm user's account email,
POST /api/calendar/{id}/respond as the only way to move status off
"pending", and _calendar_visibility_clause() gating list_calendar.

Called directly as plain async functions, same convention as this repo's
other backend tests (see tests/test_docx_export.py's docstring for why).
Unlike most other test files here, several tests need a REAL (non-None)
authenticated user id to exercise the new per-user filtering — AUTH_ENABLED
is False by default so get_current_user() normally returns a synthetic
id=None dev user, so those tests monkeypatch backend.main.get_current_user
directly to a specific fake user instead of relying on the synthetic
default.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from backend.main import (
    FIRM_ID,
    Attendee,
    CalendarEvent,
    CalendarInviteResponseRequest,
    add_calendar_event,
    list_calendar,
    list_pending_calendar_invites,
    respond_to_calendar_invite,
)


class FakeConnection:
    def __init__(self, users=None, events=None):
        self.users = users if users is not None else []
        self.events = events if events is not None else []

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

        if q.startswith("SELECT * FROM calendar_events WHERE id=$1 AND firm_id=$2"):
            eid, firm_id = args
            for e in self.events:
                if e["id"] == eid and e["firm_id"] == firm_id:
                    return dict(e)
            return None

        if q.startswith("UPDATE calendar_events SET attendees=$1::jsonb WHERE id=$2"):
            attendees_json, eid = args
            for e in self.events:
                if e["id"] == eid:
                    e["attendees"] = json.loads(attendees_json)
                    return dict(e)
            return None

        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT id, email FROM users WHERE firm_id=$1"):
            firm_id, emails = args
            return [{"id": u["id"], "email": u["email"]} for u in self.users
                    if u["firm_id"] == firm_id and (u.get("email") or "").lower() in emails]

        if "jsonb_array_elements(attendees) att" in q and "status' = 'accepted'" in q:
            # the creator-or-accepted-attendee visibility query
            firm_id, user_id = args
            user_id_str = str(user_id)
            out = []
            for e in self.events:
                if e["firm_id"] != firm_id:
                    continue
                if e.get("created_by") == user_id:
                    out.append(e)
                    continue
                if self._attendee_matches(e, user_id_str, "accepted"):
                    out.append(e)
            return [dict(x) for x in out]

        if "jsonb_array_elements(attendees) att" in q and "status' = 'pending'" in q:
            # the pending-invites query
            firm_id, user_id_str = args
            out = [e for e in self.events if e["firm_id"] == firm_id
                   and self._attendee_matches(e, user_id_str, "pending")]
            return [dict(x) for x in out]

        if q.startswith("SELECT * FROM calendar_events WHERE firm_id=$1 ORDER BY"):
            # old unscoped query — the synthetic AUTH_ENABLED=False dev-user fallback
            firm_id, = args
            return [dict(e) for e in self.events if e["firm_id"] == firm_id]

        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")

    @staticmethod
    def _attendee_matches(event, user_id_str, status):
        attendees = event.get("attendees") or []
        if isinstance(attendees, str):
            attendees = json.loads(attendees)
        return any(a.get("user_id") == user_id_str and a.get("status") == status for a in attendees)

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
    def __init__(self, users=None, events=None):
        self.conn = FakeConnection(users, events)

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _user(display_name, email, role="associate"):
    return {"id": uuid.uuid4(), "firm_id": FIRM_ID, "email": email, "role": role, "display_name": display_name}


def _as_current_user(monkeypatch, m, user_dict):
    """Makes get_current_user() return this exact dict for the duration of
    the test — needed because AUTH_ENABLED=False's synthetic default has no
    real id/email, and these tests are specifically about per-user scoping."""
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def _fake_request():
    return None


# ── creator sees their own event ─────────────────────────────────────────

def test_creator_sees_their_own_event(monkeypatch):
    import backend.main as m
    creator = _user("Alice Partner", "alice@sm.co.zw", role="partner")
    pool = FakePool(users=[creator])
    monkeypatch.setattr(m, "_db_pool", pool)

    _as_current_user(monkeypatch, m, creator)
    asyncio.run(add_calendar_event(
        CalendarEvent(title="Court Date", event_type="court_date", date="2026-08-10"),
        background_tasks=_NullTasks(), request=_fake_request(),
    ))

    results = asyncio.run(list_calendar(_fake_request()))
    assert len(results) == 1
    assert results[0]["title"] == "Court Date"


# ── non-invited user does not see it ─────────────────────────────────────

def test_non_invited_user_does_not_see_it(monkeypatch):
    import backend.main as m
    creator = _user("Alice Partner", "alice@sm.co.zw", role="partner")
    bystander = _user("Bob Associate", "bob@sm.co.zw")
    pool = FakePool(users=[creator, bystander])
    monkeypatch.setattr(m, "_db_pool", pool)

    _as_current_user(monkeypatch, m, creator)
    asyncio.run(add_calendar_event(
        CalendarEvent(title="Court Date", event_type="court_date", date="2026-08-10"),
        background_tasks=_NullTasks(), request=_fake_request(),
    ))

    _as_current_user(monkeypatch, m, bystander)
    results = asyncio.run(list_calendar(_fake_request()))
    assert results == []


# ── invited-but-not-yet-accepted user does not see it as confirmed ───────

def test_invited_not_yet_accepted_user_does_not_see_it(monkeypatch):
    import backend.main as m
    creator = _user("Alice Partner", "alice@sm.co.zw", role="partner")
    invitee = _user("Bob Associate", "bob@sm.co.zw")
    pool = FakePool(users=[creator, invitee])
    monkeypatch.setattr(m, "_db_pool", pool)

    _as_current_user(monkeypatch, m, creator)
    asyncio.run(add_calendar_event(
        CalendarEvent(title="Court Date", event_type="court_date", date="2026-08-10",
                      attendees=[Attendee(email="bob@sm.co.zw", name="Bob")]),
        background_tasks=_NullTasks(), request=_fake_request(),
    ))

    _as_current_user(monkeypatch, m, invitee)
    results = asyncio.run(list_calendar(_fake_request()))
    assert results == []

    # It shows up as a pending invite instead, so it isn't just unreachable.
    pending = asyncio.run(list_pending_calendar_invites(_fake_request()))
    assert len(pending) == 1
    assert pending[0]["title"] == "Court Date"


# ── invited-and-accepted user does see it ────────────────────────────────

def test_invited_and_accepted_user_sees_it(monkeypatch):
    import backend.main as m
    creator = _user("Alice Partner", "alice@sm.co.zw", role="partner")
    invitee = _user("Bob Associate", "bob@sm.co.zw")
    pool = FakePool(users=[creator, invitee])
    monkeypatch.setattr(m, "_db_pool", pool)

    _as_current_user(monkeypatch, m, creator)
    created = asyncio.run(add_calendar_event(
        CalendarEvent(title="Court Date", event_type="court_date", date="2026-08-10",
                      attendees=[Attendee(email="bob@sm.co.zw", name="Bob")]),
        background_tasks=_NullTasks(), request=_fake_request(),
    ))
    assert created["attendees"][0]["user_id"] == str(invitee["id"])
    assert created["attendees"][0]["status"] == "pending"

    _as_current_user(monkeypatch, m, invitee)
    asyncio.run(respond_to_calendar_invite(
        created["id"], CalendarInviteResponseRequest(status="accepted"), _fake_request(),
    ))

    results = asyncio.run(list_calendar(_fake_request()))
    assert len(results) == 1
    assert results[0]["title"] == "Court Date"

    # No longer a pending invite once accepted.
    pending = asyncio.run(list_pending_calendar_invites(_fake_request()))
    assert pending == []


def test_declined_invite_never_appears_on_calendar(monkeypatch):
    import backend.main as m
    creator = _user("Alice Partner", "alice@sm.co.zw", role="partner")
    invitee = _user("Bob Associate", "bob@sm.co.zw")
    pool = FakePool(users=[creator, invitee])
    monkeypatch.setattr(m, "_db_pool", pool)

    _as_current_user(monkeypatch, m, creator)
    created = asyncio.run(add_calendar_event(
        CalendarEvent(title="Court Date", event_type="court_date", date="2026-08-10",
                      attendees=[Attendee(email="bob@sm.co.zw", name="Bob")]),
        background_tasks=_NullTasks(), request=_fake_request(),
    ))

    _as_current_user(monkeypatch, m, invitee)
    asyncio.run(respond_to_calendar_invite(
        created["id"], CalendarInviteResponseRequest(status="declined"), _fake_request(),
    ))

    assert asyncio.run(list_calendar(_fake_request())) == []
    assert asyncio.run(list_pending_calendar_invites(_fake_request())) == []


# ── external attendee (not a firm user) is unaffected ────────────────────

def test_external_attendee_email_gets_no_user_id_or_status(monkeypatch):
    import backend.main as m
    creator = _user("Alice Partner", "alice@sm.co.zw", role="partner")
    pool = FakePool(users=[creator])
    monkeypatch.setattr(m, "_db_pool", pool)

    _as_current_user(monkeypatch, m, creator)
    created = asyncio.run(add_calendar_event(
        CalendarEvent(title="Settlement Meeting", event_type="meeting", date="2026-08-10",
                      attendees=[Attendee(email="opposing.counsel@otherfirm.co.zw", name="Opposing Counsel")]),
        background_tasks=_NullTasks(), request=_fake_request(),
    ))
    assert created["attendees"][0]["user_id"] is None
    assert created["attendees"][0]["status"] is None


# ── respond_to_calendar_invite guards ────────────────────────────────────

def test_respond_rejects_user_not_an_attendee(monkeypatch):
    import backend.main as m
    creator = _user("Alice Partner", "alice@sm.co.zw", role="partner")
    stranger = _user("Carol Secretary", "carol@sm.co.zw", role="secretary")
    pool = FakePool(users=[creator, stranger])
    monkeypatch.setattr(m, "_db_pool", pool)

    _as_current_user(monkeypatch, m, creator)
    created = asyncio.run(add_calendar_event(
        CalendarEvent(title="Court Date", event_type="court_date", date="2026-08-10"),
        background_tasks=_NullTasks(), request=_fake_request(),
    ))

    _as_current_user(monkeypatch, m, stranger)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(respond_to_calendar_invite(
            created["id"], CalendarInviteResponseRequest(status="accepted"), _fake_request(),
        ))
    assert exc_info.value.status_code == 403


def test_respond_rejects_invalid_status(monkeypatch):
    import backend.main as m
    creator = _user("Alice Partner", "alice@sm.co.zw", role="partner")
    invitee = _user("Bob Associate", "bob@sm.co.zw")
    pool = FakePool(users=[creator, invitee])
    monkeypatch.setattr(m, "_db_pool", pool)

    _as_current_user(monkeypatch, m, creator)
    created = asyncio.run(add_calendar_event(
        CalendarEvent(title="Court Date", event_type="court_date", date="2026-08-10",
                      attendees=[Attendee(email="bob@sm.co.zw", name="Bob")]),
        background_tasks=_NullTasks(), request=_fake_request(),
    ))

    _as_current_user(monkeypatch, m, invitee)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(respond_to_calendar_invite(
            created["id"], CalendarInviteResponseRequest(status="maybe"), _fake_request(),
        ))
    assert exc_info.value.status_code == 422


class _NullTasks:
    """Stand-in for FastAPI's BackgroundTasks — no email sending is under
    test here, so just swallow add_task() calls."""
    def add_task(self, *args, **kwargs):
        pass
