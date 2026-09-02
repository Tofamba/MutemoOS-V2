"""
Unit tests for the corpus-scope-honesty fix (backend/grounding.py,
backend/main.py, 2026-09-02) -- a real incident: a meta-phrased query
("is section 17's text retrievable right now?") retrieved unrelated
court-rules/news content, and the AI synthesis asserted the relevant
Money Laundering and Proceeds of Crime Act sections were "entirely
absent from the corpus" with "High" confidence, when the actual content
was present, correctly indexed, and ranked #1 the moment it was searched
for directly (confirmed by a real side-by-side reproduction against
production). A single query's retrieval can only ever show what THAT
query surfaced -- never what does or doesn't exist in the corpus as a
whole. Three independent layers fixed, all covered here:

1. run_legal_research_agent()'s own prompt -- the Haiku gap-analyst that
   was echoing the query's own "entirely absent" framing back as a
   finding.
2. synthesise_answer_sync()'s RESEARCH GAP MAP instruction to the final
   model -- strengthened beyond the pre-existing (already-present but
   evidently insufficient) "state precisely what the retrieved sources
   do not establish" wording.
3. scope_corpus_absence_claims() (new) -- a deterministic backstop, same
   convention as verify_citations()/verify_inline_case_citations(),
   rewriting any surviving "absent from/does not exist in the corpus"
   phrasing regardless of what the model wrote.

Plus apply_confidence_safeguard() now always appends a neutral
rephrasing suggestion when grounding is insufficient (not just when it
also detects assertive banned terms) -- worded not to assert either
"this is genuinely absent" or "this is definitely just badly queried",
since a single thin retrieval cannot distinguish the two.

Called directly as plain function calls / mocked client.messages.create,
same convention as tests/test_firm_identity_prompts.py.
"""
from types import SimpleNamespace

import backend.grounding as g
import backend.main as m
from backend.grounding import (
    REPHRASE_SUGGESTION,
    apply_confidence_safeguard,
    run_legal_research_agent,
    scope_corpus_absence_claims,
)
from backend.main import synthesise_answer_sync


# ── scope_corpus_absence_claims() ───────────────────────────────────────────

def test_rewrites_entirely_absent_from_the_corpus():
    text = "The customer identification provisions are entirely absent from the corpus."
    result, qc_log = scope_corpus_absence_claims(text)
    assert "entirely absent from the corpus" not in result
    assert "not found in this search" in result
    assert len(qc_log) == 1
    assert qc_log[0]["qc_status"] == "absence_claim_rescoped"


def test_rewrites_several_close_variants():
    variants = [
        "Section 20 is missing from the corpus.",
        "The PEP definition does not exist in the corpus.",
        "These sections are not present in the corpus.",
        "The provision is not found in the corpus.",
        "This clause no longer exists in the corpus.",
    ]
    for text in variants:
        result, qc_log = scope_corpus_absence_claims(text)
        assert "not found in this search" in result, f"failed for: {text!r}"
        assert len(qc_log) == 1, f"failed for: {text!r}"


def test_leaves_normal_text_completely_untouched():
    text = "Section 17 requires a customer's full name and date of birth."
    result, qc_log = scope_corpus_absence_claims(text)
    assert result == text
    assert qc_log == []


def test_leaves_scoped_honest_language_untouched():
    """The correctly-scoped replacement wording itself should never be
    re-flagged or re-rewritten -- no infinite-rewrite risk."""
    text = "This provision was not found in this search."
    result, qc_log = scope_corpus_absence_claims(text)
    assert result == text
    assert qc_log == []


def test_empty_text_returns_unchanged():
    assert scope_corpus_absence_claims("") == ("", [])
    assert scope_corpus_absence_claims(None) == (None, [])


def test_rewrites_multiple_occurrences_in_one_answer():
    text = (
        "Section 17 is absent from the corpus. "
        "Section 20's PEP language is also absent from the corpus."
    )
    result, qc_log = scope_corpus_absence_claims(text)
    assert result.count("not found in this search") == 2
    assert len(qc_log) == 2


# ── apply_confidence_safeguard() ────────────────────────────────────────────

