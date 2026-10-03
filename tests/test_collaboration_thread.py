"""
Unit tests for the Matter Collaboration discussion thread (backend/main.py,
2026-10-02, stage 3). This is the feature's real security boundary: one thread
per matter, readable and writable ONLY by the current responsible lawyer and
active (not revoked, not expired) collaborators -- checked against the
database on every request, so a revoked collaborator loses read access
immediately and no role (including partner/admin) grants access by itself.

Plain async calls against a hand-built fake DB, same convention as
tests/test_matter_collaboration.py.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from backend.main import (
    COLLABORATION_MESSAGE_MAX_LENGTH, FIRM_ID, CollaborationMessageBody,
    list_collaboration_messages, post_collaboration_message,
)

T0 = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)


class FakeConnection:
    def __init__(self, matters, users):
        self.matters, self.users = matters, users
        self.collabs, self.messages, self.notifications, self.executed = [], [], [], []
        self._tick = 0

    def _active(self, c):
        now = datetime.now(timezone.utc)
        return c["revoked_at"] is None and (c["expires_at"] is None or c["expires_at"] > now)

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT id, name, matter_number, responsible_lawyer_id FROM matters"):
            return next((m for m in self.matters if m["id"] == args[0] and m["firm_id"] == args[1]), None)
        if q.startswith("SELECT 1 AS ok FROM matter_collaborators"):
            mid, firm, uid = args
            hit = any(c["matter_id"] == mid and c["firm_id"] == firm and c["user_id"] == uid and self._active(c)
                      for c in self.collabs)
            return {"ok": 1} if hit else None
        if q.startswith("INSERT INTO matter_collaboration_messages"):
            firm, mid, author, content = args
            self._tick += 1
            row = {"id": uuid.uuid4(), "firm_id": firm, "matter_id": mid, "author_id": author,
                   "content": content, "created_at": T0 + timedelta(minutes=self._tick)}
            self.messages.append(row)
            return {k: row[k] for k in ("id", "author_id", "content", "created_at")}
        raise NotImplementedError(q)

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT msg.id, msg.author_id, msg.content, msg.created_at"):
            mid, firm, limit = args
            rows = [m for m in self.messages if m["matter_id"] == mid and m["firm_id"] == firm]
            rows.sort(key=lambda m: m["created_at"], reverse=True)
            out = []
            for m in rows[:limit]:
                u = next((u for u in self.users if u["id"] == m["author_id"]), None)
                out.append({**m, "author_name": u["display_name"] if u else None})
            return out
        if q.startswith("SELECT user_id FROM matter_collaborators WHERE matter_id=$1"):
            mid, firm = args
            return [{"user_id": c["user_id"]} for c in self.collabs
                    if c["matter_id"] == mid and c["firm_id"] == firm and self._active(c)]
        if q.startswith("SELECT id FROM users WHERE firm_id=$1 AND is_active=TRUE AND id = ANY"):
            firm, ids = args
            return [{"id": u["id"]} for u in self.users if u["firm_id"] == firm and u["is_active"] and u["id"] in ids]
        if q.startswith("SELECT DISTINCT user_id FROM user_notifications"):
            firm, mid, ids = args
            return [{"user_id": u} for u in {n["user_id"] for n in self.notifications
                    if n["matter_id"] == mid and n["kind"] == "collaboration_message"
                    and n["read_at"] is None and n["user_id"] in ids}]
        raise NotImplementedError(q)

    async def execute(self, query, *args):
        q = " ".join(query.split())
        self.executed.append((q, args))
        if q.startswith("INSERT INTO user_notifications"):
            firm, uid, kind, title, body, mid = args
            self.notifications.append({"user_id": uid, "kind": kind, "title": title, "body": body,
                                       "matter_id": mid, "read_at": None})
        elif q.startswith("UPDATE user_notifications SET read_at=NOW()"):
            uid, firm, mid = args
            for n in self.notifications:
                if (n["user_id"] == uid and n["matter_id"] == mid and n["read_at"] is None
                        and n["kind"] in ("collaboration_message", "matter_collaboration_invited")):
                    n["read_at"] = datetime.now(timezone.utc)
        return "OK"


class _Ctx:
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
        return _Ctx(self.conn)


def _user(name, role="associate", active=True):
    return {"id": uuid.uuid4(), "firm_id": FIRM_ID, "display_name": name, "role": role, "is_active": active}


def _collab(matter, user, **kw):
    row = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "matter_id": matter["id"], "user_id": user["id"],
           "revoked_at": None, "expires_at": None}
    row.update(kw)
    return row


def _setup(monkeypatch):
    import backend.main as m
    owner, ralph = _user("Owner Lawyer"), _user("Ralph Labour")
    partner, admin = _user("Big Partner", "partner"), _user("Firm Admin", "admin")
    matter = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "name": "Moyo v Dube", "matter_number": "DU-009-01",
              "responsible_lawyer_id": owner["id"]}
    other_matter = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "name": "Other", "matter_number": None,
                    "responsible_lawyer_id": owner["id"]}
    conn = FakeConnection([matter, other_matter], [owner, ralph, partner, admin])
    conn.collabs.append(_collab(matter, ralph))
    monkeypatch.setattr(m, "_db_pool", FakePool(conn))
    emails = []

    async def fake_send(user_id, subject, body, **kw):
        emails.append((user_id, subject, body))
        return True
    monkeypatch.setattr(m, "send_user_notification", fake_send)
    return m, conn, matter, other_matter, owner, ralph, partner, admin, emails


def _as(monkeypatch, m, user):
    async def fake(request):
        return {"id": user["id"], "firm_id": FIRM_ID, "role": user["role"], "display_name": user["display_name"]}
    monkeypatch.setattr(m, "get_current_user", fake)


def _read(matter):
    return list_collaboration_messages(str(matter["id"]), None)


def _post(matter, text):
    return post_collaboration_message(str(matter["id"]), CollaborationMessageBody(content=text), None)


# ── who can read / write ─────────────────────────────────────────────────────

def test_responsible_lawyer_and_collaborator_can_exchange_messages(monkeypatch):
    m, conn, matter, _, owner, ralph, partner, admin, emails = _setup(monkeypatch)

    _as(monkeypatch, m, owner)
    first = asyncio.run(_post(matter, "Have you considered the prescription issue in Smith v Jones?"))
    _as(monkeypatch, m, ralph)
    asyncio.run(_post(matter, "Yes - s 15 applies; see the 2019 judgment."))

    view = asyncio.run(_read(matter))["messages"]
    assert [x["content"] for x in view] == [
        "Yes - s 15 applies; see the 2019 judgment.",
        "Have you considered the prescription issue in Smith v Jones?",
    ]  # newest first, matching the Activity feed
    assert view[0]["author_name"] == "Ralph Labour" and view[0]["mine"] is True
    assert view[1]["author_name"] == "Owner Lawyer" and view[1]["mine"] is False
    assert view[0]["created_at"] > view[1]["created_at"]
    assert first["author_name"] == "Owner Lawyer" and first["mine"] is True


@pytest.mark.parametrize("who", ["partner", "admin"])
def test_a_non_participant_with_broad_role_cannot_read_or_post(monkeypatch, who):
    m, conn, matter, _, owner, ralph, partner, admin, emails = _setup(monkeypatch)
    _as(monkeypatch, m, owner)
    asyncio.run(_post(matter, "private conferral"))
    intruder = partner if who == "partner" else admin
    _as(monkeypatch, m, intruder)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_read(matter))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_post(matter, "let me in"))
    assert exc.value.status_code == 403
    assert len(conn.messages) == 1  # nothing written by the intruder


def test_a_revoked_collaborator_loses_read_and_write_immediately(monkeypatch):
    m, conn, matter, _, owner, ralph, partner, admin, emails = _setup(monkeypatch)
    _as(monkeypatch, m, ralph)
    asyncio.run(_post(matter, "before revoke"))
    assert len(asyncio.run(_read(matter))["messages"]) == 1

    conn.collabs[0]["revoked_at"] = datetime.now(timezone.utc)  # responsible lawyer revokes

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_read(matter))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_post(matter, "still here?"))
    assert exc.value.status_code == 403


def test_an_expired_collaboration_grants_no_access(monkeypatch):
    m, conn, matter, _, owner, ralph, partner, admin, emails = _setup(monkeypatch)
    conn.collabs[0]["expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
    _as(monkeypatch, m, ralph)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_read(matter))
    assert exc.value.status_code == 403


def test_the_responsible_lawyer_always_has_access_even_with_no_collaborators(monkeypatch):
    m, conn, matter, _, owner, ralph, partner, admin, emails = _setup(monkeypatch)
    conn.collabs.clear()
    _as(monkeypatch, m, owner)
    asyncio.run(_post(matter, "thinking out loud"))
    assert len(asyncio.run(_read(matter))["messages"]) == 1


def test_access_follows_the_current_responsible_lawyer_after_reassignment(monkeypatch):
    m, conn, matter, _, owner, ralph, partner, admin, emails = _setup(monkeypatch)
    _as(monkeypatch, m, owner)
    asyncio.run(_post(matter, "history"))

    matter["responsible_lawyer_id"] = partner["id"]  # reassigned to the partner

    _as(monkeypatch, m, owner)  # former owner is no longer a participant
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_read(matter))
    assert exc.value.status_code == 403
    _as(monkeypatch, m, partner)  # new owner inherits the thread
    assert [x["content"] for x in asyncio.run(_read(matter))["messages"]] == ["history"]
    _as(monkeypatch, m, ralph)  # collaboration continues across reassignment
    assert len(asyncio.run(_read(matter))["messages"]) == 1


def test_a_collaborator_on_one_matter_cannot_use_another_matters_thread(monkeypatch):
    m, conn, matter, other_matter, owner, ralph, partner, admin, emails = _setup(monkeypatch)
    _as(monkeypatch, m, owner)
    asyncio.run(_post(other_matter, "secret on the other matter"))
    _as(monkeypatch, m, ralph)  # collaborator on `matter` only
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_read(other_matter))
    assert exc.value.status_code == 403
    assert asyncio.run(_read(matter))["messages"] == []  # and never sees it on their own


def test_threads_are_separate_per_matter(monkeypatch):
    m, conn, matter, other_matter, owner, ralph, partner, admin, emails = _setup(monkeypatch)
    _as(monkeypatch, m, owner)
    asyncio.run(_post(matter, "A"))
    asyncio.run(_post(other_matter, "B"))
    assert [x["content"] for x in asyncio.run(_read(matter))["messages"]] == ["A"]


# ── validation ───────────────────────────────────────────────────────────────

def test_empty_overlong_and_malformed_requests(monkeypatch):
    m, conn, matter, _, owner, ralph, partner, admin, emails = _setup(monkeypatch)
    _as(monkeypatch, m, owner)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_post(matter, "   \n  "))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_post(matter, "x" * (COLLABORATION_MESSAGE_MAX_LENGTH + 1)))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        asyncio.run(list_collaboration_messages("nope", None))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        asyncio.run(list_collaboration_messages(str(uuid.uuid4()), None))
    assert exc.value.status_code == 404
    assert conn.messages == []


def test_synthetic_user_without_identity_is_refused(monkeypatch):
    m, conn, matter, _, owner, ralph, partner, admin, emails = _setup(monkeypatch)

    async def fake(request):
        return {"id": None, "firm_id": FIRM_ID, "role": "partner", "display_name": "Demo User"}
    monkeypatch.setattr(m, "get_current_user", fake)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_read(matter))
    assert exc.value.status_code == 403


def test_message_text_is_stored_verbatim_after_trimming(monkeypatch):
    m, conn, matter, _, owner, ralph, partner, admin, emails = _setup(monkeypatch)
    _as(monkeypatch, m, owner)
    asyncio.run(_post(matter, "  <b>line one</b>\nline two  "))
    assert conn.messages[0]["content"] == "<b>line one</b>\nline two"  # escaped on display, not mangled here


# ── notifications ────────────────────────────────────────────────────────────

def test_new_message_notifies_the_other_participants_not_the_author(monkeypatch):
    m, conn, matter, _, owner, ralph, partner, admin, emails = _setup(monkeypatch)
    _as(monkeypatch, m, ralph)
    asyncio.run(_post(matter, "Prescription runs from the date of demand."))

    assert [n["user_id"] for n in conn.notifications] == [owner["id"]]
    assert conn.notifications[0]["kind"] == "collaboration_message"
    assert conn.notifications[0]["matter_id"] == matter["id"]
    assert [e[0] for e in emails] == [owner["id"]]
    assert "Ralph Labour: Prescription runs" in emails[0][2]


def test_only_current_participants_are_notified(monkeypatch):
    m, conn, matter, _, owner, ralph, partner, admin, emails = _setup(monkeypatch)
    conn.collabs.append(_collab(matter, admin, revoked_at=datetime.now(timezone.utc)))  # revoked: not notified
    third = _user("Dormant Dan", active=False)
    conn.users.append(third)
    conn.collabs.append(_collab(matter, third))  # deactivated: not notified
    _as(monkeypatch, m, owner)
    asyncio.run(_post(matter, "hello"))
    assert {n["user_id"] for n in conn.notifications} == {ralph["id"]}


def test_fast_back_and_forth_does_not_pile_up_notifications_or_emails(monkeypatch):
    m, conn, matter, _, owner, ralph, partner, admin, emails = _setup(monkeypatch)
    _as(monkeypatch, m, ralph)
    for i in range(3):
        asyncio.run(_post(matter, f"message {i}"))
    assert len(conn.notifications) == 1 and len(emails) == 1  # one unread pointer, one email

    _as(monkeypatch, m, owner)
    asyncio.run(_read(matter))  # opening the thread clears the owner's pointer
    assert conn.notifications[0]["read_at"] is not None

    _as(monkeypatch, m, ralph)
    asyncio.run(_post(matter, "after they looked"))
    assert len(conn.notifications) == 2 and len(emails) == 2  # notifies again


def test_reading_clears_only_the_readers_own_collaboration_notifications(monkeypatch):
    m, conn, matter, _, owner, ralph, partner, admin, emails = _setup(monkeypatch)
    conn.notifications += [
        {"user_id": ralph["id"], "kind": "matter_collaboration_invited", "title": "t", "body": "b",
         "matter_id": matter["id"], "read_at": None},
        {"user_id": owner["id"], "kind": "collaboration_message", "title": "t", "body": "b",
         "matter_id": matter["id"], "read_at": None},
        {"user_id": ralph["id"], "kind": "matter_assigned", "title": "t", "body": "b",
         "matter_id": matter["id"], "read_at": None},
    ]
    _as(monkeypatch, m, ralph)
    asyncio.run(_read(matter))
    by = {(n["user_id"], n["kind"]): n["read_at"] for n in conn.notifications}
    assert by[(ralph["id"], "matter_collaboration_invited")] is not None
    assert by[(owner["id"], "collaboration_message")] is None     # someone else's: untouched
    assert by[(ralph["id"], "matter_assigned")] is None           # unrelated kind: untouched


def test_notification_failure_never_fails_posting(monkeypatch):
    m, conn, matter, _, owner, ralph, partner, admin, emails = _setup(monkeypatch)

    async def boom(*a, **k):
        raise RuntimeError("resend down")
    monkeypatch.setattr(m, "send_user_notification", boom)
    _as(monkeypatch, m, ralph)
    out = asyncio.run(_post(matter, "still saved"))
    assert out["content"] == "still saved" and len(conn.messages) == 1


def test_messages_are_not_audited_individually(monkeypatch):
    m, conn, matter, _, owner, ralph, partner, admin, emails = _setup(monkeypatch)
    _as(monkeypatch, m, owner)
    asyncio.run(_post(matter, "one"))
    asyncio.run(_post(matter, "two"))
    assert not [q for q, a in conn.executed if q.startswith("INSERT INTO audit_logs")]
