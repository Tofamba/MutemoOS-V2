#!/usr/bin/env python3
"""
NGM Master Case List Import
============================
One-time migration: imports Nyaradzo Gilbertina Maphosa's original master
case-list Word document (one small key-value table per client-matter
record — File Name, Name of client, Case Number, Re, Law, Action done,
Next action, Latest communication; not every field present on every
record) into MutemoOS.

This is NOT a fresh import — this document is very likely the original
source for the clients already in the system (created earlier via
scripts/migrate_clients.py and the bulk-onboarding endpoint). Every
record is cross-checked against what's already in the database before
anything is created:

  1. Client name -> match_client_name() (backend/client_migration.py,
     the same fuzzy-matching primitives migrate_clients.py and the
     bulk-onboarding endpoint already use) against a pool of existing DB
     clients PLUS clients already resolved earlier in this same run (so a
     repeated/near-identical name later in the document reuses the same
     client rather than creating a near-duplicate — same growing-pool
     technique as bulk_onboard_from_excel).
       no_match   -> new client, assigned the next available client_number
                      under --initials (default NGM), continuing from
                      wherever numbering currently stands.
       matched    -> reuse the existing client.
       ambiguous  -> flagged for review. NEVER auto-merged. Its matter(s)
                      are still created (client_id NULL, client_name set
                      to the raw document name) — the same "legacy"
                      fallback state bulk_onboard_from_excel already
                      uses for this case — so nothing is silently
                      dropped, and a human can relink it afterward via
                      the existing Clients tab / matter detail UI.

  2. For a resolved (matched or newly-created) client, each record's
     matter (Re + Law, combined — see _combine_matter_description) is
     scored against that client's matters already in the database AND
     against matters already created earlier in this same run (catches
     the document's own internal near-duplicates too, e.g. two
     back-to-back rows for the same client that are really one matter
     entered twice) — see _matter_dedup_score. A close/exact match is
     SKIPPED, not duplicated; a genuinely new matter is created and
     numbered as the next matter under that client. Every skip decision
     is listed in the report with what it matched to and its score, so
     the threshold's aggressiveness can be sanity-checked by eye rather
     than trusted blindly.

  3. A record with no usable client name is skipped and listed in the
     report, never guessed at.

  4. A record with a client name but literally nothing else (no Re, Law,
     Action done, Next action, or Latest communication) gets its client
     matched/created as normal, but no matter is created for it — there's
     nothing to name or describe a matter with. Listed separately in the
     report.

Design decision beyond the 5 numbered requirements, flagged here rather
than buried: matters has no free-text status field, so each newly created
matter also gets one progress_notes entry capturing File Name/Case
Number/Action done/Next action/Latest communication — otherwise that
narrative is silently discarded despite being explicitly present in the
source document. The report states this count; pass --no-progress-notes
to skip it if you'd rather not.

Preview-first, matching this project's migrate_clients.py /
backfill_client_matter_numbers.py report/--yes convention: `report` is
fully read-only; `apply --yes` recomputes the plan fresh from live DB
state (not from a saved file) and writes it in one transaction.
Recomputing fresh also makes apply naturally safe to re-run: matters
created by an earlier run are picked up as "already exists" by the same
dedup check and skipped on a second run, rather than duplicated.

Usage:
    DATABASE_URL=postgresql://... python3 scripts/import_ngm_case_list.py report --docx "List of Cases NGM.docx"
    DATABASE_URL=postgresql://... python3 scripts/import_ngm_case_list.py apply --docx "List of Cases NGM.docx" --yes
"""

import argparse
import asyncio
import difflib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import asyncpg
except ImportError:
    print("ERROR: asyncpg not installed. Run: pip install asyncpg")
    sys.exit(1)

try:
    from docx import Document
except ImportError:
    print("ERROR: python-docx not installed. Run: pip install python-docx")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.client_migration import match_client_name  # noqa: E402
from backend.numbering import (  # noqa: E402
    next_sequence, format_client_number, format_matter_number,
)

FIRM_ID = os.environ.get("MUTEMO_FIRM_ID", "a1b2c3d4-0000-0000-0000-000000000001")

DEFAULT_REPORT_PATH = "ngm_case_list_import_review.json"

