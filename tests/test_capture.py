"""
Unit tests for Rapid Precedent Capture (POST /api/capture, backend/main.py) --
the fast, low-friction phone/camera capture path, distinct from the general
POST /api/upload: minimal required input (a matter, or nothing -- defaults
to the firm's sentinel "General / Firm Precedents" matter), no manual
tagging gate, multi-page photo sessions assembled into one document (not
N), and a 'Final' document_status default rather than upload's 'Draft'.

Called directly as plain async functions with a real BackgroundTasks()
instance, same convention as tests/test_document_provenance.py -- see that
file's docstring for why optional Form(...) fields are always passed
explicitly rather than relying on FastAPI's own default-resolution, which
calling directly (bypassing routing) never goes through.
"""
import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import BackgroundTasks, HTTPException

import backend.main as m
from backend.main import capture_documents


class FakeUploadFile:
    def __init__(self, filename, content: bytes = b"fake image bytes"):
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
        self.executed = []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT id FROM matters WHERE id=$1 AND firm_id=$2"):
            matter_id, firm_id = args
            for mt in self.matters:
                if mt["id"] == matter_id and mt["firm_id"] == firm_id:
                    return {"id": mt["id"]}
            return None

        if q.startswith("SELECT id FROM matters WHERE firm_id=$1 AND number='GENERAL'"):
            (firm_id,) = args
            for mt in self.matters:
                if mt["firm_id"] == firm_id and mt.get("number") == "GENERAL":
                    return {"id": mt["id"]}
            return None

        if q.startswith("INSERT INTO documents"):
            # document_status/is_capture are hardcoded SQL literals in the
            # real query ('Final', TRUE), not bind params -- only 5 args.
            (doc_id, matter_id, firm_id, filename, uploaded_by) = args
            row = {
                "id": doc_id, "matter_id": matter_id, "firm_id": firm_id, "filename": filename,
                "status": "processing", "uploaded_at": datetime.now(timezone.utc), "uploaded_by": uploaded_by,
                "document_status": "Final", "is_capture": True,
            }
            self.documents.append(row)
            return row

        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def execute(self, query, *args):
        self.executed.append((" ".join(query.split()), args))
        return "OK"

    async def fetch(self, query, *args):
        return []


class FakePool:
    def __init__(self, matters=None):
        self.conn = FakeConnection(matters)

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _matter_row(firm_id, number=None):
    return {"id": uuid.uuid4(), "firm_id": firm_id, "number": number}


def _call_capture(files, matter_id=None):
    return capture_documents(BackgroundTasks(), files, matter_id, None)


# ── Minimal-input path: no matter_id required ────────────────────────────

def test_capture_with_no_matter_id_resolves_to_general_sentinel_matter(monkeypatch):
    firm_id = m.FIRM_ID
    general = _matter_row(firm_id, number="GENERAL")
    pool = FakePool(matters=[general])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(_call_capture([FakeUploadFile("page1.jpg")]))

    assert result["matter_id"] == str(general["id"])
    assert pool.conn.documents[0]["matter_id"] == general["id"]


def test_capture_with_explicit_matter_id_uses_that_matter(monkeypatch):
    firm_id = m.FIRM_ID
    general = _matter_row(firm_id, number="GENERAL")
    specific = _matter_row(firm_id)
    pool = FakePool(matters=[general, specific])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(_call_capture([FakeUploadFile("page1.jpg")], matter_id=str(specific["id"])))

    assert result["matter_id"] == str(specific["id"])


def test_capture_404s_for_a_matter_id_that_does_not_exist(monkeypatch):
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[]))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_call_capture([FakeUploadFile("page1.jpg")], matter_id=str(uuid.uuid4())))
    assert exc_info.value.status_code == 404


def test_capture_rejects_an_empty_file_list(monkeypatch):
    general = _matter_row(m.FIRM_ID, number="GENERAL")
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[general]))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_call_capture([]))
    assert exc_info.value.status_code == 422


# ── document_status default: Final for capture, distinct from Draft ─────

def test_capture_defaults_document_status_to_final_not_draft(monkeypatch):
    """Capture's own default -- deliberately distinct from /api/upload's
    'Draft' default (tests/test_document_provenance.py), not copy-pasted."""
    general = _matter_row(m.FIRM_ID, number="GENERAL")
    pool = FakePool(matters=[general])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(_call_capture([FakeUploadFile("page1.jpg")]))

    assert result["document_status"] == "Final"
    assert pool.conn.documents[0]["document_status"] == "Final"
    assert pool.conn.documents[0]["is_capture"] is True


