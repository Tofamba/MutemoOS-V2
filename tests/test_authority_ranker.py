"""
Unit tests for backend/authority_ranker.py.

Includes a synthetic before/after reproduction of the exact bug reported
against the "Police Amendment Bill H.B.11, 2025" query: pure vector
similarity surfacing correspondence, a mining judgment, and an employment
judgment ahead of the Bill, the Act it amends, the Constitution, and an
on-point Supreme Court case. No live Chroma/Postgres connection is used —
every fixture below is a plain dict shaped exactly like the dicts
_semantic_search_firm/_semantic_search_legal/_zlr_semantic_search produce.
"""

import pytest

from backend.authority_ranker import (
    AUTHORITY_WEIGHTS,
    RELEVANCE_FLOOR,
    classify_confidence,
    compute_authority_score,
    extract_query_understanding,
    passes_hard_filter,
    rerank,
    source_tier,
    suggest_cross_references,
)
from backend.legal_taxonomy import LegalSourceType


# ── Authority weights / tiers ────────────────────────────────────────────────

def test_authority_weight_ordering_matches_court_hierarchy():
    w = AUTHORITY_WEIGHTS
    assert w[LegalSourceType.CONSTITUTION] > w[LegalSourceType.STATUTE]
    assert w[LegalSourceType.STATUTE] > w[LegalSourceType.STATUTORY_INSTRUMENT]
    assert w[LegalSourceType.SUPREME_COURT] > w[LegalSourceType.HIGH_COURT]
    assert w[LegalSourceType.HIGH_COURT] > w[LegalSourceType.LABOUR_COURT]
    assert w[LegalSourceType.LABOUR_COURT] > w[LegalSourceType.ADMIN_TRIBUNAL]
    assert w[LegalSourceType.FIRM_PRECEDENT] > w[LegalSourceType.CORRESPONDENCE]
    assert w[LegalSourceType.CORRESPONDENCE] == 0
    assert w[LegalSourceType.UNKNOWN] == 0


@pytest.mark.parametrize("source_type,expected_tier", [
    (LegalSourceType.CONSTITUTION, "primary"),
    (LegalSourceType.STATUTE, "primary"),
    (LegalSourceType.BILL, "primary"),
    (LegalSourceType.STATUTORY_INSTRUMENT, "primary"),
    (LegalSourceType.SUPREME_COURT, "secondary"),
    (LegalSourceType.HIGH_COURT, "secondary"),
    (LegalSourceType.LABOUR_COURT, "secondary"),
    (LegalSourceType.FIRM_PRECEDENT, "commentary"),
    (LegalSourceType.OPINION, "commentary"),
    (LegalSourceType.CORRESPONDENCE, "background"),
    (LegalSourceType.UNKNOWN, "background"),
])
def test_source_tier_grouping(source_type, expected_tier):
    assert source_tier(source_type) == expected_tier


# ── Stage 1: query understanding ─────────────────────────────────────────────

def test_extracts_bill_and_implied_principal_act():
    qu = extract_query_understanding("Police Amendment Bill H.B.11, 2025")
    assert qu["bills"] == ["Police Amendment Bill"]
    assert qu["bill_numbers"] == ["H.B.11, 2025"]
    assert qu["implied_principal_acts"] == ["Police Act"]
    assert "police act" in qu["anchor_terms"]
    assert "commissioner-general" in qu["anchor_terms"]  # curated domain term, not literal query text
    assert qu["has_specific_legal_reference"] is True


def test_extracts_act_directly():
    qu = extract_query_understanding("What does the Labour Act [Chapter 28:01] say about retrenchment?")
    assert qu["acts"] == ["Labour Act"]


def test_extracts_constitution_sections_only_when_constitution_mentioned():
    qu = extract_query_understanding(
        "What does the Constitution say in sections 207 to 223 about the Police Service?"
    )
    assert qu["constitution_sections"] == [("207", "223")]

    # "sections 12 to 15" alone, with no mention of "Constitution", should
    # not be misread as a constitutional reference.
    qu2 = extract_query_understanding("What do sections 12 to 15 of the lease say?")
    assert qu2["constitution_sections"] == []


def test_extracts_statutory_instrument():
    qu = extract_query_understanding("What does Statutory Instrument 45 of 2023 require?")
    assert qu["statutory_instruments"] == ["S.I. 45 of 2023"]


def test_extracts_case_citation():
    qu = extract_query_understanding("In light of Smith v Jones (2019) ZLR 12, what is the notice period rule?")
    assert qu["cases"] == ["Smith v Jones"]


def test_query_with_no_legal_entities_has_no_anchor_terms():
    qu = extract_query_understanding("What should I have for lunch?")
    assert qu["acts"] == []
    assert qu["bills"] == []
    assert qu["anchor_terms"] == set()
    assert qu["has_specific_legal_reference"] is False


