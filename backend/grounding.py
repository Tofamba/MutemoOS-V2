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

import json
import logging
import re

import anthropic

logger = logging.getLogger(__name__)

# Separate Anthropic client instance — grounding.py is imported by main.py,
# so importing main's `client` back here would be circular.
ai_client = anthropic.Anthropic()

AUTHORITY_FLOOR = 0.6

# legal_results source_types that count as background context, not legal authority.
CONTEXT_SOURCE_TYPES = {"news", "press_statement", "zlhr"}

BANNED_ASSERTIVE_TERMS = ["strong", "clear", "fatal", "void", "certain", "direct authority"]

TEXTURE_RULES = """
  - Any direct quote or citation from a FIRM PRECEDENT, ZLR CASE LAW, or LEGAL UPDATE source must be presented as a markdown blockquote (> ...) with the source reference bolded
  - Any use of BACKGROUND CONTEXT (news, press statements) must be prefixed with "Background Context from [Source]:" and italicized — never given the same weight as an authoritative citation
  - If you draw an analogy rather than citing something directly on point, prefix that reasoning with "By Analogy:" in italics
  - When quoting a source directly in a blockquote (> ...), the blockquote must contain ONLY the exact verbatim text from the source — nothing else. Any framing phrase such as "Section 8(4) states:" or "Section 13(2) provides that..." must be written as ordinary prose BEFORE the blockquote, never inside it.
    Correct:
    Section 8(4) states:

    > discussions shall be held on the contents of the convening notice...

    Incorrect:
    > Section 8(4) states: "discussions shall be held...\""""

FACT_EXTRACTION_RULES = """
- If a specific number, timeframe, deadline, percentage, or figure needed to answer the question is not present verbatim in the retrieved excerpts, you must say so explicitly (e.g. "the retrieved excerpts do not contain the specific notice period under section X — the full statutory text should be retrieved before relying on this deadline"). You must NEVER substitute a plausible-sounding but unsourced figure, and you must NEVER use phrases like "standard practice," "typically," "generally requires," or "comparable legislation" to imply a specific number without a direct citation to a retrieved source stating that exact number.
- If the query references a specific date, and a directly-cited retrieved source establishes an exact time-based rule (e.g. "notice required at least N days before"), calculate the resulting deadline from that rule only, showing the arithmetic step by step: event date, required period, resulting deadline date, and days remaining from today's date. If no such directly-cited rule was retrieved, do not calculate or state any deadline at all — say plainly that the exact deadline cannot be determined from the retrieved sources."""

LAWYER_JUDGMENT_RULES = """
- Where a conclusion depends on litigation strategy, tactical or forum choices, timing/urgency judgment calls, settlement or appeal decisions, or facts that cannot be resolved from the retrieved authorities, you MUST start a new paragraph (a blank line before it) that begins with the exact line "Lawyer judgment required:" on its own, followed by a clear statement of what depends on counsel's professional assessment rather than on the retrieved law alone. Never embed this marker mid-paragraph — it must always open its own paragraph, exactly like the "Requires verification:" and "By analogy:" markers."""

STATUTORY_MECHANISM_PRECISION = """
- When discussing statutory powers that create legal consequences in stages (e.g. a power that first makes conduct unlawful, followed by a separate power that acts on that unlawfulness), always name the SPECIFIC mechanism precisely rather than using a general term for multiple distinct provisions. For example, under MOPA: distinguish a "condition" imposed under s.8(6) from a "prohibition notice," "direction," or "order" under s.8 — these have different legal consequences — and distinguish either of those (which may render a gathering unlawful) from the SEPARATE question of whether s.13 dispersal/force is then permitted. Never use a general word like "banned" or "prohibited" to refer to more than one of these distinct mechanisms in the same answer without specifying which one applies."""

BILL_COMPARISON_RULES = """
- If this query concerns a Bill, proposed amendment, or draft legislation, begin your analysis of each substantive change with a compact summary in this exact format before the narrative discussion:

  | Current Provision | Proposed Change | Effect | Constitutional/Legal Issue |
  |---|---|---|---|
  | [what the law currently says/does — cite directly if quoting] | [what the Bill changes it to] | [practical consequence] | [any constitutional or legal concern, or "None identified" if none] |

  Only the first two columns ("Current Provision" and "Proposed Change") should ever contain direct quotations or paraphrases of retrieved text — cite these normally per the existing citation rules. The "Effect" and "Constitutional/Legal Issue" columns are your own analysis and inference; if either column states an inference rather than a directly grounded fact, prefix that cell's content with "Analysis:" so it's visually distinguishable from the quoted/grounded columns.
  After the table, provide the fuller narrative discussion as usual."""


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
        context_parts.append(f"[FIRM PRECEDENT — {r.get('filename', 'Unknown Document')}]\n{r['text']}")
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


