#!/usr/bin/env python3
"""
MutemoOS v1 → v2 Data Migration Script
=======================================
Reads mutemo_state.json (v1 JSON persistence) and inserts all data
into the PostgreSQL database provisioned for v2.

Usage:
    DATABASE_URL=postgresql://... python3 migrate_to_postgres.py [path/to/mutemo_state.json]

The script is idempotent — safe to run multiple times. It uses
INSERT ... ON CONFLICT DO NOTHING for all records, so re-running
will not duplicate data.

Prerequisites:
    pip install asyncpg

What gets migrated:
    ✓ Matters (all fields including status, internal_ref, client_name)
    ✓ Progress notes (with author attribution)
    ✓ Documents (metadata only — text chunks are re-indexed into ChromaDB by the app)
    ✓ Calendar events
    ✓ ZLR entries (metadata + raw_text)
    ✓ Legal updates (metadata)
    ✓ Reminder settings

What is NOT migrated:
    - ChromaDB vector embeddings (the app rebuilds these automatically via /api/admin/reindex)
    - Session tokens (all users will need to log in again after migration)
    - OTP codes (ephemeral, not worth migrating)
"""

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, date
from pathlib import Path

try:
    import asyncpg
except ImportError:
    print("ERROR: asyncpg not installed. Run: pip install asyncpg")
    sys.exit(1)

# ── Configuration ─────────────────────────────────────────────────────────────

# The fixed firm_id seeded in postgres_schema.sql for Sawyer & Mkushi
FIRM_ID = "a1b2c3d4-0000-0000-0000-000000000001"

# Default partner phone — will be used to create the partner user record
# Override via env var: PARTNER_PHONE=+263772xxxxxx
PARTNER_PHONE    = os.environ.get("PARTNER_PHONE", "+263772000000")
PARTNER_NAME     = os.environ.get("PARTNER_NAME", "Advocate Nyari Maphosa")
SECRETARY_PHONE  = os.environ.get("SECRETARY_PHONE", "")   # optional
SECRETARY_NAME   = os.environ.get("SECRETARY_NAME", "Secretary")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _uuid(val=None):
    """Return a deterministic UUID from a string key, or a fresh one."""
    if val is None:
        return str(uuid.uuid4())
    # Deterministic: namespace UUID5 so the same old ID always maps to the same new UUID
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"mutemo.v1.{val}"))

def _parse_dt(val):
    """Parse an ISO datetime string to a datetime object, or return None."""
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except Exception:
        return None

def _parse_date(val):
    """Parse a YYYY-MM-DD string to a date object, or return None."""
    if not val:
        return None
    try:
        return date.fromisoformat(str(val)[:10])
    except Exception:
        return None

def _parse_time(val):
    """Parse HH:MM string to a time object, or return None."""
    if not val:
        return None
    try:
        from datetime import time as dtime
        parts = str(val).strip().split(":")
        return dtime(int(parts[0]), int(parts[1]))
    except Exception:
        return None

def _trunc(val, n=500):
    if not val:
        return None
    return str(val)[:n]

# ── Migration ─────────────────────────────────────────────────────────────────

