#!/usr/bin/env python3
"""
Practice Area Backfill — One-Time Migration for Existing Matters
====================================================================
Assigns practice_area (backend/practice_areas.py's fixed PRACTICE_AREAS
list) to every matter that doesn't have one yet.

There's no separate "Law" column stored anywhere to backfill from — the
NGM case-list import (scripts/import_ngm_case_list.py) never stored the
source document's "Law" field on its own; it only used it to help build
each matter's free-text name ("{Re} — {Law}"), and the progress_notes it
created capture File Name/Case Number/Action done/Next action/Latest
communication — NOT Law (checked directly, it isn't in there). The
original docx is re-parseable, but turned out to be unnecessary: every
matter firm-wide already carries its own classification signal on
`matters.name` itself, via the same "{Reference} — {description}"
convention used everywhere in this app (bulk_onboard_from_excel's own
upload format, the New Matter form's placeholder text, and the NGM
import). backend.practice_areas.extract_classification_text() pulls the
description half out of that convention (falling back to the whole name
when there's no separator) — one uniform source for every matter
regardless of how it was created.

Classification (backend.practice_areas.classify_practice_area) matches
keyword phrases per category against that text. Exactly one category
matching is treated as confident enough to apply automatically; zero or
2+ categories matching goes to the review list rather than being guessed
— see that module's docstring for why (and the worked "Debt collection/
fraud" example that legitimately matches two categories at once).

Preview-first, matching this project's other report/apply scripts.
Recomputes fresh from live DB state each run (not from a saved report),
so it's naturally safe to re-run — a matter already given a
practice_area (by this script or manually) is simply skipped.

Usage:
    DATABASE_URL=postgresql://... python3 scripts/backfill_practice_areas.py report
    DATABASE_URL=postgresql://... python3 scripts/backfill_practice_areas.py apply --yes
"""

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

try:
    import asyncpg
except ImportError:
    print("ERROR: asyncpg not installed. Run: pip install asyncpg")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.practice_areas import classify_practice_area, extract_classification_text  # noqa: E402

FIRM_ID = os.environ.get("MUTEMO_FIRM_ID", "a1b2c3d4-0000-0000-0000-000000000001")


async def _build_plan(conn) -> dict:
    rows = await conn.fetch(
        "SELECT id, name FROM matters WHERE firm_id=$1 AND practice_area IS NULL ORDER BY created_at ASC",
        uuid.UUID(FIRM_ID),
    )

    to_apply, review = [], []
    for r in rows:
        classification_text = extract_classification_text(r["name"])
        result = classify_practice_area(classification_text)
        if result["status"] == "matched":
            to_apply.append({
                "id": str(r["id"]), "name": r["name"],
                "classification_text": classification_text, "practice_area": result["practice_area"],
            })
        elif result["status"] == "ambiguous":
            review.append({
                "id": str(r["id"]), "name": r["name"], "classification_text": classification_text,
                "reason": "ambiguous", "candidates": result["candidates"],
            })
        else:
            review.append({
                "id": str(r["id"]), "name": r["name"], "classification_text": classification_text,
                "reason": "no_match", "candidates": [],
            })

    return {"to_apply": to_apply, "review": review}


def _print_plan(plan: dict) -> None:
    print("=" * 70)
    print("  Practice Area Backfill — Preview (read-only)")
    print("=" * 70)
    print(f"  Matters to auto-categorize:  {len(plan['to_apply'])}")
    print(f"  Matters needing review:      {len(plan['review'])}")
    print()

    if plan["to_apply"]:
        print("  WOULD ASSIGN:")
        for e in plan["to_apply"]:
            print(f"    {e['practice_area']:22} <- \"{e['classification_text']}\"  ({e['name']})")
        print()

    if plan["review"]:
        print("  NEEDS REVIEW (not guessed):")
        for e in plan["review"]:
            if e["reason"] == "ambiguous":
                print(f"    AMBIGUOUS ({', '.join(e['candidates'])}) <- \"{e['classification_text']}\"  ({e['name']})")
            else:
                print(f"    NO MATCH <- \"{e['classification_text']}\"  ({e['name']})")
        print()

    print("  No rows were modified. Re-run with `apply --yes` to write this plan.")


async def cmd_report(args):
    conn = await asyncpg.connect(args.database_url)
    try:
        plan = await _build_plan(conn)
    finally:
        await conn.close()
    _print_plan(plan)


async def cmd_apply(args):
    conn = await asyncpg.connect(args.database_url)
    try:
        plan = await _build_plan(conn)
        if not plan["to_apply"] and not plan["review"]:
            print("  Nothing to backfill — every matter already has a practice_area.")
            return

        if not args.yes:
            _print_plan(plan)
            print("\n  This is a preview only — re-run with --yes to apply.")
            return

        async with conn.transaction():
            for e in plan["to_apply"]:
                await conn.execute(
                    "UPDATE matters SET practice_area=$1 WHERE id=$2",
                    e["practice_area"], uuid.UUID(e["id"]),
                )

        print(f"  Assigned practice_area to {len(plan['to_apply'])} matter(s).")
        print(f"  {len(plan['review'])} matter(s) left for manual review — see the report above "
              f"(or re-run `report` to see it again).")
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Backfill practice_area for existing matters")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    sub = parser.add_subparsers(dest="command", required=True)

    p_report = sub.add_parser("report", help="Read-only: print the plan, touch nothing")
    p_report.set_defaults(func=cmd_report)

    p_apply = sub.add_parser("apply", help="Apply the backfill plan")
    p_apply.add_argument("--yes", action="store_true", help="Actually apply (default: preview only)")
    p_apply.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    if not args.database_url:
        print("ERROR: DATABASE_URL environment variable not set.")
        sys.exit(1)

    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