# ── Parsing ───────────────────────────────────────────────────────────────
# The source document is a hand-maintained Word doc — field labels vary in
# capitalization, spacing, and the occasional stray backtick/typo (observed
# directly in the actual file: "`File Name", "File name", "Case number",
# "Next action latest", "Latestcommunication", "Latest", "Communication").
# Normalize aggressively rather than assume a clean, consistent label per
# row.
_FIELD_ALIASES = {
    "file name": "file_name",
    "name of client": "client_name",
    "case number": "case_number",
    "re": "re",
    "law": "law",
    "action done": "action_done",
    "next action": "next_action",
    "next action latest": "next_action",
    "latest communication": "latest_communication",
    "latestcommunication": "latest_communication",
    "latest": "latest_communication",
    "communication": "latest_communication",
}


def _normalize_field_key(raw_key: str):
    k = (raw_key or "").strip().strip("`").lower()
    k = re.sub(r"\s+", " ", k)
    return _FIELD_ALIASES.get(k)


def parse_case_list_docx(path: str) -> dict:
    """
    Reads the document's tables (one per client-matter record — a 2-column
    key/value table, field name in column 0, value in column 1) into
    structured records.

    Returns {"records": [...], "unrecognized_field_warnings": [...]}.
    Each record: {"table_index", "file_name", "client_name", "case_number",
    "re", "law", "action_done", "next_action", "latest_communication"}
    (any field not present on that record's table is None, not "").
    unrecognized_field_warnings lists any row whose label didn't match a
    known field alias (excluding blank/malformed label cells, which are a
    known artifact of a couple of tables and are silently ignored) —
    surfaced so a genuinely new field name doesn't disappear unnoticed.
    """
    doc = Document(path)
    records = []
    warnings = []

    for ti, table in enumerate(doc.tables):
        record = {
            "table_index": ti, "file_name": None, "client_name": None, "case_number": None,
            "re": None, "law": None, "action_done": None, "next_action": None,
            "latest_communication": None,
        }
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if len(cells) < 2:
                continue
            raw_key, raw_val = cells[0], cells[1]
            if not raw_key.strip():
                continue  # malformed blank-label row — known artifact, not real data
            field = _normalize_field_key(raw_key)
            if field is None:
                warnings.append({"table_index": ti, "unrecognized_label": raw_key})
                continue
            if raw_val:
                record[field] = raw_val
        records.append(record)

    return {"records": records, "unrecognized_field_warnings": warnings}


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())


def _combine_matter_description(re_field, law_field):
    """
    "Re — Law", matching the same em-dash convention already used
    elsewhere (bulk_onboard_from_excel's "Reference — description",
    extract_case_reference in the reminder email). If Re and Law are the
    same text (case-insensitive — several records in this document have
    e.g. Re="Trust", Law="Trust"), just the one value, not "Trust — Trust".
    Falls back to whichever single field is present; None if both are
    blank (the caller decides what to do with that — see "no matter
    created" handling).
    """
    re_clean = (re_field or "").strip()
    law_clean = (law_field or "").strip()
    if re_clean and law_clean:
        if re_clean.lower() == law_clean.lower():
            return re_clean
        return f"{re_clean} — {law_clean}"
    return re_clean or law_clean or None


# ── Matter dedup scoring ──────────────────────────────────────────────────
# Existing matter names already follow a "{Re} — {fuller description}"
# shape (same convention bulk_onboard_from_excel uses to build matters.name
# from this exact kind of Reference/description pair) — so the Re field is
# the more distinctive, less noisy comparison point than the whole
# description. Law-style category words ("Trust", "Estate", "Property",
# "Matrimonial/divorce") recur across many genuinely different matters for
# the same client and would dilute — or falsely inflate — a whole-text
# similarity score if trusted directly. Calibrated against real matter
# text already in this database (see this migration's own commit/PR
# discussion for the worked examples that set MATTER_DEDUP_THRESHOLD).
MATTER_DEDUP_THRESHOLD = 0.75

_GENERIC_RE_TERMS = {
    "trust", "estate", "property", "divorce", "matrimonial", "eviction",
    "lease", "debt", "sale", "labour", "custody", "criminal", "land",
    "conveyancing", "inheritance", "company", "contract", "access",
    "guardianship", "agreement", "family",
}


