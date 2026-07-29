"""
Authority-first retrieval re-ranking for MutemoOS.

The retrieval pipeline's vector search alone treats "worded similarly" as
if it meant "legally authoritative on this question" — which is why a
Police Amendment Bill query can surface mining or employment judgments
that merely share incidental words like "power" or "service". This module
adds a second, independent signal — legal authority — and combines it
additively with similarity rather than replacing it, deliberately on
different scales (similarity stays 0.0-1.0, authority terms are 0-100) so
authority genuinely dominates the final ranking by construction, not by
a tunable weight that could quietly drift back toward semantic-first.

Pure functions only: no network calls, no LLM calls, no database access —
every function takes plain dicts/strings and returns plain dicts/strings,
so this is fully unit-testable without a live Chroma/Postgres connection.
See tests/test_authority_ranker.py, including a synthetic reproduction of
the exact Police Amendment Bill scenario this module was written to fix.
"""

import re
from collections import Counter

from backend.legal_taxonomy import LegalSourceType
from backend.grounding import CASE_CITATION_PATTERN
from backend.config.legal_ranking import load_authority_weights, load_tie_break_order


# ── Authority weights ────────────────────────────────────────────────────────
# Ceiling values for "this type of source, squarely on point" — added to
# similarity and the match bonuses below, so a source's TYPE alone already
# does most of the ranking work; the bonuses only refine it further.
#
# Loaded from config/legal_ranking.yml, not hard-coded, so a policy owner
# can retune weights without touching Python — and validated at import
# time (i.e. app startup) against every LegalSourceType the classifier can
# emit, so a config/classifier drift fails loudly here rather than
# silently scoring an unmapped type as 0 inside compute_authority_score().
AUTHORITY_WEIGHTS = load_authority_weights()
TIE_BREAK_ORDER = load_tie_break_order()
_TIE_BREAK_RANK = {t.value: i for i, t in enumerate(TIE_BREAK_ORDER)}
_UNRANKED_TIE_BREAK = len(TIE_BREAK_ORDER)  # types absent from tie_break_order sort last among ties

# Tier membership drives PRIMARY / SECONDARY / COMMENTARY / BACKGROUND
# grouping and confidence classification below.
PRIMARY_TYPES = {
    LegalSourceType.CONSTITUTION, LegalSourceType.STATUTE,
    LegalSourceType.BILL, LegalSourceType.STATUTORY_INSTRUMENT,
}
SECONDARY_TYPES = {
    LegalSourceType.CONSTITUTIONAL_COURT, LegalSourceType.SUPREME_COURT,
    LegalSourceType.HIGH_COURT, LegalSourceType.LABOUR_COURT,
    LegalSourceType.MAGISTRATES_COURT, LegalSourceType.ADMIN_TRIBUNAL,
}
COMMENTARY_TYPES = {
    LegalSourceType.FIRM_PRECEDENT, LegalSourceType.OPINION,
    LegalSourceType.ACADEMIC, LegalSourceType.MEMORANDUM,
    LegalSourceType.TEMPLATE,
}
BACKGROUND_TYPES = {
    LegalSourceType.CORRESPONDENCE, LegalSourceType.PLEADING,
    LegalSourceType.GOVERNMENT_PUBLICATION, LegalSourceType.UNKNOWN,
}


def source_tier(source_type: LegalSourceType) -> str:
    if source_type in PRIMARY_TYPES:
        return "primary"
    if source_type in SECONDARY_TYPES:
        return "secondary"
    if source_type in COMMENTARY_TYPES:
        return "commentary"
    return "background"


# ── Stage 1: query understanding ─────────────────────────────────────────────

_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "is",
    "what", "does", "how", "under", "with", "about", "this", "that", "are",
}

