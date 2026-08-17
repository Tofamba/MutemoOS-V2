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

FIRM_ID = os.environ.get("MUTEMO_FIRM_ID", "a1b2c3d4-0000-0000-0000-000000000001")
SOURCES = ("firm", "legal", "zlr")
COLLECTION_NAMES = {"firm": "firm_precedents", "legal": "legal_updates", "zlr": "zlr_index"}


async def _build_plan(database_url: str, chroma_dir: str) -> dict:
    conn = await asyncpg.connect(database_url)
    try:
        chroma_client = chromadb.PersistentClient(path=chroma_dir)
        plan = {"to_backfill": {}, "already_ok": {}, "missing_in_chroma": {}}

        for source in SOURCES:
            rows = await conn.fetch(
                "SELECT id, content_hash FROM chunks WHERE firm_id=$1 AND chunk_source=$2",
                FIRM_ID, source,
            )
            pg_hashes = {r["id"]: r["content_hash"] for r in rows}
            if not pg_hashes:
                continue

            collection = chroma_client.get_or_create_collection(
                COLLECTION_NAMES[source], metadata={"hnsw:space": "cosine"}
            )
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
    finally:
        await conn.close()


def _print_plan(plan: dict):
    total_backfill = sum(len(v) for v in plan["to_backfill"].values())
    total_ok = sum(plan["already_ok"].values())
    total_missing = sum(plan["missing_in_chroma"].values())

    print(f"\n  content_hash backfill plan (firm_id={FIRM_ID}):\n")
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


async def cmd_report(args):
    plan = await _build_plan(args.database_url, args.chroma_data_dir)
    _print_plan(plan)


async def cmd_apply(args):
    plan = await _build_plan(args.database_url, args.chroma_data_dir)
    total_backfill = sum(len(v) for v in plan["to_backfill"].values())
    if total_backfill == 0:
        print("  Nothing to backfill — every indexed chunk already has the correct content_hash.")
        return

    if not args.yes:
        _print_plan(plan)
        return

    chroma_client = chromadb.PersistentClient(path=args.chroma_data_dir)
    for source, entries in plan["to_backfill"].items():
        if not entries:
            continue
        collection = chroma_client.get_or_create_collection(
            COLLECTION_NAMES[source], metadata={"hnsw:space": "cosine"}
        )
        ids = [cid for cid, _ in entries]
        metadatas = [meta for _, meta in entries]
        collection.update(ids=ids, metadatas=metadatas)
        print(f"  {source}: backfilled content_hash on {len(entries)} chunk(s) (metadata only)")

    print(f"\n  Done. {total_backfill} chunk(s) backfilled.")


def main():
    parser = argparse.ArgumentParser(description="Backfill content_hash into existing Chroma chunk metadata")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--chroma-data-dir", default=os.environ.get(
        "CHROMA_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data" / "chroma")
    ))
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
