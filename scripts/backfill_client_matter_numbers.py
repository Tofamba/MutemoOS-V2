#!/usr/bin/env python3
"""
Client/Matter Numbering — One-Time Backfill for Pre-Numbering Clients
========================================================================
Assigns client_number/matter_number (backend/numbering.py) to clients (and
their matters) that predate this feature — right now that's every existing
client, since client_number didn't exist until now. None of them recorded
which lawyer created them (the Client entity existed before per-lawyer
numbering did), so which initials prefix to backfill under is a human
decision, not something this script infers.

Confirmed 2026-08-07: all existing clients trace to Nyaradzo Gilbertina
Maphosa (NGM) — the default. Pass --initials to backfill under someone else
if that's ever not true.

Preview-first, matching this project's migrate_clients.py report/--yes
convention:
    report        — READ-ONLY. Prints the exact NGM-001..NGM-0NN mapping
                    (clients ordered by created_at, oldest first) and each
                    client's matter numbering (that client's matters
                    ordered by created_at). Touches nothing.
    apply --yes   — Applies exactly that mapping. Without --yes, prints the
                    same preview and makes no changes.

Only rows with client_number/matter_number IS NULL are touched, and the
mapping is recomputed fresh from live DB state each run (not read back from
a saved report) — safe to re-run; a client/matter numbered since the last
run (normal client_number is no longer NULL) is simply left alone.

Usage:
    DATABASE_URL=postgresql://... python3 scripts/backfill_client_matter_numbers.py report
    DATABASE_URL=postgresql://... python3 scripts/backfill_client_matter_numbers.py apply --yes
    DATABASE_URL=postgresql://... python3 scripts/backfill_client_matter_numbers.py report --initials OM
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
from backend.numbering import next_sequence, format_client_number, format_matter_number  # noqa: E402

FIRM_ID = os.environ.get("MUTEMO_FIRM_ID", "a1b2c3d4-0000-0000-0000-000000000001")


async def _build_mapping(conn, initials: str) -> list:
    client_rows = await conn.fetch(
        """SELECT id, full_name FROM clients
           WHERE firm_id=$1 AND client_number IS NULL
           ORDER BY created_at ASC""",
        uuid.UUID(FIRM_ID),
    )
    existing_numbers = await conn.fetch(
        "SELECT client_number FROM clients WHERE firm_id=$1 AND client_number LIKE $2",
        uuid.UUID(FIRM_ID), f"{initials}-%",
    )
    seq = next_sequence([r["client_number"] for r in existing_numbers], initials)

    mapping = []
    for c in client_rows:
        client_number = format_client_number(initials, seq)
        seq += 1
        matter_rows = await conn.fetch(
            """SELECT id, name FROM matters
               WHERE firm_id=$1 AND client_id=$2 AND matter_number IS NULL
               ORDER BY created_at ASC""",
            uuid.UUID(FIRM_ID), c["id"],
        )
        matters = []
        mseq = 1
        for m in matter_rows:
            matters.append({
                "id": str(m["id"]), "name": m["name"],
                "matter_number": format_matter_number(client_number, mseq),
            })
            mseq += 1
        mapping.append({
            "id": str(c["id"]), "full_name": c["full_name"],
            "client_number": client_number, "matters": matters,
        })
    return mapping


def _print_mapping(mapping: list) -> None:
    for c in mapping:
        print(f"  {c['client_number']}  {c['full_name']}")
        for m in c["matters"]:
            print(f"      {m['matter_number']}  {m['name']}")
    print()
    total_matters = sum(len(c["matters"]) for c in mapping)
    print(f"  {len(mapping)} client(s), {total_matters} matter(s).")


async def cmd_report(args):
    conn = await asyncpg.connect(args.database_url)
    try:
        mapping = await _build_mapping(conn, args.initials)
    finally:
        await conn.close()

    print("=" * 60)
    print("  Client/Matter Numbering Backfill — Preview (read-only)")
    print("=" * 60)
    if not mapping:
        print("  Nothing to backfill — every client already has a client_number.")
        return
    _print_mapping(mapping)
    print("  No rows were modified. Re-run with `apply --yes` to write this mapping.")


async def cmd_apply(args):
    conn = await asyncpg.connect(args.database_url)
    try:
        mapping = await _build_mapping(conn, args.initials)
        if not mapping:
            print("  Nothing to backfill — every client already has a client_number.")
            return

        if not args.yes:
            print("  Would assign:")
            _print_mapping(mapping)
            print("  This is a preview only — re-run with --yes to apply.")
            return

        async with conn.transaction():
            for c in mapping:
                await conn.execute(
                    "UPDATE clients SET client_number=$1 WHERE id=$2",
                    c["client_number"], uuid.UUID(c["id"]),
                )
                for m in c["matters"]:
                    await conn.execute(
                        "UPDATE matters SET matter_number=$1 WHERE id=$2",
                        m["matter_number"], uuid.UUID(m["id"]),
                    )

        total_matters = sum(len(c["matters"]) for c in mapping)
        print(f"  Assigned {len(mapping)} client_number(s) and {total_matters} matter_number(s).")
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Backfill client_number/matter_number for pre-numbering clients"
    )
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument(
        "--initials", default="NGM",
        help="Initials prefix to backfill under (default: NGM — see module docstring)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_report = sub.add_parser("report", help="Read-only: print the mapping, touch nothing")
    p_report.set_defaults(func=cmd_report)

    p_apply = sub.add_parser("apply", help="Apply the backfill mapping")
    p_apply.add_argument("--yes", action="store_true", help="Actually apply (default: preview only)")
    p_apply.set_defaults(func=cmd_apply)

    args = parser.parse_args()

    if not args.database_url:
        print("ERROR: DATABASE_URL environment variable not set.")
        print("Usage: DATABASE_URL=postgresql://... python3 scripts/backfill_client_matter_numbers.py <report|apply> ...")
        sys.exit(1)

    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