_ACT_PATTERN = re.compile(
    r"\b([A-Z][A-Za-z&'\-]+(?:\s+[A-Z][A-Za-z&'\-]+)*\s+Act)\b(?:\s*\[Chapter\s*[\d:]+\])?"
)
_BILL_PATTERN = re.compile(
    r"\b([A-Z][A-Za-z&'\-]+(?:\s+[A-Za-z&'\-]+)*\s+(?:Amendment\s+)?Bill)\b"
)
_BILL_NUMBER_PATTERN = re.compile(r"\bH\.?\s*B\.?\s*(\d+)\s*,?\s*(\d{4})\b", re.IGNORECASE)
_SI_PATTERN = re.compile(
    r"\b(?:Statutory\s+Instrument|S\.?I\.?)\s*(\d+)\s*(?:of)?\s*(\d{4})?\b", re.IGNORECASE
)
_CONSTITUTION_SECTION_PATTERN = re.compile(
    r"\bsections?\s+(\d+)(?:\s*(?:[-–]|to)\s*(\d+))?\b", re.IGNORECASE
)

# Curated per-domain anchor terms. A general Act/Bill-name match already
# covers most domains on its own; this supplements it with the specific
# institutional vocabulary the brief calls out for policing, since
# "police"/"powers" alone is exactly the over-broad match causing the
# reported bug. Extend this dict as other domains show the same failure
# mode — it is a deliberately small, honest start, not a full ontology.
DOMAIN_ANCHOR_TERMS = {
    "police": [
        "police act", "police service", "commissioner-general",
        "commissioner general", "police discipline", "disciplinary",
        "internal appeal", "constitutional policing",
    ],
}


def extract_query_understanding(query: str) -> dict:
    """
    Deterministic extraction of legal entities from a query — Acts, Bills,
    Statutory Instruments, Constitution sections, and case citations —
    plus a small set of domain anchor terms used by the hard filter below.
    """
    acts = [m.group(1).strip() for m in _ACT_PATTERN.finditer(query)]
    bills = [m.group(1).strip() for m in _BILL_PATTERN.finditer(query)]
    bill_numbers = [f"H.B.{m.group(1)}, {m.group(2)}" for m in _BILL_NUMBER_PATTERN.finditer(query)]
    statutory_instruments = [
        f"S.I. {m.group(1)}" + (f" of {m.group(2)}" if m.group(2) else "")
        for m in _SI_PATTERN.finditer(query)
    ]
    constitution_sections = (
        [(m.group(1), m.group(2)) for m in _CONSTITUTION_SECTION_PATTERN.finditer(query)]
        if "constitution" in query.lower() else []
    )
    cases = [m.group(1).strip() for m in CASE_CITATION_PATTERN.finditer(query)]

    # A Bill implies its principal Act ("Police Amendment Bill" -> "Police
    # Act") — folded into act matching here, not kept as a separate,
    # unused concept, so a query that only names the Bill still recognises
    # and boosts the Act it amends.
    implied_principal_acts = []
    for bill in bills:
        base = re.sub(r"\s*Amendment\s*Bill\b", " Act", bill, flags=re.IGNORECASE).strip()
        if base and base.lower() != bill.lower():
            implied_principal_acts.append(base)

    # Subject noun: the word right before "Act"/"Bill" — "Police" out of
    # "Police Amendment Bill" — used both as an anchor term itself and as
    # a lookup key into DOMAIN_ANCHOR_TERMS.
    subject_terms = set()
    for name in acts + bills:
        words = [w for w in name.split() if w not in ("Act", "Bill", "Amendment")]
        if words:
            subject_terms.add(words[0].lower())

    anchor_terms = set()
    for name in acts + bills + implied_principal_acts:
        anchor_terms.add(name.lower())
    for subject in subject_terms:
        anchor_terms.add(subject)
        anchor_terms.update(DOMAIN_ANCHOR_TERMS.get(subject, []))

    significant_words = {
        w.strip(".,()").lower() for w in query.split()
        if w.strip(".,()").lower() not in _STOPWORDS and len(w.strip(".,()")) > 2
    }

    return {
        "acts": acts,
        "bills": bills,
        "bill_numbers": bill_numbers,
        "implied_principal_acts": implied_principal_acts,
        "statutory_instruments": statutory_instruments,
        "constitution_sections": constitution_sections,
        "cases": cases,
        "subject_terms": subject_terms,
        "anchor_terms": anchor_terms,
        "significant_words": significant_words,
        "has_specific_legal_reference": bool(acts or bills or cases or statutory_instruments),
    }


# ── Stage 4: authority scoring ───────────────────────────────────────────────

