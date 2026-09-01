"""
Unit tests for scripts/copy_shared_corpus_to_new_firm.py's do_import() --
specifically the 2026-09-01 fix for a real, silent data-loss bug found
while building the corpus-snapshot tooling on top of this script
(see [[project_corpus_snapshot_tooling]]): the import INSERTs for
legal_updates, zlr_entries, and chunks omitted legal_source_type/
authority_strength/validity_flag entirely, even though do_export()'s
`SELECT *` always captured them. Every firm ever onboarded via this
script therefore got NULL authority classification on its entire copied
shared corpus -- backend/grounding.py's compute_grounding() keys
"authority_hits"/"sources_sufficient" purely off authority_strength, so
this would have silently degraded every synthesized answer's grounding
confidence for every onboarded firm, not just legislation with a
validity dispute.

Postgres is faked (FakeConnection, same convention as every other script
test this session -- no real Postgres reachable from this environment).
Chroma is real: chromadb is a local embedded library, so upserting into
and reading back from a genuine temp-directory PersistentClient is a real
test, not a mock -- confirms do_import()'s Chroma write path actually
round-trips, not just that it was called.
"""
import asyncio
import uuid
from datetime import datetime, timezone

import chromadb

from scripts.copy_shared_corpus_to_new_firm import do_import


class FakeConnection:
    def __init__(self):
        self.executed = []

    async def execute(self, query, *args):
        q = " ".join(query.split())
        self.executed.append((q, args))
        return "INSERT 0 1"

    async def close(self):
        pass


def _legal_updates_row(**overrides):
    row = {
        "id": str(uuid.uuid4()), "filename": "Some Act.pdf", "source_type": "legislation",
        "source_name": "Veritas", "reference": "Some Act", "document_type": "statute",
        "matter_type": "constitutional", "doc_date": None, "court": None, "word_count": 100,
        "chunk_count": 2, "status": "complete", "ocr_used": False, "error_message": None,
        "uploaded_at": str(datetime.now(timezone.utc)), "source_url": None, "scraped_at": None,
        "ocr_confidence": None, "needs_review": False,
        "legal_source_type": "statute", "authority_strength": "binding", "validity_flag": None,
    }
    row.update(overrides)
    return row


def _zlr_entry_row(**overrides):
    row = {
        "id": str(uuid.uuid4()), "filename": "S v Test.pdf", "source": "ZLR",
        "jurisdiction": "Zimbabwe", "authority_weight": None, "volume_year": "2026",
        "zimlii_url": None, "case_name": "S v Test", "citation": "2026 ZWSC 1",
        "judgment_number": None, "court": "Supreme Court", "judge": None, "case_type": None,
        "hearing_date": None, "judgment_date": None, "subject_chains": "[]",
        "taxonomy_category": "General", "summary": "", "raw_text": "full text", "word_count": 50,
        "chunk_count": 1, "ocr_used": False, "ocr_confidence": None, "needs_review": False,
        "uploaded_at": str(datetime.now(timezone.utc)),
        "legal_source_type": "supreme_court_judgment", "authority_strength": "binding",
    }
    row.update(overrides)
    return row


def _chunk_row(document_id, chunk_source="legal", **overrides):
    row = {
        "id": str(uuid.uuid4()), "document_id": document_id, "matter_id": None,
        "chunk_source": chunk_source, "text": "chunk text", "chunk_index": 0, "page_number": 1,
        "zlr_item_id": document_id if chunk_source == "zlr" else None, "citation": None,
        "case_name": None, "taxonomy_category": None, "source_type": "legislation",
        "source_name": "Veritas", "reference": "Some Act", "validity_flag": None,
        "created_at": str(datetime.now(timezone.utc)),
    }
    row.update(overrides)
    return row


def _write_export(tmp_path, export):
    import json
    p = tmp_path / "export.json"
    p.write_text(json.dumps(export), encoding="utf-8")
    return str(p)


