#!/usr/bin/env python3
"""
Staging Seed Data
==================
Populates a fresh staging database with a synthetic firm, lawyers, clients,
and matters — so staging is meaningful to test against instead of an empty
database. Everything here is fictional: names, phone numbers, matter
narratives. Nothing is copied or derived from real Sawyer & Mkushi data,
anonymized or otherwise.

Idempotent: keyed on the synthetic firm's fixed client_number/matter_number
values, so re-running this against a staging DB that's already seeded skips
existing rows instead of duplicating them (ON CONFLICT DO NOTHING /
existence checks) — safe to re-run after a staging DB reset or a partial
first run.

This does NOT run itself against production — it takes whatever
DATABASE_URL/MUTEMO_FIRM_ID/MUTEMO_FIRM_NAME you give it. Point it at
staging explicitly; nothing here infers or defaults to production.

Usage:
    DATABASE_URL=<staging Postgres URL> \\
    MUTEMO_FIRM_ID=<staging firm id, matches the Railway service's env var> \\
    MUTEMO_FIRM_NAME="Chademana & Rusike Legal Practitioners" \\
    python3 scripts/seed_staging_data.py
"""

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import asyncpg
except ImportError:
    print("ERROR: asyncpg not installed. Run: pip install asyncpg")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.numbering import format_client_number, format_matter_number  # noqa: E402

FIRM_ID = os.environ.get("MUTEMO_FIRM_ID")
FIRM_NAME = os.environ.get("MUTEMO_FIRM_NAME", "Chademana & Rusike Legal Practitioners")
FIRM_CITY = os.environ.get("MUTEMO_FIRM_CITY", "Bulawayo")

# ── Synthetic lawyers ────────────────────────────────────────────────────
# Fictional names, deliberately unrelated to Sawyer & Mkushi's real staff —
# these are the "fixed initials" for this synthetic firm, same convention
# as NGM/OM/JRT/HPM/FS are for the real firm.
LAWYERS = [
    {"display_name": "Tanaka Chademana", "initials": "TC", "role": "partner", "phone": "+263771000001", "email": "tanaka@chademanarusike.example"},
    {"display_name": "Rufaro Rusike", "initials": "RR", "role": "partner", "phone": "+263771000002", "email": "rufaro@chademanarusike.example"},
    {"display_name": "Blessing Nyathi", "initials": "BN", "role": "associate", "phone": "+263771000003", "email": "blessing@chademanarusike.example"},
    {"display_name": "Farirai Gwenzi", "initials": "FG", "role": "associate", "phone": "+263771000004", "email": "farirai@chademanarusike.example"},
]

# ── Synthetic clients + matters ──────────────────────────────────────────
# Covers the practice areas actually in use (backend/practice_areas.py):
# Estate/Inheritance, Conveyancing/Property, Debt Collection, and
# Family/Matrimonial as the primary focus (per the staging brief), plus a
# couple of others for broader coverage. lawyer_initials picks which
# synthetic lawyer's numbering sequence the client is created under.
CLIENTS = [
    {"lawyer_initials": "TC", "full_name": "Estate Late Tendekai Mafuta", "phone": "+263772100001",
     "matters": [{"name": "Estate Late Tendekai Mafuta — Winding up estate, letters of administration",
                  "practice_area": "Estate/Inheritance", "status": "Active"}]},
    {"lawyer_initials": "TC", "full_name": "Chiedza Bvumbe", "phone": "+263772100002",
     "matters": [{"name": "Estate Late Bvumbe — Administration and distribution account",
                  "practice_area": "Estate/Inheritance", "status": "Awaiting Court"}]},
    {"lawyer_initials": "RR", "full_name": "Kudakwashe Marufu", "phone": "+263772100003",
     "matters": [{"name": "Marufu Stand 88 Hillside — Sale agreement and transfer",
                  "practice_area": "Conveyancing/Property", "status": "Active"}]},
    {"lawyer_initials": "RR", "full_name": "Sunshine Properties (Pvt) Ltd", "phone": "+263772100004",
     "contact_person": "Nomvula Sibanda (Company Secretary)",
     "matters": [{"name": "Sunshine Properties — Commercial property lease dispute",
                  "practice_area": "Conveyancing/Property", "status": "Awaiting Court"}]},
    {"lawyer_initials": "BN", "full_name": "Blue Ridge Traders (Pvt) Ltd", "phone": "+263772100005",
     "contact_person": "Kelvin Moyo (Director)",
     "matters": [{"name": "Blue Ridge Traders — Debt collection against Musademba Enterprises",
                  "practice_area": "Debt Collection", "status": "Active"}]},
    {"lawyer_initials": "BN", "full_name": "Tapiwa Nyoni", "phone": "+263772100006",
     "matters": [{"name": "Nyoni — Recovery of loan advance",
                  "practice_area": "Debt Collection", "status": "On Hold"}]},
    {"lawyer_initials": "FG", "full_name": "Rutendo Chikwavaire", "phone": "+263772100007",
     "matters": [{"name": "Chikwavaire v Chikwavaire — Divorce and distribution of matrimonial property",
                  "practice_area": "Family/Matrimonial", "status": "Active"}]},
    {"lawyer_initials": "FG", "full_name": "Believe Mangoma", "phone": "+263772100008",
     "matters": [{"name": "Mangoma — Custody and access application",
                  "practice_area": "Family/Matrimonial", "status": "Awaiting Client"}]},
    {"lawyer_initials": "TC", "full_name": "Mafuta Family Trust", "phone": "+263772100009",
     "matters": [{"name": "Mafuta Family Trust — Trust registration",
                  "practice_area": "Trust", "status": "Active"}]},
    {"lawyer_initials": "RR", "full_name": "Tinashe Museba", "phone": "+263772100010",
     "matters": [{"name": "Museba v Golden Hills Mine — Unfair dismissal",
                  "practice_area": "Labour", "status": "Active"}]},
]


