"""
Unit tests for scripts/backfill_client_ownership.py's _build_plan() — the
one-time backfill that assigns clients.created_by by matching the initials
prefix embedded in client_number against the firm's current users.initials.

Confirms the core promise: a confident single-match gets applied, while
anything ambiguous, unmatched, or missing a client_number entirely lands
in the review list rather than being guessed — same convention as
scripts/backfill_practice_areas.py (see its test file's docstring).
"""

import asyncio
import uuid
from datetime import datetime, timezone

from scripts.backfill_client_ownership import FIRM_ID, _build_plan

FIRM_UUID = uuid.UUID(FIRM_ID)


class FakeConnection:
    def __init__(self, clients, users):
        self.clients = clients
        self.users = users

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT id, full_name, client_number FROM clients WHERE firm_id=$1 AND created_by IS NULL"):
            firm_id, = args
            rows = [c for c in self.clients if c["firm_id"] == firm_id and c.get("created_by") is None]
            rows.sort(key=lambda c: c["created_at"])
            return [{"id": c["id"], "full_name": c["full_name"], "client_number": c.get("client_number")} for c in rows]
        if q.startswith("SELECT id, initials, display_name FROM users WHERE firm_id=$1 AND initials IS NOT NULL"):
            firm_id, = args
            rows = [u for u in self.users if u["firm_id"] == firm_id and u.get("initials")]
            return [{"id": u["id"], "initials": u["initials"], "display_name": u["display_name"]} for u in rows]
        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")


def _client(full_name, client_number=None, created_by=None, created_at=None):
    return {
        "id": uuid.uuid4(), "firm_id": FIRM_UUID, "full_name": full_name,
        "client_number": client_number, "created_by": created_by,
        "created_at": created_at or datetime.now(timezone.utc),
    }


def _user(display_name, initials):
    return {"id": uuid.uuid4(), "firm_id": FIRM_UUID, "display_name": display_name, "initials": initials}


def test_confident_single_match_gets_applied():
    rufaro = _user("Rufaro Rusike", "RR")
    clients = [_client("John Moyo", client_number="RR-007")]
    conn = FakeConnection(clients, [rufaro])

    plan = asyncio.run(_build_plan(conn))

    assert len(plan["to_apply"]) == 1
    assert plan["to_apply"][0]["user_id"] == str(rufaro["id"])
    assert plan["to_apply"][0]["user_name"] == "Rufaro Rusike"
    assert plan["review"] == []


def test_ambiguous_initials_shared_by_two_users_lands_in_review_not_guessed():
    """Shouldn't happen given idx_users_firm_initials' uniqueness constraint
    in practice, but the plan builder checks defensively anyway rather than
    silently picking one."""
    u1 = _user("Tanaka Chademana", "TC")
    u2 = _user("Tapiwa Chikafu", "TC")
    clients = [_client("Jane Sithole", client_number="TC-003")]
    conn = FakeConnection(clients, [u1, u2])

    plan = asyncio.run(_build_plan(conn))

    assert plan["to_apply"] == []
    assert len(plan["review"]) == 1
    assert plan["review"][0]["reason"] == "ambiguous"
    assert set(plan["review"][0]["candidates"]) == {"Tanaka Chademana", "Tapiwa Chikafu"}


def test_no_matching_user_lands_in_review_not_guessed():
    """The initials prefix doesn't match any current user — e.g. the lawyer
    has since left the firm."""
    clients = [_client("Old Client", client_number="XYZ-001")]
    conn = FakeConnection(clients, [_user("Someone Else", "SE")])

    plan = asyncio.run(_build_plan(conn))

    assert plan["to_apply"] == []
    assert len(plan["review"]) == 1
    assert plan["review"][0]["reason"] == "no_match"
    assert plan["review"][0]["initials"] == "XYZ"


def test_client_with_no_client_number_at_all_lands_in_review():
    clients = [_client("Never Numbered Client", client_number=None)]
    conn = FakeConnection(clients, [_user("Rufaro Rusike", "RR")])

    plan = asyncio.run(_build_plan(conn))

    assert plan["to_apply"] == []
    assert len(plan["review"]) == 1
    assert plan["review"][0]["reason"] == "no_client_number"


def test_already_owned_clients_are_skipped_idempotent_rerun():
    rufaro = _user("Rufaro Rusike", "RR")
    already_owned = _client("Existing Client", client_number="RR-001", created_by=uuid.uuid4())
    unowned = _client("New Client", client_number="RR-002")
    conn = FakeConnection([already_owned, unowned], [rufaro])

    plan = asyncio.run(_build_plan(conn))

    # Only the unowned client is even considered (matches the WHERE
    # created_by IS NULL filter in the real query) — re-running after a
    # previous apply doesn't re-touch anything.
    assert len(plan["to_apply"]) == 1
    assert plan["to_apply"][0]["full_name"] == "New Client"


def test_empty_database_produces_empty_plan():
    conn = FakeConnection([], [])
    plan = asyncio.run(_build_plan(conn))
    assert plan == {"to_apply": [], "review": []}


def test_multiple_clients_under_same_initials_number_sequentially_and_all_match():
    rufaro = _user("Rufaro Rusike", "RR")
    clients = [_client("Client One", client_number="RR-001"), _client("Client Two", client_number="RR-002")]
    conn = FakeConnection(clients, [rufaro])

    plan = asyncio.run(_build_plan(conn))

    assert len(plan["to_apply"]) == 2
    assert all(e["user_id"] == str(rufaro["id"]) for e in plan["to_apply"])
