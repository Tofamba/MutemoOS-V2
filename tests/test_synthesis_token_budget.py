"""
Unit tests for synthesise_answer_sync()'s token-budget fix (backend/main.py).

Before this fix, the plain Search Vault branch (no attached document) used
a flat max_tokens=4000 regardless of how much source material/how many
distinct legal issues the query actually involved -- fine for a narrow
single-issue query, but IRAC_STRUCTURE_RULES (backend/grounding.py) mandates
a full 7-section structure PER legal issue, so a genuinely broad multi-issue
query reliably blew through 4000 tokens and got cut off mid-analysis. The
attached-document branch already scaled dynamically off
len(attached_doc_text); this extends the same shape of formula to the plain
branch, keyed off len(synthesis_context) instead.

Also covers the prompt-text fix: the plain branch's closing instruction used
to say "max 4 paragraphs" in the same breath IRAC_STRUCTURE_RULES had just
mandated 7 sections per issue -- a direct, self-defeating contradiction.

Same monkeypatch.setattr(m.client.messages, "create", ...) convention as
tests/test_document_status_synthesis_caveat.py and
tests/test_contract_review_verification.py -- we can't unit-test what the
real model does with the prompt, but we can and do assert the actual
max_tokens value and prompt text that reach the API call, which is exactly
the mechanism this fix changes.
"""
from types import SimpleNamespace

from backend.main import synthesise_answer_sync


def _fake_message(text="An answer.", stop_reason="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(text=text)], stop_reason=stop_reason)


def _capture_kwargs(monkeypatch):
    import backend.main as m
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_message()

    monkeypatch.setattr(m.client.messages, "create", fake_create)
    return captured


# ── Plain query (no attached document): dynamic scaling ─────────────────

def test_plain_query_short_context_gets_the_8000_floor_not_4000(monkeypatch):
    captured = _capture_kwargs(monkeypatch)
    results = [{"filename": "Short Note.pdf", "text": "Brief text.", "document_status": "Final"}]

    synthesise_answer_sync("a narrow single-issue query", results, [], [])

    # min(8000 + len(context)//5, 24000) with a short context is ~8000,
    # not the old flat 4000 -- the floor moved up, it didn't stay put.
    assert captured["max_tokens"] >= 8000
    assert captured["max_tokens"] < 8100  # short context, so close to the floor


def test_plain_query_max_tokens_scales_with_context_length(monkeypatch):
    captured = _capture_kwargs(monkeypatch)
    long_text = "Relevant statutory text and analysis. " * 500  # long source material
    results = [{"filename": "Long Precedent.pdf", "text": long_text, "document_status": "Final"}]

    synthesise_answer_sync("a broad multi-issue query", results, [], [])

    from backend.grounding import format_context
    expected_context = format_context(results, [], [])
    expected = min(8000 + len(expected_context) // 5, 24000)
    assert captured["max_tokens"] == expected


def test_plain_query_max_tokens_is_capped_at_24000(monkeypatch):
    captured = _capture_kwargs(monkeypatch)
    huge_text = "Relevant statutory text and extensive analysis. " * 20000
    results = [{"filename": "Huge Precedent.pdf", "text": huge_text, "document_status": "Final"}]

    synthesise_answer_sync("an enormous query", results, [], [])

    assert captured["max_tokens"] == 24000


# ── Attached-document branch: unchanged regression check ────────────────

def test_attached_document_max_tokens_formula_is_unchanged(monkeypatch):
    captured = _capture_kwargs(monkeypatch)
    attached_doc_text = "Some contract text. " * 100
    results = [{"filename": "Firm Precedent.pdf", "text": "x", "document_status": "Final"}]

    synthesise_answer_sync(
        "is this lease valid", results, [], [],
        attached_doc_text=attached_doc_text, attached_doc_name="lease.pdf",
    )

    expected = min(8000 + len(attached_doc_text) // 5, 24000)
    assert captured["max_tokens"] == expected


def test_attached_document_max_tokens_ignores_synthesis_context_length(monkeypatch):
    """The attached-document formula must still key off attached_doc_text,
    not the (possibly much longer) firm-precedent synthesis_context -- these
    are two different sizes and the fix must not have conflated them."""
    captured = _capture_kwargs(monkeypatch)
    short_attached_doc = "Short clause."
    long_firm_context = [{"filename": "Long Precedent.pdf", "text": "text " * 5000,
                           "document_status": "Final"}]

    synthesise_answer_sync(
        "question about the attached clause", long_firm_context, [], [],
        attached_doc_text=short_attached_doc, attached_doc_name="clause.pdf",
    )

    expected = min(8000 + len(short_attached_doc) // 5, 24000)
    assert captured["max_tokens"] == expected


# ── Prompt text: the max-4-paragraphs / IRAC contradiction is gone ──────

def test_plain_query_prompt_no_longer_says_max_4_paragraphs(monkeypatch):
    captured = _capture_kwargs(monkeypatch)
    results = [{"filename": "Note.pdf", "text": "text", "document_status": "Final"}]

    synthesise_answer_sync("a query", results, [], [])

    prompt = captured["messages"][0]["content"]
    assert "max 4 paragraphs" not in prompt
    assert "issue-by-issue structure" in prompt


def test_attached_document_prompt_still_asks_for_headed_sections(monkeypatch):
    """Regression: the attached-document branch's own closing instruction
    (unrelated to the paragraphs/IRAC contradiction, which was plain-query-
    only) must be unchanged."""
    captured = _capture_kwargs(monkeypatch)
    results = [{"filename": "Note.pdf", "text": "text", "document_status": "Final"}]

    synthesise_answer_sync(
        "review this", results, [], [],
        attached_doc_text="Some text", attached_doc_name="doc.pdf",
    )

    prompt = captured["messages"][0]["content"]
    assert "clear headed sections for a thorough document review" in prompt
    assert "Clearly distinguish the attached document's own content" in prompt
