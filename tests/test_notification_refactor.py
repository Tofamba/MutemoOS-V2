"""
Unit tests for the shared notification primitive extraction (backend/main.py,
2026-09-13):

  send_user_notification(user_id, subject, body, link=None, html_body=None,
                          ics_content=None)
    -- the generic "tell this user something" primitive extracted from the
    deadline reminder engine's own inline sending logic. No calendar/
    deadline/compliance concept lives in it: resolve user_id's effective
    email (recipient_email override, else account email -- the same
    fallback every per-user email in this app already used), send via
    the same Resend integration everything else goes through.

  send_reminder_email(user_id, events, test=False, review_matters=None)
    -- REFACTORED, not rewritten. build_reminder_email_body()/the subject
    logic/build_ics() are untouched; only the final send step changed,
    from calling _send_via_resend_sync directly to calling
    send_user_notification(). test_reminder_email_produces_identical_
    content_through_the_shared_primitive below proves the exact subject/
    text/html/ics that reach the shared primitive are byte-identical to
    what the pre-refactor code would have sent directly.

  The compliance-agent notification trigger inside _run_ai_action_queue_
  scan() -- fires on escalation-level transition (draft/partner_escalation
  only, never flag), calls the SAME send_user_notification() primitive,
  not a second implementation.

Same FakeConnection/FakePool convention as tests/test_ai_action_queue.py
and tests/test_compliance_exceptions.py.
"""

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.main import (
    FIRM_ID,
    send_user_notification,
    send_reminder_email,
    build_reminder_email_body,
    build_ics,
    _run_ai_action_queue_scan,
)


class FakeConnection:
    def __init__(self, users=None, user_reminder_settings=None,
                 clients=None, compliance_exceptions=None, firm=None, ai_action_queue=None):
        self.users = users or []
        self.user_reminder_settings = user_reminder_settings or []
        # ai_action_queue-scan fixtures, same shape as test_ai_action_queue.py
        self.clients = clients or []
        self.compliance_exceptions = compliance_exceptions or []
        self.firm = firm if firm is not None else {"id": FIRM_ID, "escalation_config": None,
                                                     "ai_action_queue_last_run_date": None}
        self.ai_action_queue = ai_action_queue or []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT urs.recipient_email, u.email AS account_email"):
            (uid,) = args
            user = next((u for u in self.users if u["id"] == uid), None)
            if not user:
                return None
            settings = next((s for s in self.user_reminder_settings if s["user_id"] == uid), None)
            return {
                "recipient_email": settings["recipient_email"] if settings else None,
                "account_email": user.get("email"),
            }

        if q.startswith("SELECT escalation_config FROM firms WHERE id=$1"):
            return {"escalation_config": self.firm.get("escalation_config")}

        if q.startswith("SELECT full_name FROM clients WHERE id=$1 AND firm_id=$2"):
            cid, firm_id = args
            for c in self.clients:
                if c["id"] == cid and c["firm_id"] == firm_id:
                    return {"full_name": c["full_name"]}
            return None

        if q.startswith("SELECT id FROM ai_action_queue WHERE compliance_exception_id=$1"):
            eid, action_type, level = args
            for a in self.ai_action_queue:
                if (a["compliance_exception_id"] == eid and a["action_type"] == action_type
                        and a["escalation_level"] == level and a.get("superseded_by") is None):
                    return {"id": a["id"]}
            return None

        if q.startswith("INSERT INTO ai_action_queue"):
            (firm_id, eid, cid, action_type, level, recommended_action, draft_text,
             draft_grounding_payload) = args
            row = {
                "id": uuid.uuid4(), "firm_id": firm_id, "compliance_exception_id": eid, "client_id": cid,
                "action_type": action_type, "escalation_level": level,
                "recommended_action": recommended_action, "draft_text": draft_text,
                "draft_grounding_payload": draft_grounding_payload, "status": "pending_review",
                "superseded_by": None, "created_at": datetime.now(timezone.utc),
                "reviewed_at": None, "reviewed_by": None,
            }
            self.ai_action_queue.append(row)
            return dict(row)

        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT * FROM compliance_exceptions WHERE firm_id=$1 AND status IN"):
            (firm_id,) = args
            statuses = {"Open", "InProgress", "AwaitingClient"}
            return [dict(e) for e in self.compliance_exceptions
                    if e["firm_id"] == firm_id and e["status"] in statuses]
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


def _user_row(user_id=None, **overrides):
    row = {"id": user_id or uuid.uuid4(), "firm_id": FIRM_ID, "email": "account@example.com"}
    row.update(overrides)
    return row


