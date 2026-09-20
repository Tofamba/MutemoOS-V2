"""
Tests for the three-way grounding state (2026-09-20): compute_grounding()
used to have only two buckets (authority / contextual), so retrieved content
whose authority classification is NULL counted in neither and got the same
note as "nothing retrieved at all" ("No binding or contextual legal sources
found. Reliance is on general principles only.") -- a false absence claim.

It is a reporting-honesty fix, NOT a reclassification: an unclassified
source is never counted as authoritative or contextual, and never makes
sources_sufficient true. NULL is common in production (most legacy legal
updates / ZLR entries were never backfilled), so the distinction matters.

Matrix (per the agreed plan): zero sources; binding only; contextual only;
NULL/unclassified only; mixed classified + unclassified. Plus the
attached-document variants and apply_confidence_safeguard()'s handling of
the new state ("Retrieval was thin" is dropped ONLY for unclassified-only).
"""
import pytest

from backend.grounding import (
    AUTHORITY_FLOOR,
    REPHRASE_SUGGESTION,
    apply_confidence_safeguard,
    compute_grounding,
)

NO_SOURCES_NOTE = "No binding or contextual legal sources found. Reliance is on general principles only."


def hit(strength, sim=0.9, **extra):
    d = {"similarity": sim, "text": "t"}
    if strength != "__missing__":
        d["authority_strength"] = strength
    d.update(extra)
    return d


# ── zero retrieved sources ───────────────────────────────────────────────

def test_zero_sources():
    g = compute_grounding([], [], [])
    assert g["grounding_state"] == "no_sources"
    assert g["grounding_note"] == NO_SOURCES_NOTE
    assert g["source_tier_breakdown"] == {"authority": 0, "context": 0, "unclassified": 0}
    assert g["sources_sufficient"] is False


# ── binding sources only ─────────────────────────────────────────────────

def test_binding_only_above_floor_is_grounded():
    g = compute_grounding([], [hit("binding", 0.9)], [])
    assert g["grounding_state"] == "grounded"
    assert g["grounding_note"] == "✓ Grounded in 1 authoritative source(s)."
    assert g["source_tier_breakdown"] == {"authority": 1, "context": 0, "unclassified": 0}
    assert g["sources_sufficient"] is True


def test_persuasive_counts_as_authority_and_below_floor_is_reported_as_such():
    g = compute_grounding([], [hit("persuasive", AUTHORITY_FLOOR - 0.1)], [])
    assert g["grounding_state"] == "authority_below_threshold"
    assert g["sources_sufficient"] is False
    assert "below the confidence threshold" in g["grounding_note"]


# ── contextual sources only ──────────────────────────────────────────────

def test_contextual_only():
    g = compute_grounding([], [hit("contextual")], [hit("contextual")])
    assert g["grounding_state"] == "contextual_only"
    assert g["grounding_note"].startswith("No binding or persuasive authority found. Supported only by 2 contextual source(s)")
    assert g["source_tier_breakdown"] == {"authority": 0, "context": 2, "unclassified": 0}
    assert g["sources_sufficient"] is False


# ── NULL / unclassified sources only ─────────────────────────────────────

@pytest.mark.parametrize("value", [None, "", "not-a-real-strength", "__missing__"])
def test_unclassified_only_is_not_reported_as_nothing_retrieved(value):
    """NULL, empty, an unknown value, or a key missing entirely -- all
    'retrieved but unclassified'. The third state must not read as the first."""
    g = compute_grounding([], [hit(value, 0.95)], [])
    assert g["grounding_state"] == "unclassified_only"
    assert g["grounding_note"] != NO_SOURCES_NOTE
    assert "No binding or contextual legal sources found" not in g["grounding_note"]
    assert "Sources retrieved (1)" in g["grounding_note"]
    assert "authority classification is unavailable" in g["grounding_note"]
    assert g["source_tier_breakdown"] == {"authority": 0, "context": 0, "unclassified": 1}


def test_unclassified_is_never_made_authoritative_or_sufficient():
    """Even a near-perfect similarity must not let unclassified content pass
    the authority floor -- this is honesty reporting, not reclassification."""
    g = compute_grounding([hit(None, 0.99)], [hit(None, 0.99)], [hit(None, 0.99)])
    assert g["source_tier_breakdown"]["authority"] == 0
    assert g["source_tier_breakdown"]["context"] == 0
    assert g["sources_sufficient"] is False
    assert g["max_similarity_score"] == 0
    assert g["source_tier_breakdown"]["unclassified"] == 3


def test_unclassified_count_spans_firm_legal_and_zlr_results():
    g = compute_grounding([hit(None)], [hit(None), hit(None)], [hit(None)])
    assert g["source_tier_breakdown"]["unclassified"] == 4
    assert "Sources retrieved (4)" in g["grounding_note"]


# ── mixed classified and unclassified ────────────────────────────────────

