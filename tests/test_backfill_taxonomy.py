"""
Tests for the content-aware classification backfill (2026-09-20).

Context: 133 legal_updates and 349 zlr_entries in production had NULL
legal_source_type/authority_strength. The pre-existing backfill endpoint's
rules were wrong for real data (found by a read-only dry run): it had no
statutory_instrument branch, treated case_law as "unknown", called anything
typed "legislation" binding with no content check (26 rows were scraper
bot-block pages and 20 were ~1,000-character header-only stubs), used the
court field alone for ZLR (wrong on 9 of the 24 uploaded judgments that carry
a neutral citation) and silently defaulted blank courts to High Court.

The rewrite is dry-run by default, holds rows that contain no law (leaving
them NULL = "authority unverified"), and applies only the exact reviewed set.
"""
import asyncio

import pytest
from fastapi import HTTPException

from backend.legal_taxonomy import (
    AuthorityStrength as A,
    LegalSourceType as T,
    MIN_SUBSTANTIVE_CHARS,
    classify_legal_update,
    classify_legal_update_for_backfill,
    classify_zlr_entry,
    classify_zlr_entry_for_backfill,
    court_from_neutral_citation,
)

BOT_BLOCK = "This website uses a security service to protect against malicious bots. This page is displayed while the website verifies you are not a bot."
REAL = 12000  # comfortably substantive


def lu(**over):
    base = dict(source_type="legislation", filename="Arbitration Act.pdf", reference="", court=None, validity_flag=None,
                chunk_count=10, total_chars=REAL, head_text="ARBITRATION ACT [CHAPTER 7:15] An Act to provide for")
    base.update(over)
    return classify_legal_update_for_backfill(**base)


def zlr(**over):
    base = dict(court="High Court, Harare", filename="Zuva Petroleum v Motsi", case_name="Zuva Petroleum (Pvt) Ltd v Motsi & Anor",
                jurisdiction="Zimbabwe", chunk_count=3)
    base.update(over)
    return classify_zlr_entry_for_backfill(**base)


# ── neutral citation helper ──────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("FBC Bank v Kwangwari 2025 ZWSC 17 (27 February 2025).docx", T.SUPREME_COURT),
    ("Fuza v Parliament 2026 ZWCC 3", T.CONSTITUTIONAL_COURT),
    ("Role Security v Gruwo 2026 ZWHHC 127", T.HIGH_COURT),
    ("Ndlovu v Ndlovu [2026] ZWBHC 90", T.HIGH_COURT),
    ("Nyathi v Pioneer 2026 ZWLC 4", T.LABOUR_COURT),
    ("zwsc lower case 2025 zwsc 9", T.SUPREME_COURT),
])
def test_neutral_citation_names_the_court(text, expected):
    assert court_from_neutral_citation(text) == expected


def test_no_neutral_citation_means_none():
    assert court_from_neutral_citation("Arbitration Act.pdf", None, "") is None
    assert court_from_neutral_citation("Some Case (HCH 5667/2) with SC 45/18 in the text") is None  # loose SC/HC tokens are not neutral citations


# ── ingestion path: only the safe improvements ───────────────────────────

def test_ingestion_case_law_uses_the_neutral_citation_when_the_filename_has_one():
    assert classify_legal_update("case_law", None, "Govan v Govan (SC 77125) 2026 ZWSC 10 (3 March 2026).pdf") == T.SUPREME_COURT


def test_ingestion_case_law_without_a_citation_is_unchanged():
    assert classify_legal_update("case_law") == T.UNKNOWN
    assert classify_legal_update("case_law", None, "judgment.txt") == T.UNKNOWN


def test_ingestion_deliberately_still_has_no_statutory_instrument_branch():
    """Ingestion never sees the text, so a bare "SI -> binding" rule would make
    every future scraper bot-block stub typed statutory_instrument binding
    (all 53 such rows in production are stubs). Only the content-aware
    backfill classifies real SIs."""
    assert classify_legal_update("statutory_instrument", None, "Unknown Legislation") == T.UNKNOWN