def _client_row(client_id=None, **overrides):
    row = {"id": client_id or uuid.uuid4(), "firm_id": FIRM_ID, "full_name": "Test Client"}
    row.update(overrides)
    return row


def _exception_row(client_id, issue_code, created_days_ago=0, due_date=None, status="Open", **overrides):
    row = {
        "id": uuid.uuid4(), "firm_id": FIRM_ID, "client_id": client_id,
        "issue_code": issue_code, "issue_label": issue_code.replace("_", " ").title(),
        "status": status, "due_date": due_date, "responsible_user_id": None,
        "created_at": datetime.now(timezone.utc) - timedelta(days=created_days_ago),
        "updated_at": datetime.now(timezone.utc),
    }
    row.update(overrides)
    return row


def _capture_resend_calls(monkeypatch, m):
    """Patches _send_via_resend_sync (the one real network call, several
    layers below send_user_notification) and returns the list it records
    into -- (to, subject, html_body, text_body, ics_content) tuples."""
    calls = []
    def fake_send(to, subject, html_body, text_body, ics_content=None):
        calls.append((to, subject, html_body, text_body, ics_content))
    monkeypatch.setattr(m, "_send_via_resend_sync", fake_send)
    return calls


# ── send_user_notification() -- the generic primitive itself ──────────────

def test_uses_recipient_email_override_when_set(monkeypatch):
    import backend.main as m
    uid = uuid.uuid4()
    user = _user_row(uid, email="account@example.com")
    settings = {"user_id": uid, "recipient_email": "override@example.com"}
    monkeypatch.setattr(m, "_db_pool", FakePool(users=[user], user_reminder_settings=[settings]))
    calls = _capture_resend_calls(monkeypatch, m)

    sent = asyncio.run(send_user_notification(uid, "Subject", "Body text"))

    assert sent is True
    assert len(calls) == 1
    assert calls[0][0] == "override@example.com"


def test_falls_back_to_account_email_when_no_override(monkeypatch):
    import backend.main as m
    uid = uuid.uuid4()
    user = _user_row(uid, email="account@example.com")
    monkeypatch.setattr(m, "_db_pool", FakePool(users=[user]))  # no user_reminder_settings row at all
    calls = _capture_resend_calls(monkeypatch, m)

    sent = asyncio.run(send_user_notification(uid, "Subject", "Body text"))

    assert sent is True
    assert calls[0][0] == "account@example.com"


def test_returns_false_when_user_has_no_email_at_all(monkeypatch):
    import backend.main as m
    uid = uuid.uuid4()
    user = _user_row(uid, email=None)
    monkeypatch.setattr(m, "_db_pool", FakePool(users=[user]))
    calls = _capture_resend_calls(monkeypatch, m)

    sent = asyncio.run(send_user_notification(uid, "Subject", "Body text"))

    assert sent is False
    assert calls == []


def test_returns_false_for_unknown_user_id(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool(users=[]))
    sent = asyncio.run(send_user_notification(uuid.uuid4(), "Subject", "Body text"))
    assert sent is False


def test_returns_false_when_db_pool_is_unavailable(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", None)
    sent = asyncio.run(send_user_notification(uuid.uuid4(), "Subject", "Body text"))
    assert sent is False


def test_link_is_appended_to_both_text_and_html_bodies(monkeypatch):
    import backend.main as m
    uid = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(users=[_user_row(uid)]))
    calls = _capture_resend_calls(monkeypatch, m)

    asyncio.run(send_user_notification(uid, "Subject", "Body text", link="https://example.com/x"))

    _, _, html_body, text_body, _ = calls[0]
    assert "https://example.com/x" in text_body
    assert "https://example.com/x" in html_body


def test_no_link_means_no_link_text(monkeypatch):
    import backend.main as m
    uid = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(users=[_user_row(uid)]))
    calls = _capture_resend_calls(monkeypatch, m)

    asyncio.run(send_user_notification(uid, "Subject", "Body text"))

    _, _, html_body, text_body, _ = calls[0]
    assert "View" not in text_body
    assert "<a href" not in html_body


def test_custom_html_body_override_is_used_verbatim(monkeypatch):
    """send_reminder_email's whole reason for needing this parameter:
    it already has a richer HTML body than the generic default wrapper
    would produce, and that must survive unchanged."""
    import backend.main as m
    uid = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(users=[_user_row(uid)]))
    calls = _capture_resend_calls(monkeypatch, m)

    asyncio.run(send_user_notification(uid, "Subject", "plain text version",
                                        html_body="<div>rich custom html</div>"))

    _, _, html_body, text_body, _ = calls[0]
    assert html_body == "<div>rich custom html</div>"
    assert text_body == "plain text version"


