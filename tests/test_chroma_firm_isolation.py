"""
Unit tests for Multi-tenancy hardening Part 3 (ChromaDB firm isolation):

  1. index_chunks_in_chroma() writes firm_id into the metadata of chunks
     indexed into the "firm" collection (firm_precedents), and only that
     collection -- legal_updates/zlr_index are shared corpora and must
     stay unscoped.
  2. _semantic_search_firm() always passes an explicit firm_id filter to
     Chroma's query(), combined with the existing matter_id filter via
     $and when both are present (a flat multi-key `where` dict raises in
     chromadb -- verified directly against the installed package before
     writing this fix; Chroma requires exactly one top-level operator).
  3. scripts/backfill_chroma_firm_id.py's build_plan()/apply_plan() and
     the admin endpoint that reuses them in-process, mirroring
     tests/test_admin_backfill_chunk_hashes.py's conventions exactly.

Called directly as plain functions/coroutines, same convention as this
repo's other backend tests.
"""
import asyncio

import pytest
from fastapi import HTTPException

from backend.main import (
    FIRM_ID,
    index_chunks_in_chroma,
    _semantic_search_firm,
    admin_backfill_chroma_firm_id,
)
from scripts.backfill_chroma_firm_id import build_plan, apply_plan


# ── index_chunks_in_chroma(): firm_id metadata, firm collection only ────────

class _RecordingCollection:
    def __init__(self):
        self.upsert_calls = []

    def upsert(self, ids, embeddings, documents, metadatas):
        self.upsert_calls.append({"ids": ids, "metadatas": metadatas})


def _fake_chunk(cid="c1", matter_id="m1"):
    return {
        "id": cid, "text": "some chunk text", "document_id": "d1",
        "matter_id": matter_id, "chunk_index": 0, "page_number": 1,
        "content_hash": "hash1",
    }


def test_firm_collection_chunks_get_firm_id_metadata(monkeypatch):
    import backend.main as m
    firm_col = _RecordingCollection()
    monkeypatch.setattr(m, "get_chroma_collections", lambda: (firm_col, _RecordingCollection(), _RecordingCollection()))
    monkeypatch.setattr(m, "embed_texts", lambda texts: [[0.1, 0.2] for _ in texts])

    index_chunks_in_chroma([_fake_chunk()], collection_type="firm")

    meta = firm_col.upsert_calls[0]["metadatas"][0]
    assert meta["firm_id"] == str(FIRM_ID)


def test_legal_collection_chunks_do_not_get_firm_id_metadata(monkeypatch):
    import backend.main as m
    legal_col = _RecordingCollection()
    monkeypatch.setattr(m, "get_chroma_collections", lambda: (_RecordingCollection(), legal_col, _RecordingCollection()))
    monkeypatch.setattr(m, "embed_texts", lambda texts: [[0.1, 0.2] for _ in texts])

    index_chunks_in_chroma([_fake_chunk(matter_id=None)], collection_type="legal")

    meta = legal_col.upsert_calls[0]["metadatas"][0]
    assert "firm_id" not in meta


def test_zlr_collection_chunks_do_not_get_firm_id_metadata(monkeypatch):
    import backend.main as m
    zlr_col = _RecordingCollection()
    monkeypatch.setattr(m, "get_chroma_collections", lambda: (_RecordingCollection(), _RecordingCollection(), zlr_col))
    monkeypatch.setattr(m, "embed_texts", lambda texts: [[0.1, 0.2] for _ in texts])

    index_chunks_in_chroma([_fake_chunk(matter_id=None)], collection_type="zlr")

    meta = zlr_col.upsert_calls[0]["metadatas"][0]
    assert "firm_id" not in meta


# ── _semantic_search_firm(): explicit firm_id where-filter ──────────────────

class _QueryCapturingCollection:
    """Returns one real hit so the zero-regression shape can be asserted,
    while recording exactly the `where` clause it was called with."""
    def __init__(self):
        self.query_calls = []

    def count(self):
        return 1

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        return {"ids": [["c1"]], "distances": [[0.1]]}


class _FakeSearchRequest:
    def __init__(self, query="rent arrears", limit=8, matter_id=None):
        self.query = query
        self.limit = limit
        self.matter_id = matter_id


def _install_firm_col(monkeypatch, m):
    firm_col = _QueryCapturingCollection()
    monkeypatch.setattr(m, "get_chroma_collections", lambda: (firm_col, None, None))
    monkeypatch.setattr(m, "embed_texts", lambda texts: [[0.1, 0.2]])
    return firm_col


def test_query_without_matter_id_filters_on_firm_id_only(monkeypatch):
    import backend.main as m
    firm_col = _install_firm_col(monkeypatch, m)
    chunks = [{"id": "c1", "text": "t", "document_id": "d1"}]

    results = _semantic_search_firm(_FakeSearchRequest(), chunks)

    assert firm_col.query_calls[0]["where"] == {"firm_id": str(FIRM_ID)}
    assert len(results) == 1
    assert results[0]["chunk_id"] == "c1"


def test_query_with_matter_id_combines_both_filters_via_and(monkeypatch):
    """A flat {"firm_id": ..., "matter_id": ...} dict is invalid chromadb
    syntax (raises "Expected where to have exactly one operator") --
    confirmed empirically against the installed chromadb package. Must be
    combined via $and, not merged into one dict."""
    import backend.main as m
    firm_col = _install_firm_col(monkeypatch, m)
    chunks = [{"id": "c1", "text": "t", "document_id": "d1"}]

    _semantic_search_firm(_FakeSearchRequest(matter_id="m1"), chunks)

    where = firm_col.query_calls[0]["where"]
    assert where == {"$and": [{"firm_id": str(FIRM_ID)}, {"matter_id": "m1"}]}