def test_ingestion_zlr_neutral_citation_outranks_a_wrong_court_field():
    # Real case: a 2025 ZWSC judgment whose parsed court field read "Labour Court".
    assert classify_zlr_entry("Labour Court", "Treger Plastics v Dube (8 of 2025) 2025 ZWSC 8.docx", None) == T.SUPREME_COURT
    assert classify_zlr_entry("Labour Court") == T.LABOUR_COURT  # no citation -> court field, unchanged


# ── backfill, legal_updates: hold rows that contain no law ───────────────

def test_ghost_row_with_no_chunks_is_held():
    d = lu(chunk_count=0, total_chars=0, head_text=None)
    assert d.action == "hold" and "no chunks" in d.reason


@pytest.mark.parametrize("st", ["statutory_instrument", "legislation"])
def test_scraper_bot_block_stub_is_held_never_authority(st):
    d = lu(source_type=st, filename="Unknown Legislation", chunk_count=1, total_chars=len(BOT_BLOCK), head_text=BOT_BLOCK)
    assert d.action == "hold" and "stub" in d.reason
    assert d.legal_source_type is None and d.authority_strength is None


def test_404_page_is_held():
    d = lu(source_type="legislation", chunk_count=1, total_chars=228, head_text="# Not found (Error 404) We couldn't find the page you're looking for.")
    assert d.action == "hold"


def test_header_only_act_txt_stub_is_held():
    d = lu(filename="Administration_of_Estates_Act.txt", chunk_count=1, total_chars=988,
           head_text="URL: https://zimlii.org/akn/zw/act/ord/1907/6/eng@2025-02-24 TITLE: Administration of Estates Act")
    assert d.action == "hold" and str(MIN_SUBSTANTIVE_CHARS) in d.reason


def test_thin_news_is_still_classified_because_news_is_short_by_nature():
    d = lu(source_type="news", filename="Some headline", chunk_count=1, total_chars=499, head_text="## [Local News](https://www.newsday.co.zw/...")
    assert d.action == "classify" and d.legal_source_type == T.UNKNOWN and d.authority_strength == A.CONTEXTUAL


def test_statutory_instrument_with_real_content_is_binding():
    d = lu(source_type="statutory_instrument", filename="SI 2022-107 Collective Bargaining Agreement", total_chars=REAL,
           head_text="STATUTORY INSTRUMENT 107 OF 2022 [CAP. 28:01 Collective Bargaining Agreement")
    assert d.action == "classify" and d.legal_source_type == T.STATUTORY_INSTRUMENT and d.authority_strength == A.BINDING


# ── backfill, legal_updates: what real rows become ───────────────────────

def test_real_act_is_statute_binding():
    d = lu()
    assert (d.action, d.legal_source_type, d.authority_strength) == ("classify", T.STATUTE, A.BINDING)


def test_bill_by_reference_is_contextual_not_binding():
    d = lu(reference="Cyber Security Bill")
    assert d.legal_source_type == T.BILL and d.authority_strength == A.CONTEXTUAL


def test_reviewed_manual_override_journal_commentary_is_contextual():
    d = lu(filename="Amendments to the Zimbabwean Labour Act Chapter 2801.docx", total_chars=REAL)
    assert (d.legal_source_type, d.authority_strength) == (T.ACADEMIC, A.CONTEXTUAL)
    assert "manual override" in d.reason