async def migrate(state_path: str, db_url: str):
    print(f"\n{'='*60}")
    print(f"  MutemoOS v1 → v2 Migration")
    print(f"  Source: {state_path}")
    print(f"  Target: {db_url[:40]}...")
    print(f"{'='*60}\n")

    # Load state
    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)

    matters_db        = state.get("matters_db", {})
    calendar_db       = state.get("calendar_db", [])
    zlr_db            = state.get("zlr_db", {})
    legal_updates_db  = state.get("legal_updates_db", {})
    reminder_settings = state.get("reminder_settings", {})

    # Handle both dict and list formats for zlr_db and legal_updates_db
    if isinstance(zlr_db, dict):
        zlr_db = list(zlr_db.values())
    if isinstance(legal_updates_db, dict):
        legal_updates_db = list(legal_updates_db.values())

    print(f"Loaded state:")
    print(f"  Matters:        {len(matters_db)}")
    print(f"  Calendar events:{len(calendar_db)}")
    print(f"  ZLR entries:    {len(zlr_db)}")
    print(f"  Legal updates:  {len(legal_updates_db)}")
    print()

    conn = await asyncpg.connect(db_url)
    try:
        # ── 1. Ensure firm exists ──────────────────────────────────────────────
        print("Step 1: Ensuring firm record exists...")
        await conn.execute("""
            INSERT INTO firms (id, name, short_name, city, country)
            VALUES ($1, 'Sawyer & Mkushi Legal Practitioners', 'S&M', 'Harare', 'Zimbabwe')
            ON CONFLICT (id) DO NOTHING
        """, uuid.UUID(FIRM_ID))
        print("  ✓ Firm: Sawyer & Mkushi Legal Practitioners")

        # ── 2. Create user records ─────────────────────────────────────────────
        print("\nStep 2: Creating user records...")
        partner_id = _uuid("partner_ngm")
        await conn.execute("""
            INSERT INTO users (id, firm_id, phone, display_name, role)
            VALUES ($1, $2, $3, $4, 'partner')
            ON CONFLICT (firm_id, phone) DO NOTHING
        """, uuid.UUID(partner_id), uuid.UUID(FIRM_ID), PARTNER_PHONE, PARTNER_NAME)
        print(f"  ✓ Partner: {PARTNER_NAME} ({PARTNER_PHONE})")

        if SECRETARY_PHONE:
            sec_id = _uuid("secretary_1")
            await conn.execute("""
                INSERT INTO users (id, firm_id, phone, display_name, role)
                VALUES ($1, $2, $3, $4, 'secretary')
                ON CONFLICT (firm_id, phone) DO NOTHING
            """, uuid.UUID(sec_id), uuid.UUID(FIRM_ID), SECRETARY_PHONE, SECRETARY_NAME)
            print(f"  ✓ Secretary: {SECRETARY_NAME} ({SECRETARY_PHONE})")

        # ── 3. Migrate matters ─────────────────────────────────────────────────
        print(f"\nStep 3: Migrating {len(matters_db)} matters...")
        matter_id_map = {}  # old_id → new UUID
        migrated_matters = 0

        for old_id, m in matters_db.items():
            new_id = _uuid(f"matter_{old_id}")
            matter_id_map[old_id] = new_id

            last_activity = _parse_dt(m.get("last_activity") or m.get("created_at"))
            created_at    = _parse_dt(m.get("created_at")) or datetime.utcnow()

            await conn.execute("""
                INSERT INTO matters (
                    id, firm_id, name, number, internal_ref, external_ref,
                    client_name, matter_type, status, custom_status,
                    document_count, last_activity, created_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                ON CONFLICT (id) DO NOTHING
            """,
                uuid.UUID(new_id),
                uuid.UUID(FIRM_ID),
                m.get("name") or m.get("title") or "Unnamed Matter",
                _trunc(m.get("number"), 100),
                _trunc(m.get("internal_ref") or m.get("number"), 100),
                _trunc(m.get("external_ref"), 100),
                _trunc(m.get("client_name"), 255),
                _trunc(m.get("matter_type"), 100),
                m.get("status") or "Active",
                _trunc(m.get("custom_status"), 100),
                int(m.get("document_count", 0)),
                last_activity,
                created_at,
            )
            migrated_matters += 1

            # Migrate progress notes for this matter
            notes = m.get("progress_notes", [])
            for note in notes:
                note_id = _uuid(f"note_{old_id}_{note.get('id', note.get('timestamp',''))}")
                note_created = _parse_dt(note.get("timestamp") or note.get("created_at")) or datetime.utcnow()
                await conn.execute("""
                    INSERT INTO progress_notes (id, matter_id, firm_id, text, author, created_at)
                    VALUES ($1,$2,$3,$4,$5,$6)
                    ON CONFLICT (id) DO NOTHING
                """,
                    uuid.UUID(note_id),
                    uuid.UUID(new_id),
                    uuid.UUID(FIRM_ID),
                    note.get("text") or note.get("content") or "",
                    note.get("author") or "NGM",
                    note_created,
                )

            # Migrate documents for this matter
            docs = m.get("documents", [])
            for doc in docs:
                doc_id = _uuid(f"doc_{old_id}_{doc.get('id', doc.get('filename',''))}")
                doc_uploaded = _parse_dt(doc.get("uploaded_at") or doc.get("created_at")) or datetime.utcnow()
                doc_date = _parse_date(doc.get("doc_date") or doc.get("date"))
                await conn.execute("""
                    INSERT INTO documents (
                        id, matter_id, firm_id, filename, document_type, matter_type,
                        parties, doc_date, court, word_count, page_count,
                        chunk_count, ocr_used, status, uploaded_at
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                    ON CONFLICT (id) DO NOTHING
                """,
                    uuid.UUID(doc_id),
                    uuid.UUID(new_id),
                    uuid.UUID(FIRM_ID),
                    doc.get("filename") or "unknown",
                    _trunc(doc.get("document_type"), 100),
                    _trunc(doc.get("matter_type"), 100),
                    _trunc(doc.get("parties"), 500),
                    doc_date,
                    _trunc(doc.get("court"), 200),
                    int(doc.get("word_count", 0)),
                    int(doc.get("page_count", 1)),
                    int(doc.get("chunk_count", 0)),
                    bool(doc.get("ocr_used", False)),
                    doc.get("status") or "complete",
                    doc_uploaded,
                )

        print(f"  ✓ {migrated_matters} matters migrated")

        # ── 4. Migrate calendar events ─────────────────────────────────────────
        print(f"\nStep 4: Migrating {len(calendar_db)} calendar events...")
        migrated_cal = 0
        for ev in calendar_db:
            ev_id = _uuid(f"cal_{ev.get('id', ev.get('title',''))}{ev.get('date','')}")
            ev_date = _parse_date(ev.get("date"))
            if not ev_date:
                print(f"  ! Skipping event with no date: {ev.get('title', '?')}")
                continue

            # Resolve matter_id to new UUID if possible
            old_mid = ev.get("matter_id")
            new_mid = matter_id_map.get(old_mid) if old_mid else None

            await conn.execute("""
                INSERT INTO calendar_events (
                    id, firm_id, matter_id, title, date, time,
                    event_type, court, matter_name, notes, source
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                ON CONFLICT (id) DO NOTHING
            """,
                uuid.UUID(ev_id),
                uuid.UUID(FIRM_ID),
                uuid.UUID(new_mid) if new_mid else None,
                ev.get("title") or "Untitled Event",
                ev_date,
                _parse_time(ev.get("time")),
                ev.get("event_type") or "other",
                _trunc(ev.get("court"), 200),
                _trunc(ev.get("matter_name"), 255),
                _trunc(ev.get("notes"), 1000),
                ev.get("source") or "migrated",
            )
            migrated_cal += 1
        print(f"  ✓ {migrated_cal} calendar events migrated")

        # ── 5. Migrate ZLR entries ─────────────────────────────────────────────
        print(f"\nStep 5: Migrating {len(zlr_db)} ZLR entries...")
        migrated_zlr = 0
        for entry in zlr_db:
            zlr_id = _uuid(f"zlr_{entry.get('id', entry.get('citation',''))}")
            uploaded_at = _parse_dt(entry.get("uploaded_at") or entry.get("created_at")) or datetime.utcnow()

            subject_chains = entry.get("subject_chains", [])
            if isinstance(subject_chains, str):
                try:
                    subject_chains = json.loads(subject_chains)
                except Exception:
                    subject_chains = [subject_chains]

            await conn.execute("""
                INSERT INTO zlr_entries (
                    id, firm_id, filename, source, jurisdiction, authority_weight,
                    volume_year, zimlii_url, case_name, citation, judgment_number,
                    court, judge, case_type, hearing_date, judgment_date,
                    subject_chains, taxonomy_category, summary, raw_text,
                    word_count, chunk_count, ocr_used, uploaded_at
                ) VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,
                    $12,$13,$14,$15,$16,$17,$18,$19,$20,
                    $21,$22,$23,$24
                )
                ON CONFLICT (id) DO NOTHING
            """,
                uuid.UUID(zlr_id),
                uuid.UUID(FIRM_ID),
                _trunc(entry.get("filename"), 255),
                entry.get("source") or "ZLR",
                _trunc(entry.get("jurisdiction"), 100),
                _trunc(entry.get("authority_weight"), 50),
                _trunc(entry.get("volume_year"), 20),
                _trunc(entry.get("zimlii_url"), 500),
                _trunc(entry.get("case_name"), 500),
                _trunc(entry.get("citation"), 200),
                _trunc(entry.get("judgment_number"), 100),
                _trunc(entry.get("court"), 200),
                _trunc(entry.get("judge"), 200),
                _trunc(entry.get("case_type"), 100),
                _trunc(entry.get("hearing_date"), 50),
                _trunc(entry.get("judgment_date"), 50),
                json.dumps(subject_chains),
                _trunc(entry.get("taxonomy_category") or "General", 100),
                _trunc(entry.get("summary"), 2000),
                entry.get("raw_text") or entry.get("text"),
                int(entry.get("word_count", 0)),
                int(entry.get("chunk_count", 0)),
                bool(entry.get("ocr_used", False)),
                uploaded_at,
            )
            migrated_zlr += 1
        print(f"  ✓ {migrated_zlr} ZLR entries migrated")

        # ── 6. Migrate legal updates ───────────────────────────────────────────
        print(f"\nStep 6: Migrating {len(legal_updates_db)} legal updates...")
        migrated_lu = 0
        for lu in legal_updates_db:
            lu_id = _uuid(f"lu_{lu.get('id', lu.get('filename',''))}")
            uploaded_at = _parse_dt(lu.get("uploaded_at") or lu.get("created_at")) or datetime.utcnow()
            doc_date = _parse_date(lu.get("doc_date") or lu.get("date"))

            await conn.execute("""
                INSERT INTO legal_updates (
                    id, firm_id, filename, source_type, source_name, reference,
                    document_type, matter_type, doc_date, court,
                    word_count, chunk_count, status, ocr_used, uploaded_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                ON CONFLICT (id) DO NOTHING
            """,
                uuid.UUID(lu_id),
                uuid.UUID(FIRM_ID),
                lu.get("filename") or "unknown",
                _trunc(lu.get("source_type"), 100),
                _trunc(lu.get("source_name"), 200),
                _trunc(lu.get("reference"), 200),
                _trunc(lu.get("document_type"), 100),
                _trunc(lu.get("matter_type"), 100),
                doc_date,
                _trunc(lu.get("court"), 200),
                int(lu.get("word_count", 0)),
                int(lu.get("chunk_count", 0)),
                lu.get("status") or "complete",
                bool(lu.get("ocr_used", False)),
                uploaded_at,
            )
            migrated_lu += 1
        print(f"  ✓ {migrated_lu} legal updates migrated")

        # ── 7. Migrate reminder settings ──────────────────────────────────────
        print("\nStep 7: Migrating reminder settings...")
        if reminder_settings:
            await conn.execute("""
                INSERT INTO reminder_settings (firm_id, enabled, recipient_email, send_hour_utc)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (firm_id) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    recipient_email = EXCLUDED.recipient_email,
                    send_hour_utc = EXCLUDED.send_hour_utc
            """,
                uuid.UUID(FIRM_ID),
                bool(reminder_settings.get("enabled", False)),
                reminder_settings.get("recipient_email") or "",
                int(reminder_settings.get("send_hour_utc", 5)),
            )
            print(f"  ✓ Reminders: enabled={reminder_settings.get('enabled')}, "
                  f"recipient={reminder_settings.get('recipient_email', 'not set')}")
        else:
            print("  - No reminder settings found in state, skipping")

        # ── Summary ───────────────────────────────────────────────────────────
        print(f"\n{'='*60}")
        print("  Migration Complete")
        print(f"{'='*60}")
        print(f"  Matters:        {migrated_matters}")
        print(f"  Calendar events:{migrated_cal}")
        print(f"  ZLR entries:    {migrated_zlr}")
        print(f"  Legal updates:  {migrated_lu}")
        print()
        print("  NEXT STEPS:")
        print("  1. Deploy v2 backend to Railway")
        print("  2. Run: POST /api/admin/reindex to rebuild ChromaDB vectors")
        print("     (Header: X-Admin-Token: <your token>)")
        print("  3. Log in via OTP with the partner phone number above")
        print("  4. Verify matters, calendar, and ZLR entries are visible")
        print()

    finally:
        await conn.close()