def verify_citations(answer_text: str, retrieved_context: str) -> tuple:
    """
    Deterministic QC pass. Verifies blockquoted text (the existing
    TEXTURE_RULES convention for DIRECTLY GROUNDED quotations) against
    the EXACT retrieved context sent to synthesis — never the whole
    database — so a "verified" result specifically means "the model
    could have seen this," not "this text exists somewhere."

    A failed match does NOT mean the quote was fabricated — it only
    means the quote could not be automatically confirmed against the
    exact context provided. The original quote is preserved alongside
    the downgrade note so a lawyer can inspect both.
    """
    paragraphs = re.split(r'\n\s*\n', answer_text)
    normalized_context = ' '.join(retrieved_context.split())
    qc_log = []
    new_paragraphs = []

    for para in paragraphs:
        stripped = para.strip()
        if stripped.startswith('>'):
            quote_text = ' '.join(
                line.lstrip('>').strip() for line in stripped.split('\n')
            ).strip()
            normalized_quote = ' '.join(quote_text.split())

            if normalized_quote and normalized_quote in normalized_context:
                new_paragraphs.append(para)
            else:
                match_index = normalized_context.find(normalized_quote)

                if match_index == -1:
                    quote_tokens = normalized_quote.split()
                    first_ten = quote_tokens[:10]
                    context_tokens_set = set(normalized_context.split())
                    token_matches = [(t, t in context_tokens_set) for t in first_ten]

                    logger.warning(
                        "[citation_qc] MISMATCH\n"
                        "QUOTE_LENGTH: %s\n"
                        "CONTEXT_LENGTH: %s\n"
                        "FIRST_10_TOKENS_AND_MATCH: %r\n"
                        "FULL_QUOTE: %r",
                        len(normalized_quote),
                        len(normalized_context),
                        token_matches,
                        normalized_quote,
                    )
                qc_log.append({
                    "original_label": "DIRECTLY_GROUNDED",
                    "qc_status": "citation_unmatched",
                    "qc_reason": "Quoted text could not be automatically matched to the retrieved context provided to the model.",
                    "quote_excerpt": normalized_quote[:200],
                })
                new_paragraphs.append(
                    f'Requires verification: The following statement was presented as a direct quotation, but could not be '
                    f'automatically matched to the retrieved context provided to the model — verify against the original '
                    f'source before relying on it.\n\nQuoted text under review: "{quote_text}"'
                )
        else:
            new_paragraphs.append(para)

    return '\n\n'.join(new_paragraphs), qc_log


def run_legal_research_agent(query: str, context: str) -> dict:
    """
    Haiku-based structured gap-analysis pass. NOT a second legal opinion —
    a research completeness map. Only invoked when compute_grounding
    already found the retrieval insufficient.
    """
    prompt = f"""You are a legal research completeness analyst. You do not answer the client's question or give legal advice.
Analyze ONLY whether the retrieved sources below are sufficient to answer the query, and identify specific gaps.

Do not infer missing statutory text from general legal knowledge, similar legislation, or the query itself. Only report what the retrieved material does or does not establish — never fill a gap with a value you believe is likely correct.

Query: {query}

Retrieved sources:
{context}

Respond ONLY with valid JSON, no other text, in this exact structure:
{{
  "research_sufficient": true or false,
  "gaps": [
    {{
      "issue": "short description of the legal issue",
      "missing_authority": "what specific provision/text is missing — describe the GAP, never state what you believe the missing value is",
      "reason": "why the retrieved excerpts don't resolve this",
      "priority": "high" | "medium" | "low"
    }}
  ]
}}"""
    try:
        resp = ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())
        return json.loads(raw)
    except Exception as e:
        logger.error(f"[research_agent] failed: {e}")
        return {"research_sufficient": None, "gaps": [], "error": str(e)}
