"""
Unit tests for scripts/backfill_next_review_date.py's _build_plan() and
cmd_apply() -- the one-time backfill that sets next_review_date to a
reasonable default (today + 30 days) on every matter that's never had one,
so backend/matter_health.py's "never entered the review cycle -> Red"
rule doesn't flood real users with Red matters just because they predate
the review safety net (2026-08-30).
"""

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from scripts.backfill_next_review_date import FIRM_ID, _build_plan, cmd_apply

FIRM_UUID = uuid.UUID(FIRM_ID)
TODAY = date(2026, 9, 1)


class FakeConnection:
    def __init__(self, matters):
        self.matters = matters

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT id, name, status, created_at FROM matters WHERE firm_id=$1 AND next_review_date IS NULL"):
            firm_id, = args
            rows = [
                m for m in self.matters
                if m["firm_id"] == firm_id and m.get("next_review_date") is None and not m.get("is_sentinel")
            ]
            rows.sort(key=lambda m: m["created_at"])
            return [{"id": m["id"], "name": m["name"], "status": m["status"], "created_at": m["created_at"]}
                    for m in rows]
        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")


def _matter(name, status="Active", next_review_date=None, created_at=None, is_sentinel=False):
    return {
        "id": uuid.uuid4(), "firm_id": FIRM_UUID, "name": name, "status": status,
        "next_review_date": next_review_date,
        "created_at": created_at or datetime.now(timezone.utc), "is_sentinel": is_sentinel,
    }


# ── _build_plan() ────────────────────────────────────────────────────────

def test_matter_with_no_review_date_gets_the_default():
    matters = [_matter("Estate of Chikafu")]
    conn = FakeConnection(matters)

    plan = asyncio.run(_build_plan(conn, today=TODAY))

    assert len(plan["to_apply"]) == 1
    assert plan["to_apply"][0]["next_review_date"] == TODAY + timedelta(days=30)
    assert plan["default_date"] == (TODAY + timedelta(days=30)).isoformat()


def test_matter_with_review_date_already_set_is_skipped():
    matters = [_matter("Estate of Chikafu", next_review_date=TODAY)]
    conn = FakeConnection(matters)

    plan = asyncio.run(_build_plan(conn, today=TODAY))

    assert plan["to_apply"] == []


def test_sentinel_matter_excluded():
    matters = [_matter("General / Firm Precedents", is_sentinel=True)]
    conn = FakeConnection(matters)

    plan = asyncio.run(_build_plan(conn, today=TODAY))

    assert plan["to_apply"] == []


def test_every_status_gets_backfilled_not_just_active():
    """No status-based skipping -- a Closed or On Hold matter with no
    next_review_date still gets one; it's Grey either way in Matter
    Health regardless of next_review_date, so backfilling it is harmless,
    and skipping it would just be an extra judgment call nobody asked for."""
    matters = [
        _matter("Closed Matter", status="Closed"),
        _matter("On Hold Matter", status="On Hold"),
    ]
    conn = FakeConnection(matters)

    plan = asyncio.run(_build_plan(conn, today=TODAY))

    assert len(plan["to_apply"]) == 2


def test_sorted_by_created_at_ascending():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    m1 = _matter("First", created_at=base)
    m2 = _matter("Second", created_at=base + timedelta(days=1))
    m3 = _matter("Third", created_at=base + timedelta(days=2))
    conn = FakeConnection([m3, m1, m2])  # deliberately out of order

    plan = asyncio.run(_build_plan(conn, today=TODAY))

    assert [e["name"] for e in plan["to_apply"]] == ["First", "Second", "Third"]


def test_empty_database_produces_empty_plan():
    conn = FakeConnection([])
    plan = asyncio.run(_build_plan(conn, today=TODAY))
    assert plan["to_apply"] == []


# ── cmd_apply() end-to-end idempotency ──────────────────────────────────────

class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ApplyFakeConnection(FakeConnection):
    def __init__(self, matters):
        super().__init__(matters)
        self.executed = []

    def transaction(self):
        return _FakeTransaction()

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("UPDATE matters SET next_review_date=$1 WHERE id=$2"):
            next_review_date, matter_id = args
            for m in self.matters:
                if m["id"] == matter_id:
                    m["next_review_date"] = next_review_date
            self.executed.append((next_review_date, matter_id))
            return "UPDATE 1"
        raise NotImplementedError(f"FakeConnection.execute: unhandled query: {q}")

    async def close(self):
        pass


def _apply_args(yes=True):
    return SimpleNamespace(database_url="postgres://fake", yes=yes)


def _run_apply_against(monkeypatch, conn, yes=True):
    import scripts.backfill_next_review_date as m

    class _FakeAsyncpg:
        @staticmethod
        async def connect(database_url):
            return conn
    monkeypatch.setattr(m, "asyncpg", _FakeAsyncpg())
    asyncio.run(cmd_apply(_apply_args(yes=yes)))


def test_apply_writes_the_default_to_every_matter_missing_one(monkeypatch):
    a = _matter("Estate of Chikafu")
    b = _matter("Trust deed variation")
    conn = _ApplyFakeConnection([a, b])

    _run_apply_against(monkeypatch, conn)

    assert a["next_review_date"] is not None
    assert b["next_review_date"] is not None
    assert len(conn.executed) == 2


def test_apply_leaves_last_reviewed_date_untouched():
    """Confirmed with the user: this is an automated data-quality fix, not
    a real review -- last_reviewed_date must never be touched by it. The
    real UPDATE statement itself only ever sets next_review_date, so this
    is really a check on the SQL shape, not runtime behavior."""
    import scripts.backfill_next_review_date as m
    import inspect
    source = inspect.getsource(m.cmd_apply)
    assert "last_reviewed_date" not in source


def test_apply_is_idempotent_second_run_touches_nothing(monkeypatch):
    a = _matter("Estate of Chikafu")
    b = _matter("Trust deed variation")
    conn = _ApplyFakeConnection([a, b])

    _run_apply_against(monkeypatch, conn)
    first_run_writes = list(conn.executed)
    assert len(first_run_writes) == 2

    _run_apply_against(monkeypatch, conn)

    # Zero additional writes -- every matter now has a real next_review_date,
    # so _build_plan()'s WHERE next_review_date IS NULL excludes all of them.
    assert conn.executed == first_run_writes


def test_apply_dry_run_without_yes_writes_nothing(monkeypatch):
    a = _matter("Estate of Chikafu")
    conn = _ApplyFakeConnection([a])

    _run_apply_against(monkeypatch, conn, yes=False)

    assert conn.executed == []
    assert a["next_review_date"] is None


def test_apply_with_nothing_to_backfill_is_a_clean_no_op(monkeypatch, capsys):
    conn = _ApplyFakeConnection([])

    _run_apply_against(monkeypatch, conn)

    assert conn.executed == []
    assert "Nothing to backfill" in capsys.readouterr().out
