#!/usr/bin/env python3
"""
Split 5 clients incorrectly merged (or correctly left unlinked) by
scripts/import_ngm_case_list.py's apply run.

Source data for every fix comes directly from ngm_case_list_import_review.json
(the report that run produced) — never re-derived from memory or guessed
by name similarity. table_index identifies each record unambiguously (its
index into the source docx's tables, and into that JSON's own entries).

  FUZZY_MERGE_SPLITS (4 records) — clients.fuzzy_merged_within_run flagged
  these as folded into a different, wrong existing/newly-created client
  during the import (match_client_name's shared-significant-token fuzzy
  logic, e.g. "Estate Late X" matching "Estate Late Y" on the shared
  words "estate"/"late"). For each: create a new, separate client, then
  move that one matter from the wrong client to it.

  UNLINK_FIXES (1 record) — clients.review correctly flagged this as
  ambiguous and left it unlinked (client_id NULL on the matter, raw name
  preserved as matters.client_name) rather than guessing. Create the real
  client now and link the existing matter to it.

Every DB write is preceded by a fresh, direct lookup + verification that
the actual current row (matter_number for the merge cases; client_name +
matter name for the unlinked case) still matches what the review JSON
recorded — if the database has drifted since the import (already fixed,
matter edited, etc.), this aborts with a clear error rather than silently
acting on stale assumptions.

Preview-first, same convention as import_ngm_case_list.py and the earlier
backfill scripts.

Usage:
    DATABASE_URL=postgresql://... python3 scripts/split_ngm_merged_clients.py report
    DATABASE_URL=postgresql://... python3 scripts/split_ngm_merged_clients.py apply --yes
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import asyncpg
except ImportError:
    print("ERROR: asyncpg not installed. Run: pip install asyncpg")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.numbering import next_sequence, format_client_number, format_matter_number  # noqa: E402

FIRM_ID = os.environ.get("MUTEMO_FIRM_ID", "a1b2c3d4-0000-0000-0000-000000000001")
DEFAULT_REVIEW_JSON = "ngm_case_list_import_review.json"

# Wrongly fuzzy-merged during the import — split into a new client, moving
# that one matter along with it.
FUZZY_MERGE_SPLITS = [43, 62, 81, 83]
# Correctly left unlinked (ambiguous, never guessed) — create the real
# client now and link the existing matter to it.
UNLINK_FIXES = [82]


def _load_review(review_json_path: str) -> dict:
    with open(review_json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _find_by_table_index(entries: list, table_index: int):
    for e in entries:
        if e["table_index"] == table_index:
            return e
    return None


def build_split_plan(review: dict) -> list:
    """
    Pulls the exact source data for each of the 5 records straight out of
    the review JSON — never re-derived from name similarity or memory.
    Raises if a record's expected entries aren't both present, rather than
    proceeding on a partial/guessed match.
    """
    items = []
    for ti in FUZZY_MERGE_SPLITS:
        merge_entry = _find_by_table_index(review["clients"]["fuzzy_merged_within_run"], ti)
        matter_entry = _find_by_table_index(review["matters"]["created"], ti)
        if not merge_entry or not matter_entry:
            raise ValueError(
                f"table_index {ti}: could not find both a clients.fuzzy_merged_within_run entry "
                f"and a matters.created entry in the review JSON — aborting rather than guessing."
            )
        items.append({
            "kind": "merged",
            "table_index": ti,
            "new_client_name": merge_entry["name"],
            "wrongly_merged_into": merge_entry["merged_into_name"],
            "wrong_client_number": merge_entry["client_number"],
            "matter_name": matter_entry["matter_name"],
            "current_matter_number": matter_entry["matter_number"],
        })

    for ti in UNLINK_FIXES:
        review_entry = _find_by_table_index(review["clients"]["review"], ti)
        matter_entry = _find_by_table_index(review["matters"]["created_unlinked_pending_review"], ti)
        if not review_entry or not matter_entry:
            raise ValueError(
                f"table_index {ti}: could not find both a clients.review entry and a "
                f"matters.created_unlinked_pending_review entry in the review JSON."
            )
        items.append({
            "kind": "unlinked",
            "table_index": ti,
            "new_client_name": matter_entry["client_name"],
            "matter_name": matter_entry["matter_name"],
        })

    return items


async def _resolve_db_actions(conn, items: list) -> list:
    """
    For each plan item, looks up the real matter row in the live database
    and verifies it still matches what the review JSON recorded, before
    anything is touched. Also assigns each new client the next available
    NGM- number, computed fresh from current DB state.
    """
    existing_client_rows = await conn.fetch(
        "SELECT client_number FROM clients WHERE firm_id=$1 AND client_number LIKE 'NGM-%'",
        uuid.UUID(FIRM_ID),
    )
    next_seq = next_sequence([r["client_number"] for r in existing_client_rows], "NGM")

    resolved = []
    for item in items:
        if item["kind"] == "merged":
            row = await conn.fetchrow(
                "SELECT id, name, client_id FROM matters WHERE firm_id=$1 AND matter_number=$2",
                uuid.UUID(FIRM_ID), item["current_matter_number"],
            )
            if not row:
                raise ValueError(
                    f"[{item['table_index']}] matter_number {item['current_matter_number']!r} not found — "
                    f"has this already been split, or has the database changed since the review was generated?"
                )
            if row["name"] != item["matter_name"]:
                raise ValueError(
                    f"[{item['table_index']}] matter {item['current_matter_number']} name mismatch: "
                    f"database has {row['name']!r}, review JSON expected {item['matter_name']!r}."
                )
            wrong_client = await conn.fetchrow(
                "SELECT client_number, full_name FROM clients WHERE id=$1", row["client_id"]
            )
            if not wrong_client or wrong_client["client_number"] != item["wrong_client_number"]:
                raise ValueError(
                    f"[{item['table_index']}] matter {item['current_matter_number']} is not currently linked "
                    f"to {item['wrongly_merged_into']} ({item['wrong_client_number']}) as the review JSON "
                    f"recorded — found {dict(wrong_client) if wrong_client else None} instead."
                )
            item["matter_id"] = str(row["id"])
        else:  # unlinked
            rows = await conn.fetch(
                "SELECT id FROM matters WHERE firm_id=$1 AND client_id IS NULL AND client_name=$2 AND name=$3",
                uuid.UUID(FIRM_ID), item["new_client_name"], item["matter_name"],
            )
            if len(rows) != 1:
                raise ValueError(
                    f"[{item['table_index']}] expected exactly 1 unlinked matter matching "
                    f"client_name={item['new_client_name']!r} name={item['matter_name']!r}, found {len(rows)}."
                )
            item["matter_id"] = str(rows[0]["id"])

        item["new_client_number"] = format_client_number("NGM", next_seq)
        next_seq += 1
        item["new_matter_number"] = format_matter_number(item["new_client_number"], 1)
        resolved.append(item)
    return resolved


def _print_plan(items: list) -> None:
    print("=" * 70)
    print("  NGM Client Split — Preview (read-only)")
    print("=" * 70)
    for item in items:
        print(f"  [{item['table_index']}] \"{item['new_client_name']}\" -> {item['new_client_number']} (new client)")
        if item["kind"] == "merged":
            print(f"      matter \"{item['matter_name']}\" moves: "
                  f"{item['wrong_client_number']} ({item['wrongly_merged_into']}) -> {item['new_client_number']}")
            print(f"      matter_number: {item['current_matter_number']} -> {item['new_matter_number']}")
        else:
            print(f"      matter \"{item['matter_name']}\" links: (unlinked) -> {item['new_client_number']}")
            print(f"      matter_number: (none) -> {item['new_matter_number']}")
        print()
    print("  No rows were modified. Re-run with `apply --yes` to write this plan.")


async def cmd_report(args):
    review = _load_review(args.review_json)
    items = build_split_plan(review)
    conn = await asyncpg.connect(args.database_url)
    try:
        resolved = await _resolve_db_actions(conn, items)
    finally:
        await conn.close()
    _print_plan(resolved)


async def cmd_apply(args):
    review = _load_review(args.review_json)
    items = build_split_plan(review)
    conn = await asyncpg.connect(args.database_url)
    try:
        resolved = await _resolve_db_actions(conn, items)

        if not args.yes:
            _print_plan(resolved)
            print("\n  This is a preview only — re-run with --yes to apply.")
            return

        now = datetime.now(timezone.utc)
        async with conn.transaction():
            for item in resolved:
                new_id = uuid.uuid4()
                await conn.execute(
                    """INSERT INTO clients (id, firm_id, full_name, client_number, created_at, updated_at)
                       VALUES ($1,$2,$3,$4,$5,$6)""",
                    new_id, uuid.UUID(FIRM_ID), item["new_client_name"], item["new_client_number"], now, now,
                )
                await conn.execute(
                    """UPDATE matters SET client_id=$1, client_name=$2, matter_number=$3, last_activity=$4
                       WHERE id=$5""",
                    new_id, item["new_client_name"], item["new_matter_number"], now, uuid.UUID(item["matter_id"]),
                )

        print(f"  Split {len(resolved)} client(s): created each new client and moved/linked its matter.")
        for item in resolved:
            print(f"    [{item['table_index']}] {item['new_client_name']} -> {item['new_client_number']}")
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Split clients incorrectly merged (or left unlinked) by import_ngm_case_list.py"
    )
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--review-json", default=DEFAULT_REVIEW_JSON,
                         help="Path to the review JSON from the original import run")
    sub = parser.add_subparsers(dest="command", required=True)

    p_report = sub.add_parser("report", help="Read-only: print the plan, touch nothing")
    p_report.set_defaults(func=cmd_report)

    p_apply = sub.add_parser("apply", help="Apply the split plan")
    p_apply.add_argument("--yes", action="store_true", help="Actually apply (default: preview only)")
    p_apply.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    if not args.database_url:
        print("ERROR: DATABASE_URL environment variable not set.")
        sys.exit(1)
    if not Path(args.review_json).exists():
        print(f"ERROR: review JSON not found: {args.review_json}")
        sys.exit(1)

    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
