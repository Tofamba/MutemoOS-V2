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
                      a human has reviewed and APPROVED it as one identity:
                      creates ONE Client record and links all of that
                      group's matters to it. Requires --yes; without it,
                      prints a preview and makes no changes.
  4. `apply-split`   — For a group a human has REJECTED as a false-positive
                      merge (its members are not all the same person/entity
                      — e.g. "Kudzai Madzingira" and "Kudzai Ndanga" only
                      clustered because they share the token "kudzai"):
                      creates one Client per distinct exact name within the
                      group instead of one merged Client. Members that are
                      literally the same name on multiple matters still
                      collapse into a single client (not duplicated).
                      Requires --yes, same preview-first convention.

Every command that actually CREATES a client (apply-auto, apply-group,
apply-split) requires --owner-user-id — the real users.id this batch of
clients belongs to. There's no sensible default: whoever runs this
script is very often not the lawyer the clients actually belong to (an
admin/developer running a one-time import on someone else's behalf is
the normal case, not the exception), so silently attributing them to
"whoever ran the script" would be actively wrong, not just imprecise.
Found the hard way (2026-08-30): 71 real clients backfilled this way
~2 months ago all landed at created_by=NULL because the original version
of this script never set it at all -- invisible in the data until a
frontend feature ("My Clients") started filtering on it.

Usage:
    DATABASE_URL=postgresql://... python3 scripts/migrate_clients.py report
    DATABASE_URL=postgresql://... python3 scripts/migrate_clients.py report --out review.json

    DATABASE_URL=postgresql://... python3 scripts/migrate_clients.py apply-auto \\
        --owner-user-id <real-users.id-uuid>
    DATABASE_URL=postgresql://... python3 scripts/migrate_clients.py apply-auto \\
        --owner-user-id <real-users.id-uuid> --yes

    DATABASE_URL=postgresql://... python3 scripts/migrate_clients.py apply-group \\
        --report-file review.json --group-index 0 --owner-user-id <real-users.id-uuid> \\
        --yes [--name "Override Name"]

    DATABASE_URL=postgresql://... python3 scripts/migrate_clients.py apply-split \\
        --report-file review.json --group-index 0 --owner-user-id <real-users.id-uuid> --yes

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
from backend.client_migration import group_client_names, split_review_group_by_exact_name  # noqa: E402

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
        owner_uuid = uuid.UUID(args.owner_user_id)
        for entry in auto:
            async with conn.transaction():
                client_row = await conn.fetchrow(
                    """
                    INSERT INTO clients (id, firm_id, full_name, created_by, created_at, updated_at)
                    VALUES ($1,$2,$3,$4,$5,$5)
                    RETURNING id
                    """,
                    uuid.uuid4(), uuid.UUID(FIRM_ID), entry["client_name"], owner_uuid, now,
                )
                await conn.execute(
                    "UPDATE matters SET client_id=$1 WHERE id=$2 AND firm_id=$3 AND client_id IS NULL",
                    client_row["id"], uuid.UUID(entry["matter_id"]), uuid.UUID(FIRM_ID),
                )
                created += 1
        print(f"  Created {created} Client record(s) and linked {created} matter(s).")
    finally:
        await conn.close()


def _load_group(args):
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
    return group


async def _filter_stale_members(conn, members: list) -> tuple:
    """
    Re-verify a group's members against current DB state before writing —
    the report may be stale if other groups/auto-resolves were applied
    since it was generated, or if a matter's client_name changed. Returns
    (to_link, skipped) matter_id lists; skips anything that no longer
    matches rather than silently overwriting an unrelated change. Shared
    by apply-group and apply-split.
    """
    to_link, skipped = [], []
    for m in members:
        row = await conn.fetchrow(
            "SELECT client_name, client_id FROM matters WHERE id=$1 AND firm_id=$2",
            uuid.UUID(m["matter_id"]), uuid.UUID(FIRM_ID),
        )
        if not row or row["client_id"] is not None or row["client_name"] != m["client_name"]:
            skipped.append(m["matter_id"])
            continue
        to_link.append(m["matter_id"])
    return to_link, skipped


async def _create_client_and_link(conn, full_name: str, matter_ids: list, now, owner_uuid: uuid.UUID) -> None:
    async with conn.transaction():
        client_row = await conn.fetchrow(
            """
            INSERT INTO clients (id, firm_id, full_name, created_by, created_at, updated_at)
            VALUES ($1,$2,$3,$4,$5,$5)
            RETURNING id
            """,
            uuid.uuid4(), uuid.UUID(FIRM_ID), full_name, owner_uuid, now,
        )
        for matter_id in matter_ids:
            await conn.execute(
                "UPDATE matters SET client_id=$1 WHERE id=$2 AND firm_id=$3 AND client_id IS NULL",
                client_row["id"], uuid.UUID(matter_id), uuid.UUID(FIRM_ID),
            )