@pytest.mark.parametrize("fname,expected,strength", [
    ("Petrozim Line (Private) Limited v Hova (SC 48424) 2026 ZWSC 11 (23 February 2026).pdf", T.SUPREME_COURT, A.BINDING),
    ("Fuza and Another v Parliament Of Zimbabwe (CCZ 3625) 2026 ZWCC 3.pdf", T.CONSTITUTIONAL_COURT, A.BINDING),
    ("Role Security (Pvt) Ltd v Gruwo and Others (HCH 566724) 2026 ZWHHC 127.pdf", T.HIGH_COURT, A.PERSUASIVE),
])
def test_case_law_is_classified_by_its_neutral_citation(fname, expected, strength):
    d = lu(source_type="case_law", filename=fname)
    assert (d.action, d.legal_source_type, d.authority_strength) == ("classify", expected, strength)


def test_a_judgment_typed_as_legislation_follows_its_citation_not_the_type():
    d = lu(source_type="legislation", filename="Ahmed v Docking Station Safaris (Civil Appeal SC 177 of 2018) 2018 ZWSC 50.pdf")
    assert d.legal_source_type == T.SUPREME_COURT


def test_case_law_without_a_citation_uses_an_explicit_court_field():
    d = lu(source_type="case_law", filename="Some Judgment.pdf", court="High Court of Zimbabwe")
    assert d.legal_source_type == T.HIGH_COURT and d.authority_strength == A.PERSUASIVE


def test_foreign_court_judgment_is_kept_contextual_not_held_and_not_authority():
    d = lu(source_type="case_law", filename="SA Constitutional Court Judgement.pdf", court="Constitutional Court of South Africa")
    assert d.action == "classify" and d.legal_source_type == T.UNKNOWN and d.authority_strength == A.CONTEXTUAL
    assert "foreign" in d.reason


def test_case_law_with_no_court_evidence_is_kept_contextual():
    d = lu(source_type="case_law", filename="Some Handout.docx", court=None)
    assert d.legal_source_type == T.UNKNOWN and d.authority_strength == A.CONTEXTUAL


def test_validity_flagged_row_is_forced_contextual_never_binding():
    d = lu(reference="Constitution of Zimbabwe Amendment Act No. 6 of 2026", validity_flag="Enactment challenged — no referendum held per s.328")
    assert d.legal_source_type == T.CONSTITUTION
    assert d.authority_strength == A.CONTEXTUAL and "validity_flag" in d.reason


# ── backfill, zlr_entries ────────────────────────────────────────────────

@pytest.mark.parametrize("court,expected,strength", [
    ("High Court, Harare", T.HIGH_COURT, A.PERSUASIVE),
    ("High Court, Bulawayo", T.HIGH_COURT, A.PERSUASIVE),
    ("Constitutional Court of Zimbabwe", T.CONSTITUTIONAL_COURT, A.BINDING),
    ("Supreme Court", T.SUPREME_COURT, A.BINDING),
    ("Labour Court", T.LABOUR_COURT, A.PERSUASIVE),
])
def test_zlr_court_field_classifies_when_there_is_no_citation(court, expected, strength):
    d = zlr(court=court, filename="judgment.txt", case_name="S v Konson")
    assert (d.action, d.legal_source_type, d.authority_strength) == ("classify", expected, strength)


def test_zlr_neutral_citation_outranks_a_wrong_court_field_in_both_directions():
    over_claim = zlr(court="Supreme Court", filename="DANDIRA v ZIMPOST 2026 ZWHHC 12.pdf")   # field says Supreme, citation says High Court
    under_claim = zlr(court="Labour Court", filename="Treger Plastics v Dube 2025 ZWSC 8.docx")  # field says Labour, citation says Supreme
    assert (over_claim.legal_source_type, over_claim.authority_strength) == (T.HIGH_COURT, A.PERSUASIVE)
    assert (under_claim.legal_source_type, under_claim.authority_strength) == (T.SUPREME_COURT, A.BINDING)


def test_zlr_blank_court_without_a_citation_is_held_not_defaulted_to_high_court():
    d = zlr(court=None, filename="MATRIMONIAL SUMMONS - MR CHOTO.doc", case_name=None)
    assert d.action == "hold" and "refusing to default" in d.reason


