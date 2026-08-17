"""
Unit tests for the temporary POST /api/admin/backfill-chunk-hashes endpoint
in backend/main.py — auth-gating and correct wiring to the real
build_plan()/apply_plan() functions in scripts/backfill_chunk_content_hash.py
(not a reimplementation; the endpoint imports and calls them directly).

The underlying backfill logic itself is already covered against real
staging Postgres + real local ChromaDB elsewhere in this session's work —
these tests only need to prove the endpoint's own wiring: admin-token
gating, and that it correctly hands the app's live _db_pool/Chroma
collections to the shared functions and returns their summary.

Called directly as plain async functions, same convention as
tests/test_clients_api.py.
"""
import asyncio
import uuid

import pytest
from fastapi import HTTPException

from backend.main import FIRM_ID, admin_backfill_chunk_hashes, admin_verify_chunk_hashes


class FakeChromaCollection:
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


class FakeConnection:
    def __init__(self, chunks):
        self.chunks = chunks

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT id, content_hash FROM chunks WHERE firm_id=$1 AND chunk_source=$2"):
            firm_id, source = args[0], args[1]
            rows = [{"id": c["id"], "content_hash": c["content_hash"]}
                    for c in self.chunks if c["firm_id"] == firm_id and c["chunk_source"] == source]
            if len(args) >= 3 and args[2] is not None:
                rows = rows[:args[2]]
            return rows
        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, chunks):
        self.conn = FakeConnection(chunks)

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


class FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}


def _chunk(cid, content_hash, source="firm"):
    return {"id": cid, "firm_id": FIRM_ID, "chunk_source": source, "content_hash": content_hash}


def test_rejects_without_a_valid_admin_token(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "ADMIN_TOKEN", "real-admin-token")
    monkeypatch.setattr(m, "_db_pool", FakePool([]))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(admin_backfill_chunk_hashes(FakeRequest(headers={})))
    assert exc_info.value.status_code == 403


def test_backfills_only_the_mismatched_chunks_and_reports_a_summary(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "ADMIN_TOKEN", "real-admin-token")

    chunks = [_chunk("a", "correct-hash-a"), _chunk("b", "correct-hash-b")]
    monkeypatch.setattr(m, "_db_pool", FakePool(chunks))

    firm_col = FakeChromaCollection(
        ids=["a", "b"],
        # 'a' is drifted/missing content_hash (pre-fix state); 'b' already correct
        metadatas=[{"document_id": "d1"}, {"content_hash": "correct-hash-b"}],
    )
    legal_col = FakeChromaCollection()
    zlr_col = FakeChromaCollection()
    monkeypatch.setattr(m, "get_chroma_collections", lambda: (firm_col, legal_col, zlr_col))

    result = asyncio.run(admin_backfill_chunk_hashes(FakeRequest(headers={"X-Admin-Token": "real-admin-token"})))

    assert result["backfilled"] == 1
    assert result["already_correct"] == 1
    assert firm_col.update_calls == [["a"]]
    assert legal_col.update_calls == []
    assert zlr_col.update_calls == []


# ── admin_verify_chunk_hashes ────────────────────────────────────────────

def test_verify_rejects_without_a_valid_admin_token(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "ADMIN_TOKEN", "real-admin-token")
    monkeypatch.setattr(m, "_db_pool", FakePool([]))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(admin_verify_chunk_hashes(FakeRequest(headers={})))
    assert exc_info.value.status_code == 403


def test_verify_reports_real_match_and_mismatch_per_chunk(monkeypatch):
    """The whole point of this endpoint: real per-chunk hash values, not a
    count that could itself be wrong -- a matching and a mismatched chunk
    must be distinguishable in the response, not just summarized away."""
    import backend.main as m
    monkeypatch.setattr(m, "ADMIN_TOKEN", "real-admin-token")

    chunks = [_chunk("a", "hash-a"), _chunk("b", "hash-b")]
    monkeypatch.setattr(m, "_db_pool", FakePool(chunks))

    firm_col = FakeChromaCollection(
        ids=["a", "b"],
        metadatas=[{"content_hash": "hash-a"}, {"content_hash": "WRONG"}],
    )
    legal_col = FakeChromaCollection()
    zlr_col = FakeChromaCollection()
    monkeypatch.setattr(m, "get_chroma_collections", lambda: (firm_col, legal_col, zlr_col))

    result = asyncio.run(admin_verify_chunk_hashes(FakeRequest(headers={"X-Admin-Token": "real-admin-token"})))

    by_id = {e["chunk_id"]: e for e in result["firm"]}
    assert by_id["a"]["postgres_hash"] == "hash-a"
    assert by_id["a"]["chroma_hash"] == "hash-a"
    assert by_id["a"]["match"] is True
    assert by_id["b"]["postgres_hash"] == "hash-b"
    assert by_id["b"]["chroma_hash"] == "WRONG"
    assert by_id["b"]["match"] is False


def test_verify_flags_a_chunk_missing_from_chroma_entirely(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "ADMIN_TOKEN", "real-admin-token")

    chunks = [_chunk("a", "hash-a")]
    monkeypatch.setattr(m, "_db_pool", FakePool(chunks))

    firm_col = FakeChromaCollection(ids=[], metadatas=[])  # not indexed at all
    legal_col = FakeChromaCollection()
    zlr_col = FakeChromaCollection()
    monkeypatch.setattr(m, "get_chroma_collections", lambda: (firm_col, legal_col, zlr_col))

    result = asyncio.run(admin_verify_chunk_hashes(FakeRequest(headers={"X-Admin-Token": "real-admin-token"})))

    entry = result["firm"][0]
    assert entry["present_in_chroma"] is False
    assert entry["chroma_hash"] is None
    assert entry["match"] is False