def test_do_import_writes_authority_fields_for_legal_updates(tmp_path, monkeypatch):
    import scripts.copy_shared_corpus_to_new_firm as m
    conn = FakeConnection()

    class _FakeAsyncpg:
        @staticmethod
        async def connect(database_url):
            return conn

        @staticmethod
        async def close(*a):
            pass
    monkeypatch.setattr(m, "asyncpg", _FakeAsyncpg())

    row = _legal_updates_row(validity_flag="Enactment challenged — no referendum held per s.328")
    export = {
        "source_firm_id": str(uuid.uuid4()),
        "legal_updates": [row], "zlr_entries": [], "chunks": [],
        "chroma": {"legal_updates": {"ids": [], "embeddings": [], "documents": [], "metadatas": []},
                   "zlr_index": {"ids": [], "embeddings": [], "documents": [], "metadatas": []}},
    }
    in_path = _write_export(tmp_path, export)
    chroma_path = str(tmp_path / "chroma")

    asyncio.run(m.do_import("postgres://fake", chroma_path, str(uuid.uuid4()), in_path, apply=True))

    inserts = [c for c in conn.executed if c[0].startswith("INSERT INTO legal_updates")]
    assert len(inserts) == 1
    query, args = inserts[0]
    assert "legal_source_type" in query and "authority_strength" in query and "validity_flag" in query
    assert "binding" in args
    assert "statute" in args
    assert "Enactment challenged — no referendum held per s.328" in args


def test_do_import_writes_authority_fields_for_zlr_entries(tmp_path, monkeypatch):
    import scripts.copy_shared_corpus_to_new_firm as m
    conn = FakeConnection()

    class _FakeAsyncpg:
        @staticmethod
        async def connect(database_url):
            return conn
    monkeypatch.setattr(m, "asyncpg", _FakeAsyncpg())

    row = _zlr_entry_row()
    export = {
        "source_firm_id": str(uuid.uuid4()),
        "legal_updates": [], "zlr_entries": [row], "chunks": [],
        "chroma": {"legal_updates": {"ids": [], "embeddings": [], "documents": [], "metadatas": []},
                   "zlr_index": {"ids": [], "embeddings": [], "documents": [], "metadatas": []}},
    }
    in_path = _write_export(tmp_path, export)
    chroma_path = str(tmp_path / "chroma")

    asyncio.run(m.do_import("postgres://fake", chroma_path, str(uuid.uuid4()), in_path, apply=True))

    inserts = [c for c in conn.executed if c[0].startswith("INSERT INTO zlr_entries")]
    assert len(inserts) == 1
    query, args = inserts[0]
    assert "legal_source_type" in query and "authority_strength" in query
    assert "supreme_court_judgment" in args
    assert "binding" in args


def test_do_import_writes_validity_flag_for_chunks(tmp_path, monkeypatch):
    import scripts.copy_shared_corpus_to_new_firm as m
    conn = FakeConnection()

    class _FakeAsyncpg:
        @staticmethod
        async def connect(database_url):
            return conn
    monkeypatch.setattr(m, "asyncpg", _FakeAsyncpg())

    doc_id = str(uuid.uuid4())
    chunk = _chunk_row(doc_id, validity_flag="Enactment challenged — no referendum held per s.328")
    export = {
        "source_firm_id": str(uuid.uuid4()),
        "legal_updates": [], "zlr_entries": [], "chunks": [chunk],
        "chroma": {"legal_updates": {"ids": [], "embeddings": [], "documents": [], "metadatas": []},
                   "zlr_index": {"ids": [], "embeddings": [], "documents": [], "metadatas": []}},
    }
    in_path = _write_export(tmp_path, export)
    chroma_path = str(tmp_path / "chroma")

    asyncio.run(m.do_import("postgres://fake", chroma_path, str(uuid.uuid4()), in_path, apply=True))

    inserts = [c for c in conn.executed if c[0].startswith("INSERT INTO chunks")]
    assert len(inserts) == 1
    query, args = inserts[0]
    assert "validity_flag" in query
    assert "Enactment challenged — no referendum held per s.328" in args