async def cmd_apply_group(args):
    group = _load_group(args)
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
        to_link, skipped = await _filter_stale_members(conn, members)
        if not to_link:
            print("  Nothing left to apply — all members were already linked or changed since the report was generated.")
            return

        await _create_client_and_link(conn, client_name, to_link, datetime.now(timezone.utc), uuid.UUID(args.owner_user_id))

        print(f"\n  Created client \"{client_name}\" and linked {len(to_link)} matter(s).")
        if skipped:
            print(f"  Skipped {len(skipped)} matter(s) that had already changed since the report was generated: {skipped}")
    finally:
        await conn.close()


async def cmd_apply_split(args):
    group = _load_group(args)
    splits = split_review_group_by_exact_name(group["members"])

    print(f"  Group [{group['group_index']}] — will create {len(splits)} separate client(s) (rejected merge, split by exact name):")
    for s in splits:
        print(f"    - \"{s['full_name']}\" ({len(s['members'])} matter(s)):")
        for m in s["members"]:
            print(f"        matter {m['matter_id']} (was \"{m['client_name']}\")")

    if not args.yes:
        print("\n  This is a preview only — re-run with --yes to apply.")
        return

    conn = await asyncpg.connect(args.database_url)
    try:
        now = datetime.now(timezone.utc)
        owner_uuid = uuid.UUID(args.owner_user_id)
        created = 0
        linked = 0
        all_skipped = []
        for s in splits:
            to_link, skipped = await _filter_stale_members(conn, s["members"])
            all_skipped.extend(skipped)
            if not to_link:
                continue
            await _create_client_and_link(conn, s["full_name"], to_link, now, owner_uuid)
            created += 1
            linked += len(to_link)

        print(f"\n  Created {created} client(s) and linked {linked} matter(s).")
        if all_skipped:
            print(f"  Skipped {len(all_skipped)} matter(s) that had already changed since the report was generated: {all_skipped}")
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
    p_auto.add_argument("--owner-user-id", required=True,
                         help="Real users.id these clients belong to -- no default, see the module docstring for why")
    p_auto.add_argument("--yes", action="store_true", help="Actually apply (default: preview only)")
    p_auto.set_defaults(func=cmd_apply_auto)

    p_group = sub.add_parser("apply-group", help="Apply one human-approved review group from a report file")
    p_group.add_argument("--report-file", default=DEFAULT_REPORT_PATH)
    p_group.add_argument("--group-index", type=int, required=True)
    p_group.add_argument("--name", default=None, help="Override the suggested client full_name")
    p_group.add_argument("--owner-user-id", required=True,
                          help="Real users.id these clients belong to -- no default, see the module docstring for why")
    p_group.add_argument("--yes", action="store_true", help="Actually apply (default: preview only)")
    p_group.set_defaults(func=cmd_apply_group)

    p_split = sub.add_parser("apply-split", help="Split one human-rejected review group into separate per-name clients")
    p_split.add_argument("--report-file", default=DEFAULT_REPORT_PATH)
    p_split.add_argument("--group-index", type=int, required=True)
    p_split.add_argument("--owner-user-id", required=True,
                          help="Real users.id these clients belong to -- no default, see the module docstring for why")
    p_split.add_argument("--yes", action="store_true", help="Actually apply (default: preview only)")
    p_split.set_defaults(func=cmd_apply_split)

    args = parser.parse_args()

    if not args.database_url:
        print("ERROR: DATABASE_URL environment variable not set.")
        print("Usage: DATABASE_URL=postgresql://... python3 scripts/migrate_clients.py <report|apply-auto|apply-group|apply-split> ...")
        sys.exit(1)

    # Fail fast on a malformed --owner-user-id with a clear message,
    # rather than a raw traceback partway through a transaction. Whether
    # it's a REAL user in this firm is enforced by the DB itself (a real
    # foreign key, clients.created_by -> users.id) at write time.
    owner_user_id = getattr(args, "owner_user_id", None)
    if owner_user_id is not None:
        try:
            uuid.UUID(owner_user_id)
        except ValueError:
            print(f"ERROR: --owner-user-id is not a valid UUID: {owner_user_id!r}")
            sys.exit(1)

    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