def test_sufficient_grounding_leaves_answer_completely_unchanged():
    answer = "This is a well-grounded answer."
    result = apply_confidence_safeguard(answer, {"sources_sufficient": True})
    assert result == answer


def test_insufficient_grounding_always_appends_rephrase_suggestion():
    answer = "This is a thinly-grounded answer with no assertive language."
    result = apply_confidence_safeguard(answer, {"sources_sufficient": False})
    assert result == answer + REPHRASE_SUGGESTION
    assert "⚠ WARNING" not in result


def test_insufficient_grounding_with_assertive_term_gets_both_warning_and_suggestion():
    answer = "This is a clear and certain conclusion with no room for doubt."
    result = apply_confidence_safeguard(answer, {"sources_sufficient": False})
    assert "⚠ WARNING: ANALOGOUS ANALYSIS ONLY" in result
    assert result.endswith(REPHRASE_SUGGESTION)
    # Warning comes first, then the original answer, then the suggestion.
    assert result.index("⚠ WARNING") < result.index(answer)
    assert result.index(answer) < result.index(REPHRASE_SUGGESTION.strip())


def test_rephrase_suggestion_does_not_assert_presence_or_absence():
    """The whole point: this wording must be honest for BOTH a genuinely
    absent topic and a present-but-badly-queried one -- it must not claim
    to know which case it is."""
    lowered = REPHRASE_SUGGESTION.lower()
    assert "is present" not in lowered
    assert "does exist" not in lowered
    assert "is absent" not in lowered
    assert "does not exist" not in lowered
    assert "does not mean the content is or isn't present" in lowered


def test_empty_answer_returns_unchanged():
    assert apply_confidence_safeguard("", {"sources_sufficient": False}) == ""
    assert apply_confidence_safeguard(None, {"sources_sufficient": False}) is None


# ── run_legal_research_agent(): prompt-level guardrail ──────────────────────

def test_research_agent_prompt_forbids_corpus_absence_claims(monkeypatch):
    captured = {}

    class _FakeMsg:
        content = [SimpleNamespace(text='{"research_sufficient": false, "gaps": []}')]

    def fake_create(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return _FakeMsg()

    monkeypatch.setattr(g.ai_client.messages, "create", fake_create)

    run_legal_research_agent("a query about section 17", "some retrieved context")

    prompt = captured["prompt"]
    assert "NEVER state or imply" in prompt
    assert "absent from" in prompt.lower()
    assert "cannot support" in prompt.lower() or "cannot establish" in prompt.lower()


def test_research_agent_missing_authority_field_scoped_to_retrieved_sources(monkeypatch):
    """The JSON schema instruction itself must tell the model to describe
    what the RETRIEVED SOURCES don't establish, not what's missing from
    the corpus."""
    captured = {}

    class _FakeMsg:
        content = [SimpleNamespace(text='{"research_sufficient": false, "gaps": []}')]

    def fake_create(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return _FakeMsg()

    monkeypatch.setattr(g.ai_client.messages, "create", fake_create)

    run_legal_research_agent("a query", "context")

    assert "never claim it is absent from the corpus" in captured["prompt"].lower()


# ── synthesise_answer_sync(): RESEARCH GAP MAP instruction ──────────────────

def test_research_gap_map_instruction_forbids_corpus_absence_claims(monkeypatch):
    captured = {}

    class _FakeMsg:
        content = [SimpleNamespace(text="ANSWER")]

    def fake_create(**kwargs):
        captured["content"] = kwargs["messages"][0]["content"]
        return _FakeMsg()

    orig_create = m.client.messages.create
    m.client.messages.create = fake_create
    try:
        synthesise_answer_sync(
            "a query", [{"text": "some unrelated context", "similarity": 0.3}], [], [],
            research_map={"gaps": [
                {"issue": "PEP screening", "missing_authority": "section 20 text",
                 "reason": "not found in the sources retrieved for this query"},
            ]},
        )
    finally:
        m.client.messages.create = orig_create

    content = captured["content"]
    assert "RESEARCH GAP MAP" in content
    assert 'NEVER state or imply that something "is absent from,"' in content
    assert "not found in the sources retrieved for this query" in content