def _normalize_matter_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _matter_dedup_score(doc_re: str, existing_name: str) -> float:
    """
    Scores how likely a document record's Re text is the same matter as an
    existing matter's stored name. A bare, generic Re value (in
    _GENERIC_RE_TERMS — a Law-style category word, not a distinctive party/
    matter name) never gets the containment shortcut below, since it would
    otherwise "match" almost any unrelated matter that happens to mention
    the same category word (verified directly: "Trust" vs "Family Trust —
    Sent requirement of Trust to client" scores 0.9 via naive containment,
    a false positive — restricting the shortcut to non-generic Re values
    drops that same pair to 0.59, safely under threshold).
    """
    re_norm = _normalize_matter_text(doc_re)
    existing_full_norm = _normalize_matter_text(existing_name)
    if not re_norm or not existing_full_norm:
        return 0.0

    existing_lead_raw = existing_name.split(" — ", 1)[0] if " — " in existing_name else existing_name
    existing_lead_norm = _normalize_matter_text(existing_lead_raw)

    is_generic = re_norm in _GENERIC_RE_TERMS
    contained = (not is_generic) and (re_norm in existing_full_norm or existing_full_norm in re_norm)

    lead_ratio = difflib.SequenceMatcher(None, re_norm, existing_lead_norm).ratio()
    full_ratio = difflib.SequenceMatcher(None, re_norm, existing_full_norm).ratio()
    score = max(lead_ratio, full_ratio)
    if contained:
        score = max(score, 0.9)
    return score


def _best_matter_dedup_match(doc_re: str, candidate_matters: list):
    """candidate_matters: [{"name": str, "source": "existing_db"|"this_run"}, ...].
    Returns the best-scoring candidate dict (with a "score" key added) if
    it clears MATTER_DEDUP_THRESHOLD, else None. A blank Re can't be
    safely scored at all (see _matter_dedup_score) — dedup is skipped
    entirely for those rather than falling back to the noisier Law text."""
    if not doc_re or not (doc_re or "").strip():
        return None
    best = None
    for cand in candidate_matters:
        score = _matter_dedup_score(doc_re, cand["name"])
        if score >= MATTER_DEDUP_THRESHOLD and (best is None or score > best["score"]):
            best = {**cand, "score": round(score, 3)}
    return best


# ── Plan assembly (report + apply share this — recomputed fresh each time) ─