def test_zero_regression_result_shape_unchanged_for_the_single_firm(monkeypatch):
    """The explicit filter is a no-op for the one firm whose data actually
    exists in Chroma today -- same result shape/content as before this
    change, just with a where clause now always attached."""
    import backend.main as m
    _install_firm_col(monkeypatch, m)
    chunks = [{
        "id": "c1", "text": "The lease runs for 12 months.", "document_id": "d1",
        "matter_id": "m1", "document_filename": "Lease.pdf",
    }]

    results = _semantic_search_firm(_FakeSearchRequest(), chunks)

    assert results == [{
        "result_source": "firm", "chunk_id": "c1", "text": "The lease runs for 12 months.",
        "similarity": 0.9, "document_id": "d1", "matter_id": "m1", "page_number": None,
        "chunk_index": None, "filename": "Lease.pdf", "document_type": None, "court": None,
        "matter_type": None, "legal_source_type": None, "authority_strength": None,
        "document_status": None, "provenance_document_type": None, "matter_number": None,
        "matter_name": None, "client_id": None, "client_name": None,
    }]


# ── scripts/backfill_chroma_firm_id.py: build_plan()/apply_plan() ──────────

class _FakeChromaCollection:
    def __init__(self, ids=None, metadatas=None):
        self._ids = list(ids or [])
        self._metadatas = list(metadatas or [])
        self.update_calls = []

    def get(self, ids=None, include=None):
        pairs = dict(zip(self._ids, self._metadatas))
        found = [i for i in (ids or self._ids) if i in pairs]
        return {"ids": found, "metadatas": [pairs[i] for i in found]}

    def update(self, ids, metadatas):
        self.update_calls.append(list(ids))
        for cid, meta in zip(ids, metadatas):
            idx = self._ids.index(cid)
            self._metadatas[idx] = meta


class _FakeConnection:
    def __init__(self, chunk_ids):
        self.chunk_ids = chunk_ids

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT id FROM chunks WHERE firm_id=$1 AND chunk_source='firm'"):
            return [{"id": cid} for cid in self.chunk_ids]
        raise NotImplementedError(f"unhandled query: {q}")


def test_build_plan_flags_chunks_missing_firm_id():
    conn = _FakeConnection(chunk_ids=["a", "b"])
    col = _FakeChromaCollection(
        ids=["a", "b"],
        metadatas=[{"document_id": "d1"}, {"document_id": "d2", "firm_id": str(FIRM_ID)}],
    )

    plan = asyncio.run(build_plan(conn, lambda: col, FIRM_ID))

    assert [cid for cid, _ in plan["to_backfill"]] == ["a"]
    assert plan["to_backfill"][0][1] == {"document_id": "d1", "firm_id": str(FIRM_ID)}
    assert plan["already_ok"] == 1
    assert plan["missing_in_chroma"] == 0


def test_build_plan_flags_chunks_missing_from_chroma_entirely():
    conn = _FakeConnection(chunk_ids=["a"])
    col = _FakeChromaCollection(ids=[], metadatas=[])

    plan = asyncio.run(build_plan(conn, lambda: col, FIRM_ID))

    assert plan["to_backfill"] == []
    assert plan["missing_in_chroma"] == 1


def test_apply_plan_writes_metadata_only_via_update_not_upsert():
    col = _FakeChromaCollection(ids=["a"], metadatas=[{"document_id": "d1"}])
    plan = {"to_backfill": [("a", {"document_id": "d1", "firm_id": str(FIRM_ID)})], "already_ok": 0}

    summary = asyncio.run(apply_plan(plan, lambda: col))

    assert summary == {"backfilled": 1, "already_correct": 0}
    assert col.update_calls == [["a"]]
    assert col._metadatas[0] == {"document_id": "d1", "firm_id": str(FIRM_ID)}


# ── POST /api/admin/backfill-chroma-firm-id ─────────────────────────────────

class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


class _FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}


def test_admin_endpoint_rejects_without_a_valid_admin_token(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "ADMIN_TOKEN", "real-admin-token")
    monkeypatch.setattr(m, "_db_pool", _FakePool(_FakeConnection(chunk_ids=[])))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(admin_backfill_chroma_firm_id(_FakeRequest(headers={})))
    assert exc_info.value.status_code == 403


def test_admin_endpoint_backfills_and_reports_a_summary(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "ADMIN_TOKEN", "real-admin-token")
    monkeypatch.setattr(m, "_db_pool", _FakePool(_FakeConnection(chunk_ids=["a", "b"])))

    firm_col = _FakeChromaCollection(
        ids=["a", "b"],
        metadatas=[{"document_id": "d1"}, {"document_id": "d2", "firm_id": str(FIRM_ID)}],
    )
    monkeypatch.setattr(m, "get_chroma_collections", lambda: (firm_col, object(), object()))

    result = asyncio.run(admin_backfill_chroma_firm_id(_FakeRequest(headers={"X-Admin-Token": "real-admin-token"})))

    assert result == {"backfilled": 1, "already_correct": 1}
    assert firm_col.update_calls == [["a"]]