# ── Multi-page capture: one document, not N ──────────────────────────────

def test_multi_page_capture_creates_exactly_one_document_row(monkeypatch):
    general = _matter_row(m.FIRM_ID, number="GENERAL")
    pool = FakePool(matters=[general])
    monkeypatch.setattr(m, "_db_pool", pool)

    files = [FakeUploadFile("page1.jpg"), FakeUploadFile("page2.jpg"), FakeUploadFile("page3.jpg")]
    result = asyncio.run(_call_capture(files))

    assert len(pool.conn.documents) == 1
    assert "3 pages" in pool.conn.documents[0]["filename"]
    assert result["message"].startswith("3 page(s) received")


# ── _process_capture_background: pipeline behaviour ──────────────────────

def _patch_pipeline(monkeypatch, page_texts_and_confidences, classify_result=None):
    """
    page_texts_and_confidences: list of (text, confidence) returned in
    order by successive calls to _extract_attached_document_text -- one
    call per captured page/file, same dispatch the multi-file Search Vault
    attach path already uses.
    """
    calls = iter(page_texts_and_confidences)

    def fake_extract(content, filename):
        return next(calls)

    monkeypatch.setattr(m, "_extract_attached_document_text", fake_extract)
    monkeypatch.setattr(m, "classify_document_sync", lambda text: classify_result or {})
    monkeypatch.setattr(m, "chunk_text", lambda text, page_count, doc_id, matter_id: [])
    monkeypatch.setattr(m, "index_chunks_in_chroma", lambda chunks, collection_type="firm": None)
    monkeypatch.setattr(m, "R2_ENABLED", False)


def test_low_ocr_confidence_flags_needs_review(monkeypatch):
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)
    _patch_pipeline(monkeypatch, [("some scanned text", 42.0)])

    doc_id = str(uuid.uuid4())
    matter_id = str(uuid.uuid4())
    asyncio.run(m._process_capture_background(
        doc_id, matter_id, [{"content": b"x", "filename": "page1.jpg"}]
    ))

    update_calls = [c for c in pool.conn.executed if c[0].startswith("UPDATE documents SET")]
    assert len(update_calls) == 1
    args = update_calls[0][1]
    # needs_review is the 14th positional bind ($14) in the UPDATE -- args
    # here excludes nothing, it's the exact tuple passed to conn.execute.
    needs_review_index = 13  # 0-indexed position of the $14 bind
    assert args[needs_review_index] is True


def test_high_ocr_confidence_does_not_flag_needs_review(monkeypatch):
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)
    _patch_pipeline(monkeypatch, [("clean text", 95.0)])

    asyncio.run(m._process_capture_background(
        str(uuid.uuid4()), str(uuid.uuid4()), [{"content": b"x", "filename": "page1.jpg"}]
    ))

    update_calls = [c for c in pool.conn.executed if c[0].startswith("UPDATE documents SET")]
    assert update_calls[0][1][13] is False


def test_multi_page_text_is_combined_across_pages_before_classification(monkeypatch):
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)
    captured = {}

    def fake_extract(content, filename):
        return ({"page1.jpg": "FIRST PAGE TEXT", "page2.jpg": "SECOND PAGE TEXT"}[filename], 90.0)

    monkeypatch.setattr(m, "_extract_attached_document_text", fake_extract)

    def fake_classify(text):
        captured["text"] = text
        return {}

    monkeypatch.setattr(m, "classify_document_sync", fake_classify)
    monkeypatch.setattr(m, "chunk_text", lambda text, page_count, doc_id, matter_id: [])
    monkeypatch.setattr(m, "index_chunks_in_chroma", lambda chunks, collection_type="firm": None)
    monkeypatch.setattr(m, "R2_ENABLED", False)

    asyncio.run(m._process_capture_background(
        str(uuid.uuid4()), str(uuid.uuid4()),
        [{"content": b"x", "filename": "page1.jpg"}, {"content": b"y", "filename": "page2.jpg"}]
    ))

    assert "FIRST PAGE TEXT" in captured["text"]
    assert "SECOND PAGE TEXT" in captured["text"]