if __name__ == "__main__":
    state_file = sys.argv[1] if len(sys.argv) > 1 else "data/mutemo_state.json"
    db_url = os.environ.get("DATABASE_URL")

    if not db_url:
        print("ERROR: DATABASE_URL environment variable not set.")
        print("Usage: DATABASE_URL=postgresql://... python3 migrate_to_postgres.py [state_file]")
        sys.exit(1)

    if "--dry-run" in sys.argv:
        # For dry run, just load and report — don't connect to DB
        state_path = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
        if not state_path or not Path(state_path).exists():
            print(f"ERROR: State file not found: {state_path}")
            sys.exit(1)
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        matters_db       = state.get("matters_db", {})
        calendar_db      = state.get("calendar_db", [])
        zlr_db           = state.get("zlr_db", {})
        legal_updates_db = state.get("legal_updates_db", {})
        if isinstance(zlr_db, dict):
            zlr_db = list(zlr_db.values())
        if isinstance(legal_updates_db, dict):
            legal_updates_db = list(legal_updates_db.values())
        total_notes = sum(len(m.get("progress_notes", [])) for m in matters_db.values())
        print(f"\n{'='*60}")
        print("  DRY RUN — no data will be written")
        print(f"{'='*60}")
        print(f"  Matters:        {len(matters_db)}")
        print(f"  Progress notes: {total_notes}")
        print(f"  Calendar events:{len(calendar_db)}")
        print(f"  ZLR entries:    {len(zlr_db)}")
        print(f"  Legal updates:  {len(legal_updates_db)}")
        print(f"\n  Ready to migrate. Run without --dry-run to proceed.")
        sys.exit(0)

    if not Path(state_file).exists():
        print(f"ERROR: State file not found: {state_file}")
        print("Provide the path to mutemo_state.json as the first argument.")
        sys.exit(1)

    asyncio.run(migrate(state_file, db_url))
