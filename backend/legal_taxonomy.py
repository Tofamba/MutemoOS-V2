"""
Legal source classification and authority-strength scoring.

Deterministic, not AI-driven — classification runs cheaply at ingest time
(and in a one-off backfill for existing rows) rather than per-query, since
a document's legal type and authority don't change between searches.
"""

from enum import Enum


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
# (main.py) — "legislation" | "news" | "press_statement" | "zlhr" | others.
def classify_legal_update(source_type, reference=None) -> LegalSourceType:
    if source_type == "legislation":
        ref = (reference or "").lower()
        if "bill" in ref:
            return LegalSourceType.BILL
        if "statutory instrument" in ref or " si " in f" {ref} ":
            return LegalSourceType.STATUTORY_INSTRUMENT
        if "constitution" in ref:
            return LegalSourceType.CONSTITUTION
        return LegalSourceType.STATUTE
    if source_type == "press_statement":
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


def classify_zlr_entry(court) -> LegalSourceType:
    court_lower = (court or "").lower()
    for pattern, source_type in _ZLR_COURT_PATTERNS:
        if pattern in court_lower:
            return source_type
    return LegalSourceType.HIGH_COURT  # ZLR's reported series defaults to High Court
