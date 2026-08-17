"""
Unit tests for reconcile_chroma_index()'s content_hash-based drift
detection in backend/main.py.

This is the fast, mock-based regression guard for the comparison logic —
the authoritative proof that the fix works end to end (real staging
Postgres + real local ChromaDB, reproducing the actual count-matches-but-
content-differs bug and confirming the new logic detects and repairs it)
was done separately as a real-infrastructure test, not something an
in-memory mock can substitute for (see the session's diagnosis: a bare
count comparison can't be faked into "missing" a real content mismatch
the way this test's FakeChromaCollection can trivially be told to have
one — the point of these tests is only to guard the comparison/targeting
logic against future regressions, cheaply and in CI).

Called directly as plain async functions, same convention as
tests/test_clients_api.py.
"""
import asyncio
import uuid

from backend.main import FIRM_ID, reconcile_chroma_index


class FakeChromaCollection:
    def __init__(self, name, ids=None, metadatas=None):
        self.name = name
        self._ids = list(ids or [])
        self._metadatas = list(metadatas or [])
        self.upsert_calls = []
        self.delete_calls = []

    def count(self):
        return len(self._ids)

    def get(self, ids=None, include=None):
        if ids is None:
            return {"ids": list(self._ids), "metadatas": list(self._metadatas)}
        pairs = dict(zip(self._ids, self._metadatas))
        found = [i for i in ids if i in pairs]
        return {"ids": found, "metadatas": [pairs[i] for i in found]}

    def upsert(self, ids, embeddings, documents, metadatas):
        self.upsert_calls.append(list(ids))
        for cid, meta in zip(ids, metadatas):
            if cid in self._ids:
                self._metadatas[self._ids.index(cid)] = meta
            else:
                self._ids.append(cid)
                self._metadatas.append(meta)

    def delete(self, ids):
        self.delete_calls.append(list(ids))
        for cid in ids:
            if cid in self._ids:
                idx = self._ids.index(cid)
                self._ids.pop(idx)
                self._metadatas.pop(idx)


class FakeConnection:
    def __init__(self, chunks):
        self.chunks = chunks  # list of dicts with id, content_hash, text, etc.

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT id, content_hash FROM chunks WHERE firm_id=$1 AND chunk_source=$2"):
            firm_id, source = args
            return [{"id": c["id"], "content_hash": c["content_hash"]}
                    for c in self.chunks if c["firm_id"] == firm_id and c["chunk_source"] == source]
        if q.startswith("SELECT * FROM chunks WHERE firm_id=$1 AND chunk_source=$2 AND id = ANY($3)"):
            firm_id, source, ids = args
            return [dict(c) for c in self.chunks
                    if c["firm_id"] == firm_id and c["chunk_source"] == source and c["id"] in ids]
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


def _chunk(cid, content_hash, source="firm", firm_id=FIRM_ID, text="some text"):
    return {
        "id": cid, "firm_id": firm_id, "chunk_source": source, "content_hash": content_hash,
        "text": text, "document_id": str(uuid.uuid4()), "matter_id": "m1", "chunk_index": 0, "page_number": 1,
    }


def _setup(monkeypatch, chunks, chroma_ids=None, chroma_metadatas=None):
    import backend.main as m
    pool = FakePool(chunks)
    monkeypatch.setattr(m, "_db_pool", pool)
    firm_col = FakeChromaCollection("firm", chroma_ids, chroma_metadatas)
    legal_col = FakeChromaCollection("legal")
    zlr_col = FakeChromaCollection("zlr")
    monkeypatch.setattr(m, "get_chroma_collections", lambda: (firm_col, legal_col, zlr_col))
    monkeypatch.setattr(m, "embed_texts", lambda texts: [[0.0] * 4 for _ in texts])
    return firm_col, legal_col, zlr_col


def test_matching_hashes_are_left_completely_untouched(monkeypatch):
    """The core case the old count-only check got right by luck, not
    design — must still work, and must do zero writes when truly synced."""
    chunks = [_chunk("a", "hash-a"), _chunk("b", "hash-b")]
    firm_col, _, _ = _setup(
        monkeypatch, chunks,
        chroma_ids=["a", "b"],
        chroma_metadatas=[{"content_hash": "hash-a"}, {"content_hash": "hash-b"}],
    )

    asyncio.run(reconcile_chroma_index())

    assert firm_col.upsert_calls == []
    assert firm_col.delete_calls == []


def test_content_mismatch_with_equal_counts_is_detected_and_repaired(monkeypatch):
    """The exact bug: same count, different content under one id — the old
    count-only check would have reported this as synced."""
    chunks = [_chunk("a", "correct-hash-a"), _chunk("b", "correct-hash-b")]
    firm_col, _, _ = _setup(
        monkeypatch, chunks,
        chroma_ids=["a", "b"],
        # 'a' has drifted -- Chroma's stored hash doesn't match Postgres's.
        # Count still matches (2 == 2).
        chroma_metadatas=[{"content_hash": "WRONG-DRIFTED-HASH"}, {"content_hash": "correct-hash-b"}],
    )
    assert firm_col.count() == 2  # counts match -- the old check would stop here

    asyncio.run(reconcile_chroma_index())

    assert firm_col.upsert_calls == [["a"]], "only the mismatched chunk should be re-indexed"


def test_missing_from_chroma_entirely_is_indexed(monkeypatch):
    chunks = [_chunk("a", "hash-a"), _chunk("b", "hash-b")]
    firm_col, _, _ = _setup(
        monkeypatch, chunks,
        chroma_ids=["a"],
        chroma_metadatas=[{"content_hash": "hash-a"}],
    )

    asyncio.run(reconcile_chroma_index())

    assert firm_col.upsert_calls == [["b"]]


def test_stray_chroma_entry_with_no_postgres_row_is_removed(monkeypatch):
    chunks = [_chunk("a", "hash-a")]
    firm_col, _, _ = _setup(
        monkeypatch, chunks,
        chroma_ids=["a", "orphan"],
        chroma_metadatas=[{"content_hash": "hash-a"}, {"content_hash": "whatever"}],
    )

    asyncio.run(reconcile_chroma_index())

    assert firm_col.upsert_calls == []
    assert firm_col.delete_calls == [["orphan"]]


def test_multiple_sources_are_reconciled_independently(monkeypatch):
    import backend.main as m
    chunks = [
        _chunk("a", "hash-a", source="firm"),
        _chunk("z", "hash-z", source="zlr"),
    ]
    pool = FakePool(chunks)
    monkeypatch.setattr(m, "_db_pool", pool)
    firm_col = FakeChromaCollection("firm", ["a"], [{"content_hash": "DRIFTED"}])
    legal_col = FakeChromaCollection("legal")
    zlr_col = FakeChromaCollection("zlr", ["z"], [{"content_hash": "hash-z"}])
    monkeypatch.setattr(m, "get_chroma_collections", lambda: (firm_col, legal_col, zlr_col))
    monkeypatch.setattr(m, "embed_texts", lambda texts: [[0.0] * 4 for _ in texts])

    asyncio.run(reconcile_chroma_index())

    assert firm_col.upsert_calls == [["a"]]
    assert zlr_col.upsert_calls == []  # already correct, untouched
    assert legal_col.upsert_calls == []  # nothing in Postgres for this source
