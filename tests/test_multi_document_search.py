"""
Unit tests for multi-file ad-hoc document search (POST /api/search/document,
backend/main.py) — extending the endpoint from a single UploadFile to a
list, so a query can span several uploaded documents at once (e.g. a
question of law applied to facts drawn from multiple documents), still
never storing or indexing anything — a one-off in-memory query, same as
before.

Two layers:
  - Pure-function tests for _combine_attached_documents() and
    _extract_attached_document_text() — no mocking needed.
  - An integration-style test of _run_document_search_job() itself, with
    the DB/retrieval/synthesis pipeline faked out, to prove multiple
    documents actually reach the model labeled and separated, not
    concatenated into one blob — and that a single file still produces
    exactly the same (unlabeled) shape as before this change.

Called directly as plain async functions, same convention as this repo's
other backend tests (see tests/test_docx_export.py's docstring for why).
"""

import asyncio

import pytest

from backend.main import (
    _combine_attached_documents,
    _extract_attached_document_text,
    _run_document_search_job,
    _search_jobs,
    JobStatus,
)


# ── _combine_attached_documents (pure) ───────────────────────────────────

def test_combine_single_document_is_unlabeled_passthrough():
    """Single file must produce exactly the same shape as before this
    change — no header, no behavior change for the common case."""
    docs = [{"filename": "lease.pdf", "text": "This lease shall run for 12 months."}]
    text, name = _combine_attached_documents(docs)
    assert text == "This lease shall run for 12 months."
    assert name == "lease.pdf"
    assert "=== DOCUMENT" not in text


def test_combine_two_documents_are_clearly_labeled_and_separated():
    docs = [
        {"filename": "lease.pdf", "text": "Rent is $500 per month."},
        {"filename": "invoice.pdf", "text": "Invoice total: $500 for March."},
    ]
    text, name = _combine_attached_documents(docs)

    assert "=== DOCUMENT: lease.pdf ===" in text
    assert "=== DOCUMENT: invoice.pdf ===" in text
    assert "Rent is $500 per month." in text
    assert "Invoice total: $500 for March." in text
    # Each document's text appears after its own header, not the other's.
    lease_pos = text.index("=== DOCUMENT: lease.pdf ===")
    invoice_pos = text.index("=== DOCUMENT: invoice.pdf ===")
    rent_pos = text.index("Rent is $500 per month.")
    invoice_total_pos = text.index("Invoice total: $500 for March.")
    assert lease_pos < rent_pos < invoice_pos < invoice_total_pos

    assert name == "lease.pdf, invoice.pdf"


def test_combine_only_includes_documents_actually_passed_in():
    """Simulates a file having been removed client-side before the search
    was run: if only the kept document is passed in, the removed one's
    content must not appear anywhere in what's sent for synthesis."""
    kept_only = [{"filename": "lease.pdf", "text": "Rent is $500 per month."}]
    text, name = _combine_attached_documents(kept_only)
    assert "invoice.pdf" not in text
    assert "Invoice total" not in text
    assert name == "lease.pdf"


def test_combine_three_documents_all_present():
    docs = [
        {"filename": "a.txt", "text": "Document A content."},
        {"filename": "b.txt", "text": "Document B content."},
        {"filename": "c.txt", "text": "Document C content."},
    ]
    text, name = _combine_attached_documents(docs)
    for letter in ("A", "B", "C"):
        assert f"Document {letter} content." in text
    assert name == "a.txt, b.txt, c.txt"


# ── _extract_attached_document_text (pure-ish: no I/O beyond parsing) ────

def test_extract_plain_text_fallback_for_unknown_extension():
    text, ocr_confidence = _extract_attached_document_text(b"Hello world", "notes.xyz")
    assert text == "Hello world"
    assert ocr_confidence is None


def test_extract_raises_with_filename_on_empty_content():
    with pytest.raises(ValueError) as exc_info:
        _extract_attached_document_text(b"   ", "blank.txt")
    assert "blank.txt" in str(exc_info.value)


# ── _run_document_search_job (integration, pipeline faked) ───────────────

def _fake_pool_no_chunks(monkeypatch, m):
    class FakeConn:
        async def fetch(self, query, *args):
            return []  # no firm/legal/zlr chunks — irrelevant to this test

    class _Ctx:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, *exc):
            return False

    class FakePool:
        def acquire(self):
            return _Ctx()

    monkeypatch.setattr(m, "_db_pool", FakePool())


