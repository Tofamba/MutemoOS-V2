"""
Unit tests for scripts/backfill_client_matter_numbers.py's mapping logic —
the one-time backfill that assigns client_number/matter_number to the 12
existing clients (all confirmed to trace to NGM) created before this
feature existed.

Exercises _build_mapping() directly against a small in-memory fake DB
(same convention as the other backend tests — see tests/test_clients_api.py's
docstring), rather than the CLI's print/argparse wrapper.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from scripts.backfill_client_matter_numbers import _build_mapping

FIRM_ID = uuid.UUID("a1b2c3d4-0000-0000-0000-000000000001")


class FakeConnection:
    def __init__(self, clients, matters):
        self.clients = clients
        self.matters = matters

    async def fetch(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT id, full_name FROM clients WHERE firm_id=$1 AND client_number IS NULL"):
            firm_id, = args
            rows = [c for c in self.clients if c["firm_id"] == firm_id and c.get("client_number") is None]
            rows.sort(key=lambda c: c["created_at"])
            return [dict(r) for r in rows]

        if q.startswith("SELECT client_number FROM clients WHERE firm_id=$1 AND client_number LIKE $2"):
            firm_id, pattern = args
            prefix = pattern[:-1]
            return [{"client_number": c["client_number"]} for c in self.clients
                    if c["firm_id"] == firm_id and (c.get("client_number") or "").startswith(prefix)]

        if q.startswith("SELECT id, name FROM matters WHERE firm_id=$1 AND client_id=$2 AND matter_number IS NULL"):
            firm_id, client_id = args
            rows = [m for m in self.matters
                    if m["firm_id"] == firm_id and m["client_id"] == client_id and m.get("matter_number") is None]
            rows.sort(key=lambda m: m["created_at"])
            return [dict(r) for r in rows]

        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")


def _client(id_, name, created_at, client_number=None):
    return {"id": id_, "firm_id": FIRM_ID, "full_name": name, "created_at": created_at, "client_number": client_number}


def _matter(id_, client_id, name, created_at, matter_number=None):
    return {"id": id_, "firm_id": FIRM_ID, "client_id": client_id, "name": name,
            "created_at": created_at, "matter_number": matter_number}


def test_backfill_assigns_ngm_001_through_012_in_created_at_order():
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    client_ids = [uuid.uuid4() for _ in range(12)]
    clients = [
        _client(cid, f"Client {i+1}", base + timedelta(days=i))
        for i, cid in enumerate(client_ids)
    ]
    conn = FakeConnection(clients, matters=[])

    mapping = asyncio.run(_build_mapping(conn, "NGM"))

    assert [c["client_number"] for c in mapping] == [f"NGM-{i:03d}" for i in range(1, 13)]
    # Order preserved from created_at, not client dict order.
    assert [c["id"] for c in mapping] == [str(cid) for cid in client_ids]


def test_backfill_numbers_each_clients_matters_by_created_at_order():
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    client_id = uuid.uuid4()
    clients = [_client(client_id, "Huang Li Qiang", base)]
    matter_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    matters = [
        _matter(matter_ids[0], client_id, "Matter A", base + timedelta(days=1)),
        _matter(matter_ids[1], client_id, "Matter B", base + timedelta(days=2)),
        _matter(matter_ids[2], client_id, "Matter C", base + timedelta(days=3)),
    ]
    conn = FakeConnection(clients, matters)

    mapping = asyncio.run(_build_mapping(conn, "NGM"))

    assert mapping[0]["client_number"] == "NGM-001"
    assert [m["matter_number"] for m in mapping[0]["matters"]] == [
        "NGM-001-01", "NGM-001-02", "NGM-001-03",
    ]
    assert [m["id"] for m in mapping[0]["matters"]] == [str(mid) for mid in matter_ids]


def test_backfill_skips_clients_that_already_have_a_client_number():
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    already_numbered = _client(uuid.uuid4(), "Already Numbered", base, client_number="NGM-001")
    unnumbered = _client(uuid.uuid4(), "Needs A Number", base + timedelta(days=1))
    conn = FakeConnection([already_numbered, unnumbered], matters=[])

    mapping = asyncio.run(_build_mapping(conn, "NGM"))

    assert len(mapping) == 1
    assert mapping[0]["full_name"] == "Needs A Number"
    # Continues from the existing NGM-001, not restarting at NGM-001 itself.
    assert mapping[0]["client_number"] == "NGM-002"


def test_backfill_continues_from_thirteen_after_ngm_001_through_012_exist():
    """The spec: after this backfill, new NGM clients continue from NGM-013."""
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    numbered = [_client(uuid.uuid4(), f"Client {i}", base, client_number=f"NGM-{i:03d}") for i in range(1, 13)]
    unnumbered = _client(uuid.uuid4(), "New Client", base + timedelta(days=1))
    conn = FakeConnection(numbered + [unnumbered], matters=[])

    mapping = asyncio.run(_build_mapping(conn, "NGM"))

    assert len(mapping) == 1
    assert mapping[0]["client_number"] == "NGM-013"


def test_backfill_nothing_to_do_when_all_clients_already_numbered():
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    numbered = [_client(uuid.uuid4(), "Already Numbered", base, client_number="NGM-001")]
    conn = FakeConnection(numbered, matters=[])

    mapping = asyncio.run(_build_mapping(conn, "NGM"))

    assert mapping == []