def test_a_page_that_fails_extraction_is_skipped_not_fatal(monkeypatch):
    """One unreadable photo in a multi-page batch shouldn't lose the whole
    capture -- same fail-soft-per-page behaviour, distinct from the
    Search Vault attach path's fail-fast (that's a live user query where
    silently dropping a file could mislead; this is a background capture
    job where losing the rest of a document to one bad photo is worse)."""
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    def fake_extract(content, filename):
        if filename == "bad.jpg":
            raise ValueError("could not read text from bad.jpg")
        return ("good page text", 90.0)

    monkeypatch.setattr(m, "_extract_attached_document_text", fake_extract)
    monkeypatch.setattr(m, "classify_document_sync", lambda text: {})
    monkeypatch.setattr(m, "chunk_text", lambda text, page_count, doc_id, matter_id: [])
    monkeypatch.setattr(m, "index_chunks_in_chroma", lambda chunks, collection_type="firm": None)
    monkeypatch.setattr(m, "R2_ENABLED", False)

    # Must not raise.
    asyncio.run(m._process_capture_background(
        str(uuid.uuid4()), str(uuid.uuid4()),
        [{"content": b"x", "filename": "bad.jpg"}, {"content": b"y", "filename": "good.jpg"}]
    ))

    update_calls = [c for c in pool.conn.executed if c[0].startswith("UPDATE documents SET")]
    assert len(update_calls) == 1  # still completes

# Existing pipeline unaffected: tests/test_document_provenance.py already
# covers /api/upload's own 'Draft' default end to end and is untouched by
# this change -- re-run as part of the full suite (not duplicated here)
# is the actual verification for Part 5's "existing pipeline unaffected".


# ── Sentinel matter exclusion ────────────────────────────────────────────
# The "General / Firm Precedents" matter created above is a system bucket,
# not a real client matter -- it must never inflate a matter count, an
# "active matters" list, or a stale-matter alert. A fake connection can't
# evaluate real SQL WHERE semantics, so these assert the actual query text
# sent to conn.fetch() carries the exclusion clause -- honest verification
# that the fix is present and would regress loudly (a query-text diff) if
# someone later dropped the clause, rather than pretending to validate
# real Postgres filtering without a real database.

class _QueryCapturingConnection:
    def __init__(self):
        self.queries = []

    async def fetchrow(self, query, *args):
        return None  # no organisation_roles row -> org_role stays None

    async def fetch(self, query, *args):
        self.queries.append(" ".join(query.split()))
        return []


class _QueryCapturingPool:
    def __init__(self):
        self.conn = _QueryCapturingConnection()

    def acquire(self):
        return _FakeAcquireCtxForQueryCapture(self.conn)


class _FakeAcquireCtxForQueryCapture:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


def test_list_matters_excludes_the_sentinel_matter(monkeypatch):
    from backend.main import list_matters

    pool = _QueryCapturingPool()
    monkeypatch.setattr(m, "_db_pool", pool)

    asyncio.run(list_matters(None))

    matter_queries = [q for q in pool.conn.queries if q.startswith("SELECT * FROM matters")]
    assert len(matter_queries) == 1
    assert "NOT is_sentinel" in matter_queries[0]


def test_practice_area_breakdown_excludes_the_sentinel_matter(monkeypatch):
    from backend.main import practice_area_breakdown

    pool = _QueryCapturingPool()
    monkeypatch.setattr(m, "_db_pool", pool)

    asyncio.run(practice_area_breakdown(None))

    assert "NOT is_sentinel" in pool.conn.queries[0]


def test_inactivity_check_excludes_the_sentinel_matter(monkeypatch):
    """inactivity_check() also needs a reminder_settings row with a
    recipient_email before it will reach the matters query at all."""
    from backend.main import inactivity_check

    class _Conn(_QueryCapturingConnection):
        async def fetchrow(self, query, *args):
            if query.strip().startswith("SELECT * FROM reminder_settings"):
                return {"recipient_email": "partner@example.com"}
            return await super().fetchrow(query, *args)

    pool = _QueryCapturingPool()
    pool.conn = _Conn()
    monkeypatch.setattr(m, "_db_pool", pool)

    asyncio.run(inactivity_check(None))

    matter_queries = [q for q in pool.conn.queries if "FROM matters" in q]
    assert len(matter_queries) == 1
    assert "NOT is_sentinel" in matter_queries[0]