def test_zlr_blank_court_with_a_citation_is_classified():
    d = zlr(court=None, filename="CASE: Ndlovu v Ndlovu (HCBC 824/24) [2026] ZWBHC 90 (11 March 2026)", case_name=None)
    assert d.action == "classify" and d.legal_source_type == T.HIGH_COURT


def test_zlr_non_zimbabwean_jurisdiction_is_held():
    d = zlr(court=None, jurisdiction="Other", filename="judgment.txt", case_name=None)
    assert d.action == "hold" and "non-Zimbabwean" in d.reason


def test_zlr_foreign_court_field_is_held():
    d = zlr(court="Supreme Court of South Africa", filename="judgment.txt", case_name=None)
    assert d.action == "hold" and "foreign" in d.reason


def test_zlr_ghost_row_is_held():
    assert zlr(chunk_count=0).action == "hold"


# ── the endpoint ─────────────────────────────────────────────────────────

class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, lu_rows, zlr_rows, null_state=None):
        self.lu_rows, self.zlr_rows = lu_rows, zlr_rows
        self.queries, self.updates = [], []
        self.classified = set()  # ids already classified by an earlier apply (simulates the IS NULL guard)

    def transaction(self):
        return _Tx()

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        self.queries.append(q)
        if q.startswith("SELECT lu.id"):
            return [r for r in self.lu_rows if r["id"] not in self.classified]
        if q.startswith("SELECT z.id"):
            return [r for r in self.zlr_rows if r["id"] not in self.classified]
        raise NotImplementedError(q)

    async def execute(self, query, *args):
        q = " ".join(query.split())
        self.queries.append(q)
        self.updates.append((q, args))
        row_id = args[2]
        if row_id in self.classified:
            return "UPDATE 0"
        self.classified.add(row_id)
        return "UPDATE 1"


class _AcquireCtx:
    def __init__(self, c):
        self.c = c

    async def __aenter__(self):
        return self.c

    async def __aexit__(self, *e):
        return False


class _Pool:
    def __init__(self, c):
        self.c = c

    def acquire(self):
        return _AcquireCtx(self.c)


def _lu_row(i, **over):
    r = dict(id=f"lu-{i}", filename=f"Act {i}.pdf", source_type="legislation", reference="", court=None, validity_flag=None,
             chunk_count=10, total_chars=REAL, head_text="AN ACT to provide for")
    r.update(over)
    return r


def _zlr_row(i, **over):
    r = dict(id=f"z-{i}", filename=f"case {i}", case_name=f"S v Person {i}", court="High Court, Harare",
             jurisdiction="Zimbabwe", judgment_number=f"HH-{i}-16", chunk_count=2)
    r.update(over)
    return r


def _setup(monkeypatch, lu_rows, zlr_rows):
    import backend.main as m
    conn = _Conn(lu_rows, zlr_rows)
    monkeypatch.setattr(m, "_db_pool", _Pool(conn))
    monkeypatch.setattr(m, "require_admin_token", lambda request: None)
    return m, conn


def _rows():
    lus = [_lu_row(1), _lu_row(2, source_type="statutory_instrument", filename="Unknown Legislation", chunk_count=1,
                              total_chars=200, head_text=BOT_BLOCK), _lu_row(3, chunk_count=0, total_chars=0, head_text=None)]
    zs = [_zlr_row(1), _zlr_row(2, court=None, filename="x.doc", case_name=None)]
    return lus, zs


def test_dry_run_is_the_default_and_writes_nothing(monkeypatch):
    lus, zs = _rows()
    m, conn = _setup(monkeypatch, lus, zs)
    out = asyncio.run(m.backfill_legal_taxonomy(object()))
    assert out["dry_run"] is True
    assert conn.updates == []


