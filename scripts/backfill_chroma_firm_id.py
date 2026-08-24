#!/usr/bin/env python3
"""
Chroma firm_id Backfill — One-Time Metadata Fix for Existing firm_precedents
Entries (Multi-tenancy hardening, Part 3)
==================================================================================
Must run BEFORE (or as part of) deploying the firm_id-filtered
_semantic_search_firm() query -- against every environment with existing
indexed firm documents.

Why this is needed at all: index_chunks_in_chroma() now writes a firm_id
into every new "firm"-collection chunk's metadata, but chunks indexed
before this change have no firm_id at all. Once _semantic_search_firm()
starts filtering firm_precedents queries on where={"firm_id": ...}, any
chunk missing that key simply won't match -- without this backfill, the
very first deploy of the query-side filter would make every existing
indexed document invisible to search until this is run. This script
writes the correct firm_id (this deployment only ever has one, per the
Option B model -- see FIRM_ID's definition in backend/main.py) into every
existing firm_precedents chunk's metadata directly.

Deliberately scoped to the "firm" source only. legal_updates/zlr_index
are shared corpora visible to every firm by design and are never given a
firm_id.

Cheap by construction: this NEVER re-embeds anything. It's a metadata-only
update (collection.update(metadatas=...)) -- the existing embedding vector
for each chunk is left completely untouched. The only real cost is one
Chroma metadata write per existing firm-precedent chunk.

build_plan()/apply_plan() are connection-agnostic (take an open conn and a
get_collection() callable) specifically so backend/main.py's
POST /api/admin/backfill-chroma-firm-id can call the exact same logic
in-process, against the app's own already-running _db_pool and Chroma
client, rather than duplicating it or opening a second PersistentClient
against the same on-disk data from within the same process. Mirrors
scripts/backfill_chunk_content_hash.py's structure exactly.

Preview-first, matching this project's other report/apply scripts.

Usage:
    DATABASE_URL=postgresql://... CHROMA_DATA_DIR=/data/chroma \
        python3 scripts/backfill_chroma_firm_id.py report
    DATABASE_URL=postgresql://... CHROMA_DATA_DIR=/data/chroma \
        python3 scripts/backfill_chroma_firm_id.py apply --yes
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
BATCH = 500  # matches backfill_chunk_content_hash.py's measured-safe batch size


async def build_plan(conn, get_collection, firm_id) -> dict:
    """
    conn: an open asyncpg connection (or pool-acquired connection).
    get_collection: callable() -> the firm_precedents Chroma collection.
    firm_id: the firm to backfill onto every existing chunk (str or UUID).
    """
    firm_id_str = str(firm_id)
    plan = {"to_backfill": [], "already_ok": 0, "missing_in_chroma": 0}

    rows = await conn.fetch(
        "SELECT id FROM chunks WHERE firm_id=$1 AND chunk_source='firm'",
        firm_id,
    )
    chunk_ids = [r["id"] for r in rows]
    if not chunk_ids:
        return plan

    collection = get_collection()
    existing = collection.get(ids=chunk_ids, include=["metadatas"])
    chroma_meta = dict(zip(existing["ids"], existing["metadatas"]))

    for cid in chunk_ids:
        if cid not in chroma_meta:
            plan["missing_in_chroma"] += 1
            continue  # not indexed at all -- reconcile_chroma_index() handles this, not this script
        meta = chroma_meta[cid] or {}
        if meta.get("firm_id") == firm_id_str:
            plan["already_ok"] += 1
        else:
            plan["to_backfill"].append((cid, {**meta, "firm_id": firm_id_str}))

    return plan


async def apply_plan(plan: dict, get_collection) -> dict:
    """Writes the plan's backfill entries to Chroma (metadata only, batched).
    Returns a small summary dict, the same shape the admin endpoint reports."""
    entries = plan["to_backfill"]
    summary = {"backfilled": 0, "already_correct": plan["already_ok"]}
    if not entries:
        return summary

    collection = get_collection()
    ids = [cid for cid, _ in entries]
    metadatas = [meta for _, meta in entries]
    for i in range(0, len(ids), BATCH):
        collection.update(ids=ids[i:i + BATCH], metadatas=metadatas[i:i + BATCH])
    summary["backfilled"] = len(entries)
    return summary


def _print_plan(plan: dict, firm_id):
    n_backfill = len(plan["to_backfill"])
    n_ok = plan["already_ok"]
    n_missing = plan["missing_in_chroma"]

    print(f"\n  Chroma firm_id backfill plan (firm_id={firm_id}):\n")
    print(f"    firm_precedents  needs backfill: {n_backfill:5}   already correct: {n_ok:5}   "
          f"missing from Chroma entirely (not this script's job): {n_missing}")

    print(f"\n  TOTAL: {n_backfill} chunk(s) need a metadata-only firm_id backfill "
          f"({n_ok} already correct, {n_missing} missing from Chroma and left for "
          f"reconcile_chroma_index() to index normally).")
    print("  No embeddings are touched -- this only writes the firm_id metadata key.")
    print("\n  No rows were modified. Re-run with `apply --yes` to write this plan.")


def _cli_get_collection(chroma_client):
    return chroma_client.get_or_create_collection("firm_precedents", metadata={"hnsw:space": "cosine"})


async def cmd_report(args):
    conn = await asyncpg.connect(args.database_url)
    try:
        chroma_client = chromadb.PersistentClient(path=args.chroma_data_dir)
        plan = await build_plan(conn, lambda: _cli_get_collection(chroma_client), args.firm_id)
        _print_plan(plan, args.firm_id)
    finally:
        await conn.close()


async def cmd_apply(args):
    conn = await asyncpg.connect(args.database_url)
    try:
        chroma_client = chromadb.PersistentClient(path=args.chroma_data_dir)
        get_collection = lambda: _cli_get_collection(chroma_client)
        plan = await build_plan(conn, get_collection, args.firm_id)
        if not plan["to_backfill"]:
            print("  Nothing to backfill — every indexed firm-precedent chunk already has the correct firm_id.")
            return

        if not args.yes:
            _print_plan(plan, args.firm_id)
            return

        summary = await apply_plan(plan, get_collection)
        print(f"  firm_precedents: backfilled firm_id on {summary['backfilled']} chunk(s) (metadata only)")
        print(f"\n  Done. {summary['backfilled']} chunk(s) backfilled.")
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Backfill firm_id into existing firm_precedents chunk metadata")
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
