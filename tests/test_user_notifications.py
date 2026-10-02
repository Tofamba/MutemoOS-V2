"""
Unit tests for the in-app notification endpoints (backend/main.py,
2026-10-02): GET /api/notifications, POST /api/notifications/{id}/read and
POST /api/notifications/read-all. Self-scoped by user id and firm -- a user
can never read or mark another user's notifications. Called as plain async
functions with a hand-built fake DB, same convention as
tests/test_matter_responsible_lawyer.py.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from backend.main import (
    FIRM_ID, list_my_notifications, mark_all_notifications_read, mark_notification_read,
)


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT id, kind, title, body, matter_id, created_at, read_at FROM user_notifications"):
            uid, firm = args
            mine = [r for r in self.rows if r["user_id"] == uid and r["firm_id"] == firm]
            return sorted(mine, key=lambda r: r["created_at"], reverse=True)[:30]
        raise NotImplementedError(q)

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT COUNT(*) AS n FROM user_notifications"):
            uid, firm = args
            return {"n": len([r for r in self.rows if r["user_id"] == uid and r["firm_id"] == firm and r["read_at"] is None])}
        if q.startswith("UPDATE user_notifications SET read_at=COALESCE"):
            nid, uid, firm = args
            for r in self.rows:
                if r["id"] == nid and r["user_id"] == uid and r["firm_id"] == firm:
                    r["read_at"] = r["read_at"] or datetime.now(timezone.utc)
                    return {"id": r["id"]}
            return None
        raise NotImplementedError(q)

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("UPDATE user_notifications SET read_at=NOW() WHERE user_id=$1"):
            uid, firm = args
            n = 0
            for r in self.rows:
                if r["user_id"] == uid and r["firm_id"] == firm and r["read_at"] is None:
                    r["read_at"] = datetime.now(timezone.utc)
                    n += 1
            return f"UPDATE {n}"
        raise NotImplementedError(q)


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, rows):
        self.conn = FakeConnection(rows)

    def acquire(self):
        return _Ctx(self.conn)


def _row(user_id, title="Matter assigned to you", read=False, firm_id=FIRM_ID, minutes=0):
    return {
        "id": uuid.uuid4(), "firm_id": firm_id, "user_id": user_id, "kind": "matter_assigned",
        "title": title, "body": "body", "matter_id": uuid.uuid4(),
        "created_at": datetime(2026, 10, 2, 8, minutes, tzinfo=timezone.utc),
        "read_at": datetime.now(timezone.utc) if read else None,
    }


def _as(monkeypatch, user):
    import backend.main as m

    async def fake_get_current_user(request):
        return user
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def test_lists_only_the_callers_own_notifications_newest_first(monkeypatch):
    import backend.main as m
    me, someone = uuid.uuid4(), uuid.uuid4()
    pool = FakePool([_row(me, "older", minutes=1), _row(me, "newer", minutes=5), _row(someone, "not mine")])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as(monkeypatch, {"id": me, "firm_id": FIRM_ID, "role": "associate"})

    result = asyncio.run(list_my_notifications(None))

    assert [n["title"] for n in result["notifications"]] == ["newer", "older"]
    assert result["unread_count"] == 2
    assert all(isinstance(n["id"], str) for n in result["notifications"])


def test_unread_count_excludes_read_items(monkeypatch):
    import backend.main as m
    me = uuid.uuid4()
    pool = FakePool([_row(me, "a"), _row(me, "b", read=True)])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as(monkeypatch, {"id": me, "firm_id": FIRM_ID, "role": "associate"})

    assert asyncio.run(list_my_notifications(None))["unread_count"] == 1


def test_synthetic_user_without_identity_gets_an_empty_list(monkeypatch):
    _as(monkeypatch, {"id": None, "firm_id": FIRM_ID, "role": "partner"})
    assert asyncio.run(list_my_notifications(None)) == {"unread_count": 0, "notifications": []}


def test_unauthenticated_is_401(monkeypatch):
    _as(monkeypatch, None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(list_my_notifications(None))
    assert exc.value.status_code == 401


def test_mark_read_sets_read_at_for_own_notification(monkeypatch):
    import backend.main as m
    me = uuid.uuid4()
    row = _row(me)
    pool = FakePool([row])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as(monkeypatch, {"id": me, "firm_id": FIRM_ID, "role": "associate"})

    assert asyncio.run(mark_notification_read(str(row["id"]), None)) == {"read": True}
    assert row["read_at"] is not None


def test_cannot_mark_another_users_notification_read(monkeypatch):
    import backend.main as m
    me, someone = uuid.uuid4(), uuid.uuid4()
    row = _row(someone)
    pool = FakePool([row])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as(monkeypatch, {"id": me, "firm_id": FIRM_ID, "role": "partner"})

    with pytest.raises(HTTPException) as exc:
        asyncio.run(mark_notification_read(str(row["id"]), None))
    assert exc.value.status_code == 404
    assert row["read_at"] is None


def test_mark_read_rejects_malformed_id(monkeypatch):
    _as(monkeypatch, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "associate"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(mark_notification_read("not-a-uuid", None))
    assert exc.value.status_code == 400


def test_mark_all_read_only_touches_the_callers_unread_items(monkeypatch):
    import backend.main as m
    me, someone = uuid.uuid4(), uuid.uuid4()
    mine_unread, mine_read, theirs = _row(me), _row(me, read=True), _row(someone)
    pool = FakePool([mine_unread, mine_read, theirs])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as(monkeypatch, {"id": me, "firm_id": FIRM_ID, "role": "associate"})

    assert asyncio.run(mark_all_notifications_read(None)) == {"updated": 1}
    assert mine_unread["read_at"] is not None
    assert theirs["read_at"] is None
