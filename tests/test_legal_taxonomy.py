"""
Unit tests for backend/legal_taxonomy.py's classify_legal_update() and
authority_strength_for() -- previously only exercised indirectly through
higher-level integration tests (tests/test_legislation_validity_dispute_
caveat.py). These pin the actual source_type -> LegalSourceType ->
AuthorityStrength mapping directly, including "guidance" (2026-09-10,
added for FIU's AML/CFT risk-based-approach guidance and any future
regulator/government guidance document -- see that commit for the full
reasoning on why this is a distinct source_type from "press_statement"
sharing the same GOVERNMENT_PUBLICATION/CONTEXTUAL treatment, not a new
authority tier and not reused "legislation").
"""

import pytest

from backend.legal_taxonomy import (
    AuthorityStrength,
    LegalSourceType,
    authority_strength_for,
    classify_legal_update,
)


# ── classify_legal_update() ─────────────────────────────────────────────

def test_legislation_with_no_reference_hint_defaults_to_statute():
    assert classify_legal_update("legislation", None) == LegalSourceType.STATUTE
    assert classify_legal_update("legislation", "") == LegalSourceType.STATUTE


def test_legislation_referencing_a_bill():
    assert classify_legal_update("legislation", "Cyber Security Bill") == LegalSourceType.BILL


def test_legislation_referencing_a_statutory_instrument():
    assert classify_legal_update("legislation", "Statutory Instrument 76 of 2025") == LegalSourceType.STATUTORY_INSTRUMENT
    assert classify_legal_update("legislation", "SI 76 of 2025") == LegalSourceType.STATUTORY_INSTRUMENT


def test_legislation_referencing_the_constitution():
    assert classify_legal_update("legislation", "Constitution of Zimbabwe Amendment (No. 2) Act") == LegalSourceType.CONSTITUTION


def test_press_statement_is_government_publication():
    assert classify_legal_update("press_statement") == LegalSourceType.GOVERNMENT_PUBLICATION


def test_guidance_is_also_government_publication_not_legislation():
    """The actual case this test file was added for: FIU's "Guidance for
    Legal Professionals on the Risk Based Approach to Implementation of
    AML/CFT/CPF Obligations" must NOT classify as STATUTE/BINDING (that
    would misrepresent regulator guidance as the Act itself) -- it gets
    the same GOVERNMENT_PUBLICATION type press_statement already uses."""
    assert classify_legal_update("guidance") == LegalSourceType.GOVERNMENT_PUBLICATION
    assert classify_legal_update("guidance", "FIU Guidance V3 (June 2026)") == LegalSourceType.GOVERNMENT_PUBLICATION


def test_unrecognized_source_type_is_unknown():
    assert classify_legal_update("case_law") == LegalSourceType.UNKNOWN
    assert classify_legal_update("something_new") == LegalSourceType.UNKNOWN
    assert classify_legal_update(None) == LegalSourceType.UNKNOWN


# ── authority_strength_for() ─────────────────────────────────────────────

def test_guidance_gets_contextual_not_binding_or_persuasive():
    """The other half of the same real concern: even if classify_legal_
    update() were somehow wrong, authority_strength_for() is the actual
    gate compute_grounding() reads -- confirm GOVERNMENT_PUBLICATION
    (guidance's real type) lands on CONTEXTUAL, not BINDING (legislation/
    top courts) or PERSUASIVE (lower courts/tribunals) -- this taxonomy's
    3-tier scheme reserves those for actual law and judicial precedent."""
    source_type = classify_legal_update("guidance")
    assert authority_strength_for(source_type) == AuthorityStrength.CONTEXTUAL


def test_authority_strength_for_accepts_the_raw_string_value_too():
    """authority_strength_for() is also called with a plain string in some
    call sites (e.g. a stored legal_source_type column value), not just
    the enum -- confirm both forms agree."""
    assert authority_strength_for("government_publication") == AuthorityStrength.CONTEXTUAL
    assert authority_strength_for(LegalSourceType.GOVERNMENT_PUBLICATION) == AuthorityStrength.CONTEXTUAL


def test_statute_and_constitution_are_binding():
    assert authority_strength_for(LegalSourceType.STATUTE) == AuthorityStrength.BINDING
    assert authority_strength_for(LegalSourceType.CONSTITUTION) == AuthorityStrength.BINDING


def test_unknown_source_type_string_falls_back_to_contextual():
    assert authority_strength_for("not_a_real_type") == AuthorityStrength.CONTEXTUAL