def test_do_import_backward_compatible_with_export_missing_new_fields(tmp_path, monkeypatch):
    """An export produced by the pre-fix script (or a Postgres instance
    predating these columns) has legal_updates/zlr_entries/chunks rows
    with no legal_source_type/authority_strength/validity_flag keys at
    all. do_import() must not crash -- these should import as NULL, not
    raise KeyError."""
    import scripts.copy_shared_corpus_to_new_firm as m
    conn = FakeConnection()

    class _FakeAsyncpg:
        @staticmethod
        async def connect(database_url):
            return conn
    monkeypatch.setattr(m, "asyncpg", _FakeAsyncpg())

    row = _legal_updates_row()
    del row["legal_source_type"], row["authority_strength"], row["validity_flag"]
    export = {
        "source_firm_id": str(uuid.uuid4()),
        "legal_updates": [row], "zlr_entries": [], "chunks": [],
        "chroma": {"legal_updates": {"ids": [], "embeddings": [], "documents": [], "metadatas": []},
                   "zlr_index": {"ids": [], "embeddings": [], "documents": [], "metadatas": []}},
    }
    in_path = _write_export(tmp_path, export)
    chroma_path = str(tmp_path / "chroma")

    # Must not raise.
    asyncio.run(m.do_import("postgres://fake", chroma_path, str(uuid.uuid4()), in_path, apply=True))

    inserts = [c for c in conn.executed if c[0].startswith("INSERT INTO legal_updates")]
    assert len(inserts) == 1
    _, args = inserts[0]
    assert args[-1] is None  # validity_flag
    assert args[-2] is None  # authority_strength
    assert args[-3] is None  # legal_source_type


def test_do_import_chroma_write_really_round_trips(tmp_path, monkeypatch):
    """Real chromadb (embedded, local -- no mocking) round trip: a vector
    written by do_import() is actually retrievable afterward by id, with
    its metadata intact. Confirms the Chroma write path really works, not
    just that upsert() was called."""
    import scripts.copy_shared_corpus_to_new_firm as m
    conn = FakeConnection()

    class _FakeAsyncpg:
        @staticmethod
        async def connect(database_url):
            return conn
    monkeypatch.setattr(m, "asyncpg", _FakeAsyncpg())

    doc_id = str(uuid.uuid4())
    chunk_id = str(uuid.uuid4())
    export = {
        "source_firm_id": str(uuid.uuid4()),
        "legal_updates": [], "zlr_entries": [], "chunks": [],
        "chroma": {
            "legal_updates": {
                "ids": [chunk_id],
                "embeddings": [[0.1, 0.2, 0.3]],
                "documents": ["Section 1 of the Act..."],
                "metadatas": [{"document_id": doc_id, "matter_id": "legal_updates", "chunk_index": 0, "page_number": 1}],
            },
            "zlr_index": {"ids": [], "embeddings": [], "documents": [], "metadatas": []},
        },
    }
    in_path = _write_export(tmp_path, export)
    chroma_path = str(tmp_path / "chroma")

    asyncio.run(m.do_import("postgres://fake", chroma_path, str(uuid.uuid4()), in_path, apply=True))

    client = chromadb.PersistentClient(path=chroma_path)
    col = client.get_collection("legal_updates")
    result = col.get(ids=[chunk_id], include=["documents", "metadatas"])
    assert result["ids"] == [chunk_id]
    assert result["documents"][0] == "Section 1 of the Act..."
    assert result["metadatas"][0]["document_id"] == doc_id


def test_do_import_dry_run_writes_nothing(tmp_path, monkeypatch):
    """apply=False must not touch Postgres or Chroma at all -- the
    existing --apply/dry-run contract, unaffected by this fix."""
    import scripts.copy_shared_corpus_to_new_firm as m
    conn = FakeConnection()

    class _FakeAsyncpg:
        @staticmethod
        async def connect(database_url):
            return conn
    monkeypatch.setattr(m, "asyncpg", _FakeAsyncpg())

    row = _legal_updates_row()
    export = {
        "source_firm_id": str(uuid.uuid4()),
        "legal_updates": [row], "zlr_entries": [], "chunks": [],
        "chroma": {"legal_updates": {"ids": [], "embeddings": [], "documents": [], "metadatas": []},
                   "zlr_index": {"ids": [], "embeddings": [], "documents": [], "metadatas": []}},
    }
    in_path = _write_export(tmp_path, export)
    chroma_path = str(tmp_path / "chroma")

    asyncio.run(m.do_import("postgres://fake", chroma_path, str(uuid.uuid4()), in_path, apply=False))

    assert conn.executed == []
