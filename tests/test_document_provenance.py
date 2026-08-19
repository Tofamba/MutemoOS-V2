"""
Unit tests for client/matter document provenance metadata (backend/main.py) —
document_type/document_status/description/confidentiality on the Vault
documents table, and surfacing document_status/provenance_document_type/
matter_number/matter_name/client_id/client_name in firm-document search
results.

This is entirely separate from legal_source_type / authority_ranker.py,
which grounds the ZLR/legislation/case-law corpus and is untouched here —
see run_migrations()'s provenance-metadata migration comment for the full
reasoning on why this uses a new provenance_document_type column rather
than reusing the existing (AI-classified) documents.document_type.

Called directly as plain async functions, same convention as
tests/test_client_intake.py and tests/test_bulk_import_clients.py.

Note on upload_document()'s signature: its optional fields default to
Form(None) (a FastAPI marker object), not plain None — that default is
only resolved to None by FastAPI's own request-parsing/dependency-injection
layer, which calling the function directly (bypassing routing, same as
every other endpoint test in this repo) never goes through. So every call
below passes all four optional fields explicitly rather than omitting any
and relying on the bare Python default.
"""
import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import BackgroundTasks, HTTPException

from backend.main import (
    DOCUMENT_CONFIDENTIALITY_LEVELS,
    DOCUMENT_STATUSES,
    PROVENANCE_DOCUMENT_TYPES,
    SearchRequest,
    _semantic_search_firm,
    upload_document,
)


class FakeUploadFile:
    def __init__(self, filename, content: bytes = b"some document text"):
        self.filename = filename
        self._content = content

    async def read(self):
        return self._content


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, matters=None):
        self.matters = matters if matters is not None else []
        self.documents = []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT id FROM matters WHERE id=$1 AND firm_id=$2"):
            matter_id, firm_id = args
            for m in self.matters:
                if m["id"] == matter_id and m["firm_id"] == firm_id:
                    return {"id": m["id"]}
            return None

        if q.startswith("INSERT INTO documents"):
            (doc_id, matter_id, firm_id, filename, uploaded_by,
             provenance_document_type, document_status, description, confidentiality) = args
            row = {
                "id": doc_id, "matter_id": matter_id, "firm_id": firm_id, "filename": filename,
                "status": "processing", "uploaded_at": datetime.now(timezone.utc), "uploaded_by": uploaded_by,
                "provenance_document_type": provenance_document_type, "document_status": document_status,
                "description": description, "confidentiality": confidentiality,
            }
            self.documents.append(row)
            return row

        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")


class FakePool:
    def __init__(self, matters=None):
        self.conn = FakeConnection(matters)

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _matter_row(firm_id):
    return {"id": uuid.uuid4(), "firm_id": firm_id}


def _call_upload(matter_id, provenance_document_type=None, document_status=None,
                  description=None, confidentiality=None, filename="doc.pdf"):
    return upload_document(
        BackgroundTasks(), FakeUploadFile(filename), str(matter_id),
        provenance_document_type, document_status, description, confidentiality,
    )


# ── Manual upload: enum validation ───────────────────────────────────────

def test_upload_rejects_invalid_provenance_document_type(monkeypatch):
    import backend.main as m
    matter = _matter_row(m.FIRM_ID)
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter]))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_call_upload(matter["id"], provenance_document_type="NotARealType"))
    assert exc_info.value.status_code == 422


def test_upload_rejects_invalid_document_status(monkeypatch):
    import backend.main as m
    matter = _matter_row(m.FIRM_ID)
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter]))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_call_upload(matter["id"], document_status="NotARealStatus"))
    assert exc_info.value.status_code == 422


def test_upload_rejects_invalid_confidentiality(monkeypatch):
    import backend.main as m
    matter = _matter_row(m.FIRM_ID)
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter]))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_call_upload(matter["id"], confidentiality="TopSecret"))
    assert exc_info.value.status_code == 422


def test_every_enum_value_is_accepted(monkeypatch):
    """Every real value in each fixed enum must round-trip cleanly -- not
    just "something is rejected", but "the actual valid set works"."""
    import backend.main as m
    matter = _matter_row(m.FIRM_ID)
    pool = FakePool(matters=[matter])
    monkeypatch.setattr(m, "_db_pool", pool)

    for pdt in PROVENANCE_DOCUMENT_TYPES:
        result = asyncio.run(_call_upload(matter["id"], provenance_document_type=pdt))
        assert result["provenance_document_type"] == pdt

    for status in DOCUMENT_STATUSES:
        result = asyncio.run(_call_upload(matter["id"], document_status=status))
        assert result["document_status"] == status

    for level in DOCUMENT_CONFIDENTIALITY_LEVELS:
        result = asyncio.run(_call_upload(matter["id"], confidentiality=level))
        assert result["confidentiality"] == level


