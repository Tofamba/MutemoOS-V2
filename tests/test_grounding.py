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
    CASE_CITATION_PATTERN,
    REPHRASE_SUGGESTION,
    apply_confidence_safeguard,
    run_legal_research_agent,
    scope_corpus_absence_claims,
    verify_citations,
    verify_inline_case_citations,
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
    """Exact replacement wording differs by grammatical family (see
    test_rewrites_preserve_grammar_not_just_substring_presence for the
    precise expected string per case) -- what every variant must share is
    that "the corpus" is gone and it's rescoped to this specific search,
    logged exactly once."""
    variants = [
        "Section 20 is missing from the corpus.",
        "The PEP definition does not exist in the corpus.",
        "These sections are not present in the corpus.",
        "The provision is not found in the corpus.",
        "This clause no longer exists in the corpus.",
    ]
    for text in variants:
        result, qc_log = scope_corpus_absence_claims(text)
        assert "the corpus" not in result.lower(), f"failed for: {text!r} -> {result!r}"
        assert "this search" in result, f"failed for: {text!r} -> {result!r}"
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


def test_rewrites_preserve_grammar_not_just_substring_presence():
    """The adjectival family (absent from/missing from) leaves the
    original auxiliary verb (is/are) untouched before the match; the
    verb-phrase family (does not exist/no longer exists/not found/not
    present) captures its own verb and only rewrites the object -- a
    single blind replacement across both broke grammar in real
    production testing (2026-09-02), see the regression test below."""
    cases = [
        ("Section 20 is missing from the corpus.",
         "Section 20 is not found in this search."),
        ("The PEP definition does not exist in the corpus.",
         "The PEP definition does not exist in the sources retrieved for this search."),
        ("These sections are not present in the corpus.",
         "These sections are not present in the sources retrieved for this search."),
        ("The provision is not found in the corpus.",
         "The provision is not found in the sources retrieved for this search."),
        ("This clause no longer exists in the corpus.",
         "This clause no longer exists in the sources retrieved for this search."),
    ]
    for original, expected in cases:
        result, _ = scope_corpus_absence_claims(original)
        assert result == expected, f"{original!r} -> {result!r}, expected {expected!r}"


def test_regression_real_production_sentence_now_reads_grammatically():
    """The exact class of sentence a real production answer produced
    (2026-09-02) before this fix: 'are absent from the corpus' swallowed
    whole by a single blind replacement produced 'they not found in this
    search' -- missing the auxiliary verb entirely."""
    original = (
        "The fact that sections 17 and 20 were not retrieved in this search "
        "does not establish that they are absent from the corpus."
    )
    result, qc_log = scope_corpus_absence_claims(original)
    assert result == (
        "The fact that sections 17 and 20 were not retrieved in this search "
        "does not establish that they are not found in this search."
    )
    assert "not found in this search" in result
    assert len(qc_log) == 1


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


# ── verify_citations(): typographic normalization (2026-09-30 false-positive fix) ──
#
# Real incident: accurate, verbatim quotes from the VAT Act [Chapter
# 23:12] and G (Private) Limited v ZIMRA (HH 11-22) were repeatedly
# flagged "Requires verification" despite being byte-for-byte present in
# the retrieved context. Root cause confirmed by direct execution and a
# live retrieval trace against real staging data (2026-09-30): the real
# ingested corpus genuinely contains curly quotes/apostrophes and
# en/em-dashes (PDF/Word-sourced ingestion), but verify_citations() only
# ever normalized whitespace -- any single typographic character
# difference between the stored chunk and the model's reproduced
# blockquote caused a full false-positive mismatch.

def test_verify_citations_identical_quote_passes():
    context = "The applicant sought a declaratur regarding input tax credits."
    answer = f"> {context}"
    result, qc_log = verify_citations(answer, context)
    assert qc_log == []
    assert result == answer


def test_verify_citations_curly_vs_straight_quotes_now_passes():
    context = 'For the purposes of this Act, “enterprise” means any activity.'
    answer = '> For the purposes of this Act, "enterprise" means any activity.'
    result, qc_log = verify_citations(answer, context)
    assert qc_log == []


def test_verify_citations_smart_apostrophe_vs_straight_now_passes():
    context = "the appellant’s objection to the penalty"
    answer = "> the appellant's objection to the penalty"
    result, qc_log = verify_citations(answer, context)
    assert qc_log == []


