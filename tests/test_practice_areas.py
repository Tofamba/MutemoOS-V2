"""
Unit tests for backend/practice_areas.py — the fixed PRACTICE_AREAS list,
extract_classification_text(), and the classify_practice_area() keyword
classifier behind scripts/backfill_practice_areas.py.

Samples below are real "Law" field values from this week's NGM case-list
import (109 records, already cross-checked against the actual database) —
not invented text — so this doubles as the "representative sample"
calibration check for the backfill's keyword-matching logic.
"""

from backend.practice_areas import (
    PRACTICE_AREAS,
    classify_practice_area,
    extract_classification_text,
)


# ── extract_classification_text ──────────────────────────────────────────

def test_extract_takes_segment_after_em_dash():
    assert extract_classification_text("Atlas — Contract, letter of demand sent") == \
        "Contract, letter of demand sent"


def test_extract_falls_back_to_whole_name_when_no_dash():
    assert extract_classification_text("Matrimonial / divorce") == "Matrimonial / divorce"


def test_extract_uses_last_dash_when_multiple_present():
    assert extract_classification_text("A — B — C") == "C"


def test_extract_blank_input():
    assert extract_classification_text("") == ""
    assert extract_classification_text(None) == ""


# ── classify_practice_area: confident single-category matches ───────────
# Each of these is a real "Law" field value from the NGM import.

def test_classify_confident_matches_from_real_data():
    cases = {
        "Estate / Inheritance": "Estate/Inheritance",
        "Matrimonial/divorce": "Family/Matrimonial",
        "Lease/ Eviction": "Conveyancing/Property",
        "Trust": "Trust",
        "Family Trust": "Trust",
        "Company law": "Company/Commercial",
        "contract": "Company/Commercial",
        "Labour": "Labour",
        "Debt collection mining": "Debt Collection",
        "Debt collection / stands replacement": "Debt Collection",
        "Criminal fraud": "Criminal",
        "Malpractice fraud": "Criminal",
        "Land / Subdivision": "Conveyancing/Property",
        "Sale of an immovable property": "Conveyancing/Property",
        "Custody/ access": "Family/Matrimonial",
        "Family law -Access": "Family/Matrimonial",
    }
    for text, expected in cases.items():
        result = classify_practice_area(text)
        assert result == {"status": "matched", "practice_area": expected}, f"{text!r} -> {result}"


def test_classify_all_returned_categories_are_in_the_fixed_list():
    for text in ["Trust", "Labour", "Criminal fraud", "Company law", "Estate"]:
        result = classify_practice_area(text)
        if result["status"] == "matched":
            assert result["practice_area"] in PRACTICE_AREAS


# ── classify_practice_area: genuine ambiguity, never guessed ────────────

def test_classify_ambiguous_when_two_categories_both_match():
    # Real record: Huang Li Qiang's "Mukweva and Paswa Civil" matter.
    result = classify_practice_area("Debt collection/ fraud")
    assert result["status"] == "ambiguous"
    assert set(result["candidates"]) == {"Debt Collection", "Criminal"}


def test_classify_ambiguous_family_and_property_both_match():
    result = classify_practice_area("Family law and land dispute")
    assert result["status"] == "ambiguous"
    assert "Family/Matrimonial" in result["candidates"]
    assert "Conveyancing/Property" in result["candidates"]


def test_classify_ambiguous_criminal_and_matrimonial():
    result = classify_practice_area("Criminal / matrimonial")
    assert result["status"] == "ambiguous"
    assert set(result["candidates"]) == {"Criminal", "Family/Matrimonial"}


# ── classify_practice_area: no keyword match, never force-categorized ───

def test_classify_no_match_for_vague_real_values():
    for text in ["General", "Legal", "NGO", "Mining", "Mining claims", "Municipality"]:
        result = classify_practice_area(text)
        assert result["status"] == "no_match", f"{text!r} unexpectedly matched: {result}"


def test_classify_no_match_for_blank_text():
    assert classify_practice_area("") == {"status": "no_match"}
    assert classify_practice_area(None) == {"status": "no_match"}
    assert classify_practice_area("   ") == {"status": "no_match"}


def test_classify_never_returns_a_category_outside_the_fixed_list_even_when_ambiguous():
    result = classify_practice_area("Criminal / matrimonial")
    for c in result["candidates"]:
        assert c in PRACTICE_AREAS
