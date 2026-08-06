"""
Unit tests for the ChromaDB post-deploy readiness guard in backend/main.py.

Bug: lifespan() reaches `yield` (and Uvicorn starts accepting connections)
as soon as warm_up() is *scheduled*, not when reconcile_chroma_index()
actually finishes rebuilding the firm/legal/zlr collections — confirmed in
production, a request landed with legal=0 zlr=0 retrieval results over a
minute before "[startup] semantic search ready" printed. _retrieval_ready
(default False, flipped True once warm_up() finishes — success or not)
gates the four endpoints that actually read those collections:
search_zlr, search_documents, search_with_document, generate_document.

Called directly as plain async functions, same convention as this repo's
other backend tests — see tests/test_docx_export.py's docstring for why
(AUTH_ENABLED is False by default, so get_current_user() never touches
`request`).
"""

import asyncio

import pytest
from fastapi import HTTPException

import backend.main as m
from backend.main import (
    DocumentRequest,
    LegalUpdateSearchRequest,
    SearchRequest,
    _require_retrieval_ready,
    generate_document,
    search_documents,
    search_with_document,
    search_zlr,
)


class _PoisonPool:
    """A _db_pool stand-in that fails the test if anything actually tries
    to touch the database — proof the readiness guard short-circuits
    before any DB/ChromaDB access, not just before returning a result."""
    def acquire(self):
        raise AssertionError("DB should not be touched while retrieval is not ready")


class _SentinelTouchedError(Exception):
    """Raised by _SentinelPool the moment a query is attempted — proves the
    guard let execution proceed past it to normal endpoint logic, without
    needing to mock the entire downstream chroma/embedding pipeline."""


class _SentinelConn:
    async def fetch(self, *a, **k):
        raise _SentinelTouchedError()

    async def fetchrow(self, *a, **k):
        raise _SentinelTouchedError()


class _SentinelAcquireCtx:
    async def __aenter__(self):
        return _SentinelConn()

    async def __aexit__(self, *exc):
        return False


class _SentinelPool:
    def acquire(self):
        return _SentinelAcquireCtx()


class _EmptyConn:
    async def fetch(self, *a, **k):
        return []


class _EmptyAcquireCtx:
    async def __aenter__(self):
        return _EmptyConn()

    async def __aexit__(self, *exc):
        return False


class _EmptyPool:
    """Real (if trivial) DB responses — no chunks indexed for this firm —
    used to prove the endpoint runs its normal early-return logic once
    the guard has passed, not just that it avoided a crash."""
    def acquire(self):
        return _EmptyAcquireCtx()


class FakeUploadFile:
    def __init__(self, filename, content: bytes):
        self.filename = filename
        self._content = content

    async def read(self):
        return self._content


# ── _require_retrieval_ready itself ─────────────────────────────────────────

def test_raises_503_when_not_ready(monkeypatch):
    monkeypatch.setattr(m, "_retrieval_ready", False)
    with pytest.raises(HTTPException) as exc_info:
        _require_retrieval_ready()
    assert exc_info.value.status_code == 503
    assert "initializing" in exc_info.value.detail.lower()
    assert exc_info.value.headers.get("Retry-After") == "20"


def test_no_op_when_ready(monkeypatch):
    monkeypatch.setattr(m, "_retrieval_ready", True)
    _require_retrieval_ready()  # must not raise


# ── search_zlr ───────────────────────────────────────────────────────────

def test_search_zlr_503s_when_not_ready_without_touching_db(monkeypatch):
    monkeypatch.setattr(m, "_retrieval_ready", False)
    monkeypatch.setattr(m, "_db_pool", _PoisonPool())

    req = LegalUpdateSearchRequest(query="ministry of local government")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(search_zlr(req, None))
    assert exc_info.value.status_code == 503


def test_search_zlr_works_normally_once_ready(monkeypatch):
    monkeypatch.setattr(m, "_retrieval_ready", True)
    monkeypatch.setattr(m, "_db_pool", _EmptyPool())

    req = LegalUpdateSearchRequest(query="ministry of local government")
    result = asyncio.run(search_zlr(req, None))
    assert result == {"results": [], "message": "No ZLR entries indexed yet."}


# ── search_documents (/api/search) ──────────────────────────────────────

def test_search_documents_503s_when_not_ready_without_touching_db(monkeypatch):
    monkeypatch.setattr(m, "_retrieval_ready", False)
    monkeypatch.setattr(m, "_db_pool", _PoisonPool())

    req = SearchRequest(query="Chikwanha v Ministry of Local Government")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(search_documents(req, None))
    assert exc_info.value.status_code == 503


def test_search_documents_proceeds_past_guard_once_ready(monkeypatch):
    """Doesn't fully execute the retrieval pipeline (would need ChromaDB
    mocked too) — proves the guard let it through to normal logic by
    checking it reaches the DB, rather than stopping at 503."""
    monkeypatch.setattr(m, "_retrieval_ready", True)
    monkeypatch.setattr(m, "_db_pool", _SentinelPool())

    req = SearchRequest(query="Chikwanha v Ministry of Local Government")
    with pytest.raises(_SentinelTouchedError):
        asyncio.run(search_documents(req, None))


