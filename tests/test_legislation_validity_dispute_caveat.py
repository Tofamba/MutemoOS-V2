"""
Unit tests for the legislation validity-dispute caveat (2026-09-01): a
legislation chunk matched into a synthesized answer must not be presented
as settled, binding law when its own enactment is genuinely disputed --
e.g. Veritas's published position that Constitution of Zimbabwe Amendment
Act No. 6 of 2026 was never validly enacted (s.328 required a referendum
that never happened).

This is a distinct mechanism from the existing DRAFT/REVIEW/SUPERSEDED
firm-precedent caveat (test_document_status_synthesis_caveat.py) -- that
one is keyed off documents.document_status, a firm-precedent lifecycle
concept with no equivalent on legislation. Confirmed by reading
_semantic_search_legal()/format_context() directly before building this,
per instruction, rather than assuming the existing mechanism already
covered it -- it didn't.

Two things changed, both covered here:
- backend/grounding.py's format_context() labels a legal_results entry
  with a non-empty validity_flag explicitly in the source block sent to
  the model (e.g. "[LEGISLATION — {ref} — ⚠ VALIDITY DISPUTED: {reason}]"),
  leaving unflagged legislation on the original format.
- backend/main.py's synthesise_answer_sync() instructions (both the
  attached-document and plain-query branches) now tell the model to
  explicitly state that a VALIDITY DISPUTED-labeled source's enactment is
  disputed and why, never presenting it as settled/binding law.

Same "can't unit-test what the real model does with an instruction, but
CAN test that the label and instruction actually reach the prompt sent to
client.messages.create" limitation and convention as
test_document_status_synthesis_caveat.py.
"""
from types import SimpleNamespace

from backend.grounding import format_context
from backend.main import synthesise_answer_sync


# ── format_context(): the label itself ───────────────────────────────────

def test_validity_flag_gets_labeled_explicitly():
    legal_results = [{
        "reference": "Constitution of Zimbabwe Amendment Act No. 6 of 2026", "text": "Section 2...",
        "validity_flag": "Enactment challenged — no referendum held per s.328",
    }]
    context = format_context([], legal_results, [])
    assert ("[LEGISLATION — Constitution of Zimbabwe Amendment Act No. 6 of 2026 — "
            "⚠ VALIDITY DISPUTED: Enactment challenged — no referendum held per s.328]") in context


def test_unflagged_legislation_keeps_the_original_format():
    """The base Constitution (no validity dispute) must format exactly as
    before -- the common case, zero regression."""
    legal_results = [{"reference": "Constitution of Zimbabwe", "text": "Section 1...", "source_type": "legislation"}]
    context = format_context([], legal_results, [])
    assert "VALIDITY DISPUTED" not in context
    assert "[Constitution of Zimbabwe]\nSection 1..." in context


def test_missing_validity_flag_field_keeps_the_original_format():
    """Every existing legal_results dict (predating this field) must format
    exactly as before -- zero regression for the common case."""
    legal_results = [{"reference": "Some Act", "text": "text"}]
    context = format_context([], legal_results, [])
    assert context == "[Some Act]\ntext"


def test_validity_flag_takes_priority_over_background_context_labeling():
    """A source that's both a CONTEXT_SOURCE_TYPES entry AND validity-flagged
    must show the validity dispute, not the plainer BACKGROUND CONTEXT label
    -- the dispute is the more important thing for a lawyer to see."""
    legal_results = [{
        "reference": "Some Bulletin", "text": "text", "source_type": "press_statement",
        "validity_flag": "Disputed",
    }]
    context = format_context([], legal_results, [])
    assert "VALIDITY DISPUTED" in context
    assert "BACKGROUND CONTEXT" not in context


# ── synthesise_answer_sync(): the label and instruction reach the model ──

