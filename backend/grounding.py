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
import os
import re
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

# Separate Anthropic client instance — grounding.py is imported by main.py,
# so importing main's `client` back here would be circular.
ai_client = anthropic.Anthropic()

# Same reasoning as ai_client above, and same default as main.py's own
# FIRM_NAME — re-reads the env var directly rather than importing it, since
# importing from main.py here would be circular.
FIRM_NAME = os.environ.get("MUTEMO_FIRM_NAME", "Sawyer & Mkushi Legal Practitioners")

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

ADVERSARIAL_ANALYSIS_RULES = """
- For any conclusion that is genuinely contestable (i.e., a reasonable opposing counsel could argue the other way — not for settled procedural facts or uncontroversial statutory readings), after stating your position, add a new paragraph starting with the exact line "Counterargument:" followed by the strongest reasonable opposing reading or argument, genuinely steelmanned — not a weak strawman. Then, in a separate following paragraph, start with the exact line "Assessment:" followed by your own reasoned view on which position is stronger and why.
- Do NOT apply this to every point — only to conclusions where genuine legal disagreement is plausible. A rough guide: 1-3 genuinely contestable points per analysis, not every clause or every sentence. Overuse dilutes its value."""

# Superseded by IRAC_STRUCTURE_RULES below, which absorbs both the Bill
# comparison table and adversarial-analysis behaviour into one structure.
# Left defined (unused) rather than deleted in case either is needed again
# independently of the full IRAC format.
IRAC_STRUCTURE_RULES = """
- Structure your analysis around each distinct legal issue raised by the query, in this exact sequence for every issue, using these exact section markers on their own line:

Issue:
[one-sentence statement of the specific legal question]

Relevant provisions:
[the specific statutory/constitutional provisions bearing on this issue. If this concerns a Bill or amendment, present this as a markdown table: | Current Provision | Proposed Change | with only these two columns containing direct quotes/citations of retrieved text.]

Retrieved authorities:
[case law or other authority bearing on this issue, cited per the existing citation rules]

Analysis:
[your reasoned application of the provisions and authorities to the issue]

Counterarguments:
[Only if this issue is genuinely contestable — a reasonable opposing counsel could argue the other way. Within this section: "Counterargument: [steelmanned opposing position]" followed by "Assessment: [your reasoned view on which position is stronger]." If the issue is not genuinely contestable, write "Counterarguments: Not applicable — this point is not genuinely contestable." rather than inventing a weak opposing argument.]

Practical risk:
[the practical consequence or risk for the client if this issue is resolved unfavorably]

Confidence:
[state exactly one of: High, Moderate, or Low, followed by a one-sentence reason]

- Do not omit any section for a given issue, but keep sections concise — this structure is meant to make the analysis scannable, not to pad length.
- Confine each issue's markers to that issue before starting the next issue's "Issue:" marker."""


def enforce_confidence_consistency(answer_text: str) -> tuple:
    """
    Deterministic backstop for the self-reported Confidence: field in each
    IRAC issue block. A model stating "Confidence: High" while its own
    Analysis/Retrieved authorities text for that same issue contains a
    "Requires verification:" marker is self-contradictory — this
    downgrades the stated confidence rather than trusting the self-report
    uncritically, consistent with apply_confidence_safeguard's design.
    """
    issue_blocks = re.split(r'(?=\nIssue:)', answer_text)
    qc_log = []
    result_blocks = []

    for block in issue_blocks:
        confidence_match = re.search(r'Confidence:\s*(High|Moderate|Low)', block)
        has_verification_gap = 'Requires verification:' in block

        if confidence_match and confidence_match.group(1) == 'High' and has_verification_gap:
            downgraded = block.replace(
                confidence_match.group(0),
                'Confidence: Moderate [automatically downgraded from High — this issue contains an unresolved "Requires verification" point]'
            )
            qc_log.append({
                "qc_status": "confidence_downgraded",
                "qc_reason": "Self-reported High confidence downgraded to Moderate because this issue's own analysis contains a verification gap.",
            })
            result_blocks.append(downgraded)
        else:
            result_blocks.append(block)

    return ''.join(result_blocks), qc_log