def test_verify_citations_en_dash_em_dash_vs_hyphen_now_passes():
    context = "see also section 6 — as amended."
    answer = "> see also section 6 - as amended."
    result, qc_log = verify_citations(answer, context)
    assert qc_log == []

    context2 = "see also section 6 – as amended."
    answer2 = "> see also section 6 - as amended."
    result2, qc_log2 = verify_citations(answer2, context2)
    assert qc_log2 == []


def test_verify_citations_genuine_mismatch_still_flagged():
    """The fix must not mask real content differences -- only typographic
    ones. A footnote marker genuinely added to the quote (not present in
    the source) is a real mismatch and must still be caught."""
    context = "any activity carried on continuously or regularly."
    answer = "> any activity carried on continuously or regularly.[1]"
    result, qc_log = verify_citations(answer, context)
    assert len(qc_log) == 1
    assert qc_log[0]["qc_status"] == "citation_unmatched"
    assert "Requires verification" in result


def test_verify_citations_real_hh1122_quote_with_curly_chars_passes():
    """Regression for the actual reported case: a realistic reproduction
    of HH 11-22's style (curly quotes, curly apostrophe) as ingested,
    quoted back with straight equivalents -- the exact real-world shape
    of the original false positive."""
    context = (
        "stated his reason for disallowing the appellant’s objection to the "
        "100% penalty as follows: “In this case, ZIMRA only registered your "
        "client compulsorily after an analysis of the nature of services "
        "rendered.”"
    )
    answer = (
        '> stated his reason for disallowing the appellant\'s objection to the '
        '100% penalty as follows: "In this case, ZIMRA only registered your '
        'client compulsorily after an analysis of the nature of services '
        'rendered."'
    )
    result, qc_log = verify_citations(answer, context)
    assert qc_log == []


# ── CASE_CITATION_PATTERN / verify_inline_case_citations(): truncation fix ──
#
# Real incident (same 2026-09-30 investigation): "G (Private) Limited v
# ZIMRA (HH 11-22)" was flagged [UNVERIFIED] even in a title line, while
# the real case was genuinely retrieved and genuinely present in context
# (confirmed via a live retrieval trace against real staging data). Root
# cause: the old pattern required every party-name token to be >=2 chars
# via a `+` quantifier and excluded parentheses from the token character
# class, so it couldn't match "G" (a single anonymized initial) or
# "(Private)" at all -- it silently slid the match onto the next
# capitalized word ("Limited v ZIMRA") and checked that arbitrary
# fragment instead of the real case name.

def test_regex_no_longer_truncates_single_letter_anonymized_party():
    text = "G (Private) Limited v ZIMRA (HH 11-22)"
    m_ = CASE_CITATION_PATTERN.search(text)
    assert m_ is not None
    assert m_.group(1) == "G (Private) Limited v ZIMRA"
    assert m_.group(2) == "HH 11-22"


def test_regex_handles_pvt_abbreviation_and_markdown_heading():
    text = "## G (Pvt) Ltd v ZIMRA (HH 11-22): VAT input tax analysis"
    m_ = CASE_CITATION_PATTERN.search(text)
    assert m_ is not None
    assert m_.group(1) == "G (Pvt) Ltd v ZIMRA"


def test_regex_handles_parenthetical_corporate_form_on_both_parties():
    text = "Moyo (Private) Limited v Chikwanha (Pvt) Ltd (HH 5-21)"
    m_ = CASE_CITATION_PATTERN.search(text)
    assert m_ is not None
    assert m_.group(1) == "Moyo (Private) Limited v Chikwanha (Pvt) Ltd"
    assert m_.group(2) == "HH 5-21"


def test_regex_still_matches_ordinary_case_with_no_parenthetical_parties():
    """Regression guard: the fix must not break the common, already-working
    case with no corporate-form parentheses at all."""
    text = "Moyo v Chikwanha (SC 45/20)"
    m_ = CASE_CITATION_PATTERN.search(text)
    assert m_ is not None
    assert m_.group(1) == "Moyo v Chikwanha"
    assert m_.group(2) == "SC 45/20"


def test_regex_still_matches_v_with_period():
    text = "Ndlovu v. Mangwana (HH 200-19)"
    m_ = CASE_CITATION_PATTERN.search(text)
    assert m_ is not None
    assert m_.group(1) == "Ndlovu v. Mangwana"


