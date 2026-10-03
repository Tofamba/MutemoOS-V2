"""
Tests for GET /api/matters/shared-with-me (backend/main.py, 2026-10-03,
Matter Collaboration stage 4): the "Shared with me" scope on the Matters tab.

The endpoint's SQL is the single server-side definition of the scope, so these
tests execute the PRODUCTION SQL string itself (SHARED_WITH_ME_SQL) against an
in-memory SQLite database after a mechanical dialect shim (NOW(), IS DISTINCT
FROM, $n placeholders). That exercises the real joins and predicates rather
than a Python re-implementation of them. It does not exercise Postgres itself;
that is covered by the real staging verification.

Scope rule under test: matters where the caller is an ACTIVE collaborator
(not revoked, not expired, same firm), excluding matters the caller owns
(responsible lawyer, else creator -- the same owner definition as My Matters),
excluding sentinel matters, as DISTINCT matters.
"""

import asyncio
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.main import FIRM_ID, SHARED_WITH_ME_SQL, list_matters_shared_with_me


def _ts(delta_minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _to_sqlite(sql: str) -> str:
    out = (sql.replace("NOW()", "datetime('now')")
              .replace("IS DISTINCT FROM", "IS NOT")
              .replace("$1", "?1").replace("$2", "?2"))
    assert "$" not in out and "NOW()" not in out, "dialect shim missed something"
    return out


class SqliteConn:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE matters (id TEXT, firm_id TEXT, responsible_lawyer_id TEXT, created_by TEXT, is_sentinel INT DEFAULT 0);
            CREATE TABLE matter_collaborators (id TEXT, firm_id TEXT, matter_id TEXT, user_id TEXT,
                                               revoked_at TEXT, expires_at TEXT);
        """)

    def add_matter(self, owner=None, creator=None, firm=FIRM_ID, sentinel=0):
        mid = str(uuid.uuid4())
        self.db.execute("INSERT INTO matters VALUES (?,?,?,?,?)",
                        (mid, str(firm), str(owner) if owner else None, str(creator) if creator else None, sentinel))
        return mid

    def add_collab(self, matter_id, user_id, firm=FIRM_ID, revoked=None, expires=None):
        self.db.execute("INSERT INTO matter_collaborators VALUES (?,?,?,?,?,?)",
                        (str(uuid.uuid4()), str(firm), matter_id, str(user_id), revoked, expires))

    async def fetch(self, query, *args):
        assert query == SHARED_WITH_ME_SQL, "endpoint must run the shared SQL definition"
        cur = self.db.execute(_to_sqlite(query), tuple(str(a) for a in args))
        return [dict(r) for r in cur.fetchall()]


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


def _call(monkeypatch, conn, user_id, role="associate"):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool(conn))

    async def fake(request):
        return {"id": user_id, "firm_id": FIRM_ID, "role": role, "display_name": "Caller"}
    monkeypatch.setattr(m, "get_current_user", fake)
    return asyncio.run(list_matters_shared_with_me(None))


ME, OWNER, OTHER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def test_user_with_one_active_collaboration_sees_exactly_that_matter(monkeypatch):
    c = SqliteConn()
    shared = c.add_matter(owner=OWNER)
    c.add_matter(owner=OWNER)  # unrelated matter, no collaboration
    c.add_collab(shared, ME)
    result = _call(monkeypatch, c, ME)
    assert result == {"matter_ids": [shared], "count": 1}


def test_zero_collaborations_returns_empty(monkeypatch):
    c = SqliteConn()
    c.add_matter(owner=OWNER)
    assert _call(monkeypatch, c, ME) == {"matter_ids": [], "count": 0}


@pytest.mark.parametrize("role", ["associate", "secretary", "partner", "admin"])
def test_any_role_can_use_shared_with_me(monkeypatch, role):
    c = SqliteConn()
    shared = c.add_matter(owner=OWNER)
    c.add_collab(shared, ME)
    assert _call(monkeypatch, c, ME, role=role)["matter_ids"] == [shared]


def test_revoked_collaboration_no_longer_qualifies(monkeypatch):
    c = SqliteConn()
    shared = c.add_matter(owner=OWNER)
    c.add_collab(shared, ME, revoked=_ts(-5))
    assert _call(monkeypatch, c, ME) == {"matter_ids": [], "count": 0}


def test_revocation_takes_effect_on_the_very_next_request(monkeypatch):
    c = SqliteConn()
    shared = c.add_matter(owner=OWNER)
    c.add_collab(shared, ME)
    assert _call(monkeypatch, c, ME)["count"] == 1
    c.db.execute("UPDATE matter_collaborators SET revoked_at=datetime('now')")
    assert _call(monkeypatch, c, ME)["count"] == 0


def test_expired_collaboration_no_longer_qualifies_but_future_expiry_does(monkeypatch):
    c = SqliteConn()
    expired, future = c.add_matter(owner=OWNER), c.add_matter(owner=OWNER)
    c.add_collab(expired, ME, expires=_ts(-1))
    c.add_collab(future, ME, expires=_ts(60))
    assert _call(monkeypatch, c, ME)["matter_ids"] == [future]


def test_count_is_the_number_of_distinct_matters_not_collaboration_records(monkeypatch):
    c = SqliteConn()
    a, b, d = c.add_matter(owner=OWNER), c.add_matter(owner=OWNER), c.add_matter(owner=OWNER)
    c.add_collab(a, ME)
    c.add_collab(a, ME)                  # duplicate active record for the same matter
    c.add_collab(a, ME, revoked=_ts(-9))  # plus a historical revoked one
    c.add_collab(b, ME)
    c.add_collab(d, ME, revoked=_ts(-9))  # revoked only: does not count
    result = _call(monkeypatch, c, ME)
    assert result["count"] == 2 and sorted(result["matter_ids"]) == sorted([a, b])


def test_collaborator_on_multiple_matters_gets_each_once(monkeypatch):
    c = SqliteConn()
    ids = [c.add_matter(owner=OWNER) for _ in range(4)]
    for mid in ids:
        c.add_collab(mid, ME)
    result = _call(monkeypatch, c, ME)
    assert result["count"] == 4 and sorted(result["matter_ids"]) == sorted(ids)


def test_other_peoples_collaborations_do_not_appear(monkeypatch):
    c = SqliteConn()
    mine, theirs = c.add_matter(owner=OWNER), c.add_matter(owner=OWNER)
    c.add_collab(mine, ME)
    c.add_collab(theirs, OTHER)
    assert _call(monkeypatch, c, ME)["matter_ids"] == [mine]


def test_a_matter_i_now_own_belongs_in_my_matters_not_shared(monkeypatch):
    c = SqliteConn()
    reassigned_to_me = c.add_matter(owner=ME, creator=OWNER)  # became mine after reassignment
    c.add_collab(reassigned_to_me, ME)                        # stale collaboration row remains
    still_shared = c.add_matter(owner=OWNER)
    c.add_collab(still_shared, ME)
    assert _call(monkeypatch, c, ME)["matter_ids"] == [still_shared]


def test_owner_definition_matches_my_matters_creator_fallback(monkeypatch):
    """My Matters treats a matter with no responsible lawyer as owned by its
    creator; Shared with me must exclude exactly the same set."""
    c = SqliteConn()
    mine_by_creator = c.add_matter(owner=None, creator=ME)
    c.add_collab(mine_by_creator, ME)
    shared = c.add_matter(owner=None, creator=OWNER)
    c.add_collab(shared, ME)
    assert _call(monkeypatch, c, ME)["matter_ids"] == [shared]


def test_firm_isolation_other_firm_data_never_qualifies(monkeypatch):
    c = SqliteConn()
    foreign_firm = uuid.uuid4()
    foreign_matter = c.add_matter(owner=OWNER, firm=foreign_firm)
    c.add_collab(foreign_matter, ME, firm=foreign_firm)
    mixed = c.add_matter(owner=OWNER)                  # our matter, collaboration row from another firm
    c.add_collab(mixed, ME, firm=foreign_firm)
    mixed2 = c.add_matter(owner=OWNER, firm=foreign_firm)  # other firm's matter, our-firm collaboration row
    c.add_collab(mixed2, ME)
    assert _call(monkeypatch, c, ME) == {"matter_ids": [], "count": 0}


def test_sentinel_matter_is_excluded(monkeypatch):
    c = SqliteConn()
    sentinel = c.add_matter(owner=OWNER, sentinel=1)
    c.add_collab(sentinel, ME)
    assert _call(monkeypatch, c, ME)["count"] == 0


def test_synthetic_user_without_identity_gets_nothing(monkeypatch):
    c = SqliteConn()
    c.add_collab(c.add_matter(owner=OWNER), ME)
    assert _call(monkeypatch, c, None) == {"matter_ids": [], "count": 0}