async def seed(conn):
    if not FIRM_ID:
        raise SystemExit("MUTEMO_FIRM_ID is required — point this at the staging firm's id explicitly.")
    firm_id = uuid.UUID(FIRM_ID)

    firm_row = await conn.fetchrow("SELECT id, name FROM firms WHERE id=$1", firm_id)
    if not firm_row:
        raise SystemExit(
            f"No firm row found for {firm_id}. Start the staging app once first (run_migrations() seeds "
            f"the firms row from MUTEMO_FIRM_ID/MUTEMO_FIRM_NAME on startup) before running this script."
        )
    print(f"Seeding into firm: {firm_row['name']} ({firm_id})")

    now = datetime.now(timezone.utc)
    lawyer_id_by_initials = {}

    for lawyer in LAWYERS:
        existing = await conn.fetchrow(
            "SELECT id FROM users WHERE firm_id=$1 AND phone=$2", firm_id, lawyer["phone"]
        )
        if existing:
            lawyer_id_by_initials[lawyer["initials"]] = existing["id"]
            print(f"  lawyer already exists: {lawyer['display_name']} ({lawyer['initials']})")
            continue
        new_id = uuid.uuid4()
        await conn.execute(
            """INSERT INTO users (id, firm_id, phone, email, display_name, role, initials, is_active, created_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,TRUE,$8)""",
            new_id, firm_id, lawyer["phone"], lawyer["email"], lawyer["display_name"],
            lawyer["role"], lawyer["initials"], now,
        )
        lawyer_id_by_initials[lawyer["initials"]] = new_id
        print(f"  created lawyer: {lawyer['display_name']} ({lawyer['initials']})")

    next_seq_by_initials = {}
    for client in CLIENTS:
        existing = await conn.fetchrow(
            "SELECT id FROM clients WHERE firm_id=$1 AND full_name=$2", firm_id, client["full_name"]
        )
        if existing:
            print(f"  client already exists, skipping: {client['full_name']}")
            continue

        initials = client["lawyer_initials"]
        seq = next_seq_by_initials.get(initials, 1)
        client_number = format_client_number(initials, seq)
        next_seq_by_initials[initials] = seq + 1

        client_id = uuid.uuid4()
        await conn.execute(
            """INSERT INTO clients (id, firm_id, full_name, phone, contact_person, client_number,
                                    created_at, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$7)""",
            client_id, firm_id, client["full_name"], client.get("phone"),
            client.get("contact_person"), client_number, now,
        )
        print(f"  created client: {client_number}  {client['full_name']}")

        for i, matter in enumerate(client["matters"], start=1):
            matter_number = format_matter_number(client_number, i)
            await conn.execute(
                """INSERT INTO matters (id, firm_id, name, client_name, client_id, status,
                                        practice_area, matter_number, created_by, created_at, last_activity)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$10)""",
                uuid.uuid4(), firm_id, matter["name"], client["full_name"], client_id,
                matter["status"], matter["practice_area"], matter_number,
                lawyer_id_by_initials[initials], now - timedelta(days=i * 3),
            )
            print(f"      matter: {matter_number}  {matter['name']}  [{matter['practice_area']}]")

    print("\nDone.")


async def main():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable not set.")
        print("This must point at the STAGING database — never run this against production.")
        sys.exit(1)

    conn = await asyncpg.connect(database_url)
    try:
        await seed(conn)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
