#!/usr/bin/env python3
"""
next_review_date Backfill — One-Time Migration for Existing Matters
=====================================================================
Sets next_review_date to a reasonable default (today + 30 days) on every
matter that has never had one set.

Why this exists: backend/matter_health.py's compute_matter_health()
correctly treats next_review_date IS NULL as more urgent than a lapsed-
but-once-set review date (confirmed with the user, not a bug) -- a
matter nobody has ever assessed is genuinely more concerning than one
that was assessed and later fell behind. But that rule, applied against
real data, showed exactly what was expected: 161/176 production matters
and most of staging's real matters predate the review safety net
(2026-08-30) and have never had next_review_date touched at all, so they
all land Red on day one. That's a one-time data-quality gap, not a
reason to soften the health rule -- this script is the fix, run once,
not a permanent part of the health logic.

30 days matches DEFAULT_REVIEW_INTERVAL_DAYS (backend/main.py) -- the
exact same default the app already applies at matter creation and on
every note/PATCH ("review this within 30 days of the last time someone
touched it"). Hardcoded here rather than imported: this project's other
standalone backfill scripts (e.g. backfill_practice_areas.py) don't
import from backend.main either, to avoid pulling in the full app's
import chain (DB pool setup, R2/Chroma client construction, etc.) just
for one constant.

Deliberately does NOT touch last_reviewed_date -- that column is a real,
honest record of when someone actually reviewed the matter (see its own
comment in run_migrations()); this backfill is an automated data-quality
fix, not a real review, so last_reviewed_date stays whatever it already
was (NULL, in every case this script touches, since nothing has ever
reviewed these matters either).

Excludes the sentinel "General / Firm Precedents" matter (NOT
is_sentinel) -- same convention as every other real matter listing in
this app (list_matters(), _fetch_matter_review_status_rows()), even
though backfill_practice_areas.py happened to omit this filter; it's a
system bucket, not a real client matter, and never surfaces in Matter
Health computation anyway.

Preview-first, matching this project's other report/apply scripts.
Recomputes fresh from live DB state each run (not from a saved report),
so it's naturally safe to re-run -- a matter that already has a
next_review_date (set by this script, or manually, or by the normal
create/note-add path) is simply skipped.

Usage:
    DATABASE_URL=postgresql://... python3 scripts/backfill_next_review_date.py report
    DATABASE_URL=postgresql://... python3 scripts/backfill_next_review_date.py apply --yes
"""

import argparse
import asyncio
import os
import sys
import uuid
from datetime import date, timedelta

try:
    import asyncpg
except ImportError:
    print("ERROR: asyncpg not installed. Run: pip install asyncpg")
    sys.exit(1)

FIRM_ID = os.environ.get("MUTEMO_FIRM_ID", "a1b2c3d4-0000-0000-0000-000000000001")
DEFAULT_REVIEW_INTERVAL_DAYS = 30  # mirrors backend/main.py's own constant


async def _build_plan(conn, today: date = None) -> dict:
    today = today or date.today()
    rows = await conn.fetch(
        "SELECT id, name, status, created_at FROM matters "
        "WHERE firm_id=$1 AND next_review_date IS NULL AND NOT is_sentinel "
        "ORDER BY created_at ASC",
        uuid.UUID(FIRM_ID),
    )
    default_date = today + timedelta(days=DEFAULT_REVIEW_INTERVAL_DAYS)
    to_apply = [
        {"id": str(r["id"]), "name": r["name"], "status": r["status"], "next_review_date": default_date}
        for r in rows
    ]
    return {"to_apply": to_apply, "default_date": default_date.isoformat()}


def _print_plan(plan: dict) -> None:
    print("=" * 70)
    print("  next_review_date Backfill — Preview (read-only)")
    print("=" * 70)
    print(f"  Default next_review_date to assign: {plan['default_date']}")
    print(f"  Matters to backfill: {len(plan['to_apply'])}")
    print()
    if plan["to_apply"]:
        for e in plan["to_apply"][:50]:
            print(f"    [{e['status']:16}] {e['name']}")
        if len(plan["to_apply"]) > 50:
            print(f"    ... and {len(plan['to_apply']) - 50} more")
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
        if not plan["to_apply"]:
            print("  Nothing to backfill — every matter already has a next_review_date.")
            return

        if not args.yes:
            _print_plan(plan)
            print("\n  This is a preview only — re-run with --yes to apply.")
            return

        async with conn.transaction():
            for e in plan["to_apply"]:
                await conn.execute(
                    "UPDATE matters SET next_review_date=$1 WHERE id=$2",
                    e["next_review_date"], uuid.UUID(e["id"]),
                )

        print(f"  Assigned next_review_date={plan['default_date']} to {len(plan['to_apply'])} matter(s).")
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Backfill next_review_date for existing matters")
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