def test_ics_content_passes_through_to_the_real_send(monkeypatch):
    import backend.main as m
    uid = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(users=[_user_row(uid)]))
    calls = _capture_resend_calls(monkeypatch, m)

    asyncio.run(send_user_notification(uid, "Subject", "Body", ics_content="BEGIN:VCALENDAR..."))

    assert calls[0][4] == "BEGIN:VCALENDAR..."


def test_send_failure_is_caught_and_returns_false(monkeypatch):
    import backend.main as m
    uid = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(users=[_user_row(uid)]))
    def raising_send(*a, **kw):
        raise RuntimeError("Resend API error 500")
    monkeypatch.setattr(m, "_send_via_resend_sync", raising_send)

    sent = asyncio.run(send_user_notification(uid, "Subject", "Body"))
    assert sent is False


# ── send_reminder_email() -- refactor produces identical content ──────────

def _sample_event(**overrides):
    e = {
        "event_type": "hearing", "title": "Application hearing", "date": date.today().isoformat(),
        "time": "10:00", "court": "Harare High Court", "matter_name": None, "matter_number": "NGM-001-01",
        "case_number": None, "resolved_client_name": "Test Client", "days_until": 2,
    }
    e.update(overrides)
    return e


def test_reminder_email_produces_identical_content_through_the_shared_primitive(monkeypatch):
    """The core 'pure refactor' proof: call send_reminder_email() and
    capture exactly what reaches send_user_notification() (subject,
    text body, html body, ics content) -- then compute the SAME four
    things by calling build_reminder_email_body()/build_ics() directly,
    completely independent of send_reminder_email()'s own internals.
    They must match exactly -- if the refactor changed a single
    character of subject/body/html/ics, this catches it."""
    import backend.main as m
    uid = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(users=[_user_row(uid, email="lawyer@example.com")]))
    captured = {}

    async def fake_notify(user_id, subject, body, link=None, html_body=None, ics_content=None):
        captured.update(user_id=user_id, subject=subject, body=body, html_body=html_body, ics_content=ics_content)
        return True
    monkeypatch.setattr(m, "send_user_notification", fake_notify)

    events = [_sample_event()]
    review_matters = [{"id": uuid.uuid4(), "name": "Some Matter", "next_review_date": "2026-09-01",
                        "days_until": -5, "last_reviewed_date": None, "matter_number": "AB-001-01"}]

    sent = asyncio.run(send_reminder_email(uid, events, review_matters=review_matters))
    assert sent is True

    expected_text, expected_html = build_reminder_email_body(events, review_matters)
    expected_ics = build_ics(events)
    expected_subject = "⚖ Mutemo Desk — Daily reminder (1 upcoming)"  # not days_until==0, so this branch

    assert captured["user_id"] == uid
    assert captured["subject"] == expected_subject
    assert captured["body"] == expected_text
    assert captured["html_body"] == expected_html
    assert captured["ics_content"] == expected_ics


def test_reminder_email_today_subject_unchanged(monkeypatch):
    import backend.main as m
    uid = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(users=[_user_row(uid)]))
    captured = {}
    async def fake_notify(user_id, subject, body, link=None, html_body=None, ics_content=None):
        captured["subject"] = subject
        return True
    monkeypatch.setattr(m, "send_user_notification", fake_notify)

    events = [_sample_event(days_until=0)]
    asyncio.run(send_reminder_email(uid, events))

    assert captured["subject"] == "⚖ Mutemo Desk — Court date TODAY + upcoming"


def test_reminder_email_test_flag_still_prefixes_subject_and_banner(monkeypatch):
    import backend.main as m
    uid = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(users=[_user_row(uid)]))
    captured = {}
    async def fake_notify(user_id, subject, body, link=None, html_body=None, ics_content=None):
        captured.update(subject=subject, body=body, html_body=html_body)
        return True
    monkeypatch.setattr(m, "send_user_notification", fake_notify)

    events = [_sample_event(days_until=0)]
    asyncio.run(send_reminder_email(uid, events, test=True))

    assert captured["subject"].startswith("[TEST] ")
    assert captured["body"].startswith("[TEST EMAIL]")
    assert "This is a test email" in captured["html_body"]


def test_reminder_email_nothing_upcoming_subject(monkeypatch):
    import backend.main as m
    uid = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(users=[_user_row(uid)]))
    captured = {}
    async def fake_notify(user_id, subject, body, link=None, html_body=None, ics_content=None):
        captured["subject"] = subject
        return True
    monkeypatch.setattr(m, "send_user_notification", fake_notify)

    asyncio.run(send_reminder_email(uid, []))
    assert captured["subject"] == "⚖ Mutemo Desk — Daily reminder (nothing upcoming)"