def test_regex_does_not_swallow_digit_bearing_citation_as_party_form():
    """The disambiguation the whole fix relies on: a party-form
    parenthetical is letters-only, so a digit-bearing trailing citation
    (always the real terminator) is never mistaken for one."""
    text = "G (Private) Limited v ZIMRA (HH 11-22)"
    m_ = CASE_CITATION_PATTERN.search(text)
    assert m_.group(2) == "HH 11-22"
    assert "HH 11-22" not in m_.group(1)


def test_inline_verification_real_case_full_form_not_flagged():
    answer = "## G (Private) Limited v ZIMRA (HH 11-22): VAT input tax analysis\n\nThe court held that..."
    context = (
        "G (PRIVATE) LIMITED v ZIMRA HH 11-22 "
        "The applicant, G (Private) Limited, sought a declaratur regarding input tax credits."
    )
    result, qc_log = verify_inline_case_citations(answer, context)
    assert qc_log == []
    assert "UNVERIFIED" not in result


def test_inline_verification_real_case_abbreviated_form_not_flagged():
    """Real, independent failure mode found live: even with the regex
    fixed, a correctly-retrieved case could still false-positive purely
    because the model wrote the full form ("Limited"/"Private") while the
    source spells it abbreviated ("Ltd"/"Pvt"), or vice versa. Covered by
    _normalize_case_name_abbreviations()."""
    answer = "## G (Private) Limited v ZIMRA (HH 11-22): VAT input tax analysis\n\nThe court held that..."
    context = (
        "G (Pvt) Ltd v ZIMRA HH 11-22 "
        "The applicant, G (Pvt) Ltd, sought a declaratur regarding input tax credits."
    )
    result, qc_log = verify_inline_case_citations(answer, context)
    assert qc_log == []


def test_inline_verification_real_case_context_reversed_abbreviation_not_flagged():
    """Same abbreviation gap in the other direction: model writes the
    abbreviated form, source spells it out in full."""
    answer = "## G (Pvt) Ltd v ZIMRA (HH 11-22): VAT input tax analysis\n\nThe court held that..."
    context = (
        "G (Private) Limited v ZIMRA HH 11-22 "
        "The applicant, G (Private) Limited, sought a declaratur regarding input tax credits."
    )
    result, qc_log = verify_inline_case_citations(answer, context)
    assert qc_log == []


def test_inline_verification_genuinely_absent_case_still_flagged():
    """The fix must not become a rubber stamp -- a case genuinely absent
    from the retrieved context must still be flagged."""
    answer = "## Chirwa v ZIMRA (HH 99-23): unrelated case\n\nThe court held that..."
    context = "G (Private) Limited v ZIMRA HH 11-22 sought a declaratur regarding input tax credits."
    result, qc_log = verify_inline_case_citations(answer, context)
    assert len(qc_log) == 1
    assert qc_log[0]["case_name"] == "Chirwa v ZIMRA"
    assert "UNVERIFIED" in result


def test_inline_verification_real_hh1122_live_retrieval_regression():
    """Exact regression for the live-retrieval trace run against real
    staging data (2026-09-30): the real chunk 0 text of HH 11-22, as
    actually ingested, must no longer flag the correctly-cited case."""
    answer = "## G (Private) Limited v ZIMRA (HH 11-22): VAT input tax analysis\n\nThe court held that..."
    context = (
        "1 HH 11-22 FA 6/20 G (PRIVATE) LIMITED versus ZIMBABWE REVENUE AUTHORITY "
        "SPECIAL COURT FOR INCOME TAX APPEALS ZIYAMBI AJ HARARE, 6 January 2022 "
        "Income Tax Appeal D. Ochieng, for the appellant S. Bhebhe, for the "
        "respondent ZIYAMBI AJ: [1] On 28 December 2017, the respondent's "
        "Commissioner General (“the Commissioner”) disallowed the appellant's "
        "objection to a number of VAT assessments made against it for the years "
        "2015-2016 totalling US$206 880. This is an appeal against that decision "
        "and it is brought in terms of S 33 of the Value Added Tax Act "
        "[Chapter 23:12] (“the Act”). "
        "In this case, ZIMRA only registered your client compulsorily after an "
        "analysis of the nature of services rendered."
    )
    result, qc_log = verify_inline_case_citations(answer, context)
    assert qc_log == []
    assert "UNVERIFIED" not in result
