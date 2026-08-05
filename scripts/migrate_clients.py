#!/usr/bin/env python3
"""
Clients Backfill Migration
===========================
Backfills the new `clients` table and `matters.client_id` from the legacy
free-text `matters.client_name` column, for matters created before Client
became a first-class entity.

This is a THREE-STEP, human-gated process — nothing here runs silently or
automatically. Run it manually, review its output, and only then apply it:

  1. `report`      — READ-ONLY. Groups existing client_name values by
                      normalized/fuzzy-matched identity and writes a review
                      JSON. Does not touch any matter or client row.
  2. `apply-auto`  — Creates Client records (and sets matters.client_id) only
                      for names that appear EXACTLY ONCE across all matters —
                      there's no merge ambiguity for these, so no human
                      review step is needed before applying them.
  3. `apply-group` — For a single fuzzy-matched group of 2+ matters from the
                      report (e.g. "Huang" / "Mr. Huang" / "H. Huang"), after
                      a human has reviewed it: creates ONE Client record and
                      links all of that group's matters to it. Requires
                      --yes; without it, prints a preview and makes no
                      changes. Run once per approved group — reject or split
                      a bad grouping by simply not applying it (split it
                      into separate matter_id lists and invoke per-matter
                      instead, or handle those matters by hand).

Usage:
    DATABASE_URL=postgresql://... python3 scripts/migrate_clients.py report
    DATABASE_URL=postgresql://... python3 scripts/migrate_clients.py report --out review.json

    DATABASE_URL=postgresql://... python3 scripts/migrate_clients.py apply-auto
    DATABASE_URL=postgresql://... python3 scripts/migrate_clients.py apply-auto --yes

    DATABASE_URL=postgresql://... python3 scripts/migrate_clients.py apply-group \\
        --report-file review.json --group-index 0 --yes [--name "Override Name"]

The actual grouping/fuzzy-matching logic lives in backend/client_migration.py
(pure functions, no DB) so it can be unit-tested independently — see
tests/test_client_migration.py.
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
from backend.client_migration import group_client_names  # noqa: E402

# Fixed firm_id seeded in postgres_schema.sql — override via env var for a
# different deployment, same convention as scripts/migrate_to_postgres.py.
FIRM_ID = os.environ.get("MUTEMO_FIRM_ID", "a1b2c3d4-0000-0000-0000-000000000001")

DEFAULT_REPORT_PATH = "client_migration_review.json"


async def _fetch_unlinked_matters(conn) -> list:
    rows = await conn.fetch(
        """
        SELECT id, client_name FROM matters
        WHERE firm_id=$1 AND client_name IS NOT NULL AND client_name != ''
          AND client_id IS NULL
        ORDER BY created_at ASC
        """,
        uuid.UUID(FIRM_ID),
    )
    return [{"matter_id": str(r["id"]), "client_name": r["client_name"]} for r in rows]


async def cmd_report(args):
    conn = await asyncpg.connect(args.database_url)
    try:
        matters = await _fetch_unlinked_matters(conn)
    finally:
        await conn.close()

    result = group_client_names(matters)

    print(f"{'='*60}")
    print("  Clients Migration — Review Report (read-only)")
    print(f"{'='*60}")
    print(f"  Matters scanned:            {len(matters)}")
    print(f"  Auto-resolvable (unique):   {len(result['auto_resolve'])}")
    print(f"  Groups needing review:      {len(result['review_groups'])}")
    print(f"  Skipped (blank client_name):{len(result['skipped'])}")
    print()
    if result["review_groups"]:
        print("  Review groups:")
        for g in result["review_groups"]:
            names = ", ".join(f'"{m["client_name"]}"' for m in g["members"])
            print(f"    [{g['group_index']}] suggested: \"{g['suggested_name']}\" "
                  f"({len(g['members'])} matters) — {names}")
    print()
    print("  No rows were modified. Next steps:")
    print(f"    1. Review the groups above (or in {args.out}).")
    print("    2. Run `apply-auto` to create Clients for the unique names.")
    print("    3. For each approved group, run `apply-group --group-index N --yes`.")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"\n  Full report written to: {args.out}")


async def cmd_apply_auto(args):
    conn = await asyncpg.connect(args.database_url)
    try:
        matters = await _fetch_unlinked_matters(conn)
        result = group_client_names(matters)
        auto = result["auto_resolve"]

        if not auto:
            print("  Nothing to apply — no unique (unambiguous) client names found.")
            return

        if not args.yes:
            print(f"  Would create {len(auto)} Client record(s) and link the same "
                  f"number of matters:")
            for e in auto:
                print(f"    - \"{e['client_name']}\" (matter {e['matter_id']})")
            print("\n  Re-run with --yes to apply.")
            return

        created = 0
        now = datetime.now(timezone.utc)
        for entry in auto:
            async with conn.transaction():
                client_row = await conn.fetchrow(
                    """
                    INSERT INTO clients (id, firm_id, full_name, created_at, updated_at)
                    VALUES ($1,$2,$3,$4,$4)
                    RETURNING id
                    """,
                    uuid.uuid4(), uuid.UUID(FIRM_ID), entry["client_name"], now,
                )
                await conn.execute(
                    "UPDATE matters SET client_id=$1 WHERE id=$2 AND firm_id=$3 AND client_id IS NULL",
                    client_row["id"], uuid.UUID(entry["matter_id"]), uuid.UUID(FIRM_ID),
                )
                created += 1
        print(f"  Created {created} Client record(s) and linked {created} matter(s).")
    finally:
        await conn.close()


async def cmd_apply_group(args):
    report_path = Path(args.report_file)
    if not report_path.exists():
        print(f"ERROR: report file not found: {report_path}")
        sys.exit(1)
    with open(report_path, "r", encoding="utf-8") as f:
        report = json.load(f)

    group = next((g for g in report.get("review_groups", []) if g["group_index"] == args.group_index), None)
    if not group:
        print(f"ERROR: no review group with group_index={args.group_index} in {report_path}")
        sys.exit(1)

    client_name = args.name or group["suggested_name"]
    members = group["members"]

    print(f"  Group [{group['group_index']}] — will create ONE client: \"{client_name}\"")
    print(f"  Linking {len(members)} matter(s):")
    for m in members:
        print(f"    - matter {m['matter_id']} (was \"{m['client_name']}\")")

    if not args.yes:
        print("\n  This is a preview only — re-run with --yes to apply.")
        return

    conn = await asyncpg.connect(args.database_url)
    try:
        # Re-verify against current DB state — the report may be stale if
        # other groups/auto-resolves were applied since it was generated, or
        # if a matter's client_name changed. Skip anything that no longer
        # matches rather than silently overwriting an unrelated change.
        skipped = []
        to_link = []
        for m in members:
            row = await conn.fetchrow(
                "SELECT client_name, client_id FROM matters WHERE id=$1 AND firm_id=$2",
                uuid.UUID(m["matter_id"]), uuid.UUID(FIRM_ID),
            )
            if not row or row["client_id"] is not None or row["client_name"] != m["client_name"]:
                skipped.append(m["matter_id"])
                continue
            to_link.append(m["matter_id"])

        if not to_link:
            print("  Nothing left to apply — all members were already linked or changed since the report was generated.")
            return

        now = datetime.now(timezone.utc)
        async with conn.transaction():
            client_row = await conn.fetchrow(
                """
                INSERT INTO clients (id, firm_id, full_name, created_at, updated_at)
                VALUES ($1,$2,$3,$4,$4)
                RETURNING id
                """,
                uuid.uuid4(), uuid.UUID(FIRM_ID), client_name, now,
            )
            for matter_id in to_link:
                await conn.execute(
                    "UPDATE matters SET client_id=$1 WHERE id=$2 AND firm_id=$3 AND client_id IS NULL",
                    client_row["id"], uuid.UUID(matter_id), uuid.UUID(FIRM_ID),
                )

        print(f"\n  Created client \"{client_name}\" and linked {len(to_link)} matter(s).")
        if skipped:
            print(f"  Skipped {len(skipped)} matter(s) that had already changed since the report was generated: {skipped}")
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Backfill clients from matters.client_name")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    sub = parser.add_subparsers(dest="command", required=True)

    p_report = sub.add_parser("report", help="Read-only: write a review report, touch nothing")
    p_report.add_argument("--out", default=DEFAULT_REPORT_PATH)
    p_report.set_defaults(func=cmd_report)

    p_auto = sub.add_parser("apply-auto", help="Create clients for unambiguous (single-occurrence) names")
    p_auto.add_argument("--yes", action="store_true", help="Actually apply (default: preview only)")
    p_auto.set_defaults(func=cmd_apply_auto)

    p_group = sub.add_parser("apply-group", help="Apply one human-approved review group from a report file")
    p_group.add_argument("--report-file", default=DEFAULT_REPORT_PATH)
    p_group.add_argument("--group-index", type=int, required=True)
    p_group.add_argument("--name", default=None, help="Override the suggested client full_name")
    p_group.add_argument("--yes", action="store_true", help="Actually apply (default: preview only)")
    p_group.set_defaults(func=cmd_apply_group)

    args = parser.parse_args()

    if not args.database_url:
        print("ERROR: DATABASE_URL environment variable not set.")
        print("Usage: DATABASE_URL=postgresql://... python3 scripts/migrate_clients.py <report|apply-auto|apply-group> ...")
        sys.exit(1)

    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
