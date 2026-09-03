"""
Unit tests for DELETE /api/matters/{matter_id} (backend/main.py).

2026-09-03 fix, same class of bug as delete_legal_update (see
tests/test_delete_legal_update.py's module docstring for the full
mechanism): this endpoint deleted the matters row and removed the
ChromaDB vectors, but never deleted the matching Postgres `chunks` rows
-- chunks.matter_id carries no FK/CASCADE to matters (chunks.matter_id
is plain TEXT, not even a UUID column), so those rows survived, fully
populated (including content_hash), orphaned. The very next
reconcile_chroma_index() run (every server boot) would see them as "in
Postgres, missing from Chroma" and silently re-index them right back
into Chroma -- undoing the deletion on the next deploy or restart. These
tests pin the fixed behavior: a real deletion also removes the chunks
rows, so there's nothing left for reconcile to resurrect.

Note the one real difference from delete_legal_update/delete_zlr_entry:
matter_id is matched against chunks.matter_id as a plain string (that
column is TEXT, unlike chunks.document_id), while the matters table's
own `id` column is UUID and gets _uuid_mod.UUID(...)-cast for that one
query -- both fetch/tests below reflect that distinction, not a copy-
paste of the other two files' UUID handling.

Same FakeConnection/FakePool/_as_current_user convention as
tests/test_delete_legal_update.py and tests/test_delete_zlr_entry.py.
Matter deletion is a higher-risk area than the other two (a matter
carries far more attached data -- notes, documents, calendar events,
compliance) so this file also confirms the fix is scoped to exactly
`chunks` and touches nothing else.
"""

import asyncio
import uuid

import pytest
from fastapi import HTTPException

from backend.main import FIRM_ID, delete_matter


class FakeConnection:
    def __init__(self, matters=None, chunks=None):
        self.matters = matters if matters is not None else []
        self.chunks = chunks if chunks is not None else []

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT id FROM chunks WHERE matter_id=$1 AND firm_id=$2"):
            matter_id, firm_id = args
            assert isinstance(matter_id, str)  # matter_id reaches this query as a plain string, not a UUID
            return [{"id": c["id"]} for c in self.chunks
                    if c["matter_id"] == matter_id and c["firm_id"] == firm_id]
        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("DELETE FROM matters WHERE id=$1 AND firm_id=$2"):
            matter_uuid, firm_id = args
            assert isinstance(matter_uuid, uuid.UUID)  # matters.id is a real UUID column
            before = len(self.matters)
            self.matters = [r for r in self.matters
                             if not (r["id"] == matter_uuid and r["firm_id"] == firm_id)]
            return f"DELETE {before - len(self.matters)}"
        if q.startswith("DELETE FROM chunks WHERE matter_id=$1 AND firm_id=$2"):
            matter_id, firm_id = args
            assert isinstance(matter_id, str)  # chunks.matter_id is TEXT, matched as a string
            before = len(self.chunks)
            self.chunks = [c for c in self.chunks
                           if not (c["matter_id"] == matter_id and c["firm_id"] == firm_id)]
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


def _matter(matter_id, firm_id=FIRM_ID):
    return {"id": matter_id, "firm_id": firm_id}


def _chunk(matter_id, chunk_id=None, firm_id=FIRM_ID):
    return {"id": chunk_id or str(uuid.uuid4()), "firm_id": firm_id, "matter_id": matter_id}


def test_associate_gets_403(monkeypatch):
    import backend.main as m
    associate = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "associate", "display_name": "Assoc"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, associate)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(delete_matter(str(uuid.uuid4()), _fake_request()))
    assert exc_info.value.status_code == 403


def test_missing_matter_returns_404(monkeypatch):
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, partner)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(delete_matter(str(uuid.uuid4()), _fake_request()))
    assert exc_info.value.status_code == 404


def test_deletion_removes_the_matter_row_the_chunks_rows_and_the_chroma_vectors(monkeypatch):
    """The real 2026-09-03 fix: a deletion must leave nothing in Postgres
    for reconcile_chroma_index() to find and silently re-index back into
    Chroma on the next server boot."""
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    matter_uuid = uuid.uuid4()
    matter_id_str = str(matter_uuid)
    matters = [_matter(matter_uuid)]
    chunks = [_chunk(matter_id_str, "c1"), _chunk(matter_id_str, "c2")]
    pool = FakePool(matters=matters, chunks=chunks)
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, partner)

    removed_calls = []
    monkeypatch.setattr(m, "remove_chunks_from_chroma",
                         lambda chunk_ids, collection_type: removed_calls.append((set(chunk_ids), collection_type)))

    result = asyncio.run(delete_matter(matter_id_str, _fake_request()))

    assert result == {"deleted": True}
    assert pool.conn.matters == []  # the matters row is gone
    assert pool.conn.chunks == []  # the chunks rows are gone too -- the actual fix
    assert removed_calls == [({"c1", "c2"}, "firm")]  # and the Chroma vectors ("firm" collection, unlike legal/zlr)


def test_deletion_of_unrelated_matter_leaves_other_chunks_untouched(monkeypatch):
    """Firm-scoped and matter-scoped -- deleting one matter must never
    touch another matter's chunks."""
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    target_uuid, other_uuid = uuid.uuid4(), uuid.uuid4()
    target_id, other_id = str(target_uuid), str(other_uuid)
    matters = [_matter(target_uuid), _matter(other_uuid)]
    chunks = [_chunk(target_id, "c1"), _chunk(other_id, "c2")]
    pool = FakePool(matters=matters, chunks=chunks)
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, partner)
    monkeypatch.setattr(m, "remove_chunks_from_chroma", lambda chunk_ids, collection_type: None)

    asyncio.run(delete_matter(target_id, _fake_request()))

    assert [r["id"] for r in pool.conn.matters] == [other_uuid]
    assert [c["id"] for c in pool.conn.chunks] == ["c2"]


def test_no_chunks_still_deletes_the_matter_and_skips_chroma_call(monkeypatch):
    """A matter with zero real chunks (never had a document uploaded)
    must still delete cleanly -- no chunk_ids means no ChromaDB call is
    even attempted."""
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    matter_uuid = uuid.uuid4()
    pool = FakePool(matters=[_matter(matter_uuid)])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, partner)

    removed_calls = []
    monkeypatch.setattr(m, "remove_chunks_from_chroma",
                         lambda chunk_ids, collection_type: removed_calls.append(chunk_ids))

    result = asyncio.run(delete_matter(str(matter_uuid), _fake_request()))

    assert result == {"deleted": True}
    assert pool.conn.matters == []
    assert removed_calls == []


def test_other_firms_matching_matter_id_is_untouched(monkeypatch):
    """Firm isolation: a different firm's matter/chunks that happen to
    share the same matter_id string must never be deleted."""
    import backend.main as m
    partner = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}
    matter_uuid = uuid.uuid4()
    matter_id_str = str(matter_uuid)
    other_firm_id = uuid.uuid4()
    matters = [_matter(matter_uuid, firm_id=FIRM_ID)]
    chunks = [_chunk(matter_id_str, "c1", firm_id=FIRM_ID),
              _chunk(matter_id_str, "c_other_firm", firm_id=other_firm_id)]
    pool = FakePool(matters=matters, chunks=chunks)
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, partner)
    monkeypatch.setattr(m, "remove_chunks_from_chroma", lambda chunk_ids, collection_type: None)

    asyncio.run(delete_matter(matter_id_str, _fake_request()))

    assert [c["id"] for c in pool.conn.chunks] == ["c_other_firm"]
