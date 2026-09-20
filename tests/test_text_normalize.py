"""
Regression tests for backend/text_normalize.py and its four boundaries
(2026-09-20). Found via a real incident: a validity_flag whose em dash was
stored as the literal characters \\xe2\\x80\\x94 (the escape spelling of the
dash's UTF-8 bytes) reached the model prompt in that form, inside the caveat
the model is instructed to act on ("⚠ VALIDITY DISPUTED: ...").

Scope is deliberately limited to that confirmed failure: literal
backslash-x-hex escapes. UTF-8-read-as-latin-1 mojibake is NOT repaired (a
corpus-wide scan found zero examples), and a test below pins that boundary.

Not the same problem as main.py's _pdf_safe() -- that is an output-side,
deliberately lossy downgrade for a latin-1 PDF font; this repairs text that
was already wrong at input. The last test pins that the two stay separate.

The literal-escape strings below are written as normal (non-raw) Python
literals with doubled backslashes, so they are the real problematic value:
backslash, x, e, 2, backslash, x, 8, 0, backslash, x, 9, 4.
"""
import asyncio

from backend.grounding import format_context
from backend.text_normalize import repair_text_encoding

EM_DASH = "\u2014"
CORRECT_FLAG = "Enactment challenged " + EM_DASH + " no referendum held per s.328"
# The exact value found stored on the orphaned Amendment Act chunks.
CORRUPT_FLAG = "Enactment challenged \\xe2\\x80\\x94 no referendum held per s.328"


# ── the normaliser itself ────────────────────────────────────────────────

def test_the_actual_stored_corrupt_value_is_repaired_to_the_intended_em_dash():
    assert "\\x" in CORRUPT_FLAG  # guards the fixture: it really is the backslash form
    assert repair_text_encoding(CORRUPT_FLAG) == CORRECT_FLAG


def test_mojibake_is_deliberately_out_of_scope_and_left_untouched():
    """Scope boundary, pinned on purpose: only the confirmed literal-escape
    corruption is repaired. UTF-8-read-as-latin-1/cp1252 mojibake was found
    nowhere in the corpus scan, so no repair path exists for it -- these
    inputs must come back byte-for-byte unchanged."""
    for mangled in (
        "Enactment challenged \u00e2\u20ac\u201d no referendum held per s.328",  # cp1252 reading of an em dash
        "Enactment challenged \u00e2\u0080\u0094 no referendum held per s.328",  # latin-1 reading
        "caf\u00c3\u00a9",
    ):
        assert repair_text_encoding(mangled) == mangled


def test_correct_text_is_left_exactly_alone():
    for ok in (CORRECT_FLAG, "caf\u00e9 na\u00efve Z\u00fcrich", "\u201cquoted\u201d \u2018x\u2019", "plain ascii text", "a \u2013 b \u2026"):
        assert repair_text_encoding(ok) == ok


def test_is_idempotent():
    once = repair_text_encoding(CORRUPT_FLAG)
    assert repair_text_encoding(once) == once


def test_none_and_empty_pass_through():
    assert repair_text_encoding(None) is None
    assert repair_text_encoding("") == ""


def test_ascii_only_escapes_are_not_touched():
    s = "\\x41\\x42"  # decodes to plain ASCII: not this bug, must not be altered
    assert repair_text_encoding(s) == s


def test_truncated_multibyte_escape_is_not_partially_converted():
    s = "cut off \\xe2\\x80 here"
    assert repair_text_encoding(s) == s


def test_a_real_backslash_x_in_other_text_is_not_touched():
    s = "C:\\xray\\xyz path"
    assert repair_text_encoding(s) == s


# ── boundary 1: ingestion, upload_legal_update() ──────────────────────────

class _AcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _RecordingConn:
    def __init__(self, fetch_map=None):
        self.fetchrow_calls = []
        self.execute_calls = []
        self.fetch_map = fetch_map or {}

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((" ".join(query.split()), args))
        if " ".join(query.split()).startswith("INSERT INTO legal_updates"):
            return {"id": args[0], "firm_id": args[1], "filename": args[2], "source_type": args[3], "source_name": args[4],
                    "reference": args[5], "status": "processing", "chunk_count": 0, "word_count": 0,
                    "uploaded_at": None, "validity_flag": args[10]}
        raise NotImplementedError(query)

    async def execute(self, query, *args):
        self.execute_calls.append((" ".join(query.split()), args))
        return "OK"

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        for prefix, rows in self.fetch_map.items():
            if q.startswith(prefix):
                return rows
        raise NotImplementedError(q)


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _AcquireCtx(self.conn)


class _Tasks:
    def add_task(self, *a, **k):
        pass


class _Req:
    headers = {}
    cookies = {}


def test_upload_stores_the_repaired_flag_not_the_corrupt_one(monkeypatch):
    import backend.main as m
    conn = _RecordingConn()
    monkeypatch.setattr(m, "_db_pool", _Pool(conn))
    monkeypatch.setattr(m, "_row_to_doc", lambda r: dict(r))

    asyncio.run(m.upload_legal_update(
        background_tasks=_Tasks(), file=None, source_type="legislation", source_name="Veritas",
        reference="Constitution of Zimbabwe Amendment Act No. 6 of 2026", source_url="", scraped_at="",
        summary="text", title="Amendment Act No. 6", validity_flag=CORRUPT_FLAG, request=_Req(),
    ))
    inserted = [args for q, args in conn.fetchrow_calls if q.startswith("INSERT INTO legal_updates")][0]
    assert inserted[10] == CORRECT_FLAG
    assert "\\x" not in inserted[10]
    # The downgrade-to-contextual safeguard keys off the flag being non-empty; repairing must not defeat it.
    assert inserted[9] == "contextual"


