"""
Legal source classification and authority-strength scoring.

Deterministic, not AI-driven — classification runs cheaply at ingest time
(and in a one-off backfill for existing rows) rather than per-query, since
a document's legal type and authority don't change between searches.
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class LegalSourceType(str, Enum):
    CONSTITUTION = "constitution"
    STATUTE = "statute"
    BILL = "bill"
    STATUTORY_INSTRUMENT = "statutory_instrument"
    CONSTITUTIONAL_COURT = "constitutional_court_judgment"
    SUPREME_COURT = "supreme_court_judgment"
    HIGH_COURT = "high_court_judgment"
    MAGISTRATES_COURT = "magistrates_court"
    LABOUR_COURT = "labour_court"
    ADMIN_TRIBUNAL = "administrative_tribunal"
    FIRM_PRECEDENT = "firm_precedent"
    OPINION = "opinion"
    PLEADING = "pleading"
    TEMPLATE = "template"
    MEMORANDUM = "memorandum"
    CORRESPONDENCE = "correspondence"
    ACADEMIC = "academic_source"
    GOVERNMENT_PUBLICATION = "government_publication"
    UNKNOWN = "unknown"


class AuthorityStrength(str, Enum):
    BINDING = "binding"
    PERSUASIVE = "persuasive"
    CONTEXTUAL = "contextual"


AUTHORITY_STRENGTH_MAP = {
    LegalSourceType.CONSTITUTION: AuthorityStrength.BINDING,
    LegalSourceType.STATUTE: AuthorityStrength.BINDING,
    LegalSourceType.STATUTORY_INSTRUMENT: AuthorityStrength.BINDING,
    LegalSourceType.CONSTITUTIONAL_COURT: AuthorityStrength.BINDING,
    LegalSourceType.SUPREME_COURT: AuthorityStrength.BINDING,
    LegalSourceType.HIGH_COURT: AuthorityStrength.PERSUASIVE,
    LegalSourceType.LABOUR_COURT: AuthorityStrength.PERSUASIVE,
    LegalSourceType.MAGISTRATES_COURT: AuthorityStrength.PERSUASIVE,
    LegalSourceType.ADMIN_TRIBUNAL: AuthorityStrength.PERSUASIVE,
    LegalSourceType.BILL: AuthorityStrength.CONTEXTUAL,
    LegalSourceType.FIRM_PRECEDENT: AuthorityStrength.CONTEXTUAL,
    LegalSourceType.OPINION: AuthorityStrength.CONTEXTUAL,
    LegalSourceType.PLEADING: AuthorityStrength.CONTEXTUAL,
    LegalSourceType.TEMPLATE: AuthorityStrength.CONTEXTUAL,
    LegalSourceType.MEMORANDUM: AuthorityStrength.CONTEXTUAL,
    LegalSourceType.CORRESPONDENCE: AuthorityStrength.CONTEXTUAL,
    LegalSourceType.ACADEMIC: AuthorityStrength.CONTEXTUAL,
    LegalSourceType.GOVERNMENT_PUBLICATION: AuthorityStrength.CONTEXTUAL,
    LegalSourceType.UNKNOWN: AuthorityStrength.CONTEXTUAL,
}


def authority_strength_for(source_type) -> AuthorityStrength:
    if isinstance(source_type, str):
        try:
            source_type = LegalSourceType(source_type)
        except ValueError:
            return AuthorityStrength.CONTEXTUAL
    return AUTHORITY_STRENGTH_MAP.get(source_type, AuthorityStrength.CONTEXTUAL)


# Maps classify_document_sync()'s actual document_type vocabulary (the AI
# classifier's fixed option list, backend/main.py) to a LegalSourceType.
# `court`, where set on a firm document, names which court the item was
# filed in/relates to — it does NOT mean the document IS that court's
# judgment, so it is deliberately not used to override this mapping.
FIRM_DOC_TYPE_MAP = {
    "affidavit": LegalSourceType.PLEADING,
    "founding_affidavit": LegalSourceType.PLEADING,
    "opposing_affidavit": LegalSourceType.PLEADING,
    "replying_affidavit": LegalSourceType.PLEADING,
    "heads_of_argument": LegalSourceType.PLEADING,
    "court_order": LegalSourceType.PLEADING,
    "summons": LegalSourceType.PLEADING,
    "declaration": LegalSourceType.PLEADING,
    "plea": LegalSourceType.PLEADING,
    "notice_of_motion": LegalSourceType.PLEADING,
    "lease_agreement": LegalSourceType.FIRM_PRECEDENT,
    "deed_of_settlement": LegalSourceType.FIRM_PRECEDENT,
    "power_of_attorney": LegalSourceType.FIRM_PRECEDENT,
    "will_and_testament": LegalSourceType.FIRM_PRECEDENT,
    "contract": LegalSourceType.FIRM_PRECEDENT,
    "correspondence": LegalSourceType.CORRESPONDENCE,
    "opinion": LegalSourceType.OPINION,
    "other": LegalSourceType.UNKNOWN,
}


def classify_firm_document(document_type) -> LegalSourceType:
    """
    Only genuinely precedent-shaped documents (lease agreements, contracts,
    settlement deeds, wills) keep the "Firm Precedent" label; litigation
    documents, correspondence, and opinions each get their own real type
    instead of being flattened into "Firm Precedent".
    """
    if not document_type:
        return LegalSourceType.UNKNOWN
    return FIRM_DOC_TYPE_MAP.get(document_type, LegalSourceType.UNKNOWN)


# legal_updates.source_type values, as written by upload_legal_update()
# (main.py) — "legislation" | "news" | "press_statement" | "guidance" |
# "zlhr" | others.
# Zimbabwe neutral citations ("2025 ZWSC 17", "2026 ZWHHC 127", ...) name the
# court far more reliably than a court field parsed out of an uploaded
# docx/pdf: a production audit (2026-09-20) found the parsed court wrong on
# 9 of the 24 uploaded judgments that carry a neutral citation in the
# filename (e.g. a 2025 ZWSC judgment whose court field read "Labour Court"),
# while the ZLR headnote series' court field agreed with its judgment-number
# prefix (HH-/HB-/CC-) on all 316 rows checked. So the citation wins when
# present.
_NEUTRAL_CITATION = re.compile(r"\bZW(CC|SC|HHC|HC|BHC|MDHC|MTHC|MHC|MVHC|MSHC|LC)\b", re.IGNORECASE)
_NEUTRAL_CITATION_COURTS = {
    "cc": LegalSourceType.CONSTITUTIONAL_COURT,
    "sc": LegalSourceType.SUPREME_COURT,
    "lc": LegalSourceType.LABOUR_COURT,
    "hhc": LegalSourceType.HIGH_COURT, "hc": LegalSourceType.HIGH_COURT, "bhc": LegalSourceType.HIGH_COURT,
    "mdhc": LegalSourceType.HIGH_COURT, "mthc": LegalSourceType.HIGH_COURT, "mhc": LegalSourceType.HIGH_COURT,
    "mvhc": LegalSourceType.HIGH_COURT, "mshc": LegalSourceType.HIGH_COURT,
}


def court_from_neutral_citation(*texts) -> Optional[LegalSourceType]:
    """The court a neutral citation token (e.g. ZWSC) in any of `texts`
    names, or None if none of them carries one."""
    for text in texts:
        match = _NEUTRAL_CITATION.search(text or "")
        if match:
            return _NEUTRAL_CITATION_COURTS[match.group(1).lower()]
    return None


def classify_legal_update(source_type, reference=None, filename=None) -> LegalSourceType:
    # Ingestion path: the document's text isn't available yet, so this can
    # only use metadata. Deliberately has NO "statutory_instrument" branch:
    # without seeing content it would make every future scraper bot-block
    # stub typed that way "binding" (all 53 statutory_instrument rows in
    # production are ZimLII "security service / malicious bots" stub pages).
    # Real SIs are classified by the content-aware backfill path below.
    if source_type == "case_law":
        return court_from_neutral_citation(filename, reference) or LegalSourceType.UNKNOWN
    if source_type == "legislation":
        ref = (reference or "").lower()
        if "bill" in ref:
            return LegalSourceType.BILL
        if "statutory instrument" in ref or " si " in f" {ref} ":
            return LegalSourceType.STATUTORY_INSTRUMENT
        if "constitution" in ref:
            return LegalSourceType.CONSTITUTION
        return LegalSourceType.STATUTE
    if source_type in ("press_statement", "guidance"):
        # "guidance" (2026-09-10, FIU AML/CFT risk-based-approach guidance)
        # -- regulator/government guidance to practitioners, e.g. an FIU,
        # RBZ, or Law Society circular explaining how to apply a statute,
        # not the statute itself. Same GOVERNMENT_PUBLICATION type (and
        # CONTEXTUAL authority strength) as a press statement -- neither is
        # binding law or judicial precedent, so this taxonomy's existing
        # 3-tier scheme (BINDING = law/top courts, PERSUASIVE = other
        # courts/tribunals, CONTEXTUAL = everything else) already has the
        # right bucket for it; deliberately NOT reusing "legislation" (that
        # would wrongly grant it BINDING/STATUTE-tier authority) and
        # deliberately a distinct source_type from "press_statement" (a
        # 77-page technical implementation guidance document is not
        # honestly a "press statement", even though both share the same
        # authority tier).
        return LegalSourceType.GOVERNMENT_PUBLICATION
    return LegalSourceType.UNKNOWN


# zlr_entries.court, as parsed by parse_zlr_headnote()/parse_zlr_subject_index()
# (main.py) — free text; matched by substring since scraped headnotes vary
# in exact phrasing ("Supreme Court of Zimbabwe", "In the Supreme Court", etc).
_ZLR_COURT_PATTERNS = [
    ("constitutional court", LegalSourceType.CONSTITUTIONAL_COURT),
    ("supreme court", LegalSourceType.SUPREME_COURT),
    ("labour court", LegalSourceType.LABOUR_COURT),
    ("magistrates", LegalSourceType.MAGISTRATES_COURT),
    ("high court", LegalSourceType.HIGH_COURT),
]


def classify_zlr_entry(court, filename=None, case_name=None) -> LegalSourceType:
    # A neutral citation in the filename/case name outranks the parsed court
    # field (see court_from_neutral_citation()).
    from_citation = court_from_neutral_citation(filename, case_name)
    if from_citation:
        return from_citation
    court_lower = (court or "").lower()
    for pattern, source_type in _ZLR_COURT_PATTERNS:
        if pattern in court_lower:
            return source_type
    return LegalSourceType.HIGH_COURT  # ZLR's reported series defaults to High Court


# ── Content-aware backfill classification ────────────────────────────────────
# Used ONLY by POST /api/admin/backfill-legal-taxonomy (dry-run by default).
# Unlike the ingestion functions above, these see what is actually stored, so
# they can refuse to lend authority to rows that contain no law. A "hold"
# decision leaves the row's classification NULL -- the honest "authority
# unverified" state -- rather than guessing.

@dataclass(frozen=True)
class BackfillDecision:
    action: str  # "classify" | "hold"
    legal_source_type: Optional[LegalSourceType]
    authority_strength: Optional[AuthorityStrength]
    reason: str


def _hold(reason: str) -> BackfillDecision:
    return BackfillDecision("hold", None, None, reason)


def _classify(source_type: LegalSourceType, reason: str, strength: Optional[AuthorityStrength] = None) -> BackfillDecision:
    return BackfillDecision("classify", source_type, strength or authority_strength_for(source_type), reason)


# Scraper captures of a bot-protection / error page instead of the document
# (ZimLII "This website uses a security service to protect against malicious
# bots", "Not found (Error 404)", Cloudflare challenge pages).
_STUB_PAGE = re.compile(
    r"security service|malicious bots|not found \(error 404\)|just a moment|checking your browser|"
    r"access denied|403 forbidden", re.IGNORECASE)

# Below this much stored text a non-news row is a header/index card (e.g. the
# ~1,000-character "URL: ... TITLE: ..." Act stubs), not the document itself.
MIN_SUBSTANTIVE_CHARS = 2000

# Reviewed, explicit per-row overrides (filename -> type), approved 2026-09-20.
MANUAL_LEGAL_UPDATE_OVERRIDES = {
    # A Zimbabwe Electronic Law Journal commentary uploaded as "legislation";
    # confirmed from its first chunk. Not law.
    "Amendments to the Zimbabwean Labour Act Chapter 2801.docx": LegalSourceType.ACADEMIC,
}

_FOREIGN_COURT_MARKERS = (
    "south africa", "england", "botswana", "zambia", "malawi", "namibia", "kenya",
    "tanzania", "privy council", "house of lords", "united kingdom",
)


def _explicit_court(court) -> Optional[object]:
    """A court named in the court field: a LegalSourceType, the string
    "foreign" for a non-Zimbabwean court, or None. Unlike
    classify_zlr_entry() there is NO silent High Court default."""
    court_lower = (court or "").lower()
    if any(marker in court_lower for marker in _FOREIGN_COURT_MARKERS):
        return "foreign"
    for pattern, source_type in _ZLR_COURT_PATTERNS:
        if pattern in court_lower:
            return source_type
    return None


def classify_legal_update_for_backfill(*, source_type, filename, reference, court, validity_flag,
                                       chunk_count, total_chars, head_text) -> BackfillDecision:
    # 1. Nothing to classify: no chunks, a scraper block page, or a header-only stub.
    if not chunk_count:
        return _hold("no chunks (ghost row)")
    if head_text and _STUB_PAGE.search(head_text):
        return _hold("scraper stub: bot-protection/404 page, not the document")
    if source_type != "news" and (total_chars or 0) < MIN_SUBSTANTIVE_CHARS:
        return _hold(f"header-only/thin stub (<{MIN_SUBSTANTIVE_CHARS} chars of text)")

    # 2. What kind of thing is it?
    if filename in MANUAL_LEGAL_UPDATE_OVERRIDES:
        legal_type = MANUAL_LEGAL_UPDATE_OVERRIDES[filename]
        reason = "reviewed manual override (not legislation)"
    else:
        cited_court = court_from_neutral_citation(filename, reference)
        if cited_court:
            legal_type, reason = cited_court, "court named by the neutral citation in the filename"
        elif source_type == "case_law":
            explicit = _explicit_court(court)
            if isinstance(explicit, LegalSourceType):
                legal_type, reason = explicit, "court named in the court field"
            elif explicit == "foreign":
                legal_type, reason = LegalSourceType.UNKNOWN, "foreign court: no taxonomy type, kept contextual"
            else:
                legal_type, reason = LegalSourceType.UNKNOWN, "no Zimbabwean court evidence, kept contextual"
        elif source_type == "statutory_instrument":
            legal_type, reason = LegalSourceType.STATUTORY_INSTRUMENT, "statutory instrument with real content"
        else:
            legal_type = classify_legal_update(source_type, reference)
            reason = f"source_type '{source_type}'"

    # 3. A validity-disputed source can never be binding/persuasive, exactly as
    # at ingestion (upload_legal_update()).
    if validity_flag:
        return _classify(legal_type, reason + "; validity_flag set, forced contextual", AuthorityStrength.CONTEXTUAL)
    return _classify(legal_type, reason)


def classify_zlr_entry_for_backfill(*, court, filename, case_name, jurisdiction, chunk_count) -> BackfillDecision:
    if jurisdiction is not None and jurisdiction != "Zimbabwe":
        return _hold(f"non-Zimbabwean jurisdiction ('{jurisdiction}'): no taxonomy type")
    if not chunk_count:
        return _hold("no chunks (ghost row)")
    cited_court = court_from_neutral_citation(filename, case_name)
    if cited_court:
        return _classify(cited_court, "court named by the neutral citation (outranks the court field)")
    explicit = _explicit_court(court)
    if isinstance(explicit, LegalSourceType):
        return _classify(explicit, "court named in the court field")
    if explicit == "foreign":
        return _hold("foreign court in the court field: no taxonomy type")
    return _hold("blank court and no neutral citation: refusing to default to High Court")
