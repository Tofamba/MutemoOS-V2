#!/usr/bin/env python3
"""
Chunk content_hash Backfill — One-Time Metadata Fix for Existing Chroma Entries
==================================================================================
Must run BEFORE (or as part of) deploying the content_hash-based
reconcile_chroma_index() rewrite, against every environment with existing
indexed data.

Why this is needed at all: existing chunks already indexed in ChromaDB
have no content_hash in their metadata (it didn't exist before this fix).
Without this backfill, the very first reconciliation run after deploying
would see every single existing chunk as "mismatched" (Postgres has a real
hash, Chroma has none) and re-index the entire corpus through the normal
repair path -- exactly the expensive full-rebuild the content_hash fix is
meant to avoid triggering unnecessarily. This script pre-populates the
correct hash (already computed by chunks.content_hash's GENERATED column)
into each existing chunk's Chroma metadata directly, so reconciliation's
first run after deploy correctly sees existing content as already in sync
and only flags genuine drift.

Cheap by construction: this NEVER re-embeds anything. It's a metadata-only
update (collection.update(metadatas=...)) — the existing embedding vector
for each chunk is left completely untouched. The only real cost is one
Chroma metadata write per existing chunk.

build_plan()/apply_plan() are connection-agnostic (take an open conn and a
get_collection(source) callable) specifically so backend/main.py's
POST /api/admin/backfill-chunk-hashes can call the exact same logic
in-process, against the app's own already-running _db_pool and Chroma
client, rather than duplicating it or opening a second PersistentClient
against the same on-disk data from within the same process.

Preview-first, matching this project's other report/apply scripts.

Usage:
    DATABASE_URL=postgresql://... CHROMA_DATA_DIR=/data/chroma \
        python3 scripts/backfill_chunk_content_hash.py report
    DATABASE_URL=postgresql://... CHROMA_DATA_DIR=/data/chroma \
        python3 scripts/backfill_chunk_content_hash.py apply --yes
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

try:
    import asyncpg
except ImportError:
    print("ERROR: asyncpg not installed. Run: pip install asyncpg")
    sys.exit(1)

try:
    import chromadb
except ImportError:
    print("ERROR: chromadb not installed. Run: pip install chromadb")
    sys.exit(1)

DEFAULT_FIRM_ID = os.environ.get("MUTEMO_FIRM_ID", "a1b2c3d4-0000-0000-0000-000000000001")
SOURCES = ("firm", "legal", "zlr")
COLLECTION_NAMES = {"firm": "firm_precedents", "legal": "legal_updates", "zlr": "zlr_index"}
BATCH = 500  # defensive -- some Chroma backends cap max_batch_size; measured
             # clean and fast (~1s/3700 chunks total) at this batch size locally


async def build_plan(conn, get_collection, firm_id) -> dict:
    """
    conn: an open asyncpg connection (or pool-acquired connection).
    get_collection: callable(source: str) -> Chroma collection for that source.
    firm_id: the firm to scope chunks to (str or UUID, matches chunks.firm_id).
    """
    plan = {"to_backfill": {}, "already_ok": {}, "missing_in_chroma": {}}

    for source in SOURCES:
        rows = await conn.fetch(
            "SELECT id, content_hash FROM chunks WHERE firm_id=$1 AND chunk_source=$2",
            firm_id, source,
        )
        pg_hashes = {r["id"]: r["content_hash"] for r in rows}
        if not pg_hashes:
            continue

        collection = get_collection(source)
        existing = collection.get(ids=list(pg_hashes.keys()), include=["metadatas"])
        chroma_meta = dict(zip(existing["ids"], existing["metadatas"]))

        to_backfill = []
        already_ok = 0
        for cid, pg_hash in pg_hashes.items():
            if cid not in chroma_meta:
                continue  # not indexed at all -- reconcile_chroma_index() handles this, not this script
            meta = chroma_meta[cid] or {}
            if meta.get("content_hash") == pg_hash:
                already_ok += 1
            else:
                to_backfill.append((cid, {**meta, "content_hash": pg_hash}))

        plan["to_backfill"][source] = to_backfill
        plan["already_ok"][source] = already_ok
        plan["missing_in_chroma"][source] = len(pg_hashes) - len(chroma_meta)

    return plan


async def apply_plan(plan: dict, get_collection) -> dict:
    """Writes the plan's backfill entries to Chroma (metadata only, batched).
    Returns a small summary dict, the same shape the admin endpoint reports."""
    summary = {"backfilled": 0, "already_correct": sum(plan["already_ok"].values()), "by_source": {}}
    for source, entries in plan["to_backfill"].items():
        summary["by_source"][source] = {
            "backfilled": len(entries),
            "already_correct": plan["already_ok"].get(source, 0),
        }
        if not entries:
            continue
        collection = get_collection(source)
        ids = [cid for cid, _ in entries]
        metadatas = [meta for _, meta in entries]
        for i in range(0, len(ids), BATCH):
            collection.update(ids=ids[i:i + BATCH], metadatas=metadatas[i:i + BATCH])
        summary["backfilled"] += len(entries)
    return summary


def _print_plan(plan: dict, firm_id):
    total_backfill = sum(len(v) for v in plan["to_backfill"].values())
    total_ok = sum(plan["already_ok"].values())
    total_missing = sum(plan["missing_in_chroma"].values())

    print(f"\n  content_hash backfill plan (firm_id={firm_id}):\n")
    for source in SOURCES:
        n_backfill = len(plan["to_backfill"].get(source, []))
        n_ok = plan["already_ok"].get(source, 0)
        n_missing = plan["missing_in_chroma"].get(source, 0)
        if n_backfill or n_ok or n_missing:
            print(f"    {source:8} needs backfill: {n_backfill:5}   already correct: {n_ok:5}   "
                  f"missing from Chroma entirely (not this script's job): {n_missing}")

    print(f"\n  TOTAL: {total_backfill} chunk(s) need a metadata-only content_hash backfill "
          f"({total_ok} already correct, {total_missing} missing from Chroma and left for "
          f"reconcile_chroma_index() to index normally).")
    print("  No embeddings are touched -- this only writes the content_hash metadata key.")
    print("\n  No rows were modified. Re-run with `apply --yes` to write this plan.")


def _cli_get_collection(chroma_client, source):
    return chroma_client.get_or_create_collection(COLLECTION_NAMES[source], metadata={"hnsw:space": "cosine"})


async def cmd_report(args):
    conn = await asyncpg.connect(args.database_url)
    try:
        chroma_client = chromadb.PersistentClient(path=args.chroma_data_dir)
        plan = await build_plan(conn, lambda source: _cli_get_collection(chroma_client, source), args.firm_id)
        _print_plan(plan, args.firm_id)
    finally:
        await conn.close()


async def cmd_apply(args):
    conn = await asyncpg.connect(args.database_url)
    try:
        chroma_client = chromadb.PersistentClient(path=args.chroma_data_dir)
        get_collection = lambda source: _cli_get_collection(chroma_client, source)
        plan = await build_plan(conn, get_collection, args.firm_id)
        total_backfill = sum(len(v) for v in plan["to_backfill"].values())
        if total_backfill == 0:
            print("  Nothing to backfill — every indexed chunk already has the correct content_hash.")
            return

        if not args.yes:
            _print_plan(plan, args.firm_id)
            return

        summary = await apply_plan(plan, get_collection)
        for source, counts in summary["by_source"].items():
            if counts["backfilled"]:
                print(f"  {source}: backfilled content_hash on {counts['backfilled']} chunk(s) (metadata only)")
        print(f"\n  Done. {summary['backfilled']} chunk(s) backfilled.")
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Backfill content_hash into existing Chroma chunk metadata")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--chroma-data-dir", default=os.environ.get(
        "CHROMA_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data" / "chroma")
    ))
    parser.add_argument("--firm-id", default=DEFAULT_FIRM_ID)
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
