"""
Unit tests for the plain Search Vault query path's fire-and-poll job
pattern (POST /api/search, backend/main.py) -- extended from a single
synchronous request/response to the same job-store shape already used by
POST /api/search/document (see tests/test_multi_document_search.py),
because the full retrieval+synthesis pipeline can now run long enough on
a genuinely broad query (confirmed: 123.5s in production on a real
3-issue query) to exceed Cloudflare's ~100s edge timeout if held behind
one synchronous request.

This does not re-test the retrieval/ranking/citation-verification
pipeline itself (rerank, compute_grounding, verify_citations, etc. all
have their own coverage elsewhere, and none of that logic changed here --
only where it runs). These tests are scoped to the job-orchestration
mechanics: does the endpoint return a job_id and schedule background
work, does the background job reach COMPLETE with the right result shape
in the shared job store, does the "no results" case complete as a job
rather than as a synchronous early return, does an exception land the
job in FAILED, and does the status endpoint serve/guard it correctly.

Called directly as plain async functions, same convention as
tests/test_multi_document_search.py and tests/test_retrieval_readiness.py.
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from backend.main import (
    JobStatus,
    SearchRequest,
    _run_plain_search_job,
    _search_jobs,
    get_plain_search_job_status,
    search_documents,
)


@pytest.fixture(autouse=True)
def _fake_firm_identity(monkeypatch):
    # _run_plain_search_job() now resolves firm name/city live via
    # get_firm_identity() (backend/main.py) rather than the frozen
    # FIRM_NAME/FIRM_CITY constants -- not what these tests exercise.
    import backend.main as m
    async def _fake():
        return {"name": "Sawyer & Mkushi", "city": "Harare"}
    monkeypatch.setattr(m, "get_firm_identity", _fake)


def _empty_chunk_pool(monkeypatch, m):
    class FakeConn:
        async def fetch(self, query, *args):
            return []  # no firm/legal/zlr chunks indexed for this firm

        async def execute(self, query, *args):
            return "INSERT 0 1"

    class _Ctx:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, *exc):
            return False

    class FakePool:
        def acquire(self):
            return _Ctx()

    monkeypatch.setattr(m, "_db_pool", FakePool())


def _seed_job(job_id, firm_id="f1", created_at=None):
    _search_jobs[job_id] = {
        "status": JobStatus.PENDING, "result": None, "error": None,
        "firm_id": firm_id, "created_at": created_at or datetime.utcnow().isoformat(),
    }


# ── _run_plain_search_job: no results ────────────────────────────────────

def test_no_results_completes_the_job_rather_than_erroring(monkeypatch):
    import backend.main as m
    _empty_chunk_pool(monkeypatch, m)
    monkeypatch.setattr(m, "_semantic_search_firm", lambda req, chunks: [])
    monkeypatch.setattr(m, "_semantic_search_legal", lambda req, chunks: [])

    job_id = "job-empty"
    _seed_job(job_id)
    req = SearchRequest(query="a query with nothing indexed yet", limit=8)

    asyncio.run(_run_plain_search_job(job_id, req, {"id": None, "display_name": "Demo User", "role": "partner"}))

    assert _search_jobs[job_id]["status"] == JobStatus.COMPLETE
    result = _search_jobs[job_id]["result"]
    assert result["answer"] is None
    assert result["results"] == []
    assert "a query with nothing indexed yet" in result["message"]
    del _search_jobs[job_id]


# ── _run_plain_search_job: full pipeline reaches a grounded answer ──────

def _fake_full_pipeline(monkeypatch, m, captured):
    firm_result = {
        "result_source": "firm", "chunk_id": "c1", "text": "The lease runs for 12 months.",
        "similarity": 0.85, "document_id": "d1", "filename": "Lease.pdf",
        "document_type": "lease", "court": None, "matter_type": "commercial_property",
        "legal_source_type": "firm_precedent", "authority_strength": "persuasive",
        "document_status": "Final", "provenance_document_type": "Contract",
        "matter_number": "TC-001-01", "matter_name": "Test Matter",
        "client_id": "cl1", "client_name": "Test Client",
    }
    monkeypatch.setattr(m, "_semantic_search_firm", lambda req, chunks: [dict(firm_result)])
    monkeypatch.setattr(m, "_semantic_search_legal", lambda req, chunks: [])
    monkeypatch.setattr(m, "rerank", lambda all_results, query: {"confidence": "SECONDARY AUTHORITY FOUND",
                                                                   "results": [], "source_groups": {},
                                                                   "excluded_count": 0})
    monkeypatch.setattr(m, "compute_grounding", lambda *a, **kw: {
        "sources_sufficient": True, "grounding_note": "Grounded.",
        "max_similarity_score": 0.85, "source_tier_breakdown": {"authority": 1, "context": 0},
    })

    def fake_synthesise(query, results, legal_results, zlr_results, deadline_info=None, research_map=None,
                         firm_name=None, firm_city=None):
        captured["query"] = query
        captured["results"] = results
        return "SYNTHESISED ANSWER"

    monkeypatch.setattr(m, "synthesise_answer_sync", fake_synthesise)
    monkeypatch.setattr(m, "verify_citations", lambda answer, ctx: (answer, []))
    monkeypatch.setattr(m, "verify_inline_case_citations", lambda answer, ctx: (answer, []))
    monkeypatch.setattr(m, "enforce_confidence_consistency", lambda answer: (answer, []))
    monkeypatch.setattr(m, "apply_confidence_safeguard", lambda answer, grounding: answer)


def test_full_pipeline_produces_a_completed_job_with_the_expected_shape(monkeypatch):
    import backend.main as m
    _empty_chunk_pool(monkeypatch, m)
    captured = {}
    _fake_full_pipeline(monkeypatch, m, captured)

    job_id = "job-full"
    _seed_job(job_id)
    req = SearchRequest(query="does the lease renew automatically?", limit=8)

    asyncio.run(_run_plain_search_job(job_id, req, {"id": None, "display_name": "Demo User", "role": "partner"}))

    assert _search_jobs[job_id]["status"] == JobStatus.COMPLETE
    result = _search_jobs[job_id]["result"]
    assert result["answer"] == "SYNTHESISED ANSWER"
    assert len(result["results"]) == 1
    assert result["results"][0]["filename"] == "Lease.pdf"
    assert result["sources_sufficient"] is True
    assert "authority_ranking" in result
    assert captured["query"] == "does the lease renew automatically?"
    del _search_jobs[job_id]


# ── _run_plain_search_job: failure handling ──────────────────────────────

def test_exception_during_the_job_lands_it_in_failed_status(monkeypatch):
    import backend.main as m

    class _ExplodingPool:
        def acquire(self):
            raise RuntimeError("db unavailable")

    monkeypatch.setattr(m, "_db_pool", _ExplodingPool())

    job_id = "job-explode"
    _seed_job(job_id)
    req = SearchRequest(query="anything", limit=8)

    asyncio.run(_run_plain_search_job(job_id, req, {"id": None, "display_name": "Demo User", "role": "partner"}))

    assert _search_jobs[job_id]["status"] == JobStatus.FAILED
    assert "db unavailable" in _search_jobs[job_id]["error"]
    del _search_jobs[job_id]


# ── POST /api/search: creates a job, doesn't block on the pipeline ──────

class _NeverAcquiredPool:
    """Proves the endpoint itself never touches the DB -- only the
    background task it schedules does. asyncio.create_task() schedules
    but does not start running its coroutine until the event loop next
    yields, which the endpoint's synchronous tail (building the job dict
    and returning) never does -- same reasoning/convention as
    test_search_with_document_proceeds_past_guard_once_ready in
    tests/test_retrieval_readiness.py."""
    def acquire(self):
        raise AssertionError("endpoint should not touch the DB directly")


def test_search_documents_returns_a_job_id_immediately(monkeypatch):
    """Reaching a real job_id (not a 503) is the proof the endpoint's own
    synchronous portion never touches the DB -- same scope as
    test_search_with_document_proceeds_past_guard_once_ready in
    tests/test_retrieval_readiness.py. Deliberately does NOT assert what
    status the job is in by the time this returns: asyncio.run()'s own
    task-cleanup phase can give the background task a chance to actually
    start running (and hit the poison pool, landing FAILED) before this
    coroutine's caller gets control back -- a real asyncio.run() artifact,
    not something the endpoint itself controls or that matters here."""
    import backend.main as m
    monkeypatch.setattr(m, "_retrieval_ready", True)
    monkeypatch.setattr(m, "_db_pool", _NeverAcquiredPool())

    req = SearchRequest(query="a broad multi-issue query", limit=8)
    result = asyncio.run(search_documents(req, None))

    assert "job_id" in result
    assert result["status"] == "pending"
    assert result["job_id"] in m._search_jobs
    del m._search_jobs[result["job_id"]]


def test_search_documents_purges_stale_jobs_older_than_max_age(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_retrieval_ready", True)
    monkeypatch.setattr(m, "_db_pool", _NeverAcquiredPool())

    stale_id = "stale-job"
    stale_created = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    _seed_job(stale_id, created_at=stale_created)

    req = SearchRequest(query="anything", limit=8)
    result = asyncio.run(search_documents(req, None))

    assert stale_id not in m._search_jobs  # purged as part of this request
    del m._search_jobs[result["job_id"]]


# ── GET /api/search/status/{job_id} ──────────────────────────────────────

def test_status_endpoint_404s_for_unknown_job():
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_plain_search_job_status("does-not-exist", None))
    assert exc_info.value.status_code == 404


def test_status_endpoint_403s_for_a_different_firms_job(monkeypatch):
    import backend.main as m
    job_id = "job-other-firm"
    _seed_job(job_id, firm_id=str(uuid.uuid4()))  # not this firm's job

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_plain_search_job_status(job_id, None))
    assert exc_info.value.status_code == 403
    del m._search_jobs[job_id]


def test_status_endpoint_returns_the_job_shape_once_complete(monkeypatch):
    import backend.main as m
    job_id = "job-complete"
    _seed_job(job_id, firm_id=str(m.FIRM_ID))
    m._search_jobs[job_id]["status"] = JobStatus.COMPLETE
    m._search_jobs[job_id]["result"] = {"answer": "X", "results": []}

    result = asyncio.run(get_plain_search_job_status(job_id, None))

    assert result["job_id"] == job_id
    assert result["status"] == JobStatus.COMPLETE
    assert result["result"] == {"answer": "X", "results": []}
    assert result["error"] is None
    del m._search_jobs[job_id]
