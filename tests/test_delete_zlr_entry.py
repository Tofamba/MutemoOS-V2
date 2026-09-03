"""
Unit tests for DELETE /api/zlr/{item_id} (backend/main.py).

2026-09-03 fix, same class of bug as delete_legal_update (see
tests/test_delete_legal_update.py's module docstring for the full
mechanism): this endpoint deleted the zlr_entries row and removed the
ChromaDB vectors, but never deleted the matching Postgres `chunks` rows
-- chunks.document_id carries no FK/CASCADE to zlr_entries, so those rows
survived, fully populated (including content_hash), orphaned. The very
next reconcile_chroma_index() run (every server boot) would see them as
"in Postgres, missing from Chroma" and silently re-index them right back
into Chroma -- undoing the deletion on the next deploy or restart. These
tests pin the fixed behavior: a real deletion also removes the chunks
rows, so there's nothing left for reconcile to resurrect.

Same FakeConnection/FakePool/_as_current_user convention as
tests/test_delete_legal_update.py, which this file mirrors closely --
same bug, same fix shape, different table name.
"""

import asyncio
import uuid

import pytest
from fastapi import HTTPException

from backend.main import FIRM_ID, delete_zlr_entry


class FakeConnection:
    def __init__(self, zlr_entries=None, chunks=None):
        self.zlr_entries = zlr_entries if zlr_entries is not None else []
        self.chunks = chunks if chunks is not None else []

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT id FROM chunks WHERE document_id=$1 AND firm_id=$2"):
            document_id, firm_id = args
            return [{"id": c["id"]} for c in self.chunks
                    if c["document_id"] == document_id and c["firm_id"] == firm_id]
        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("DELETE FROM zlr_entries WHERE id=$1 AND firm_id=$2"):
            item_id, firm_id = args
            before = len(self.zlr_entries)
            self.zlr_entries = [r for r in self.zlr_entries
                                 if not (r["id"] == item_id and r["firm_id"] == firm_id)]
            return f"DELETE {before - len(self.zlr_entries)}"
        if q.startswith("DELETE FROM chunks WHERE document_id=$1 AND firm_id=$2"):
            document_id, firm_id = args
            before = len(self.chunks)
            self.chunks = [c for c in self.chunks
                           if not (c["document_id"] == document_id and c["firm_id"] == firm_id)]
            return f"DELETE {before - len(self.chunks)}"
        raise NotImplementedError(f"FakeConnection.execute: unhandled query: {q}")


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


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def _fake_request():
    return None


def _chunk(document_id, chunk_id=None):
    return {"id": chunk_id or str(uuid.uuid4()), "firm_id": FIRM_ID, "document_id": document_id}


def test_associate_gets_403(monkeypatch):
    import backend.main as m
    associate = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "associate", "display_name": "Assoc"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, associate)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(delete_zlr_entry(str(uuid.uuid4()), _fake_request()))
    assert exc_info.value.status_code == 403


def test_missing_item_returns_404(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, partner)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(delete_zlr_entry(str(uuid.uuid4()), _fake_request()))
    assert exc_info.value.status_code == 404


def test_deletion_removes_the_zlr_entries_row_the_chunks_rows_and_the_chroma_vectors(monkeypatch):
    """The real 2026-09-03 fix: a deletion must leave nothing in Postgres
    for reconcile_chroma_index() to find and silently re-index back into
    Chroma on the next server boot."""
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    item_id = uuid.uuid4()
    zlr_entries = [{"id": item_id, "firm_id": FIRM_ID, "case_name": "Some v Case"}]
    chunks = [_chunk(item_id, "c1"), _chunk(item_id, "c2")]
    pool = FakePool(zlr_entries=zlr_entries, chunks=chunks)
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, partner)

    removed_calls = []
    monkeypatch.setattr(m, "remove_chunks_from_chroma",
                         lambda chunk_ids, collection_type: removed_calls.append((set(chunk_ids), collection_type)))

    result = asyncio.run(delete_zlr_entry(str(item_id), _fake_request()))

    assert result == {"deleted": True}
    assert pool.conn.zlr_entries == []  # the zlr_entries row is gone
    assert pool.conn.chunks == []  # the chunks rows are gone too -- the actual fix
    assert removed_calls == [({"c1", "c2"}, "zlr")]  # and the Chroma vectors


def test_deletion_of_unrelated_entry_leaves_other_chunks_untouched(monkeypatch):
    """Firm-scoped and document-scoped -- deleting one zlr_entries row
    must never touch another entry's chunks."""
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    target_id = uuid.uuid4()
    other_id = uuid.uuid4()
    zlr_entries = [
        {"id": target_id, "firm_id": FIRM_ID, "case_name": "Target v Case"},
        {"id": other_id, "firm_id": FIRM_ID, "case_name": "Unrelated v Case"},
    ]
    chunks = [_chunk(target_id, "c1"), _chunk(other_id, "c2")]
    pool = FakePool(zlr_entries=zlr_entries, chunks=chunks)
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, partner)
    monkeypatch.setattr(m, "remove_chunks_from_chroma", lambda chunk_ids, collection_type: None)

    asyncio.run(delete_zlr_entry(str(target_id), _fake_request()))

    assert [r["id"] for r in pool.conn.zlr_entries] == [other_id]
    assert [c["id"] for c in pool.conn.chunks] == ["c2"]


def test_no_chunks_still_deletes_the_zlr_entries_row_and_skips_chroma_call(monkeypatch):
    """A zlr_entries row with zero real chunks must still delete cleanly
    -- no chunk_ids means no ChromaDB call is even attempted."""
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    item_id = uuid.uuid4()
    pool = FakePool(zlr_entries=[{"id": item_id, "firm_id": FIRM_ID, "case_name": "Ghost Case"}])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, partner)

    removed_calls = []
    monkeypatch.setattr(m, "remove_chunks_from_chroma",
                         lambda chunk_ids, collection_type: removed_calls.append(chunk_ids))

    result = asyncio.run(delete_zlr_entry(str(item_id), _fake_request()))

    assert result == {"deleted": True}
    assert pool.conn.zlr_entries == []
    assert removed_calls == []
