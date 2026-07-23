"""
Source-quality tiering for semantic search grounding.

Splits retrieved sources into an "authority" tier (firm precedents, ZLR case
law, and legal-update sources that carry legal weight — legislation, gazette
notices, court rules) and a "context" tier (news, press statements, ZLHR
commentary — useful background, but not something a lawyer can cite as
binding authority). Grounding and prompt formatting both respect that split
so a confident-sounding answer built only on background context is never
indistinguishable from one backed by real authority.
"""

AUTHORITY_FLOOR = 0.6

# legal_results source_types that count as background context, not legal authority.
CONTEXT_SOURCE_TYPES = {"news", "press_statement", "zlhr"}

BANNED_ASSERTIVE_TERMS = ["strong", "clear", "fatal", "void", "certain", "direct authority"]

TEXTURE_RULES = """
  - Any direct quote or citation from a FIRM PRECEDENT, ZLR CASE LAW, or LEGAL UPDATE source must be presented as a markdown blockquote (> ...) with the source reference bolded
  - Any use of BACKGROUND CONTEXT (news, press statements) must be prefixed with "Background Context from [Source]:" and italicized — never given the same weight as an authoritative citation
  - If you draw an analogy rather than citing something directly on point, prefix that reasoning with "By Analogy:" in italics"""


def compute_grounding(results: list, legal_results: list, zlr_results: list,
                       has_attached_doc: bool = False) -> dict:
    """
    Determine whether an AI answer is actually grounded in retrieved firm/
    legal/case-law sources, or is unsupported general reasoning — and say so
    explicitly. This was previously dead code: the frontend has had a
    warning UI for this since it was built, but no backend endpoint ever
    populated sources_sufficient/grounding_note/source_gap, so a
    zero-source answer looked identical to a well-grounded one. For a legal
    tool, that's a real risk — confident-sounding output with nothing behind
    it needs to be unmistakable, not indistinguishable from a verified one.
    """
    authority_items = list(results or []) + list(zlr_results or [])
    context_items = []
    for r in (legal_results or []):
        if r.get("source_type") in CONTEXT_SOURCE_TYPES:
            context_items.append(r)
        else:
            authority_items.append(r)

    total = len(authority_items) + len(context_items)
    max_similarity_score = max(
        (r.get("similarity", 0) for r in authority_items + context_items), default=0
    )

    if total == 0:
        if has_attached_doc:
            note = ("No firm precedents or case law were found to cross-reference this "
                    "document. This analysis is based on the document's own content and "
                    "general legal principles only — verify against ZimLII, applicable "
                    "legislation, and firm records before relying on it.")
        else:
            note = ("No firm precedents, legal updates, or case law matched this query. "
                    "This analysis reflects general legal knowledge only — verify against "
                    "ZimLII, applicable legislation, and firm records before relying on it.")
        return {
            "sources_sufficient": False,
            "grounding_note": note,
            "source_gap": "No matching sources in the vault",
            "source_tier_breakdown": {"authority": 0, "context": 0},
            "max_similarity_score": 0,
        }

    sources_sufficient = any(r.get("similarity", 0) >= AUTHORITY_FLOOR for r in authority_items)

    if sources_sufficient:
        grounding_note = f"Grounded in {total} retrieved source(s) from the vault."
        source_gap = None
    else:
        grounding_note = ("Retrieved sources did not meet the confidence threshold for binding "
                           "legal authority. This analysis relies on background context and "
                           "general legal principles only — verify against ZimLII, applicable "
                           "legislation, and firm records before relying on it.")
        source_gap = "No authority-tier source met the similarity threshold"

    return {
        "sources_sufficient": sources_sufficient,
        "grounding_note": grounding_note,
        "source_gap": source_gap,
        "source_tier_breakdown": {"authority": len(authority_items), "context": len(context_items)},
        "max_similarity_score": max_similarity_score,
    }


def format_context(results: list, legal_results: list, zlr_results: list) -> str:
    """Build the source context block injected into the synthesis prompt."""
    context_parts = []
    for r in (results or [])[:5]:
        context_parts.append(f"[FIRM PRECEDENT — {r.get('document_id','')}]\n{r['text']}")
    for r in (legal_results or [])[:3]:
        ref = r.get("reference") or r.get("source_name") or "Legal Source"
        if r.get("source_type") in CONTEXT_SOURCE_TYPES:
            context_parts.append(f"[BACKGROUND CONTEXT — {ref} ({r.get('source_type')})]\n{r['text']}")
        else:
            context_parts.append(f"[{ref}]\n{r['text']}")
    for r in (zlr_results or [])[:3]:
        ref = r.get("filename") or r.get("citation") or "ZLR Case Law"
        context_parts.append(f"[ZLR CASE LAW — {ref}]\n{r['text']}")
    return "\n\n---\n\n".join(context_parts)


def display_label(r: dict) -> str:
    """Human-facing label for a retrieved source."""
    result_source = r.get("result_source")

    if result_source == "firm":
        return "Firm Precedent"

    if result_source == "zlr":
        return "Zimbabwe Case Law"

    if result_source == "legal":
        source_type = r.get("source_type")

        if source_type == "legislation":
            return "Constitution / Legislation"
        if source_type == "news":
            return "Current News"
        if source_type == "press_statement":
            return "Legal Feed — Press Statement"

        return "Legal Feed"

    return "Unknown Source"


def apply_confidence_safeguard(answer_text: str, grounding: dict) -> str:
    """Prepend a hard warning when an under-grounded answer still reads as assertive."""
    if not answer_text or grounding.get("sources_sufficient", True):
        return answer_text
    snippet = answer_text[:500].lower()
    if any(term.lower() in snippet for term in BANNED_ASSERTIVE_TERMS):
        warning = (
            "**⚠ WARNING: ANALOGOUS ANALYSIS ONLY.** No binding Zimbabwean authority was "
            "found above the confidence threshold. This response relies on general "
            "principles and non-binding background context — verify all citations "
            "independently."
        )
        return f"{warning}\n\n{answer_text}"
    return answer_text
