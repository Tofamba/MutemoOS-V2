"""
Unit tests for GET /api/documents/recent in backend/main.py — the one new
backend endpoint the Dashboard tab needed (everything else it uses is
reused directly from already-existing endpoints/data: GET /api/matters
already carries progress_notes and fee/deadline fields per matter,
GET /api/reports/practice-area-breakdown and GET /api/reports/history
already exist unchanged).

Called directly as plain async functions, same convention as
tests/test_clients_api.py.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from backend.main import FIRM_ID, list_recent_documents


class FakeConnection:
    def __init__(self, documents, matters):
        self.documents = documents
        self.matters = {m["id"]: m for m in matters}

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT d.*, m.name AS matter_name, m.client_name AS matter_client_name"):
            firm_id, limit = args
            rows = [d for d in self.documents if d["firm_id"] == firm_id and d["status"] == "complete"]
            rows.sort(key=lambda d: d["uploaded_at"], reverse=True)
            rows = rows[:limit]
            out = []
            for d in rows:
                m = self.matters.get(d["matter_id"], {})
                merged = dict(d)
                merged["matter_name"] = m.get("name")
                merged["matter_client_name"] = m.get("client_name")
                out.append(merged)
            return out
        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, documents, matters):
        self.conn = FakeConnection(documents, matters)

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _doc(doc_id, matter_id, filename, status="complete", uploaded_at=None, firm_id=FIRM_ID):
    return {
        "id": doc_id, "matter_id": matter_id, "firm_id": firm_id, "filename": filename,
        "document_type": None, "matter_type": None, "parties": None, "doc_date": None, "court": None,
        "word_count": 0, "page_count": 1, "chunk_count": 0, "ocr_used": False, "ocr_confidence": None,
        "needs_review": False, "status": status, "error_message": None,
        "uploaded_at": uploaded_at or datetime.now(timezone.utc), "uploaded_by": None,
    }


def _matter(matter_id, name, client_name, firm_id=FIRM_ID):
    return {"id": matter_id, "name": name, "client_name": client_name, "firm_id": firm_id}


def _fake_request():
    return None


def test_returns_only_complete_documents_ordered_newest_first(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    documents = [
        _doc(uuid.uuid4(), matter_id, "old.pdf", uploaded_at=now - timedelta(days=2)),
        _doc(uuid.uuid4(), matter_id, "new.pdf", uploaded_at=now),
        _doc(uuid.uuid4(), matter_id, "still-processing.pdf", status="processing", uploaded_at=now),
    ]
    matters = [_matter(matter_id, "Moyo v Dube", "John Moyo")]
    pool = FakePool(documents, matters)
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(list_recent_documents(_fake_request()))

    filenames = [r["filename"] for r in result]
    assert filenames == ["new.pdf", "old.pdf"]  # processing one excluded, newest first


def test_includes_matter_and_client_context(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    documents = [_doc(uuid.uuid4(), matter_id, "lease.pdf")]
    matters = [_matter(matter_id, "Moyo v Dube", "John Moyo")]
    pool = FakePool(documents, matters)
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(list_recent_documents(_fake_request()))

    assert result[0]["matter_name"] == "Moyo v Dube"
    assert result[0]["matter_client_name"] == "John Moyo"


def test_respects_limit_parameter(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    documents = [_doc(uuid.uuid4(), matter_id, f"doc{i}.pdf") for i in range(5)]
    matters = [_matter(matter_id, "Moyo v Dube", "John Moyo")]
    pool = FakePool(documents, matters)
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(list_recent_documents(_fake_request(), limit=2))

    assert len(result) == 2
