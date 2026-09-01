"""
Fixed practice-area categories for matters, and the pure keyword
classifier behind scripts/backfill_practice_areas.py — kept separate from
backend/main.py (like backend/numbering.py and backend/client_migration.py)
so it's importable from a standalone script without pulling in the app's
full import chain, and unit-testable without a database.

The category list was checked against the actual "Law" field values in
this week's NGM case-list import (109 real records) rather than picked
abstractly — Conveyancing/Property, Estate/Inheritance, Family/Matrimonial,
Trust, and Debt Collection each cover a real, common cluster in that data;
Litigation and Company/Commercial cover the rest; Other is the honest
catch-all for values too vague to categorize from keywords alone ("Legal",
"General", "NGO").
"""

import re

PRACTICE_AREAS = [
    "Litigation",
    "Conveyancing/Property",
    "Estate/Inheritance",
    "Family/Matrimonial",
    "Trust",
    "Company/Commercial",
    "Labour",
    "Debt Collection",
    "Criminal",
    "Other",
]

# Keyword phrases per category, checked as whole-word/phrase matches
# against normalized text. Deliberately narrow rather than broad — a vague
# or generic keyword (e.g. "dispute", "matter") would cause unrelated
# categories to collide on unrelated text, which is exactly what the
# ambiguous-match rule below is trying to catch on genuine overlaps
# ("Debt collection/fraud"), not manufacture on noise.
_PRACTICE_AREA_KEYWORDS = {
    "Litigation": [
        "litigation", "appeal", "urgent application", "interdict",
        "chamber application", "review application", "high court application",
    ],
    "Conveyancing/Property": [
        "conveyancing", "conveyancng", "property", "land", "lease", "eviction",
        "cession", "sale of", "stand", "subdivision", "immovable", "transfer",
        "agreement of sale", "agreemnt of sale",
    ],
    "Estate/Inheritance": [
        "estate", "inheritance", "probate", "deceased", "executor", "master:",
        "letters of administration",
    ],
    "Family/Matrimonial": [
        "matrimonial", "matrimonaial", "divorce", "family law", "custody",
        "access", "guardianship", "maintenance",
    ],
    "Trust": [
        "trust",
    ],
    "Company/Commercial": [
        "company", "commercial", "contract", "shareholder", "partnership",
        "business",
    ],
    "Labour": [
        "labour", "employment", "unfair dismissal", "retrenchment", "conciliator",
    ],
    "Debt Collection": [
        "debt collection", "arrears", "outstanding payment",
    ],
    "Criminal": [
        "criminal", "fraud", "theft", "malpractice", "prosecutor",
    ],
}


# Maps backend/main.py's INTAKE_MATTER_TYPES (the controlled enum
# collected by POST /api/onboarding/intake's matter_type field) onto
# PRACTICE_AREAS. No keyword classification needed for this path at all —
# the lawyer already picked an unambiguous category from a fixed list at
# intake time, so the only gap was that practice_area was never actually
# set from it. Kept here (not in main.py) so it lives next to
# PRACTICE_AREAS itself, the single source of truth for this concept.
# Values without a clean single practice area (customary_law, mining) map
# to "Other" -- an honest catch-all, same convention as the keyword
# classifier's own "no confident match" behavior, not a guess.
INTAKE_MATTER_TYPE_TO_PRACTICE_AREA = {
    "eviction": "Conveyancing/Property",
    "estate": "Estate/Inheritance",
    "trust": "Trust",
    "employment": "Labour",
    "commercial_property": "Conveyancing/Property",
    "commercial_contract": "Company/Commercial",
    "conveyancing": "Conveyancing/Property",
    "customary_law": "Other",
    "matrimonial": "Family/Matrimonial",
    "family_law": "Family/Matrimonial",
    "company_law": "Company/Commercial",
    "criminal": "Criminal",
    "constitutional": "Litigation",
    "debt_collection": "Debt Collection",
    "mining": "Other",
    "litigation_general": "Litigation",
    "other": "Other",
}


def _normalize(text: str) -> str:
    s = (text or "").lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _matched_categories(text: str) -> list:
    """Returns every category with at least one keyword phrase present as
    a whole-word/phrase match in the normalized text (padding with spaces
    so a phrase match can't land mid-word)."""
    padded = f" {_normalize(text)} "
    matched = []
    for category, keywords in _PRACTICE_AREA_KEYWORDS.items():
        for kw in keywords:
            if f" {kw} " in padded:
                matched.append(category)
                break
    return matched


def extract_classification_text(matter_name: str) -> str:
    """
    Matter names firm-wide follow (or are encouraged to follow, via the
    New Matter form's own placeholder) a "{Reference/party} — {description}"
    convention — the bulk-onboarding endpoint's own upload format, and the
    NGM case-list import's "Re — Law" combination. The description half is
    the useful classification signal; the reference/party half is mostly
    noise for this purpose (a person or company name rarely hints at
    practice area). Falls back to the whole name when there's no em-dash
    to split on.
    """
    if not matter_name:
        return ""
    if " — " in matter_name:
        return matter_name.rsplit(" — ", 1)[-1]
    return matter_name


def classify_practice_area(text: str) -> dict:
    """
    Classifies free text (typically the descriptive half of a matter's
    name, e.g. the "Law" side of "Re — Law") against PRACTICE_AREAS.

    Returns one of:
        {"status": "matched", "practice_area": str}
        {"status": "no_match"}       — no category's keywords appear at all
        {"status": "ambiguous", "candidates": [str, ...]}  — 2+ categories
            both matched (e.g. "Debt collection/fraud" hits both Debt
            Collection and Criminal) — a real, competing signal, not
            something to guess between.

    Exactly one matched category is the only case treated as confident
    enough to auto-apply — deliberately not score-weighted or
    threshold-tuned, so the rule stays easy to explain and to verify by
    reading the report: either only one category's keywords showed up, or
    they didn't.
    """
    if not text or not text.strip():
        return {"status": "no_match"}
    matched = _matched_categories(text)
    if not matched:
        return {"status": "no_match"}
    if len(matched) == 1:
        return {"status": "matched", "practice_area": matched[0]}
    return {"status": "ambiguous", "candidates": matched}