def compute_grounding(results: list, legal_results: list, zlr_results: list, has_attached_doc: bool = False) -> dict:
    """
    Determine whether an AI answer is actually grounded in retrieved firm/
    legal/case-law sources, or is unsupported general reasoning — and say so
    explicitly. Keys off each result's pre-computed authority_strength
    ('binding'/'persuasive'/'contextual', written once at ingest by
    authority_strength_for() in backend/legal_taxonomy.py) rather than
    re-deriving authority from a hand-maintained source_type list that can
    drift out of sync with the real taxonomy.
    """
    all_hits = results + legal_results + zlr_results
    authority_hits = [r for r in all_hits if r.get('authority_strength') in ('binding', 'persuasive')]
    context_hits = [r for r in all_hits if r.get('authority_strength') == 'contextual']

    max_score = max([r.get('similarity', 0) for r in authority_hits]) if authority_hits else 0
    sources_sufficient = bool(authority_hits) and max_score >= AUTHORITY_FLOOR

    if not authority_hits and not context_hits and not has_attached_doc:
        note = "No binding or contextual legal sources found. Reliance is on general principles only."
    elif not authority_hits:
        note = f"No binding or persuasive authority found. Supported only by {len(context_hits)} contextual source(s) — verify independently before relying on this."
    elif not sources_sufficient:
        note = f"Found {len(authority_hits)} authoritative source(s), but below the confidence threshold for binding reliance (best match {max_score:.0%})."
    else:
        note = f"✓ Grounded in {len(authority_hits)} authoritative source(s)."
        if context_hits:
            note += f" Supported by {len(context_hits)} contextual item(s)."

    return {
        "sources_sufficient": sources_sufficient,
        "grounding_note": note,
        "max_similarity_score": max_score,
        "source_tier_breakdown": {"authority": len(authority_hits), "context": len(context_hits)},
    }


def get_relevance_tier(similarity: float) -> str:
    """Coarse, human-facing label for a raw similarity score, consistent with AUTHORITY_FLOOR."""
    if similarity >= 0.8:
        return "Strong Match"
    if similarity >= AUTHORITY_FLOOR:
        return "Relevant"
    if similarity >= 0.4:
        return "Possibly Relevant"
    return "Weak Match"


# Case number near a CASE/JUDGMENT/REF keyword, e.g. "Case No. HC 6204/26".
_CASE_NUMBER_KEYWORD_PATTERN = re.compile(
    r"\b(?:CASE|JUDGMENT|REF)(?:\s+NO\.?)?\s*[:#]?\s*([A-Z]{1,4}[\s-]?\d{2,5}/\d{2,4})\b",
    re.IGNORECASE,
)
# Bare case-number token with no keyword — real headers routinely read
# "IN THE HIGH COURT OF ZIMBABWE HARARE HC 6204/26" with the number sitting
# right after the court name, nothing labelling it as a case number at all.
_CASE_NUMBER_BARE_PATTERN = re.compile(r"\b[A-Z]{1,4}[\s-]?\d{2,5}/\d{2,4}\b")
_COURT_HEADER_PATTERN = re.compile(r"\bIN THE .*?COURT\b", re.IGNORECASE)


def extract_identity_from_text(text: str) -> Optional[str]:
    """
    Best-effort fallback identity for a document with no case_name/
    reference/filename, used by group_results_by_document() below. Tries,
    in order: a case number next to a CASE/JUDGMENT/REF keyword; a bare
    case-number token immediately following a court-header line (the
    common real-world pattern the keyword search alone misses); then,
    if the text is on this firm's own letterhead, a generic identity
    naming the firm rather than misreading the letterhead text as a case
    name. Returns None if nothing usable is found.
    """
    if not text:
        return None
    header = text[:500]  # case captions and letterheads sit at the top of a document

    keyword_match = _CASE_NUMBER_KEYWORD_PATTERN.search(header)
    if keyword_match:
        return keyword_match.group(1).upper().replace(" ", "")

    court_match = _COURT_HEADER_PATTERN.search(header)
    if court_match:
        remainder = header[court_match.end():court_match.end() + 200]
        bare_match = _CASE_NUMBER_BARE_PATTERN.search(remainder)
        if bare_match:
            return bare_match.group(0).upper().replace(" ", "")

    if FIRM_NAME and FIRM_NAME.upper() in header.upper():
        return f"{FIRM_NAME} — Correspondence"

    return None


