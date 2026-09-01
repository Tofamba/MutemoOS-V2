"""
scripts/corpus_snapshot.py — R2-backed publish/restore for the shared
legal corpus (legal_updates/zlr_entries/chunks where chunk_source IN
('legal','zlr') + Chroma legal_updates/zlr_index collections), so a new
firm's onboarding restores from one durable, versioned snapshot instead
of a fresh point-to-point export/import against the reference firm's live
environment every time (see the corpus-snapshot-tooling project memory /
.claude/skills/firm-onboarding/SKILL.md Step 3).

Thin wrapper, not a rewrite: reuses do_export()/do_import() from
scripts/copy_shared_corpus_to_new_firm.py unchanged for the actual
Postgres/Chroma read/write logic. This script adds three things on top:

1. A HARD per-document consistency gate on publish -- for every
   legal_updates row with status='complete' and chunk_count > 0 (and every
   zlr_entries row with chunk_count > 0 -- zlr_entries has no status
   column), confirms actual chunks-table rows AND actual Chroma vectors
   both exist. If ANY such document has zero of either, the snapshot is
   REFUSED -- nothing is uploaded to R2, corpus-snapshots/latest/ is left
   untouched. This is exactly the check that would have caught the
   v1-migrated legal_updates gap (all metadata, zero real chunks) before
   it ever shipped to a new firm, and is not optional/skippable.

2. A manifest.json alongside the corpus data: publish timestamp, source
   firm id, row/vector counts, and the gate's own findings -- so a human
   (or the next onboarding run) can see what's in a snapshot without
   downloading and inspecting the full corpus file.

3. R2 (S3-compatible) transport: uploads to a DEDICATED
   CORPUS_SNAPSHOT_BUCKET (never the per-firm documents bucket used for
   client uploads) at corpus-snapshots/<utc-timestamp>/ (permanent
   history) and corpus-snapshots/latest/ (the rolling pointer real
   onboarding reads from). Same R2 endpoint/credentials as the app's
   existing document storage (R2_ENDPOINT/R2_ACCESS_KEY_ID/
   R2_SECRET_ACCESS_KEY) -- just a different bucket.

Manual trigger only, by design -- run this deliberately at whatever
cadence onboarding actually needs, not on every legal-feed scrape. Wired
to be callable from elsewhere later without needing to be re-architected,
not because an auto-trigger is being added now.

Usage:
    # Publish (run against the reference/source firm's environment):
    python3 scripts/corpus_snapshot.py publish \\
        --database-url postgresql://... \\
        --chroma-path /app/data/chroma \\
        --firm-id a1b2c3d4-0000-0000-0000-000000000001 \\
        [--dry-run]   # runs the real export + gate, prints the manifest, uploads nothing

    # Restore (run against a new/target firm's environment):
    python3 scripts/corpus_snapshot.py restore \\
        --database-url postgresql://... \\
        --chroma-path /app/data/chroma \\
        --firm-id <new-firm-uuid> \\
        [--snapshot latest]   # or a specific corpus-snapshots/<timestamp> value
        --apply   # omit for a dry-run preview (row/vector counts only, no writes)

Required env vars (both commands): R2_ENDPOINT, R2_ACCESS_KEY_ID,
R2_SECRET_ACCESS_KEY, CORPUS_SNAPSHOT_BUCKET.
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone

import boto3

# Two ways this module gets loaded: as a standalone script (`python3
# scripts/corpus_snapshot.py ...`, where sys.path[0] is scripts/ itself,
# so the sibling module is a plain top-level import) and as a package
# import for testing (`from scripts import corpus_snapshot`, run from the
# repo root, where it's scripts.copy_shared_corpus_to_new_firm instead).
# Try the package form first since that's the more specific path.
try:
    from scripts.copy_shared_corpus_to_new_firm import do_export, do_import
except ImportError:
    from copy_shared_corpus_to_new_firm import do_export, do_import


def _r2_client():
    endpoint = os.environ.get("R2_ENDPOINT")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    bucket = os.environ.get("CORPUS_SNAPSHOT_BUCKET")
    if not (endpoint and access_key and secret_key and bucket):
        print(
            "ERROR: R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, and "
            "CORPUS_SNAPSHOT_BUCKET must all be set.",
            file=sys.stderr,
        )
        sys.exit(1)
    client = boto3.client(
        "s3", endpoint_url=endpoint, aws_access_key_id=access_key,
        aws_secret_access_key=secret_key, region_name="auto",
    )
    return client, bucket


# ── Consistency gate ─────────────────────────────────────────────────────────

def compute_consistency_gate(export: dict) -> dict:
    """The hard requirement: for every 'complete' document that claims
    chunks, confirm real chunks-table rows AND real Chroma vectors both
    exist. Operates entirely on the already-exported in-memory data (no
    extra DB round trips) -- do_export() already pulled everything this
    needs.

    legal_updates gating uses status='complete' (the field that actually
    means something there); zlr_entries has no status column at all, so
    gates on chunk_count > 0 alone -- any zlr row claiming chunks it
    doesn't have is exactly the same class of gap.

    Both legal_updates and zlr_entries chunks key off chunks.document_id
    (Chroma metadata's "document_id" field, and Postgres chunks.document_id)
    -- confirmed by reading backend/main.py directly: chunk_text()'s doc_id
    argument becomes both, for every ingestion path, legal and zlr alike.
    zlr_entries also has a separate chunks.zlr_item_id column, but that is
    NOT the join key chunks.document_id uses (see the LEFT JOIN ... ON
    z.id = c.document_id pattern used throughout main.py's search queries).
    """
    chunk_counts_by_doc = Counter(
        c["document_id"] for c in export["chunks"] if c.get("document_id")
    )
    vector_counts_by_doc = Counter()
    for _collection_name, d in export["chroma"].items():
        for meta in d.get("metadatas") or []:
            doc_id = (meta or {}).get("document_id")
            if doc_id:
                vector_counts_by_doc[doc_id] += 1

    gaps = []
    checked_counts = {"legal_updates": 0, "zlr_entries": 0}

    def _in_scope(row, requires_status):
        claimed = row.get("chunk_count") or 0
        if claimed <= 0:
            return False
        if requires_status and row.get("status") != "complete":
            return False
        return True

    def _check(row, source_label, requires_status):
        if not _in_scope(row, requires_status):
            return
        checked_counts[source_label] += 1
        doc_id = row["id"]
        actual_chunks = chunk_counts_by_doc.get(doc_id, 0)
        actual_vectors = vector_counts_by_doc.get(doc_id, 0)
        if actual_chunks == 0 or actual_vectors == 0:
            gaps.append({
                "source": source_label, "id": doc_id, "filename": row.get("filename"),
                "claimed_chunk_count": row.get("chunk_count") or 0,
                "actual_chunks_rows": actual_chunks, "actual_chroma_vectors": actual_vectors,
            })

    for row in export["legal_updates"]:
        _check(row, "legal_updates", requires_status=True)
    for row in export["zlr_entries"]:
        _check(row, "zlr_entries", requires_status=False)

    return {
        "clean": len(gaps) == 0,
        "checked_legal_updates": checked_counts["legal_updates"],
        "checked_zlr_entries": checked_counts["zlr_entries"],
        "gaps": gaps,
    }


# ── Manifest ─────────────────────────────────────────────────────────────────

def build_manifest(export: dict, gate_result: dict, timestamp: str) -> dict:
    return {
        "published_at": timestamp,
        "source_firm_id": export["source_firm_id"],
        "counts": {
            "legal_updates": len(export["legal_updates"]),
            "zlr_entries": len(export["zlr_entries"]),
            "chunks": len(export["chunks"]),
        },
        "chroma_vector_counts": {
            name: len(d.get("ids") or []) for name, d in export["chroma"].items()
        },
        "consistency_gate": gate_result,
    }


# ── Publish ──────────────────────────────────────────────────────────────────

async def do_publish(database_url: str, chroma_path: str, firm_id: str, dry_run: bool = False):
    client, bucket = _r2_client()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")

    with tempfile.TemporaryDirectory() as tmp:
        export_path = os.path.join(tmp, "corpus.json")
        await do_export(database_url, chroma_path, firm_id, export_path)

        with open(export_path, "r", encoding="utf-8") as f:
            export = json.load(f)

        gate_result = compute_consistency_gate(export)
        manifest = build_manifest(export, gate_result, timestamp)

        print(f"\n[gate] checked {gate_result['checked_legal_updates']} legal_updates + "
              f"{gate_result['checked_zlr_entries']} zlr_entries claiming chunks")
        if not gate_result["clean"]:
            print(f"[gate] REFUSED -- {len(gate_result['gaps'])} document(s) claim chunks they don't have:")
            for g in gate_result["gaps"]:
                print(f"    - [{g['source']}] {g['filename']} ({g['id']}): "
                      f"claimed {g['claimed_chunk_count']}, "
                      f"actual chunks={g['actual_chunks_rows']} vectors={g['actual_chroma_vectors']}")
            print("\nNothing was uploaded to R2. Fix the underlying documents "
                  "(re-ingest, or delete the broken rows) and re-run.")
            sys.exit(1)
        print("[gate] clean -- every complete document with claimed chunks has real backing data.")

        if dry_run:
            print(f"\nDRY RUN -- would publish to corpus-snapshots/{timestamp}/ "
                  f"and corpus-snapshots/latest/ in bucket {bucket}. No upload performed.")
            print(json.dumps(manifest, indent=2))
            return

        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        with open(export_path, "rb") as f:
            corpus_bytes = f.read()

        for prefix in (f"corpus-snapshots/{timestamp}", "corpus-snapshots/latest"):
            client.put_object(Bucket=bucket, Key=f"{prefix}/corpus.json", Body=corpus_bytes,
                               ContentType="application/json")
            client.put_object(Bucket=bucket, Key=f"{prefix}/manifest.json", Body=manifest_bytes,
                               ContentType="application/json")

        print(f"\nPublished to bucket {bucket}:")
        print(f"  corpus-snapshots/{timestamp}/ (permanent)")
        print(f"  corpus-snapshots/latest/ (rolling pointer, updated)")
        print(json.dumps(manifest, indent=2))


# ── Restore ──────────────────────────────────────────────────────────────────

async def do_restore(database_url: str, chroma_path: str, firm_id: str, snapshot: str, apply: bool):
    client, bucket = _r2_client()
    prefix = f"corpus-snapshots/{snapshot}"

    with tempfile.TemporaryDirectory() as tmp:
        corpus_path = os.path.join(tmp, "corpus.json")
        manifest_path = os.path.join(tmp, "manifest.json")

        try:
            client.download_file(bucket, f"{prefix}/corpus.json", corpus_path)
            client.download_file(bucket, f"{prefix}/manifest.json", manifest_path)
        except Exception as e:
            print(f"ERROR: could not fetch snapshot '{snapshot}' from bucket {bucket}: {e}", file=sys.stderr)
            sys.exit(1)

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        print(f"[restore] snapshot '{snapshot}' published {manifest['published_at']} "
              f"from source firm {manifest['source_firm_id']}")
        print(f"[restore] gate at publish time: "
              f"{'clean' if manifest['consistency_gate']['clean'] else 'HAD GAPS -- see manifest'}")
        print(json.dumps(manifest["counts"], indent=2))

        await do_import(database_url, chroma_path, firm_id, corpus_path, apply)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_publish = sub.add_parser("publish", help="Export the shared corpus, gate it, and publish to R2")
    p_publish.add_argument("--database-url", required=True)
    p_publish.add_argument("--chroma-path", required=True)
    p_publish.add_argument("--firm-id", required=True)
    p_publish.add_argument("--dry-run", action="store_true",
                            help="Run the export and gate, print the manifest, but don't upload to R2.")

    p_restore = sub.add_parser("restore", help="Restore a published snapshot from R2 into a firm's environment")
    p_restore.add_argument("--database-url", required=True)
    p_restore.add_argument("--chroma-path", required=True)
    p_restore.add_argument("--firm-id", required=True)
    p_restore.add_argument("--snapshot", default="latest",
                            help="'latest' (default) or a specific corpus-snapshots/<timestamp> value.")
    p_restore.add_argument("--apply", action="store_true", help="Actually write. Omit for a dry-run preview.")

    args = parser.parse_args()

    if args.command == "publish":
        asyncio.run(do_publish(args.database_url, args.chroma_path, args.firm_id, args.dry_run))
    elif args.command == "restore":
        asyncio.run(do_restore(args.database_url, args.chroma_path, args.firm_id, args.snapshot, args.apply))


if __name__ == "__main__":
    main()