def _fake_pipeline(monkeypatch, m, captured):
    monkeypatch.setattr(m, "_semantic_search_firm", lambda req, chunks: [])
    monkeypatch.setattr(m, "_semantic_search_legal", lambda req, chunks: [])

    def fake_synthesise(query, results, legal_results, zlr_results, attached_doc_text=None, attached_doc_name=None):
        captured["attached_doc_text"] = attached_doc_text
        captured["attached_doc_name"] = attached_doc_name
        return "ANSWER"

    monkeypatch.setattr(m, "synthesise_answer_sync", fake_synthesise)
    monkeypatch.setattr(m, "compute_grounding", lambda *a, **kw: {"sufficient": True})
    monkeypatch.setattr(m, "apply_confidence_safeguard", lambda answer, grounding: answer)


def test_run_document_search_job_single_file_matches_prior_unlabeled_shape(monkeypatch):
    import backend.main as m
    _fake_pool_no_chunks(monkeypatch, m)
    captured = {}
    _fake_pipeline(monkeypatch, m, captured)

    job_id = "job-1"
    _search_jobs[job_id] = {"status": JobStatus.PENDING, "result": None, "error": None,
                             "firm_id": "f1", "created_at": "2026-01-01T00:00:00"}
    files = [{"filename": "lease.pdf", "content": b"Rent is $500 per month."}]

    asyncio.run(_run_document_search_job(
        job_id, files, "is the rent reasonable?", {"id": None}, None, True, 8,
    ))

    assert _search_jobs[job_id]["status"] == JobStatus.COMPLETE
    result = _search_jobs[job_id]["result"]
    assert result["answer"] == "ANSWER"
    assert len(result["attached_documents"]) == 1
    assert result["attached_documents"][0]["filename"] == "lease.pdf"

    # Single-file synthesis input is unlabeled, exactly as before this change.
    assert captured["attached_doc_text"] == "Rent is $500 per month."
    assert captured["attached_doc_name"] == "lease.pdf"
    del _search_jobs[job_id]


def test_run_document_search_job_two_files_both_considered_and_labeled(monkeypatch):
    import backend.main as m
    _fake_pool_no_chunks(monkeypatch, m)
    captured = {}
    _fake_pipeline(monkeypatch, m, captured)

    job_id = "job-2"
    _search_jobs[job_id] = {"status": JobStatus.PENDING, "result": None, "error": None,
                             "firm_id": "f1", "created_at": "2026-01-01T00:00:00"}
    files = [
        {"filename": "lease.pdf", "content": b"Rent is $500 per month."},
        {"filename": "invoice.pdf", "content": b"Invoice total: $500 for March."},
    ]

    asyncio.run(_run_document_search_job(
        job_id, files, "does the invoice match the lease rent?", {"id": None}, None, True, 8,
    ))

    result = _search_jobs[job_id]["result"]
    assert len(result["attached_documents"]) == 2
    assert {d["filename"] for d in result["attached_documents"]} == {"lease.pdf", "invoice.pdf"}

    # Both documents actually reached synthesis, clearly separated/labeled.
    assert "=== DOCUMENT: lease.pdf ===" in captured["attached_doc_text"]
    assert "=== DOCUMENT: invoice.pdf ===" in captured["attached_doc_text"]
    assert "Rent is $500 per month." in captured["attached_doc_text"]
    assert "Invoice total: $500 for March." in captured["attached_doc_text"]
    assert captured["attached_doc_name"] == "lease.pdf, invoice.pdf"
    del _search_jobs[job_id]


def test_run_document_search_job_removed_file_excluded_from_consideration(monkeypatch):
    """Simulates the frontend chip-removal flow: only the file the user
    kept attached is ever sent to the backend at all, so a "removed" file
    can't influence the answer — there's nothing server-side to clean up
    since it was never part of this request."""
    import backend.main as m
    _fake_pool_no_chunks(monkeypatch, m)
    captured = {}
    _fake_pipeline(monkeypatch, m, captured)

    job_id = "job-3"
    _search_jobs[job_id] = {"status": JobStatus.PENDING, "result": None, "error": None,
                             "firm_id": "f1", "created_at": "2026-01-01T00:00:00"}
    # Only "lease.pdf" is sent — "invoice.pdf" was removed client-side before search.
    files = [{"filename": "lease.pdf", "content": b"Rent is $500 per month."}]

    asyncio.run(_run_document_search_job(
        job_id, files, "is the rent reasonable?", {"id": None}, None, True, 8,
    ))

    result = _search_jobs[job_id]["result"]
    assert len(result["attached_documents"]) == 1
    assert result["attached_documents"][0]["filename"] == "lease.pdf"
    assert "invoice.pdf" not in captured["attached_doc_text"]
    del _search_jobs[job_id]
