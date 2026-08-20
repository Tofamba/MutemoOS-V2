"""
Unit tests for the document-status caveat fix: a firm document matched
into a Search Vault answer must not be presented to the model (and
therefore to the lawyer) as settled precedent when it's actually an
untouched Draft/Review/Superseded document.

Two things changed, both covered here:
- backend/grounding.py's format_context() now labels a non-Final/Executed
  document_status explicitly in the source block sent to the model
  (e.g. "[FIRM PRECEDENT — DRAFT — {filename}]"), leaving Final/Executed/
  legacy-no-status documents on the original unlabeled format.
- backend/main.py's synthesise_answer_sync() instructions (both the
  attached-document and plain-query branches) now tell the model to
  explicitly caveat a DRAFT/REVIEW/SUPERSEDED-labeled source rather than
  presenting it as settled firm precedent.

We can't unit-test what the real model actually does with that
instruction (no live API call in tests) -- same limitation this repo's
existing test_anthropic_call_does_not_block_the_event_loop and
test_findings_as_raw_string_raises_instead_of_inflating_dropped_count
(tests/test_contract_review_verification.py) already accept for
Anthropic-backed code. What IS directly testable, and is exactly the
mechanism this fix relies on, is that the label and the instruction both
actually reach the prompt sent to client.messages.create -- so that's
what these tests assert, following the same
monkeypatch.setattr(m.client.messages, "create", ...) convention already
used in this repo.
"""
from types import SimpleNamespace

from backend.grounding import format_context
from backend.main import synthesise_answer_sync


# ── format_context(): the label itself ───────────────────────────────────

def test_draft_status_gets_labeled_explicitly():
    results = [{"filename": "Client Engagement Letter", "text": "Dear Client...", "document_status": "Draft"}]
    context = format_context(results, [], [])
    assert "[FIRM PRECEDENT — DRAFT — Client Engagement Letter]" in context


def test_review_and_superseded_also_get_labeled():
    results = [
        {"filename": "Heads of Argument.docx", "text": "text one", "document_status": "Review"},
        {"filename": "Old Lease.pdf", "text": "text two", "document_status": "Superseded"},
    ]
    context = format_context(results, [], [])
    assert "[FIRM PRECEDENT — REVIEW — Heads of Argument.docx]" in context
    assert "[FIRM PRECEDENT — SUPERSEDED — Old Lease.pdf]" in context


def test_final_status_keeps_the_original_unlabeled_format():
    results = [{"filename": "Signed Agreement.pdf", "text": "text", "document_status": "Final"}]
    context = format_context(results, [], [])
    assert context == "[FIRM PRECEDENT — Signed Agreement.pdf]\ntext"
    assert "FINAL" not in context


def test_executed_status_keeps_the_original_unlabeled_format():
    results = [{"filename": "Executed Lease.pdf", "text": "text", "document_status": "Executed"}]
    context = format_context(results, [], [])
    assert context == "[FIRM PRECEDENT — Executed Lease.pdf]\ntext"
    assert "EXECUTED" not in context


def test_missing_document_status_keeps_the_original_unlabeled_format():
    """Documents predating this metadata (document_status not set on the
    result dict at all) must format exactly as before -- the common case,
    zero regression."""
    results = [{"filename": "Old Precedent.pdf", "text": "text"}]
    context = format_context(results, [], [])
    assert context == "[FIRM PRECEDENT — Old Precedent.pdf]\ntext"


# ── synthesise_answer_sync(): the label and instruction reach the model ──

def _fake_message(text="An answer.", stop_reason="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(text=text)], stop_reason=stop_reason)


def test_draft_document_query_sends_the_draft_label_and_caveat_instruction_to_the_model(monkeypatch):
    import backend.main as m
    captured = {}

    def fake_create(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return _fake_message()

    monkeypatch.setattr(m.client.messages, "create", fake_create)

    results = [{"filename": "Client Engagement Letter", "text": "Draft engagement terms.",
                "document_status": "Draft"}]
    synthesise_answer_sync("engagement terms", results, [], [])

    prompt = captured["prompt"]
    assert "[FIRM PRECEDENT — DRAFT — Client Engagement Letter]" in prompt
    assert "explicitly caveat any reliance on it" in prompt
    assert "DRAFT, REVIEW, or SUPERSEDED" in prompt


def test_final_document_query_prompt_is_unaffected_by_the_fix(monkeypatch):
    """Regression check: a query matching only a Final-status document
    must produce the exact same source label as before this change --
    the plain, unlabeled format, with no status word attached to that
    document's own citation. The generic caveat instruction (a static
    addition present on every query, mentioning DRAFT/REVIEW/SUPERSEDED
    as categories the model should watch for) is expected to still be
    present -- that's not a per-document label, it does nothing when
    there's nothing in the sources to caveat."""
    import backend.main as m
    captured = {}

    def fake_create(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return _fake_message()

    monkeypatch.setattr(m.client.messages, "create", fake_create)

    results = [{"filename": "Signed Agreement.pdf", "text": "Final agreed terms.",
                "document_status": "Final"}]
    synthesise_answer_sync("agreed terms", results, [], [])

    prompt = captured["prompt"]
    assert "[FIRM PRECEDENT — Signed Agreement.pdf]" in prompt
    assert "DRAFT — Signed Agreement.pdf" not in prompt
    assert "REVIEW — Signed Agreement.pdf" not in prompt
    assert "SUPERSEDED — Signed Agreement.pdf" not in prompt


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

    results = [{"filename": "Old Lease.pdf", "text": "Superseded lease terms.",
                "document_status": "Superseded"}]
    synthesise_answer_sync(
        "is this lease still valid", results, [], [],
        attached_doc_text="LEASE AGREEMENT text here", attached_doc_name="lease.pdf",
    )

    prompt = captured["prompt"]
    assert "[FIRM PRECEDENT — SUPERSEDED — Old Lease.pdf]" in prompt
    assert "explicitly caveat any reliance on it" in prompt