async def _build_plan(conn, initials: str, records: list) -> dict:
    existing_client_rows = await conn.fetch(
        "SELECT id, full_name, client_number FROM clients WHERE firm_id=$1", uuid.UUID(FIRM_ID)
    )
    pool = [{"id": str(r["id"]), "full_name": r["full_name"], "client_number": r["client_number"]}
            for r in existing_client_rows]
    next_client_seq = next_sequence([r["client_number"] for r in existing_client_rows], initials)

    # Per-client existing matters, fetched lazily and cached — seeds the
    # dedup check, and its matter_number values seed that client's next
    # matter sequence. Keyed by client_id (str) for matched/existing
    # clients, or by the newly-assigned client_number string for a client
    # created earlier in this same run (no real client_id yet in report
    # mode, and the client_number is already guaranteed unique for the run).
    matters_by_client_key = {}
    matter_seq_by_client_key = {}

    async def _matters_for(client_key: str, client_id, client_number):
        if client_key not in matters_by_client_key:
            if client_id is None:
                matters_by_client_key[client_key] = []
                matter_seq_by_client_key[client_key] = 1
            else:
                rows = await conn.fetch(
                    "SELECT name, matter_number FROM matters WHERE client_id=$1", uuid.UUID(client_id)
                )
                matters_by_client_key[client_key] = [
                    {"name": r["name"], "source": "existing_db"} for r in rows
                ]
                existing_numbers = [r["matter_number"] for r in rows if r["matter_number"]]
                # A matched client with no client_number yet (legacy, not
                # backfilled) can't have its matters numbered either — same
                # rule create_matter already follows.
                matter_seq_by_client_key[client_key] = (
                    next_sequence(existing_numbers, client_number) if client_number else 1
                )
        return matters_by_client_key[client_key]

    clients_created, clients_matched, clients_review = [], [], []
    clients_fuzzy_merged_within_run = []
    matters_created, matters_skipped_duplicate = [], []
    matters_unlinked, matters_client_only = [], []
    unparseable = []

    # Name each client_key was first created/matched under — lets a later
    # in-run match report itself when the text genuinely differs (e.g.
    # table 45 "Manyema" vs table 66 "Manyema  Lloyd", silently folded into
    # the same client by match_client_name's shared-token fuzzy logic) while
    # staying quiet for a trivial exact repeat (e.g. "Huang Li Qiang" named
    # identically across 10 records) — the former is exactly the kind of
    # judgment call worth surfacing; the latter would just be noise.
    client_key_first_name = {}

    for rec in records:
        raw_name = _clean_name(rec.get("client_name") or "")
        if not raw_name:
            unparseable.append({"table_index": rec["table_index"], "reason": "no client name"})
            continue

        match = match_client_name(raw_name, pool)
        client_id = None
        client_number = None
        client_key = None

        if match["status"] == "matched":
            client_id = match["candidate"]["id"]
            client_number = match["candidate"].get("client_number")
            # A match against a client created earlier IN THIS SAME RUN has
            # no real id yet (id=None in the pool entry — see the no_match
            # branch below) — client_key falls back to that client's
            # client_number, the same stable key it was first created
            # under, so this record's matter lands under the same client
            # rather than a dangling "matched a None client_id" state.
            client_key = client_id if client_id is not None else client_number
            if client_id is not None:
                clients_matched.append({
                    "table_index": rec["table_index"], "name": raw_name,
                    "matched_client_id": client_id, "matched_client_name": match["candidate"]["full_name"],
                    "matched_client_number": client_number,
                })
            else:
                first_name = client_key_first_name.get(client_key)
                if first_name and first_name.lower() != raw_name.lower():
                    clients_fuzzy_merged_within_run.append({
                        "table_index": rec["table_index"], "name": raw_name,
                        "merged_into_name": first_name, "client_number": client_number,
                    })
        elif match["status"] == "no_match":
            client_number = format_client_number(initials, next_client_seq)
            next_client_seq += 1
            client_key = client_number  # stable placeholder key for this run
            client_key_first_name[client_key] = raw_name
            clients_created.append({
                "table_index": rec["table_index"], "name": raw_name, "client_number": client_number,
            })
            pool.append({"id": None, "full_name": raw_name, "client_number": client_number})
        else:  # ambiguous — never guess
            clients_review.append({
                "table_index": rec["table_index"], "name": raw_name, "candidates": match["candidates"],
            })

        matter_desc = _combine_matter_description(rec.get("re"), rec.get("law"))
        has_any_case_detail = any([
            rec.get("re"), rec.get("law"), rec.get("action_done"),
            rec.get("next_action"), rec.get("latest_communication"),
        ])

        if client_key is None:
            # Ambiguous client — matter still created, unlinked, same
            # legacy fallback state bulk_onboard_from_excel already uses.
            if matter_desc:
                matters_unlinked.append({
                    "table_index": rec["table_index"], "client_name": raw_name,
                    "matter_name": matter_desc,
                })
            elif not has_any_case_detail:
                matters_client_only.append({"table_index": rec["table_index"], "client_name": raw_name})
            continue

        if not has_any_case_detail:
            matters_client_only.append({"table_index": rec["table_index"], "client_name": raw_name})
            continue

        if not matter_desc:
            # Has other case detail (Action done/Next action/Latest comm)
            # but no Re/Law to name the matter — still create it, fallback
            # name, never silently drop real case content.
            matter_desc = "Untitled matter"

        existing_and_run_matters = await _matters_for(client_key, client_id, client_number)
        dup = _best_matter_dedup_match(rec.get("re"), existing_and_run_matters)
        if dup:
            matters_skipped_duplicate.append({
                "table_index": rec["table_index"], "client_name": raw_name,
                "matter_name": matter_desc, "matched_existing_name": dup["name"],
                "score": dup["score"], "matched_source": dup["source"],
            })
            continue

        seq = matter_seq_by_client_key[client_key]
        matter_seq_by_client_key[client_key] = seq + 1
        matter_number = format_matter_number(client_number, seq) if client_number else None
        matters_by_client_key[client_key].append({"name": matter_desc, "source": "this_run"})
        matters_created.append({
            "table_index": rec["table_index"], "client_name": raw_name, "matter_name": matter_desc,
            "matter_number": matter_number, "client_id": client_id, "client_number": client_number,
            "file_name": rec.get("file_name"), "case_number": rec.get("case_number"),
            "action_done": rec.get("action_done"), "next_action": rec.get("next_action"),
            "latest_communication": rec.get("latest_communication"),
        })

    return {
        "clients": {
            "created": clients_created, "matched": clients_matched, "review": clients_review,
            "fuzzy_merged_within_run": clients_fuzzy_merged_within_run,
        },
        "matters": {
            "created": matters_created,
            "skipped_duplicate": matters_skipped_duplicate,
            "created_unlinked_pending_review": matters_unlinked,
            "client_only_no_matter": matters_client_only,
        },
        "unparseable_rows": unparseable,
    }