def group_results_by_document(all_results: list) -> list:
    """
    Collapses chunk-level results into one entry per underlying document —
    a case, Act, or firm document may otherwise appear as several separate
    "documents" in a result list purely because it was retrieved via
    multiple chunks (or, for the same case reaching the corpus through two
    ingestion paths — e.g. Vault upload and Legal Updates — under two
    different document_ids entirely, which dedup_key's normalized-name/
    source_url fallback collapses back into one).
    """
    grouped = {}
    for r in all_results:
        raw_name = (r.get("case_name") or r.get("reference") or r.get("filename") or "").strip().lower()
        normalized_name = re.sub(r'[^a-z0-9]+', '', raw_name)
        dedup_key = r.get("source_url") or r.get("zimlii_url") or normalized_name or str(r.get("document_id"))

        if dedup_key not in grouped:
            display_name = r.get("case_name") or r.get("reference") or r.get("filename")
            if not display_name or display_name == "Unknown":
                display_name = extract_identity_from_text(r["text"]) or "Unnamed Document"
            if display_name.lower().startswith("section ") and r.get("source_name"):
                display_name = f"{r['source_name']} ({display_name})"

            grouped[dedup_key] = {
                "document_id": r.get("document_id"),
                "display_name": display_name,
                "type_label": (r.get("legal_source_type") or r.get("result_source") or "document").replace("_", " ").title(),
                "max_similarity": r.get("similarity", 0),
                "authority_strength": r.get("authority_strength", "contextual"),
                "is_authority": r.get("authority_strength") in ("binding", "persuasive"),
                "excerpts": [],
            }

        grouped[dedup_key]["excerpts"].append({"text": r["text"], "similarity": r.get("similarity", 0), "page": r.get("page_number")})
        grouped[dedup_key]["max_similarity"] = max(grouped[dedup_key]["max_similarity"], r.get("similarity", 0))

    final_docs = []
    for data in grouped.values():
        data["relevance_tier"] = get_relevance_tier(data["max_similarity"])
        final_docs.append(data)
    return sorted(final_docs, key=lambda x: x["max_similarity"], reverse=True)


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
        legal_source_type = r.get("legal_source_type")
        if legal_source_type is None:
            return "Firm Precedent"  # not yet backfilled — preserves prior behaviour
        if legal_source_type == "unknown":
            return "Unknown Document"
        return legal_source_type.replace("_", " ").title()

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


CASE_CITATION_PATTERN = re.compile(
    r"([A-Z][A-Za-z\.\'\-]+(?:\s+[A-Z][A-Za-z\.\'\-]+)*\s+v\.?\s+"
    r"[A-Z][A-Za-z\.\'\-]+(?:\s+[A-Za-z\.\'\-&]+)*)\s*\(([^)]{2,40})\)"
)

def verify_inline_case_citations(
    answer_text: str,
    retrieved_context: str,
    annotation_suffix: str = "[⚠ UNVERIFIED — not found in retrieved sources, confirm independently before relying on it]",
) -> tuple:
    """
    Deterministic QC pass, complementary to verify_citations(): scans the
    ENTIRE answer text for "X v Y (citation)" patterns anywhere in the
    prose — not just inside blockquotes — since a case name can be dropped
    into ordinary narrative without ever being directly quoted. Confirms
    each matched case name appears somewhere in the exact retrieved context
    sent to synthesis; unverified matches get an inline warning annotation
    rather than being silently left to read as confirmed.

    annotation_suffix lets a caller soften the wording — e.g. drafting,
    where the corpus is far from comprehensive, uses "not found in
    retrieved sources — verify independently before filing" rather than
    the research path's "UNVERIFIED" language, since absence from a
    limited corpus isn't proof of a fabricated citation, just an
    unconfirmed one. The default preserves the original research-path
    wording exactly, so existing callers are unaffected.
    """
    normalized_context = ' '.join(retrieved_context.split()).lower()
    qc_log = []
    result_text = answer_text
    for match in CASE_CITATION_PATTERN.finditer(answer_text):
        case_name = match.group(1).strip()
        citation = match.group(2).strip()
        full_match = match.group(0)
        normalized_case_name = ' '.join(case_name.split()).lower()
        if normalized_case_name not in normalized_context:
            qc_log.append({
                "qc_status": "inline_citation_unverified",
                "case_name": case_name,
                "citation": citation,
                "qc_reason": "This case name/citation was not found in the retrieved context provided to the model — it may be drawn from general knowledge rather than the Vault, and has not been verified.",
            })
            annotation = f'{full_match} {annotation_suffix}'
            result_text = result_text.replace(full_match, annotation, 1)
    return result_text, qc_log


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