def test_dry_run_lists_every_row_with_its_decision_and_reason(monkeypatch):
    lus, zs = _rows()
    m, conn = _setup(monkeypatch, lus, zs)
    out = asyncio.run(m.backfill_legal_taxonomy(object(), dry_run=True))
    by_id = {r["id"]: r for r in out["legal_updates"]}
    assert by_id["lu-1"]["action"] == "classify" and by_id["lu-1"]["legal_source_type"] == "statute"
    assert by_id["lu-2"]["action"] == "hold" and "stub" in by_id["lu-2"]["reason"]
    assert by_id["lu-3"]["action"] == "hold" and "no chunks" in by_id["lu-3"]["reason"]
    z = {r["id"]: r for r in out["zlr_entries"]}
    assert z["z-1"]["action"] == "classify" and z["z-2"]["action"] == "hold"
    assert out["summary"]["legal_updates"]["classify"] == 1 and out["summary"]["legal_updates"]["hold"] == 2
    assert out["expected_counts_to_apply"] == {"expected_legal_updates": 1, "expected_zlr_entries": 1}


def test_the_firm_documents_table_is_never_read_or_written(monkeypatch):
    lus, zs = _rows()
    m, conn = _setup(monkeypatch, lus, zs)
    asyncio.run(m.backfill_legal_taxonomy(object(), dry_run=True))
    asyncio.run(m.backfill_legal_taxonomy(object(), dry_run=False, expected_legal_updates=1, expected_zlr_entries=1))
    assert not any("documents" in q.replace("legal_updates", "").replace("zlr_entries", "") for q in conn.queries)


def test_apply_without_the_reviewed_counts_is_refused(monkeypatch):
    lus, zs = _rows()
    m, conn = _setup(monkeypatch, lus, zs)
    with pytest.raises(HTTPException) as e:
        asyncio.run(m.backfill_legal_taxonomy(object(), dry_run=False))
    assert e.value.status_code == 400 and conn.updates == []
    with pytest.raises(HTTPException) as e2:
        asyncio.run(m.backfill_legal_taxonomy(object(), dry_run=False, expected_legal_updates=1))
    assert e2.value.status_code == 400 and conn.updates == []


def test_apply_with_stale_counts_writes_nothing(monkeypatch):
    lus, zs = _rows()
    m, conn = _setup(monkeypatch, lus, zs)
    with pytest.raises(HTTPException) as e:
        asyncio.run(m.backfill_legal_taxonomy(object(), dry_run=False, expected_legal_updates=5, expected_zlr_entries=1))
    assert e.value.status_code == 409 and conn.updates == []


def test_apply_writes_only_classify_rows_and_re_checks_the_null_guard(monkeypatch):
    lus, zs = _rows()
    m, conn = _setup(monkeypatch, lus, zs)
    out = asyncio.run(m.backfill_legal_taxonomy(object(), dry_run=False, expected_legal_updates=1, expected_zlr_entries=1))
    assert out["applied"] == {"legal_updates": 1, "zlr_entries": 1}
    assert {args[2] for _, args in conn.updates} == {"lu-1", "z-1"}  # the stub, ghost and blank-court rows are untouched
    for q, _ in conn.updates:
        assert "legal_source_type IS NULL" in q and "firm_id=$4" in q


def test_apply_is_idempotent(monkeypatch):
    lus, zs = _rows()
    m, conn = _setup(monkeypatch, lus, zs)
    asyncio.run(m.backfill_legal_taxonomy(object(), dry_run=False, expected_legal_updates=1, expected_zlr_entries=1))
    again = asyncio.run(m.backfill_legal_taxonomy(object(), dry_run=True))
    # already-classified rows no longer appear; only the held rows remain
    assert again["summary"]["legal_updates"]["classify"] == 0 and again["summary"]["zlr_entries"]["classify"] == 0
    second = asyncio.run(m.backfill_legal_taxonomy(object(), dry_run=False, expected_legal_updates=0, expected_zlr_entries=0))
    assert second["applied"] == {"legal_updates": 0, "zlr_entries": 0}