def test_binding_plus_unclassified_stays_grounded_and_discloses_the_unclassified():
    g = compute_grounding([], [hit("binding", 0.9), hit(None, 0.99)], [])
    assert g["grounding_state"] == "grounded"
    assert g["sources_sufficient"] is True
    assert g["grounding_note"].startswith("✓ Grounded in 1 authoritative source(s).")
    assert "1 further retrieved source(s) have no authority classification and are not counted above." in g["grounding_note"]
    assert g["source_tier_breakdown"] == {"authority": 1, "context": 0, "unclassified": 1}
    # The unclassified hit's higher similarity must not leak into the authority score.
    assert g["max_similarity_score"] == 0.9


def test_contextual_plus_unclassified_stays_contextual_only_and_discloses_the_unclassified():
    g = compute_grounding([], [hit("contextual"), hit(None)], [])
    assert g["grounding_state"] == "contextual_only"
    assert g["sources_sufficient"] is False
    assert "Supported only by 1 contextual source(s)" in g["grounding_note"]
    assert "1 further retrieved source(s) have no authority classification" in g["grounding_note"]


def test_unclassified_cannot_rescue_authority_that_is_below_the_floor():
    g = compute_grounding([], [hit("binding", AUTHORITY_FLOOR - 0.2), hit(None, 0.99)], [])
    assert g["grounding_state"] == "authority_below_threshold"
    assert g["sources_sufficient"] is False


def test_classified_only_results_carry_no_unclassified_sentence():
    g = compute_grounding([], [hit("binding", 0.9), hit("contextual")], [])
    assert "no authority classification" not in g["grounding_note"]
    assert g["source_tier_breakdown"]["unclassified"] == 0


# ── attached-document variants ───────────────────────────────────────────

def test_attached_doc_with_zero_hits_keeps_its_existing_note():
    g = compute_grounding([], [], [], has_attached_doc=True)
    assert g["grounding_state"] == "no_sources"
    assert g["grounding_note"].startswith("No binding or persuasive authority found. Supported only by 0 contextual source(s)")


def test_attached_doc_with_unclassified_only_is_reported_as_unclassified_not_zero_contextual():
    g = compute_grounding([], [hit(None)], [], has_attached_doc=True)
    assert g["grounding_state"] == "unclassified_only"
    assert "Supported only by 0 contextual" not in g["grounding_note"]
    assert "Sources retrieved (1)" in g["grounding_note"]


# ── response shape stays backward compatible ─────────────────────────────

def test_existing_keys_are_preserved():
    g = compute_grounding([], [hit("binding")], [])
    for key in ("sources_sufficient", "grounding_note", "max_similarity_score", "source_tier_breakdown"):
        assert key in g
    assert {"authority", "context"} <= set(g["source_tier_breakdown"])


# ── apply_confidence_safeguard(): the suffix is dropped ONLY when unclassified-only ──

ASSERTIVE = "This is clear and certain: the provision applies."
PLAIN = "The provision may apply."


def test_unclassified_only_drops_the_thin_retrieval_suffix():
    g = compute_grounding([], [hit(None, 0.95)], [])
    out = apply_confidence_safeguard(PLAIN, g)
    assert out == PLAIN
    assert REPHRASE_SUGGESTION not in out
    assert "Retrieval was thin" not in out


def test_unclassified_only_assertive_answer_gets_accurate_unverified_warning_not_the_general_principles_one():
    g = compute_grounding([], [hit(None, 0.95)], [])
    out = apply_confidence_safeguard(ASSERTIVE, g)
    assert out.startswith("**⚠ WARNING: AUTHORITY UNVERIFIED.**")
    assert "relies on general principles" not in out
    assert REPHRASE_SUGGESTION not in out
    assert ASSERTIVE in out


@pytest.mark.parametrize("sources", [
    ([], [], []),                                                   # no_sources
    ([], [hit("contextual")], []),                                  # contextual_only
    ([], [hit("binding", AUTHORITY_FLOOR - 0.2)], []),              # authority_below_threshold
    ([], [hit("contextual"), hit(None)], []),                       # mixed, still thin/insufficient
    ([], [hit("binding", AUTHORITY_FLOOR - 0.2), hit(None)], []),   # mixed, still below floor
])
def test_genuinely_thin_cases_keep_the_suffix_unchanged(sources):
    g = compute_grounding(*sources)
    assert g["sources_sufficient"] is False and g["grounding_state"] != "unclassified_only"
    out = apply_confidence_safeguard(PLAIN, g)
    assert out == f"{PLAIN}{REPHRASE_SUGGESTION}"


def test_thin_case_assertive_answer_keeps_the_original_general_principles_warning():
    g = compute_grounding([], [hit("contextual")], [])
    out = apply_confidence_safeguard(ASSERTIVE, g)
    assert out.startswith("**⚠ WARNING: ANALOGOUS ANALYSIS ONLY.**")
    assert out.endswith(REPHRASE_SUGGESTION)


def test_sufficient_grounding_is_returned_untouched():
    g = compute_grounding([], [hit("binding", 0.9), hit(None)], [])
    assert apply_confidence_safeguard(ASSERTIVE, g) == ASSERTIVE
