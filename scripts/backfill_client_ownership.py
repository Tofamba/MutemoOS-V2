#!/usr/bin/env python3
"""
Client Ownership Backfill — One-Time Migration for Existing Clients
=====================================================================
Assigns clients.created_by (the "My Clients" default-view filter's join
key) to every client that doesn't have one yet, by matching the initials
prefix already embedded in its client_number (backend/numbering.py's
"{initials}-{seq:03d}" convention, e.g. NGM-007 -> NGM) against the
current users.initials for the firm.

New clients get created_by stamped directly at creation time (see
create_client() in backend/main.py) from the real session user, which is
more reliable going forward — this script only exists to backfill
clients created before that column existed, or created while
AUTH_ENABLED was False (get_current_user() returns a synthetic user with
no real id in that case, so created_by is NULL even for clients created
today until real per-lawyer auth is turned on).

A client_number's initials prefix is a point-in-time formatted string,
not a live foreign key — it can't always be resolved back to exactly one
current user (the lawyer may have left the firm, or their initials may
have been disambiguated differently since). Matched only on a single,
confident (initials -> exactly one current user) hit; anything else
(zero matches, more than one — the latter shouldn't happen given
idx_users_firm_initials' uniqueness constraint, but checked defensively
anyway — or no client_number at all) goes to the review list and is
never guessed.

Preview-first, matching this project's other report/apply scripts
(see scripts/backfill_practice_areas.py). Recomputes fresh from live DB
state each run, so it's naturally safe to re-run — a client already
given a created_by (by this script, or stamped directly at creation) is
simply skipped.

Usage:
    DATABASE_URL=postgresql://... python3 scripts/backfill_client_ownership.py report
    DATABASE_URL=postgresql://... python3 scripts/backfill_client_ownership.py apply --yes
"""

import argparse
import asyncio
import os
import sys
import uuid

try:
    import asyncpg
except ImportError:
    print("ERROR: asyncpg not installed. Run: pip install asyncpg")
    sys.exit(1)

FIRM_ID = os.environ.get("MUTEMO_FIRM_ID", "a1b2c3d4-0000-0000-0000-000000000001")


async def _build_plan(conn) -> dict:
    clients = await conn.fetch(
        "SELECT id, full_name, client_number FROM clients "
        "WHERE firm_id=$1 AND created_by IS NULL ORDER BY created_at ASC",
        uuid.UUID(FIRM_ID),
    )
    users = await conn.fetch(
        "SELECT id, initials, display_name FROM users WHERE firm_id=$1 AND initials IS NOT NULL",
        uuid.UUID(FIRM_ID),
    )
    users_by_initials = {}
    for u in users:
        users_by_initials.setdefault(u["initials"], []).append(u)

    to_apply, review = [], []
    for c in clients:
        if not c["client_number"]:
            review.append({
                "id": str(c["id"]), "full_name": c["full_name"], "client_number": None,
                "reason": "no_client_number", "candidates": [],
            })
            continue

        initials = c["client_number"].rsplit("-", 1)[0]
        matches = users_by_initials.get(initials, [])
        if len(matches) == 1:
            to_apply.append({
                "id": str(c["id"]), "full_name": c["full_name"], "client_number": c["client_number"],
                "initials": initials, "user_id": str(matches[0]["id"]), "user_name": matches[0]["display_name"],
            })
        elif len(matches) > 1:
            review.append({
                "id": str(c["id"]), "full_name": c["full_name"], "client_number": c["client_number"],
                "reason": "ambiguous", "initials": initials,
                "candidates": [m["display_name"] for m in matches],
            })
        else:
            review.append({
                "id": str(c["id"]), "full_name": c["full_name"], "client_number": c["client_number"],
                "reason": "no_match", "initials": initials, "candidates": [],
            })

    return {"to_apply": to_apply, "review": review}


def _print_plan(plan: dict) -> None:
    print("=" * 70)
    print("  Client Ownership Backfill — Preview (read-only)")
    print("=" * 70)
    print(f"  Clients to assign created_by:  {len(plan['to_apply'])}")
    print(f"  Clients needing review:        {len(plan['review'])}")
    print()

    if plan["to_apply"]:
        print("  WOULD ASSIGN:")
        for e in plan["to_apply"]:
            print(f"    {e['client_number']:14} -> {e['user_name']}  ({e['full_name']})")
        print()

    if plan["review"]:
        print("  NEEDS REVIEW (not guessed):")
        for e in plan["review"]:
            if e["reason"] == "no_client_number":
                print(f"    NO CLIENT NUMBER  ({e['full_name']})")
            elif e["reason"] == "ambiguous":
                print(f"    AMBIGUOUS ({e['initials']} -> {', '.join(e['candidates'])})  "
                      f"[{e['client_number']}]  ({e['full_name']})")
            else:
                print(f"    NO MATCH ({e['initials']})  [{e['client_number']}]  ({e['full_name']})")
        print()
        print("  These stay unowned and will only appear under \"All Clients\", not anyone's "
              "\"My Clients\" default, until assigned manually.")

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
            print("  Nothing to backfill — every client already has a created_by.")
            return

        if not args.yes:
            _print_plan(plan)
            print("\n  This is a preview only — re-run with --yes to apply.")
            return

        async with conn.transaction():
            for e in plan["to_apply"]:
                await conn.execute(
                    "UPDATE clients SET created_by=$1 WHERE id=$2",
                    uuid.UUID(e["user_id"]), uuid.UUID(e["id"]),
                )

        print(f"  Assigned created_by to {len(plan['to_apply'])} client(s).")
        print(f"  {len(plan['review'])} client(s) left unowned for manual review — see the report above "
              f"(or re-run `report` to see it again).")
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Backfill clients.created_by for existing clients")
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