# ── search_with_document (/api/search/document) ─────────────────────────

def test_search_with_document_503s_when_not_ready_without_creating_a_job(monkeypatch):
    monkeypatch.setattr(m, "_retrieval_ready", False)
    monkeypatch.setattr(m, "_db_pool", _PoisonPool())
    jobs_before = dict(m._search_jobs)

    upload = FakeUploadFile("lease.pdf", b"not read")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(search_with_document(None, upload, "does this lease renew automatically?"))
    assert exc_info.value.status_code == 503
    assert m._search_jobs == jobs_before  # no job left behind


def test_search_with_document_proceeds_past_guard_once_ready(monkeypatch):
    monkeypatch.setattr(m, "_retrieval_ready", True)
    monkeypatch.setattr(m, "_db_pool", _SentinelPool())

    upload = FakeUploadFile("lease.pdf", b"lease body text")
    # Job creation itself doesn't touch the DB before spawning the
    # background task, so reaching this point (not 503) is the proof here.
    result = asyncio.run(search_with_document(None, upload, "does this lease renew automatically?"))
    assert "job_id" in result
    assert result["status"] == "pending"
    del m._search_jobs[result["job_id"]]  # don't leak state into other tests


# ── generate_document (/api/generate-document) ──────────────────────────

def test_generate_document_503s_when_not_ready_without_creating_a_job(monkeypatch):
    monkeypatch.setattr(m, "_retrieval_ready", False)
    monkeypatch.setattr(m, "_db_pool", _PoisonPool())
    jobs_before = dict(m._search_jobs)

    req = DocumentRequest(doc_type="heads_of_argument", facts="The respondent unlawfully increased rates.")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(generate_document(req, None))
    assert exc_info.value.status_code == 503
    assert m._search_jobs == jobs_before


def test_generate_document_proceeds_past_guard_once_ready(monkeypatch):
    monkeypatch.setattr(m, "_retrieval_ready", True)
    monkeypatch.setattr(m, "_db_pool", _SentinelPool())

    req = DocumentRequest(doc_type="heads_of_argument", facts="The respondent unlawfully increased rates.")
    result = asyncio.run(generate_document(req, None))
    assert "job_id" in result
    assert result["status"] == "pending"
    del m._search_jobs[result["job_id"]]


# ── endpoints that must NOT be gated ─────────────────────────────────────
# generate-affidavit does no corpus retrieval at all (prompt built directly
# from request fields), and legal-updates/search does plain keyword
# scoring against Postgres chunk rows, never ChromaDB — neither should be
# affected by _retrieval_ready in either state.

def test_generate_affidavit_is_not_gated(monkeypatch):
    from backend.main import AffidavitRequest, generate_affidavit

    monkeypatch.setattr(m, "_retrieval_ready", False)

    class _FakeMsg:
        content = [type("C", (), {"text": "AFFIDAVIT TEXT"})()]

    monkeypatch.setattr(m.client.messages, "create", lambda **k: _FakeMsg())

    req = AffidavitRequest(matter_summary="A dispute over unpaid rent.")
    result = asyncio.run(generate_affidavit(req, None))
    assert result == {"affidavit": "AFFIDAVIT TEXT"}


def test_legal_updates_search_is_not_gated(monkeypatch):
    from backend.main import search_legal_updates

    monkeypatch.setattr(m, "_retrieval_ready", False)
    monkeypatch.setattr(m, "_db_pool", _EmptyPool())

    req = LegalUpdateSearchRequest(query="rates increase")
    result = asyncio.run(search_legal_updates(req, None))
    assert result == {"answer": None, "results": [], "message": "No legislation or case law indexed yet."}


# ── /health/alerts observability ─────────────────────────────────────────

def test_health_alerts_reports_retrieval_ready_without_changing_status(monkeypatch):
    from backend.main import health_alerts

    monkeypatch.setattr(m, "_retrieval_ready", False)
    result = asyncio.run(health_alerts())
    assert result["retrieval_ready"] is False
    assert result["status"] == "ok"  # a normal post-deploy window, not an incident

    monkeypatch.setattr(m, "_retrieval_ready", True)
    result = asyncio.run(health_alerts())
    assert result["retrieval_ready"] is True
    assert result["status"] == "ok"


def test_health_ready_reflects_current_flag(monkeypatch):
    """/health/ready — the reliable readiness probe. Unlike /health/alerts,
    fastapi_alertengine's instrument(app) doesn't register anything at this
    path, so this handler isn't shadowed in production."""
    from backend.main import health_ready

    monkeypatch.setattr(m, "_retrieval_ready", False)
    assert asyncio.run(health_ready()) == {"retrieval_ready": False}

    monkeypatch.setattr(m, "_retrieval_ready", True)
    assert asyncio.run(health_ready()) == {"retrieval_ready": True}