def _resolve_source_type(item: dict) -> LegalSourceType:
    raw = item.get("legal_source_type")
    if not raw:
        return LegalSourceType.UNKNOWN
    try:
        return LegalSourceType(raw)
    except ValueError:
        return LegalSourceType.UNKNOWN


def _item_text_blob(item: dict) -> str:
    parts = [
        item.get("filename") or "",
        item.get("case_name") or "",
        item.get("citation") or "",
        item.get("reference") or "",
        item.get("source_name") or "",
        (item.get("text") or "")[:300],
    ]
    return " ".join(parts).lower()


def compute_authority_score(item: dict, qu: dict) -> dict:
    """
    Returns the full score breakdown, not just a number — every retrieved
    item can show its reasoning this way, not just a bare percentage.
    """
    source_type_enum = _resolve_source_type(item)
    authority_weight = AUTHORITY_WEIGHTS.get(source_type_enum, 0)
    semantic_similarity = float(item.get("similarity", 0) or 0)
    blob = _item_text_blob(item)

    act_name_match = 0
    matched_act = None
    for act in qu.get("acts", []) + qu.get("bills", []) + qu.get("implied_principal_acts", []):
        if act.lower() in blob:
            act_name_match = 25
            matched_act = act
            break

    blob_words = {w.strip(".,()").lower() for w in blob.split() if len(w) > 2}
    query_title_match = 0
    matched_terms = []
    if qu.get("significant_words"):
        matched_terms = sorted(qu["significant_words"] & blob_words)
        if matched_terms:
            query_title_match = min(15, 5 * len(matched_terms))

    citation_overlap = 0
    matched_case = None
    for case in qu.get("cases", []):
        if case.lower() in blob:
            citation_overlap = 10
            matched_case = case
            break

    final_score = round(
        semantic_similarity + authority_weight
        + citation_overlap + query_title_match + act_name_match,
        3,
    )

    reasons = []
    if matched_act:
        reasons.append(f"References {matched_act}, named directly in the query.")
    if matched_case:
        reasons.append(f"Cites {matched_case}, referenced in the query.")
    if query_title_match and matched_terms:
        reasons.append("Matches query terms: " + ", ".join(matched_terms[:4]) + ".")
    if authority_weight >= 85:
        reasons.append("Binding authority.")
    elif authority_weight >= 45:
        reasons.append("Persuasive authority.")
    if not reasons:
        reasons.append("Background/contextual material — no direct authority match found for this query.")

    return {
        "legal_source_type": source_type_enum.value,
        "tier": source_tier(source_type_enum),
        "semantic_similarity": round(semantic_similarity, 3),
        # Named authority_score, not authority_weight, deliberately — ZLR
        # results already carry a pre-existing authority_weight STRING
        # field ("Binding"/"Persuasive", from get_authority_weight() in
        # main.py, already shown in a frontend badge). Reusing that name
        # here would silently clobber it when rerank() merges this score
        # dict onto the original item via {**item, **score}.
        "authority_score": authority_weight,
        "citation_overlap": citation_overlap,
        "query_title_match": query_title_match,
        "act_name_match": act_name_match,
        "final_score": final_score,
        "reasons": reasons,
    }


# ── Hard filters / relevance threshold ───────────────────────────────────────

# matter_type values (classify_document_sync's fixed vocabulary, main.py)
# that mark a firm document as belonging to a specific, narrow client
# matter rather than being general-purpose background material.
_NARROW_MATTER_TYPES = {
    "employment", "commercial_property", "commercial_contract",
    "company_law", "estate", "eviction", "matrimonial",
}

# Below this score an item is noise regardless of type — catches cases a
# type/domain check alone would miss (e.g. an item with no type info at
# all and zero query-term overlap).
RELEVANCE_FLOOR = 5