# ── Manual upload: document_status / confidentiality defaulting ─────────

def test_manual_upload_defaults_document_status_to_draft_when_omitted(monkeypatch):
    import backend.main as m
    matter = _matter_row(m.FIRM_ID)
    pool = FakePool(matters=[matter])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(_call_upload(matter["id"]))

    assert result["document_status"] == "Draft"
    assert pool.conn.documents[0]["document_status"] == "Draft"


def test_manual_upload_accepts_explicit_document_status(monkeypatch):
    import backend.main as m
    matter = _matter_row(m.FIRM_ID)
    pool = FakePool(matters=[matter])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(_call_upload(matter["id"], document_status="Executed"))

    assert result["document_status"] == "Executed"
    assert pool.conn.documents[0]["document_status"] == "Executed"


def test_manual_upload_defaults_confidentiality_to_standard_when_omitted(monkeypatch):
    import backend.main as m
    matter = _matter_row(m.FIRM_ID)
    pool = FakePool(matters=[matter])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(_call_upload(matter["id"]))

    assert result["confidentiality"] == "Standard"


def test_upload_rejects_nonexistent_matter_with_404(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[]))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_call_upload(uuid.uuid4()))
    assert exc_info.value.status_code == 404


# ── Search results: provenance metadata surfaced end to end ─────────────

def _make_chunk(**overrides):
    chunk = {
        "id": str(uuid.uuid4()),
        "document_id": str(uuid.uuid4()),
        "matter_id": str(uuid.uuid4()),
        "text": "the lease agreement rental deposit clause",
        "page_number": 1,
        "chunk_index": 0,
        "document_filename": "Lease Agreement.pdf",
        "document_type": "lease",
        "court": None,
        "matter_type": "conveyancing",
        "legal_source_type": None,
        "authority_strength": None,
        "document_status": "Final",
        "provenance_document_type": "Contract",
        "matter_number": "TC-004-01",
        "matter_name": "DEMO — Test Client — Property Sale",
        "matter_client_id": str(uuid.uuid4()),
        "client_name": "DEMO — Test Client",
    }
    chunk.update(overrides)
    return chunk


def _force_keyword_fallback(monkeypatch):
    import backend.main as m

    def _raise():
        raise RuntimeError("chroma not configured in this test environment")

    monkeypatch.setattr(m, "get_chroma_collections", _raise)


def test_search_result_carries_provenance_matter_and_client_fields(monkeypatch):
    _force_keyword_fallback(monkeypatch)
    chunk = _make_chunk()
    req = SearchRequest(query="lease agreement rental deposit", limit=8)

    results = _semantic_search_firm(req, [chunk])

    assert len(results) == 1
    r = results[0]
    assert r["document_status"] == "Final"
    assert r["provenance_document_type"] == "Contract"
    assert r["matter_id"] == chunk["matter_id"]
    assert r["matter_number"] == "TC-004-01"
    assert r["matter_name"] == "DEMO — Test Client — Property Sale"
    assert r["client_id"] == chunk["matter_client_id"]
    assert r["client_name"] == "DEMO — Test Client"


def test_search_result_handles_missing_matter_link_gracefully(monkeypatch):
    """A chunk whose document has no linked matter (LEFT JOIN produced
    NULLs) must not crash the result-building loop -- fields degrade to
    None rather than KeyError."""
    _force_keyword_fallback(monkeypatch)
    chunk = _make_chunk(
        matter_number=None, matter_name=None, matter_client_id=None, client_name=None,
    )
    req = SearchRequest(query="lease agreement rental deposit", limit=8)

    results = _semantic_search_firm(req, [chunk])

    assert len(results) == 1
    r = results[0]
    assert r["matter_number"] is None
    assert r["client_id"] is None
    assert r["client_name"] is None


# ── Existing behaviour unaffected ────────────────────────────────────────

def test_existing_search_result_fields_still_present(monkeypatch):
    """The provenance-metadata addition is additive -- every field a
    pre-existing caller already relies on must still be there, unchanged."""
    _force_keyword_fallback(monkeypatch)
    chunk = _make_chunk()
    req = SearchRequest(query="lease agreement rental deposit", limit=8)

    r = _semantic_search_firm(req, [chunk])[0]
    for key in ("result_source", "chunk_id", "text", "similarity", "document_id",
                "filename", "document_type", "court", "matter_type",
                "legal_source_type", "authority_strength"):
        assert key in r