# ── Stage 4: authority scoring ───────────────────────────────────────────────

def test_constitution_outscores_higher_similarity_correspondence():
    """The whole point of this module: authority must dominate similarity."""
    qu = extract_query_understanding("Police Amendment Bill H.B.11, 2025")

    constitution_item = {
        "legal_source_type": "constitution",
        "similarity": 0.20,  # deliberately low
        "filename": "Constitution of Zimbabwe",
        "text": "Sections 207 to 223 govern the Police Service and the Commissioner-General.",
    }
    letter_item = {
        "legal_source_type": "correspondence",
        "similarity": 0.85,  # deliberately high — "technically similar, legally useless"
        "filename": "Letter to Carmen",
        "text": "Dear Carmen, regarding your power of attorney and service of the notice...",
    }

    constitution_score = compute_authority_score(constitution_item, qu)
    letter_score = compute_authority_score(letter_item, qu)

    assert constitution_score["final_score"] > letter_score["final_score"]
    assert constitution_score["tier"] == "primary"
    assert letter_score["tier"] == "background"


def test_act_name_match_fires_for_implied_principal_act():
    qu = extract_query_understanding("Police Amendment Bill H.B.11, 2025")
    act_item = {
        "legal_source_type": "statute",
        "similarity": 0.3,
        "filename": "Police Act [Chapter 11:10]",
        "text": "This Act establishes the Police Service under the Commissioner-General.",
    }
    score = compute_authority_score(act_item, qu)
    assert score["act_name_match"] == 25
    assert any("Police Act" in r for r in score["reasons"])


def test_citation_overlap_when_case_named_in_query():
    qu = extract_query_understanding("In light of Smith v Jones (2019) ZLR 12, what is the notice period rule?")
    case_item = {
        "legal_source_type": "high_court_judgment",
        "similarity": 0.4,
        "case_name": "Smith v Jones",
        "text": "The court held that the notice period runs from the date of receipt.",
    }
    unrelated_item = {
        "legal_source_type": "high_court_judgment",
        "similarity": 0.4,
        "case_name": "Totally Unrelated Case",
        "text": "This case concerns a boundary dispute.",
    }
    assert compute_authority_score(case_item, qu)["citation_overlap"] == 10
    assert compute_authority_score(unrelated_item, qu)["citation_overlap"] == 0


def test_unknown_source_type_gets_zero_authority_weight():
    qu = extract_query_understanding("Police Amendment Bill H.B.11, 2025")
    item = {"legal_source_type": None, "similarity": 0.9, "filename": "mystery.pdf", "text": "police"}
    score = compute_authority_score(item, qu)
    assert score["authority_score"] == 0
    assert score["legal_source_type"] == "unknown"


def test_rerank_does_not_clobber_preexisting_authority_weight_field():
    """
    Regression test: ZLR results already carry a STRING authority_weight
    field ("Binding"/"Persuasive", from get_authority_weight() in main.py,
    already shown in a frontend badge). rerank() merges {**item, **score}
    onto every result — if compute_authority_score's own output dict ever
    reused that key name for its numeric score, this merge would silently
    overwrite the pre-existing string with a number, breaking that badge.
    """
    item = {
        "legal_source_type": "supreme_court_judgment", "similarity": 0.3,
        "case_name": "Sadengu v Board President", "text": "police disciplinary appeal",
        "authority_weight": "Binding",  # pre-existing ZLR field, must survive rerank()
    }
    outcome = rerank([item], "Police Amendment Bill H.B.11, 2025")
    assert outcome["results"][0]["authority_weight"] == "Binding"
    assert outcome["results"][0]["authority_score"] == 85


# ── Hard filters ──────────────────────────────────────────────────────────

def test_offdomain_correspondence_is_hard_filtered():
    qu = extract_query_understanding("Police Amendment Bill H.B.11, 2025")
    item = {
        "legal_source_type": "correspondence",
        "matter_type": "commercial_contract",
        "similarity": 0.6,
        "filename": "Letter to Carmen",
        "text": "Dear Carmen, regarding your power of attorney...",
    }
    score = compute_authority_score(item, qu)
    assert passes_hard_filter(item, qu, score) is False


def test_correspondence_survives_if_it_explicitly_references_anchor_term():
    """The brief's own "UNLESS" clause: an explicit reference must save a document from exclusion."""
    qu = extract_query_understanding("Police Amendment Bill H.B.11, 2025")
    item = {
        "legal_source_type": "correspondence",
        "matter_type": "commercial_contract",
        "similarity": 0.3,
        "filename": "Letter regarding Police Act compliance",
        "text": "This letter addresses our client's obligations under the Police Act.",
    }
    score = compute_authority_score(item, qu)
    assert passes_hard_filter(item, qu, score) is True