def _print_report(plan: dict, warnings: list) -> None:
    c, m = plan["clients"], plan["matters"]
    print("=" * 70)
    print("  NGM Master Case List Import — Preview (read-only)")
    print("=" * 70)
    print(f"  New clients:                {len(c['created'])}")
    print(f"  Matched existing clients:   {len(c['matched'])}")
    print(f"  Fuzzy-merged within this document: {len(c['fuzzy_merged_within_run'])}")
    print(f"  Ambiguous clients (review): {len(c['review'])}")
    print(f"  New matters:                {len(m['created'])}")
    print(f"  Skipped as likely duplicate:{len(m['skipped_duplicate'])}")
    print(f"  Matters unlinked (ambiguous client): {len(m['created_unlinked_pending_review'])}")
    print(f"  Client-only, no matter:     {len(m['client_only_no_matter'])}")
    print(f"  Unparseable rows:           {len(plan['unparseable_rows'])}")
    if warnings:
        print(f"  Unrecognized field labels:  {len(warnings)}")
    print()

    if c["created"]:
        print("  NEW CLIENTS:")
        for e in c["created"]:
            print(f"    [{e['table_index']}] {e['client_number']}  {e['name']}")
        print()

    if c["fuzzy_merged_within_run"]:
        print("  FUZZY-MERGED WITHIN THIS DOCUMENT (different text, treated as the same new client — sanity-check these):")
        for e in c["fuzzy_merged_within_run"]:
            print(f"    [{e['table_index']}] \"{e['name']}\" -> folded into \"{e['merged_into_name']}\" ({e['client_number']})")
        print()

    if c["review"]:
        print("  AMBIGUOUS CLIENTS (never auto-merged — needs your review):")
        for e in c["review"]:
            cands = ", ".join(f"{cc['full_name']} ({cc['id']})" for cc in e["candidates"])
            print(f"    [{e['table_index']}] \"{e['name']}\" could be: {cands}")
        print()

    if m["skipped_duplicate"]:
        print("  MATTERS SKIPPED AS LIKELY DUPLICATES:")
        for e in m["skipped_duplicate"]:
            print(f"    [{e['table_index']}] {e['client_name']}: \"{e['matter_name']}\"")
            print(f"        -> matched \"{e['matched_existing_name']}\" (score={e['score']}, {e['matched_source']})")
        print()

    if plan["unparseable_rows"]:
        print("  UNPARSEABLE ROWS (skipped, not guessed):")
        for e in plan["unparseable_rows"]:
            print(f"    [{e['table_index']}] {e['reason']}")
        print()

    if m["client_only_no_matter"]:
        print("  CLIENT-ONLY RECORDS (client processed, no matter — no case detail in source):")
        for e in m["client_only_no_matter"]:
            print(f"    [{e['table_index']}] {e['client_name']}")
        print()

    print(f"  Progress notes that would be created (one per new matter, capturing")
    print(f"  File Name/Case Number/Action done/Next action/Latest communication —")
    print(f"  matters has no free-text status field of its own): {len(m['created'])}")
    print(f"  Pass --no-progress-notes to skip this.")
    print()
    print("  No rows were modified. Re-run with `apply --yes` to write this plan.")


async def cmd_report(args):
    parsed = parse_case_list_docx(args.docx)
    conn = await asyncpg.connect(args.database_url)
    try:
        plan = await _build_plan(conn, args.initials, parsed["records"])
    finally:
        await conn.close()

    _print_report(plan, parsed["unrecognized_field_warnings"])

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({**plan, "unrecognized_field_warnings": parsed["unrecognized_field_warnings"]}, f, indent=2)
    print(f"\n  Full report written to: {args.out}")


