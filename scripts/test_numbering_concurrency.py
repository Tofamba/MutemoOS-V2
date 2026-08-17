#!/usr/bin/env python3
"""
Real-Postgres Concurrency Test — Client/Matter Numbering
==========================================================
Proves the numbering_counters-based atomic allocator (backend/main.py's
_allocate_next_seq / _next_client_number / _next_matter_number) actually
eliminates the race condition in the old MAX(existing)+1 scan, and that
the old scan genuinely raced (not a hypothetical concern).

An in-memory mock can't validate this — the fix relies on Postgres's own
row-lock serialization on UPDATE ... RETURNING, which only a real Postgres
instance actually exercises. This script needs a real DATABASE_URL.

Two phases, run back to back:
  Phase A — the OLD logic (plain SELECT scan, compute max+1 in Python, no
            lock), hit with N concurrent creates via asyncio.gather.
            Expected: most fail with UniqueViolationError, since they all
            read the same starting state before any of them commit.
  Phase B — the NEW atomic allocator, same N concurrent creates.
            Expected: zero errors, zero duplicates, a complete gapless
            sequence.
  Then: the same test for matter numbering under one shared client.

Uses a dedicated CCTEST-prefixed firm so it never touches real seeded
data, and cleans up everything it creates (including numbering_counters
rows) at the end regardless of outcome.

Usage:
    python3 scripts/test_numbering_concurrency.py --database-url postgresql://...
    DATABASE_URL=postgresql://... python3 scripts/test_numbering_concurrency.py
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

CONCURRENCY = 25  # simultaneous creates fired via asyncio.gather


def next_sequence(existing_numbers, prefix):
    """Copy of backend/numbering.py's pure function — kept standalone here
    so this script has no dependency on importing the full backend app."""
    max_seq = 0
    marker = f"{prefix}-"
    for value in existing_numbers:
        if not value or not value.startswith(marker):
            continue
        rest = value[len(marker):]
        digits = ""
        for ch in rest:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            max_seq = max(max_seq, int(digits))
    return max_seq + 1


def format_client_number(initials, seq):
    return f"{initials}-{seq:03d}"


async def old_racy_next_client_number(pool, firm_id, initials):
    """The exact OLD logic from backend/main.py before this fix: a plain
    SELECT scan, compute max+1 in Python, no locking, no transaction."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT client_number FROM clients WHERE firm_id=$1 AND client_number LIKE $2",
            firm_id, f"{initials}-%",
        )
        seq = next_sequence([r["client_number"] for r in rows], initials)
        number = format_client_number(initials, seq)
        cid = uuid.uuid4()
        await conn.execute(
            "INSERT INTO clients (id, firm_id, full_name, client_number, created_at, updated_at) "
            "VALUES ($1,$2,$3,$4,NOW(),NOW())",
            cid, firm_id, f"CCTEST old {seq}", number,
        )
        return number


async def new_allocate_next_seq(conn, firm_id, prefix, compute_seed):
    """Exact copy of the new backend/main.py atomic allocator under test."""
    existing = await conn.fetchval(
        "SELECT 1 FROM numbering_counters WHERE firm_id=$1 AND prefix=$2", firm_id, prefix
    )
    if existing is None:
        seed = await compute_seed()
        await conn.execute(
            "INSERT INTO numbering_counters (firm_id, prefix, next_seq) VALUES ($1,$2,$3) "
            "ON CONFLICT (firm_id, prefix) DO NOTHING",
            firm_id, prefix, seed,
        )
    row = await conn.fetchrow(
        "UPDATE numbering_counters SET next_seq = next_seq + 1 WHERE firm_id=$1 AND prefix=$2 "
        "RETURNING next_seq - 1 AS allocated",
        firm_id, prefix,
    )
    return row["allocated"]


async def new_fixed_next_client_number(pool, firm_id, initials):
    async with pool.acquire() as conn:
        async def compute_seed():
            rows = await conn.fetch(
                "SELECT client_number FROM clients WHERE firm_id=$1 AND client_number LIKE $2",
                firm_id, f"{initials}-%",
            )
            return next_sequence([r["client_number"] for r in rows], initials)
        seq = await new_allocate_next_seq(conn, firm_id, initials, compute_seed)
        number = format_client_number(initials, seq)
        cid = uuid.uuid4()
        await conn.execute(
            "INSERT INTO clients (id, firm_id, full_name, client_number, created_at, updated_at) "
            "VALUES ($1,$2,$3,$4,NOW(),NOW())",
            cid, firm_id, f"CCTEST new {seq}", number,
        )
        return number


async def cleanup(pool, firm_id):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM matters WHERE firm_id=$1", firm_id)
        await conn.execute("DELETE FROM clients WHERE firm_id=$1", firm_id)
        await conn.execute("DELETE FROM numbering_counters WHERE firm_id=$1", firm_id)
        await conn.execute("DELETE FROM firms WHERE id=$1", firm_id)