def passes_hard_filter(item: dict, qu: dict, score: dict) -> bool:
    """
    Excludes exactly the categories the reported bug surfaced — firm
    correspondence and narrow-matter pleadings/precedents/templates that
    only share incidental vocabulary with the query — UNLESS the item
    explicitly references one of the query's extracted legal entities.

    Primary and secondary legal authority (Acts, Bills, case law) is never
    hard-excluded here; an off-domain judgment is demoted by scoring
    instead (it keeps whatever similarity score it earned, but gets none
    of the authority/match bonuses, so it naturally sinks below on-point
    results rather than being deleted outright).
    """
    if score["final_score"] < RELEVANCE_FLOOR:
        return False

    if not qu.get("anchor_terms"):
        return True  # nothing specific in the query to filter against

    narrow_background_types = {
        LegalSourceType.CORRESPONDENCE.value, LegalSourceType.PLEADING.value,
        LegalSourceType.FIRM_PRECEDENT.value, LegalSourceType.TEMPLATE.value,
        LegalSourceType.MEMORANDUM.value,
    }
    is_narrow_background = score["legal_source_type"] in narrow_background_types
    matter_type = (item.get("matter_type") or "").lower()
    is_narrow_matter = matter_type in _NARROW_MATTER_TYPES

    if is_narrow_background and (is_narrow_matter or matter_type == ""):
        blob = _item_text_blob(item)
        if not any(term in blob for term in qu["anchor_terms"]):
            return False

    return True


# ── Confidence classification ────────────────────────────────────────────────

def classify_confidence(scored_items: list) -> str:
    """
    Never reports "no precedent found" when primary legislation is
    present — primary legislation is stronger evidence than precedent, so
    its presence alone is enough to report a positive finding.
    """
    tiers = Counter(item["tier"] for item in scored_items)
    if tiers["primary"] > 0:
        return "PRIMARY AUTHORITY FOUND"
    if tiers["secondary"] > 0:
        return "SECONDARY AUTHORITY FOUND"
    if tiers["commentary"] > 0:
        return "ONLY BACKGROUND MATERIAL FOUND"
    return "NO RELEVANT LEGAL AUTHORITY FOUND"


# ── Stage 5: cross-reference expansion ───────────────────────────────────────

def suggest_cross_references(qu: dict) -> list:
    """
    Deterministic search-term suggestions only — this function does not
    itself query the database, the caller runs additional lookups with
    whatever it returns. Hansard integration from the original brief is
    NOT implemented: no Hansard data source exists anywhere in this
    system, and fabricating one is out of scope for a retrieval-ranking
    change. What IS implementable without new data sources: deriving the
    principal Act a Bill amends by name pattern, and — for domains with a
    curated anchor set — suggesting the relevant Constitution sections.
    """
    suggestions = [
        {"type": "principal_act", "search_term": act}
        for act in qu.get("implied_principal_acts", [])
    ]
    if "police" in qu.get("subject_terms", set()):
        suggestions.append({
            "type": "constitution_sections",
            "search_term": "Constitution sections 207-223 (Police Service)",
        })
    return suggestions


# ── Orchestration ────────────────────────────────────────────────────────────

def rerank(results: list, query: str) -> dict:
    """
    Runs stages 1 (query understanding), 4 (authority scoring/reranking),
    and confidence classification over an already-retrieved candidate set.
    Stage 3 (vector search) already happened before this is called; stage
    2 (metadata filter) and stage 5 (cross-reference expansion) are
    intentionally kept as separate steps the caller composes around this
    (see search_documents() in main.py and suggest_cross_references()
    above) rather than folded in here, since they change what gets
    *fetched*, not how what's already fetched gets *ranked*.
    """
    qu = extract_query_understanding(query)

    scored = []
    excluded = 0
    for item in results:
        score = compute_authority_score(item, qu)
        if not passes_hard_filter(item, qu, score):
            excluded += 1
            continue
        scored.append({**item, **score})

    # Primary: final_score descending. Secondary (exact-score ties only):
    # config/legal_ranking.yml's tie_break_order, earliest = wins.
    scored.sort(key=lambda r: (
        -r["final_score"],
        _TIE_BREAK_RANK.get(r["legal_source_type"], _UNRANKED_TIE_BREAK),
    ))

    groups = {"primary": [], "secondary": [], "commentary": [], "background": []}
    for item in scored:
        groups[item["tier"]].append(item)

    return {
        "query_understanding": qu,
        "results": scored,
        "source_groups": groups,
        "confidence": classify_confidence(scored),
        "cross_references": suggest_cross_references(qu),
        "excluded_count": excluded,
    }