async def cmd_apply(args):
    parsed = parse_case_list_docx(args.docx)
    conn = await asyncpg.connect(args.database_url)
    try:
        plan = await _build_plan(conn, args.initials, parsed["records"])

        if not args.yes:
            _print_report(plan, parsed["unrecognized_field_warnings"])
            print("\n  This is a preview only — re-run with --yes to apply.")
            return

        now = datetime.now(timezone.utc)
        async with conn.transaction():
            client_id_by_key = {}
            for e in plan["clients"]["created"]:
                new_id = uuid.uuid4()
                await conn.execute(
                    """INSERT INTO clients (id, firm_id, full_name, client_number, created_at, updated_at)
                       VALUES ($1,$2,$3,$4,$5,$6)""",
                    new_id, uuid.UUID(FIRM_ID), e["name"], e["client_number"], now, now,
                )
                client_id_by_key[e["client_number"]] = new_id

            for e in plan["matters"]["created"]:
                client_id = (
                    uuid.UUID(e["client_id"]) if e["client_id"]
                    else client_id_by_key.get(e["client_number"])
                )
                matter_id = uuid.uuid4()
                await conn.execute(
                    """INSERT INTO matters (id, firm_id, name, client_name, client_id, status,
                                            matter_number, created_at, last_activity)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                    matter_id, uuid.UUID(FIRM_ID), e["matter_name"], e["client_name"], client_id,
                    "Active", e["matter_number"], now, now,
                )
                if not args.no_progress_notes:
                    note_lines = []
                    if e.get("file_name"):
                        note_lines.append(f"File name (original): {e['file_name']}")
                    if e.get("case_number"):
                        note_lines.append(f"Case number: {e['case_number']}")
                    if e.get("action_done"):
                        note_lines.append(f"Action done: {e['action_done']}")
                    if e.get("next_action"):
                        note_lines.append(f"Next action: {e['next_action']}")
                    if e.get("latest_communication"):
                        note_lines.append(f"Latest communication: {e['latest_communication']}")
                    if note_lines:
                        await conn.execute(
                            """INSERT INTO progress_notes (matter_id, firm_id, text, author, created_at)
                               VALUES ($1,$2,$3,$4,$5)""",
                            matter_id, uuid.UUID(FIRM_ID), "\n".join(note_lines),
                            "Imported from master case list", now,
                        )

            for e in plan["matters"]["created_unlinked_pending_review"]:
                matter_id = uuid.uuid4()
                await conn.execute(
                    """INSERT INTO matters (id, firm_id, name, client_name, client_id, status, created_at, last_activity)
                       VALUES ($1,$2,$3,$4,NULL,$5,$6,$7)""",
                    matter_id, uuid.UUID(FIRM_ID), e["matter_name"], e["client_name"], "Active", now, now,
                )

        print(f"  Created {len(plan['clients']['created'])} client(s), "
              f"{len(plan['matters']['created'])} matter(s), "
              f"{len(plan['matters']['created_unlinked_pending_review'])} unlinked matter(s) for ambiguous clients.")
        print(f"  Skipped {len(plan['matters']['skipped_duplicate'])} likely-duplicate matter(s).")
        if plan["clients"]["review"]:
            print(f"  {len(plan['clients']['review'])} client name(s) need manual review — see the report above.")
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Import Nyari's master case-list Word doc into MutemoOS")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--docx", required=True, help="Path to the master case-list .docx file")
    parser.add_argument("--initials", default="NGM",
                         help="Initials prefix for any new clients (default: NGM)")
    parser.add_argument("--no-progress-notes", action="store_true",
                         help="Skip creating a progress note per new matter with the case narrative")
    sub = parser.add_subparsers(dest="command", required=True)

    p_report = sub.add_parser("report", help="Read-only: print the plan, touch nothing")
    p_report.add_argument("--out", default=DEFAULT_REPORT_PATH)
    p_report.set_defaults(func=cmd_report)

    p_apply = sub.add_parser("apply", help="Apply the import plan")
    p_apply.add_argument("--yes", action="store_true", help="Actually apply (default: preview only)")
    p_apply.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    if not args.database_url:
        print("ERROR: DATABASE_URL environment variable not set.")
        sys.exit(1)
    if not Path(args.docx).exists():
        print(f"ERROR: docx file not found: {args.docx}")
        sys.exit(1)

    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
