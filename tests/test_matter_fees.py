"""
Unit tests for matter-level fee tracking (backend/main.py): amount_billed,
amount_received, and the server-computed fee_balance ("billed minus
received") in _row_to_matter(). This is the firm's own professional fees
only, manually entered — explicitly not trust accounting; see the schema
comment in run_migrations() for why.

Called directly as plain async functions, same convention as
tests/test_matter_client_linking.py.
"""

import asyncio
import re
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from backend.main import FIRM_ID, MatterUpdate, update_matter


class FakeConnection:
    def __init__(self, matters):
        self.matters = matters

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

        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT * FROM progress_notes"):
            return []
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
    def __init__(self, matters=None):
        self.conn = FakeConnection(matters if matters is not None else [])

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _matter_row(matter_id, amount_billed=None, amount_received=None, firm_id=FIRM_ID):
    return {
        "id": matter_id, "firm_id": firm_id, "name": "Estate of X", "number": None,
        "internal_ref": None, "external_ref": None, "client_name": None, "client_id": None,
        "case_parties": None, "matter_type": None, "practice_area": None, "status": "Active",
        "custom_status": None, "document_count": 0, "last_activity": None,
        "created_at": datetime.now(timezone.utc), "created_by": None,
        "next_deadline": None, "next_deadline_note": None,
        "amount_billed": amount_billed, "amount_received": amount_received,
    }


def _fake_request():
    return None


def test_fee_balance_none_when_neither_figure_entered(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id)])
    monkeypatch.setattr(m, "_db_pool", pool)

    # A no-op field update just to exercise _row_to_matter via the endpoint
    result = asyncio.run(update_matter(str(matter_id), MatterUpdate(name="Estate of X (renamed)"), _fake_request()))

    assert result["fee_balance"] is None


def test_fee_balance_calculates_billed_minus_received(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id)])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_matter(
        str(matter_id), MatterUpdate(amount_billed=1500.0, amount_received=600.0), _fake_request()
    ))

    assert result["amount_billed"] == 1500.0
    assert result["amount_received"] == 600.0
    assert result["fee_balance"] == 900.0


def test_fee_balance_when_only_billed_is_set(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id)])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_matter(str(matter_id), MatterUpdate(amount_billed=800.0), _fake_request()))

    assert result["amount_billed"] == 800.0
    assert result["amount_received"] is None
    assert result["fee_balance"] == 800.0


def test_fee_balance_when_only_received_is_set(monkeypatch):
    """An edge case (payment recorded before a bill was entered), but the
    balance must still compute correctly rather than erroring on None."""
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id)])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_matter(str(matter_id), MatterUpdate(amount_received=250.0), _fake_request()))

    assert result["amount_received"] == 250.0
    assert result["fee_balance"] == -250.0


def test_amount_billed_and_amount_received_persist_independently(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id, amount_billed=1000.0)])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_matter(str(matter_id), MatterUpdate(amount_received=400.0), _fake_request()))

    # The pre-existing amount_billed wasn't touched by this PATCH, only received was
    assert result["amount_billed"] == 1000.0
    assert result["amount_received"] == 400.0
    assert result["fee_balance"] == 600.0


def test_update_matter_fees_404s_for_unknown_matter(monkeypatch):
    import backend.main as m
    pool = FakePool(matters=[])
    monkeypatch.setattr(m, "_db_pool", pool)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_matter(str(uuid.uuid4()), MatterUpdate(amount_billed=100.0), _fake_request()))
    assert exc_info.value.status_code == 404