def test_reminder_email_propagates_send_user_notification_result(monkeypatch):
    """send_reminder_email()'s return value IS send_user_notification()'s
    return value now -- no separate try/except duplicating that contract."""
    import backend.main as m
    uid = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(users=[_user_row(uid)]))
    async def fake_notify(*a, **kw):
        return False
    monkeypatch.setattr(m, "send_user_notification", fake_notify)

    sent = asyncio.run(send_reminder_email(uid, [_sample_event()]))
    assert sent is False


# ── Compliance-agent notification trigger ──────────────────────────────────

def test_flag_level_never_notifies(monkeypatch):
    import backend.main as m
    client = _client_row()
    exc = _exception_row(client["id"], "CDD_REVIEW_OUTSTANDING", created_days_ago=3,
                          responsible_user_id=uuid.uuid4())  # flag threshold is 3; has a responsible person
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance_exceptions=[exc]))
    calls = []
    async def fake_notify(*a, **kw):
        calls.append((a, kw))
        return True
    monkeypatch.setattr(m, "send_user_notification", fake_notify)

    created = asyncio.run(_run_ai_action_queue_scan())

    assert len(created) == 1 and created[0]["action_type"] == "flag"
    assert calls == []  # flag is deliberately silent


def test_draft_level_notifies_the_responsible_user(monkeypatch):
    import backend.main as m
    client = _client_row(full_name="Anchorflow Holdings")
    responsible = uuid.uuid4()
    exc = _exception_row(client["id"], "CDD_REVIEW_OUTSTANDING", created_days_ago=7,  # crosses flag AND draft
                          responsible_user_id=responsible)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance_exceptions=[exc]))
    calls = []
    async def fake_notify(user_id, subject, body, **kw):
        calls.append((user_id, subject, body))
        return True
    monkeypatch.setattr(m, "send_user_notification", fake_notify)

    created = asyncio.run(_run_ai_action_queue_scan())

    assert sorted(a["action_type"] for a in created) == ["draft", "flag"]
    assert len(calls) == 1  # only the draft-level creation notified, not the flag one
    user_id, subject, body = calls[0]
    assert user_id == responsible
    assert "Draft" in subject
    assert "Anchorflow Holdings" in subject
    assert "Anchorflow Holdings" in body


def test_partner_escalation_notifies_with_correct_label(monkeypatch):
    import backend.main as m
    client = _client_row(full_name="Mould Enterprises")
    responsible = uuid.uuid4()
    exc = _exception_row(client["id"], "CDD_REVIEW_OUTSTANDING", created_days_ago=20,  # crosses all 3 stages
                          responsible_user_id=responsible)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance_exceptions=[exc]))
    calls = []
    async def fake_notify(user_id, subject, body, **kw):
        calls.append((user_id, subject, body))
        return True
    monkeypatch.setattr(m, "send_user_notification", fake_notify)

    created = asyncio.run(_run_ai_action_queue_scan())

    assert sorted(a["action_type"] for a in created) == ["draft", "flag", "partner_escalation"]
    assert len(calls) == 2  # draft + partner_escalation notify, flag doesn't
    subjects = [c[1] for c in calls]
    assert any("Draft" in s for s in subjects)
    assert any("Partner Escalation" in s for s in subjects)


def test_no_responsible_user_id_skips_notification_without_crashing(monkeypatch):
    import backend.main as m
    client = _client_row()
    exc = _exception_row(client["id"], "CDD_REVIEW_OUTSTANDING", created_days_ago=7,
                          responsible_user_id=None)  # explicitly unset
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance_exceptions=[exc]))
    calls = []
    async def fake_notify(*a, **kw):
        calls.append((a, kw))
        return True
    monkeypatch.setattr(m, "send_user_notification", fake_notify)

    created = asyncio.run(_run_ai_action_queue_scan())  # must not raise

    assert len(created) == 2  # flag + draft rows still created
    assert calls == []  # nothing to notify with


def test_notification_failure_does_not_prevent_row_creation(monkeypatch):
    """A notification-send exception must never roll back or block the
    ai_action_queue row itself -- the row is the durable state; the
    notification is a best-effort side effect."""
    import backend.main as m
    client = _client_row()
    exc = _exception_row(client["id"], "CDD_REVIEW_OUTSTANDING", created_days_ago=7,
                          responsible_user_id=uuid.uuid4())
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance_exceptions=[exc]))
    async def failing_notify(*a, **kw):
        raise RuntimeError("Resend is down")
    monkeypatch.setattr(m, "send_user_notification", failing_notify)

    created = asyncio.run(_run_ai_action_queue_scan())  # must not raise

    assert sorted(a["action_type"] for a in created) == ["draft", "flag"]