async def run(database_url: str):
    pool = await asyncpg.create_pool(database_url, min_size=CONCURRENCY, max_size=CONCURRENCY)

    async with pool.acquire() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS numbering_counters ("
            "  firm_id UUID NOT NULL, prefix TEXT NOT NULL, next_seq INT NOT NULL,"
            "  PRIMARY KEY (firm_id, prefix)"
            ")"
        )

    # ── Phase A: reproduce the bug ──────────────────────────────────────
    firm_a = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO firms (id, name) VALUES ($1, 'CCTEST Phase A')", firm_a)

    print(f"\n{'=' * 70}\nPHASE A -- OLD racy logic, {CONCURRENCY} concurrent creates\n{'=' * 70}")
    results_a = await asyncio.gather(
        *[old_racy_next_client_number(pool, firm_a, "CCT") for _ in range(CONCURRENCY)],
        return_exceptions=True,
    )
    errors_a = [r for r in results_a if isinstance(r, Exception)]
    numbers_a = [r for r in results_a if not isinstance(r, Exception)]
    duplicates_a = len(numbers_a) - len(set(numbers_a))
    print(f"  {len(errors_a)} unhandled exceptions (e.g. UniqueViolationError)")
    print(f"  {len(numbers_a)} succeeded, {duplicates_a} duplicate number(s) among them")
    if errors_a:
        print(f"  sample error: {type(errors_a[0]).__name__}: {errors_a[0]}")
    bug_reproduced = bool(errors_a) or duplicates_a > 0
    print(f"  >>> BUG REPRODUCED: {bug_reproduced}")

    await cleanup(pool, firm_a)

    # ── Phase B: prove the fix ──────────────────────────────────────────
    firm_b = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO firms (id, name) VALUES ($1, 'CCTEST Phase B')", firm_b)

    print(f"\n{'=' * 70}\nPHASE B -- NEW atomic allocator, {CONCURRENCY} concurrent creates\n{'=' * 70}")
    results_b = await asyncio.gather(
        *[new_fixed_next_client_number(pool, firm_b, "CCT") for _ in range(CONCURRENCY)],
        return_exceptions=True,
    )
    errors_b = [r for r in results_b if isinstance(r, Exception)]
    numbers_b = [r for r in results_b if not isinstance(r, Exception)]
    duplicates_b = len(numbers_b) - len(set(numbers_b))
    expected = {format_client_number("CCT", i) for i in range(1, CONCURRENCY + 1)}
    missing = expected - set(numbers_b)
    print(f"  {len(errors_b)} unhandled exceptions")
    print(f"  {len(numbers_b)} succeeded, {duplicates_b} duplicate number(s) among them")
    print(f"  missing/skipped numbers: {sorted(missing) if missing else 'none'}")
    fix_works = len(errors_b) == 0 and duplicates_b == 0 and not missing and len(numbers_b) == CONCURRENCY
    print(f"  >>> FIX CONFIRMED (no collisions, no errors, complete 1..{CONCURRENCY} sequence): {fix_works}")

    # ── Matter numbering: concurrent creates under one shared client ─────
    firm_c = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO firms (id, name) VALUES ($1, 'CCTEST Phase C')", firm_c)
        client_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO clients (id, firm_id, full_name, client_number, created_at, updated_at) "
            "VALUES ($1,$2,'CCTEST Shared Client','CCT-001',NOW(),NOW())",
            client_id, firm_c,
        )

    async def new_fixed_next_matter_number(client_number):
        async with pool.acquire() as conn:
            async def compute_seed():
                rows = await conn.fetch(
                    "SELECT matter_number FROM matters WHERE firm_id=$1 AND matter_number LIKE $2",
                    firm_c, f"{client_number}-%",
                )
                return next_sequence([r["matter_number"] for r in rows], client_number)
            seq = await new_allocate_next_seq(conn, firm_c, client_number, compute_seed)
            number = f"{client_number}-{seq:02d}"
            mid = uuid.uuid4()
            await conn.execute(
                "INSERT INTO matters (id, firm_id, name, matter_number, client_id, status, created_at) "
                "VALUES ($1,$2,$3,$4,$5,'Active',NOW())",
                mid, firm_c, f"CCTEST matter {seq}", number, client_id,
            )
            return number

    print(f"\n{'=' * 70}\nMATTER NUMBERING -- {CONCURRENCY} concurrent creates under ONE client\n{'=' * 70}")
    results_c = await asyncio.gather(
        *[new_fixed_next_matter_number("CCT-001") for _ in range(CONCURRENCY)],
        return_exceptions=True,
    )
    errors_c = [r for r in results_c if isinstance(r, Exception)]
    numbers_c = [r for r in results_c if not isinstance(r, Exception)]
    duplicates_c = len(numbers_c) - len(set(numbers_c))
    expected_c = {f"CCT-001-{i:02d}" for i in range(1, CONCURRENCY + 1)}
    missing_c = expected_c - set(numbers_c)
    print(f"  {len(errors_c)} unhandled exceptions, {duplicates_c} duplicates, "
          f"missing: {sorted(missing_c) if missing_c else 'none'}")
    matter_fix_works = len(errors_c) == 0 and duplicates_c == 0 and not missing_c and len(numbers_c) == CONCURRENCY
    print(f"  >>> MATTER NUMBERING FIX CONFIRMED: {matter_fix_works}")

    await cleanup(pool, firm_b)
    await cleanup(pool, firm_c)
    await pool.close()

    print(f"\n{'=' * 70}\nSUMMARY\n{'=' * 70}")
    print(f"  Bug reproduced under old logic:  {bug_reproduced}")
    print(f"  Fix confirmed (clients):          {fix_works}")
    print(f"  Fix confirmed (matters):          {matter_fix_works}")
    return bug_reproduced and fix_works and matter_fix_works


def main():
    parser = argparse.ArgumentParser(description="Real-Postgres concurrency test for client/matter numbering")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args()

    if not args.database_url:
        print("ERROR: DATABASE_URL environment variable (or --database-url) not set.")
        sys.exit(1)

    ok = asyncio.run(run(args.database_url))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