def _fake_message(text="An answer.", stop_reason="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(text=text)], stop_reason=stop_reason)


def test_disputed_legislation_query_sends_the_label_and_caveat_instruction_to_the_model(monkeypatch):
    import backend.main as m
    captured = {}

    def fake_create(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return _fake_message()

    monkeypatch.setattr(m.client.messages, "create", fake_create)

    legal_results = [{
        "reference": "Constitution of Zimbabwe Amendment Act No. 6 of 2026",
        "text": "Presidential terms extended...",
        "validity_flag": "Enactment challenged — no referendum held per s.328",
    }]
    synthesise_answer_sync("how long is the presidential term now", [], legal_results, [])

    prompt = captured["prompt"]
    assert "VALIDITY DISPUTED: Enactment challenged" in prompt
    assert "explicitly state that its enactment is disputed" in prompt
    assert "VALIDITY DISPUTED" in prompt  # the instruction bullet's own label reference


def test_unflagged_legislation_query_prompt_is_unaffected_by_the_fix(monkeypatch):
    """Regression check: a query matching only unflagged legislation must
    produce the exact same source label as before this change. The
    generic caveat instruction (a static addition present on every query)
    is still expected -- it does nothing when there's nothing to caveat."""
    import backend.main as m
    captured = {}

    def fake_create(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return _fake_message()

    monkeypatch.setattr(m.client.messages, "create", fake_create)

    legal_results = [{"reference": "Constitution of Zimbabwe", "text": "Section 56 rights...",
                       "source_type": "legislation"}]
    synthesise_answer_sync("what does section 56 say", [], legal_results, [])

    prompt = captured["prompt"]
    # The source's own label is plain -- unaffected, zero regression.
    assert "[Constitution of Zimbabwe]" in prompt
    assert "Constitution of Zimbabwe —" not in prompt  # no dispute suffix attached to this source
    assert "VALIDITY DISPUTED: " not in prompt  # no flagged label text present anywhere
    # The static instruction bullet (unconditionally present on every
    # query, same as the DRAFT/REVIEW/SUPERSEDED one) is still there --
    # it's a category the model should watch for, not a per-source label,
    # so its mere presence when nothing is flagged is correct, not a bug.
    assert "If a legislation source below is labeled VALIDITY DISPUTED" in prompt


def test_attached_document_branch_also_gets_the_caveat_instruction(monkeypatch):
    """The instruction was added to both instruction blocks -- the
    attached-document branch (search-with-upload) must carry it too, not
    just the plain-query branch."""
    import backend.main as m
    captured = {}

    def fake_create(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return _fake_message()

    monkeypatch.setattr(m.client.messages, "create", fake_create)

    legal_results = [{
        "reference": "Constitution of Zimbabwe Amendment Act No. 6 of 2026", "text": "text",
        "validity_flag": "Enactment challenged — no referendum held per s.328",
    }]
    synthesise_answer_sync(
        "does this contract need updating", [], legal_results, [],
        attached_doc_text="CONTRACT text here", attached_doc_name="contract.pdf",
    )

    prompt = captured["prompt"]
    assert "VALIDITY DISPUTED: Enactment challenged" in prompt
    assert "explicitly state that its enactment is disputed" in prompt


# ── the write path: validity_flag actually reaches the chunks table ────────
# (not just the synthesis-time formatting above -- if this never got
# written in the first place, format_context() would have nothing to
# label). Uses _process_legal_update_background() directly rather than
# upload_legal_update() (whose background_tasks.add_task() call is never
# actually invoked in the existing feed-token tests -- see
# test_legal_feed_service_token.py's FakeBackgroundTasks, a deliberate
# no-op there since those tests are about token auth, not ingestion).

import asyncio
import uuid


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _FakeChunksPool:
    def __init__(self):
        self.executed = []

    def acquire(self):
        return _FakeAcquireCtx(self)

    async def execute(self, query, *args):
        q = " ".join(query.split())
        self.executed.append((q, args))
        return "OK"


def test_process_legal_update_background_writes_validity_flag_onto_every_chunk(monkeypatch):
    import backend.main as m
    pool = _FakeChunksPool()
    monkeypatch.setattr(m, "_db_pool", pool)
    monkeypatch.setattr(m, "classify_document_sync", lambda text: {})
    monkeypatch.setattr(m, "index_chunks_in_chroma", lambda chunks, collection_type: None)

    asyncio.run(m._process_legal_update_background(
        str(uuid.uuid4()), b"", "Constitution Amendment Act No. 6 of 2026.pdf", "txt",
        "legislation", "Veritas", "Act No. 6 of 2026",
        summary="Presidential terms extended from five to seven years under this Act.",
        validity_flag="Enactment challenged — no referendum held per s.328",
    ))

    chunk_inserts = [c for c in pool.executed if c[0].startswith("INSERT INTO chunks")]
    assert len(chunk_inserts) >= 1
    for query, args in chunk_inserts:
        assert "validity_flag" in query
        assert args[-1] == "Enactment challenged — no referendum held per s.328"


def test_process_legal_update_background_leaves_validity_flag_null_when_not_passed(monkeypatch):
    """The base Constitution -- ingested with no validity_flag argument at
    all -- must not pick up a stray flag."""
    import backend.main as m
    pool = _FakeChunksPool()
    monkeypatch.setattr(m, "_db_pool", pool)
    monkeypatch.setattr(m, "classify_document_sync", lambda text: {})
    monkeypatch.setattr(m, "index_chunks_in_chroma", lambda chunks, collection_type: None)

    asyncio.run(m._process_legal_update_background(
        str(uuid.uuid4()), b"", "Constitution Consolidated (2023).pdf", "txt",
        "legislation", "Veritas", "As amended up to 20 June 2023",
        summary="Chapter 1 Founding provisions of the Constitution of Zimbabwe.",
    ))

    chunk_inserts = [c for c in pool.executed if c[0].startswith("INSERT INTO chunks")]
    assert len(chunk_inserts) >= 1
    for query, args in chunk_inserts:
        assert args[-1] is None


# ── authority_strength must be downgraded when validity is disputed ────────
# Found while actually ingesting Amendment Act No. 6 of 2026 (see
# [[project_v1_migrated_legislation_unsearchable]]): its reference contains
# "Constitution", so classify_legal_update() reads it as CONSTITUTION ->
# authority_strength_for() returns BINDING -- and backend/grounding.py's
# compute_grounding() keys "sources_sufficient"/"✓ Grounded in N
# authoritative source(s)" purely off authority_strength, with no awareness
# of validity_flag at all. Without this guard, a disputed-validity source
# would silently present as confidently-grounded binding law -- exactly
# what the validity_flag mechanism exists to prevent.

import asyncio as _asyncio_authority


class _FakeInsertConnection:
    def __init__(self):
        self.inserted_legal_updates = []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO legal_updates"):
            row = {"id": args[0], "firm_id": args[1], "filename": args[2], "authority_strength": args[9]}
            self.inserted_legal_updates.append(row)
            return row
        raise NotImplementedError(f"unhandled query: {q}")

    async def fetch(self, query, *args):
        raise NotImplementedError(f"unhandled query: {query}")

    async def execute(self, query, *args):
        return "OK"


class _FakeInsertPool:
    def __init__(self):
        self.conn = _FakeInsertConnection()

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


class _FakeBackgroundTasks:
    def add_task(self, *args, **kwargs):
        pass


def test_upload_legal_update_downgrades_authority_strength_when_validity_flagged(monkeypatch):
    import backend.main as m
    pool = _FakeInsertPool()
    monkeypatch.setattr(m, "_db_pool", pool)

    class _FakeRequest:
        headers = {}
        cookies = {}

    asyncio.run(m.upload_legal_update(
        background_tasks=_FakeBackgroundTasks(),
        file=None, source_type="legislation", source_name="Veritas",
        reference="Constitution of Zimbabwe Amendment Act No. 6 of 2026",
        source_url="", scraped_at="", summary="text", title="Amendment Act No. 6",
        validity_flag="Enactment challenged — no referendum held per s.328",
        request=_FakeRequest(),
    ))

    assert pool.conn.inserted_legal_updates[0]["authority_strength"] == "contextual"


def test_upload_legal_update_leaves_authority_strength_alone_when_unflagged(monkeypatch):
    """The base Constitution -- no validity dispute -- must keep its real
    BINDING authority_strength. Zero regression for the common case."""
    import backend.main as m
    pool = _FakeInsertPool()
    monkeypatch.setattr(m, "_db_pool", pool)

    class _FakeRequest:
        headers = {}
        cookies = {}

    asyncio.run(m.upload_legal_update(
        background_tasks=_FakeBackgroundTasks(),
        file=None, source_type="legislation", source_name="Veritas",
        reference="Constitution of Zimbabwe, Consolidated as at 2023",
        source_url="", scraped_at="", summary="text", title="Constitution",
        validity_flag="",  # explicit -- calling the route function directly
        # bypasses FastAPI's Form() resolution, so the real default
        # (Form("")) would otherwise be a Form marker object, not "".
        request=_FakeRequest(),
    ))

    assert pool.conn.inserted_legal_updates[0]["authority_strength"] == "binding"