def test_offdomain_case_law_is_demoted_not_deleted():
    """Case law is never hard-excluded — only correspondence/narrow-matter firm docs are."""
    qu = extract_query_understanding("Police Amendment Bill H.B.11, 2025")
    mining_case = {
        "legal_source_type": "high_court_judgment",
        "similarity": 0.55,
        "case_name": "Mining Corp v ZMDC",
        "text": "This case concerns the exercise of statutory powers over mining rights.",
    }
    score = compute_authority_score(mining_case, qu)
    assert passes_hard_filter(mining_case, qu, score) is True  # not excluded...
    # ...but scores far below anything genuinely on point, since it earns
    # none of the authority/match bonuses (asserted properly in the full
    # scenario test below).
    assert score["act_name_match"] == 0


def test_relevance_floor_excludes_pure_noise_regardless_of_type():
    qu = extract_query_understanding("Police Amendment Bill H.B.11, 2025")
    item = {"legal_source_type": None, "similarity": 0.02, "filename": "random letters", "text": "xyz"}
    score = compute_authority_score(item, qu)
    assert score["final_score"] < RELEVANCE_FLOOR
    assert passes_hard_filter(item, qu, score) is False


def test_no_anchor_terms_means_no_domain_filtering():
    """A query with no specific legal entity has nothing to filter background docs against."""
    qu = extract_query_understanding("What are the general principles of contract law?")
    assert qu["anchor_terms"] == set()  # nothing for the domain check to filter against

    item = {
        "legal_source_type": "correspondence", "matter_type": "commercial_contract",
        "similarity": 0.3, "filename": "Letter to Carmen",
        # Shares query terms so it clears RELEVANCE_FLOOR on its own merit,
        # isolating the "no anchor_terms" branch rather than confounding it
        # with the separate relevance-floor check.
        "text": "This letter discusses the general principles of contract law relevant to your matter.",
    }
    score = compute_authority_score(item, qu)
    assert score["final_score"] >= RELEVANCE_FLOOR
    assert passes_hard_filter(item, qu, score) is True


# ── Confidence classification ────────────────────────────────────────────────

def test_confidence_primary_authority_found():
    items = [{"tier": "primary"}, {"tier": "background"}]
    assert classify_confidence(items) == "PRIMARY AUTHORITY FOUND"


def test_confidence_secondary_authority_found_when_no_primary():
    items = [{"tier": "secondary"}, {"tier": "commentary"}]
    assert classify_confidence(items) == "SECONDARY AUTHORITY FOUND"


def test_confidence_never_says_no_precedent_when_primary_legislation_present():
    """Direct requirement from the brief: primary legislation alone is a positive finding."""
    items = [{"tier": "primary"}]  # zero case law/precedent at all
    result = classify_confidence(items)
    assert result == "PRIMARY AUTHORITY FOUND"
    assert "no" not in result.lower()


def test_confidence_background_only():
    items = [{"tier": "commentary"}, {"tier": "commentary"}]
    assert classify_confidence(items) == "ONLY BACKGROUND MATERIAL FOUND"


def test_confidence_none_found_for_empty_results():
    assert classify_confidence([]) == "NO RELEVANT LEGAL AUTHORITY FOUND"


# ── Cross-reference expansion ────────────────────────────────────────────────

def test_suggests_principal_act_for_bill():
    qu = extract_query_understanding("Police Amendment Bill H.B.11, 2025")
    suggestions = suggest_cross_references(qu)
    act_suggestions = [s for s in suggestions if s["type"] == "principal_act"]
    assert act_suggestions == [{"type": "principal_act", "search_term": "Police Act"}]


def test_suggests_constitution_sections_for_police_domain():
    qu = extract_query_understanding("Police Amendment Bill H.B.11, 2025")
    suggestions = suggest_cross_references(qu)
    assert any(s["type"] == "constitution_sections" for s in suggestions)


def test_no_cross_references_for_unrelated_query():
    qu = extract_query_understanding("What should I have for lunch?")
    assert suggest_cross_references(qu) == []


# ── Full before/after scenario: the reported bug, reproduced and fixed ─────

QUERY = "Police Amendment Bill H.B.11, 2025"