# ── boundary 2: ingestion, _process_legal_update_background() ─────────────

def test_background_ingestion_denormalises_the_repaired_flag_onto_every_chunk(monkeypatch):
    import backend.main as m
    conn = _RecordingConn()
    monkeypatch.setattr(m, "_db_pool", _Pool(conn))
    monkeypatch.setattr(m, "classify_document_sync", lambda text: {})
    indexed = []
    monkeypatch.setattr(m, "index_chunks_in_chroma", lambda chunks, kind="firm": indexed.extend(chunks))

    body = ("The Assembly of Deputies shall sit for a term of five years. " * 60).encode()
    asyncio.run(m._process_legal_update_background(
        "11111111-1111-4111-8111-111111111111", body, "act.txt", "txt", "legislation", "Veritas", "Some Act",
        summary="", validity_flag=CORRUPT_FLAG,
    ))
    chunk_inserts = [args for q, args in conn.execute_calls if q.startswith("INSERT INTO chunks")]
    assert chunk_inserts, "expected chunk rows to be written"
    for args in chunk_inserts:
        assert args[-1] == CORRECT_FLAG
    assert indexed and all(c["validity_flag"] == CORRECT_FLAG for c in indexed)


# ── boundary 3: read side, _semantic_search_legal() ───────────────────────

class _FakeLegalCollection:
    def __init__(self, ids):
        self.ids = ids

    def count(self):
        return len(self.ids)

    def query(self, query_embeddings, n_results):
        return {"ids": [self.ids[:n_results]], "distances": [[0.1] * len(self.ids[:n_results])]}


def _stored_orphan_chunk(flag):
    """Shaped like a row from the search path's `SELECT c.* ... LEFT JOIN`:
    an orphaned chunk (no parent), carrying the stored flag."""
    return {"id": "chunk-1", "text": "Presidential terms extended to seven years.", "document_id": "doc-1",
            "source_type": "legislation", "source_name": "Veritas",
            "reference": "Constitution of Zimbabwe Amendment Act No. 6 of 2026",
            "legal_source_type": None, "authority_strength": None, "validity_flag": flag}


def _search(monkeypatch, chunk):
    import backend.main as m
    monkeypatch.setattr(m, "get_chroma_collections", lambda: (None, _FakeLegalCollection([chunk["id"]]), None))
    monkeypatch.setattr(m, "embed_texts", lambda texts: [[0.0, 0.0, 0.0]])
    req = m.SearchRequest(query="presidential term", limit=8)
    return m._semantic_search_legal(req, [chunk])


def test_search_result_carries_the_repaired_flag_for_an_already_stored_corrupt_chunk(monkeypatch):
    results = _search(monkeypatch, _stored_orphan_chunk(CORRUPT_FLAG))
    assert len(results) == 1
    assert results[0]["validity_flag"] == CORRECT_FLAG


def test_format_context_output_contains_the_intended_em_dash_end_to_end(monkeypatch):
    """The regression the incident asked for: stored corrupt value in ->
    real search path -> the model-facing context block out, with the
    intended em dash and no escaped representation."""
    results = _search(monkeypatch, _stored_orphan_chunk(CORRUPT_FLAG))
    context = format_context([], results, [])
    assert ("[LEGISLATION " + EM_DASH + " Constitution of Zimbabwe Amendment Act No. 6 of 2026 " + EM_DASH +
            " \u26a0 VALIDITY DISPUTED: Enactment challenged " + EM_DASH + " no referendum held per s.328]") in context
    assert "\\x" not in context


def test_search_result_for_a_correct_flag_is_unchanged(monkeypatch):
    results = _search(monkeypatch, _stored_orphan_chunk(CORRECT_FLAG))
    assert results[0]["validity_flag"] == CORRECT_FLAG


# ── boundary 4: read side, the keyword endpoint search_legal_updates() ────

def test_keyword_search_endpoint_repairs_the_flag_too(monkeypatch):
    import backend.main as m
    chunk = {"id": "chunk-1", "text": "presidential term seven years", "document_id": "doc-1", "chunk_index": 0,
             "page_number": 1, "source_type": "legislation"}
    parent = {"id": "doc-1", "filename": "Amendment Act", "source_type": "legislation", "source_name": "Veritas",
              "reference": "Ref", "validity_flag": CORRUPT_FLAG}
    conn = _RecordingConn(fetch_map={
        "SELECT * FROM chunks WHERE firm_id=$1 AND chunk_source='legal'": [chunk],
        "SELECT * FROM legal_updates WHERE firm_id=$1": [parent],
    })
    monkeypatch.setattr(m, "_db_pool", _Pool(conn))

    async def fake_user(request):
        return {"id": None, "firm_id": m.FIRM_ID, "role": "partner", "display_name": "T"}

    monkeypatch.setattr(m, "get_current_user", fake_user)
    monkeypatch.setattr(m, "_check_permission", lambda user, perm: None)
    out = asyncio.run(m.search_legal_updates(m.LegalUpdateSearchRequest(query="presidential term"), _Req()))
    assert out["results"][0]["validity_flag"] == CORRECT_FLAG


# ── the two encoding problems stay separate ───────────────────────────────

def test_pdf_safe_is_unaffected_and_still_an_output_side_downgrade():
    import backend.main as m
    assert m._pdf_safe("a " + EM_DASH + " b") == "a - b"
    assert repair_text_encoding("a " + EM_DASH + " b") == "a " + EM_DASH + " b"


def test_non_string_input_is_passed_through_untouched():
    """Calling a route function directly leaves an unresolved FastAPI Form()
    default in place of a str (an existing test does exactly this); the
    ingestion-boundary helper must ignore it rather than raise."""
    marker = object()
    assert repair_text_encoding(marker) is marker