# One item per category named in the bug report — "Actual" (noise) items
# deliberately given HIGHER raw similarity than the "Expected" (signal)
# items, to reproduce "technically correct but legally useless" similarity.
FIXTURE_RESULTS = [
    {
        "legal_source_type": "bill", "similarity": 0.42,
        "filename": "Police Amendment Bill, H.B. 11, 2025",
        "text": "This Bill amends the Police Act to revise disciplinary procedures for the Police Service.",
    },
    {
        "legal_source_type": "statute", "similarity": 0.38,
        "filename": "Police Act [Chapter 11:10]",
        "text": "This Act establishes the Police Service under the Commissioner-General.",
    },
    {
        "legal_source_type": "constitution", "similarity": 0.35,
        "filename": "Constitution of Zimbabwe",
        "text": "Sections 207 to 223 of the Constitution govern the Police Service.",
    },
    {
        "legal_source_type": "supreme_court_judgment", "similarity": 0.30,
        "case_name": "Sadengu v Board President",
        "text": "A police disciplinary appeal concerning internal review by the Commissioner-General.",
    },
    # --- noise, each given deliberately higher similarity than the above ---
    {
        "legal_source_type": "correspondence", "matter_type": "commercial_contract",
        "similarity": 0.60,
        "filename": "Letter to Carmen",
        "text": "Dear Carmen, regarding your power of attorney and the service of this notice.",
    },
    {
        "legal_source_type": "high_court_judgment", "similarity": 0.55,
        "case_name": "Mining Corp v ZMDC",
        "text": "This case concerns the exercise of statutory powers over mining rights.",
    },
    {
        "legal_source_type": "labour_court", "similarity": 0.50,
        "case_name": "Chikwanha v Employer",
        "text": "A dispute over the powers of the employer under the service contract.",
    },
    {
        "legal_source_type": "firm_precedent", "matter_type": "commercial_property",
        "similarity": 0.45,
        "filename": "Sale of Commercial Property Agreement",
        "text": "This agreement grants the buyer full power and authority over the property.",
    },
]


def test_before_pure_similarity_ranking_reproduces_the_bug():
    """
    "Before": sorting by raw similarity alone — the actual, reported
    failure mode. Noise outranks every genuine authority.
    """
    before = sorted(FIXTURE_RESULTS, key=lambda r: r["similarity"], reverse=True)
    before_types = [r["legal_source_type"] for r in before]

    assert before_types[0] == "correspondence"           # "Letter to Carmen" ranks #1
    assert before_types[:3] == ["correspondence", "high_court_judgment", "labour_court"]
    # The Bill itself doesn't even make the top 3 on similarity alone.
    assert "bill" not in before_types[:3]


def test_after_authority_reranking_fixes_the_bug():
    """
    "After": rerank() — authority genuinely dominates, noise is filtered
    or sinks to the bottom, and primary sources lead.
    """
    outcome = rerank(FIXTURE_RESULTS, QUERY)
    after_types = [r["legal_source_type"] for r in outcome["results"]]

    # Primary legislation and the on-point Bill now lead the ranking.
    assert after_types[0] in ("constitution", "statute", "bill")
    assert set(after_types[:4]) == {"bill", "statute", "constitution", "supreme_court_judgment"}

    # The correspondence letter — highest raw similarity in the fixture —
    # is hard-filtered out entirely (narrow matter_type, no anchor match).
    assert "correspondence" not in after_types

    # The commercial-property firm precedent is likewise excluded.
    assert not any(
        r["legal_source_type"] == "firm_precedent" for r in outcome["results"]
    )

    # Off-domain case law (mining, employment) is demoted, not excluded —
    # it survives but ranks below every genuine authority.
    assert "high_court_judgment" in after_types or "labour_court" in after_types
    mining_or_employment_positions = [
        i for i, t in enumerate(after_types) if t in ("high_court_judgment", "labour_court")
    ]
    on_point_positions = [
        i for i, t in enumerate(after_types)
        if t in ("bill", "statute", "constitution", "supreme_court_judgment")
    ]
    assert max(on_point_positions) < min(mining_or_employment_positions)

    # Confidence correctly reports primary authority, not "no precedent found".
    assert outcome["confidence"] == "PRIMARY AUTHORITY FOUND"

    # Cross-reference expansion surfaces the Act the Bill amends.
    act_suggestions = [s for s in outcome["cross_references"] if s["type"] == "principal_act"]
    assert act_suggestions == [{"type": "principal_act", "search_term": "Police Act"}]

    # Every surviving result carries an explanation, never a bare score.
    assert all(r["reasons"] for r in outcome["results"])

    # Source grouping separates primary/secondary from commentary/background.
    assert len(outcome["source_groups"]["primary"]) == 3        # Bill, Act, Constitution
    assert len(outcome["source_groups"]["secondary"]) >= 1       # Sadengu at minimum


def test_before_after_excluded_count_is_nonzero():
    outcome = rerank(FIXTURE_RESULTS, QUERY)
    assert outcome["excluded_count"] >= 2  # the letter and the firm precedent, at least
