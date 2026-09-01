"""
copy_shared_corpus_to_new_firm.py — one-time seed of a brand-new firm's
Search Vault with the existing shared legal corpus (ZLR judgments +
legislation/news), so a new firm's first login doesn't show an empty shell.

Background: mutemo-legal-feed's pusher.py already supports pushing newly
scraped content to multiple firms (FIRM_1_*..FIRM_10_* env vars) -- that
part is not a gap, just not yet configured for a second firm. What's
actually missing is the HISTORICAL backlog: everything already scraped
before the new firm existed. This script covers that, once, at onboarding
time. It is not a repeating sync -- run it once per new firm, then let
mutemo-legal-feed's normal daily schedule (once FIRM_N_* vars are added
for the new firm) keep both firms current going forward.

Scope, deliberately narrow:
  - Postgres: legal_updates rows, zlr_entries rows, and chunks rows where
    chunk_source IN ('legal', 'zlr') -- the shared, non-firm-specific
    corpus.
  - Chroma: vectors from the "legal_updates" and "zlr_index" collections
    only.
  - Explicitly EXCLUDES firm_precedents (Postgres chunk_source='firm' and
    the "firm_precedents" Chroma collection) -- that's the source firm's
    own private client documents, never to be copied to another firm.

Why export/import rather than one script that touches both databases at
once: in real production, the source and target firms are two separate
Railway deployments with two separate Postgres instances and two separate
Chroma volumes -- nothing has simultaneous direct access to both. `export`
runs against the reference firm's environment and writes a single
portable JSON file; `import` runs against the new firm's environment and
reads it. (A test run CAN exercise both phases back-to-back against two
reachable databases, as this script's own test does -- but the two-phase
design is what makes this usable as a real runbook, not just a
convenience for testing.)

Row/vector ids are preserved exactly across the copy (not regenerated).
This matters: chunks.id doubles as the Chroma chunk id, and
legal_updates.id/zlr_entries.id are referenced by chunks.document_id/
zlr_item_id and by Chroma's own "document_id" metadata field. Keeping
every id identical between source and target is what makes a chunk
resolvable at all after the copy -- a real query has to find a Chroma
vector AND look it up successfully against the corresponding Postgres
chunk row by that same id (see backend/main.py's _zlr_semantic_search()/
_semantic_search_legal(), and the corrected admin_zlr_item_status
investigation from 2026-08-25 that first surfaced this exact risk).
Only firm_id is remapped, since that's the tenant-scoping column and
legal_updates/zlr_index chunks carry no firm_id in their Chroma metadata
at all (confirmed in main.py -- these two collections are deliberately
shared/unscoped; only firm_precedents chunks carry firm_id in Chroma).

Idempotent by design: Postgres inserts use ON CONFLICT (id) DO NOTHING,
Chroma writes use upsert() -- safe to re-run `import` against the same
target without creating duplicates or overwriting anything already there.

Usage:
    # On/against the reference (source) firm's environment:
    python3 scripts/copy_shared_corpus_to_new_firm.py export \\
        --database-url postgresql://... \\
        --chroma-path /app/data/chroma \\
        --firm-id a1b2c3d4-0000-0000-0000-000000000001 \\
        --out corpus_export.json

    # On/against the new (target) firm's environment:
    python3 scripts/copy_shared_corpus_to_new_firm.py import \\
        --database-url postgresql://... \\
        --chroma-path /app/data/chroma \\
        --firm-id <new-firm-uuid> \\
        --in corpus_export.json \\
        --apply   # omit for a dry-run preview (row/vector counts only, no writes)
"""
import argparse
import asyncio
import json
import sys

import asyncpg
import chromadb


# ── Export ───────────────────────────────────────────────────────────────────

async def do_export(database_url: str, chroma_path: str, firm_id: str, out_path: str):
    conn = await asyncpg.connect(database_url)
    try:
        legal_updates = await conn.fetch(
            "SELECT * FROM legal_updates WHERE firm_id=$1", firm_id
        )
        zlr_entries = await conn.fetch(
            "SELECT * FROM zlr_entries WHERE firm_id=$1", firm_id
        )
        # content_hash is a GENERATED column -- excluded here, it's
        # recomputed automatically from `text` on insert at import time.
        chunk_cols = (
            "id, firm_id, document_id, matter_id, chunk_source, text, "
            "chunk_index, page_number, zlr_item_id, citation, case_name, "
            "taxonomy_category, source_type, source_name, reference, "
            "validity_flag, created_at"
        )
        chunks = await conn.fetch(
            f"SELECT {chunk_cols} FROM chunks "
            "WHERE firm_id=$1 AND chunk_source IN ('legal', 'zlr')",
            firm_id
        )
    finally:
        await conn.close()

    client = chromadb.PersistentClient(path=chroma_path)
    chroma_dump = {}
    for collection_name in ("legal_updates", "zlr_index"):
        try:
            col = client.get_collection(collection_name)
        except Exception:
            chroma_dump[collection_name] = {"ids": [], "embeddings": [], "documents": [], "metadatas": []}
            continue
        result = col.get(include=["embeddings", "documents", "metadatas"])
        embeddings = result.get("embeddings")
        # Chroma returns embedding rows as numpy arrays of np.float32 --
        # list(e) alone converts the outer array to a list but leaves
        # np.float32 scalars inside it. json.dump can't serialize those
        # natively; with default=str it would silently stringify each
        # number instead of raising, corrupting the export invisibly.
        # Cast every element to a plain Python float explicitly.
        chroma_dump[collection_name] = {
            "ids": result.get("ids", []),
            "embeddings": [[float(x) for x in e] for e in embeddings] if embeddings is not None and len(embeddings) else [],
            "documents": result.get("documents", []),
            "metadatas": result.get("metadatas", []),
        }

    # UUIDs/dates/datetimes -> JSON-safe strings via json.dumps's
    # default=str below; JSONB fields already come back as
    # str/list/dict from asyncpg, no special handling needed.
    def _row_to_dict(row):
        return dict(row)

    export = {
        "source_firm_id": firm_id,
        "legal_updates": [_row_to_dict(r) for r in legal_updates],
        "zlr_entries": [_row_to_dict(r) for r in zlr_entries],
        "chunks": [_row_to_dict(r) for r in chunks],
        "chroma": chroma_dump,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(export, f, default=str)

    print(f"[export] firm {firm_id}:")
    print(f"  legal_updates: {len(legal_updates)} rows")
    print(f"  zlr_entries:   {len(zlr_entries)} rows")
    print(f"  chunks:        {len(chunks)} rows (chunk_source IN ('legal','zlr') only)")
    for name, d in chroma_dump.items():
        print(f"  chroma[{name}]: {len(d['ids'])} vectors")
    print(f"  written to {out_path}")


# ── Import ───────────────────────────────────────────────────────────────────

def _parse_uuid_fields(d: dict, fields: list) -> dict:
    import uuid as _uuid
    out = dict(d)
    for f in fields:
        if out.get(f):
            out[f] = _uuid.UUID(out[f]) if isinstance(out[f], str) else out[f]
    return out


def _parse_datetime_fields(d: dict, timestamp_fields: list, date_fields: list) -> dict:
    """json.dump's default=str turns every datetime/date field into a
    plain string on export -- asyncpg needs real datetime.datetime/
    datetime.date instances back, or it raises TypeError on bind rather
    than silently coercing. datetime.fromisoformat()/date.fromisoformat()
    round-trip str(datetime_obj)'s own output correctly (Python 3.11+
    handles the space-separated "YYYY-MM-DD HH:MM:SS.ffffff+00:00" form
    natively, which is exactly what str() on an asyncpg-returned
    timestamptz produces)."""
    import datetime as _dt
    out = dict(d)
    for f in timestamp_fields:
        if out.get(f) and isinstance(out[f], str):
            out[f] = _dt.datetime.fromisoformat(out[f])
    for f in date_fields:
        if out.get(f) and isinstance(out[f], str):
            out[f] = _dt.date.fromisoformat(out[f])
    return out


async def do_import(database_url: str, chroma_path: str, firm_id: str, in_path: str, apply: bool):
    with open(in_path, "r", encoding="utf-8") as f:
        export = json.load(f)

    legal_updates = export["legal_updates"]
    zlr_entries = export["zlr_entries"]
    chunks = export["chunks"]
    chroma_dump = export["chroma"]

    print(f"[import] target firm {firm_id} <- source firm {export['source_firm_id']}")
    print(f"  legal_updates: {len(legal_updates)} rows to copy")
    print(f"  zlr_entries:   {len(zlr_entries)} rows to copy")
    print(f"  chunks:        {len(chunks)} rows to copy")
    for name, d in chroma_dump.items():
        print(f"  chroma[{name}]: {len(d['ids'])} vectors to copy")

    if not apply:
        print("\n  DRY RUN -- pass --apply to actually write. No changes made.")
        return

    import uuid as _uuid_mod

    conn = await asyncpg.connect(database_url)
    try:
        for row in legal_updates:
            r = _parse_uuid_fields(row, ["id"])
            r = _parse_datetime_fields(r, ["uploaded_at", "scraped_at"], ["doc_date"])
            await conn.execute("""
                INSERT INTO legal_updates (
                    id, firm_id, filename, source_type, source_name, reference,
                    document_type, matter_type, doc_date, court, word_count,
                    chunk_count, status, ocr_used, error_message, uploaded_at,
                    source_url, scraped_at, ocr_confidence, needs_review,
                    legal_source_type, authority_strength, validity_flag
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23)
                ON CONFLICT (id) DO NOTHING
            """,
                r["id"], _uuid_mod.UUID(firm_id), r["filename"], r["source_type"], r["source_name"],
                r["reference"], r["document_type"], r["matter_type"], r["doc_date"], r["court"],
                r["word_count"], r["chunk_count"], r["status"], r["ocr_used"], r["error_message"],
                r["uploaded_at"], r["source_url"], r["scraped_at"], r["ocr_confidence"], r["needs_review"],
                r.get("legal_source_type"), r.get("authority_strength"), r.get("validity_flag"),
            )

        for row in zlr_entries:
            r = _parse_uuid_fields(row, ["id"])
            r = _parse_datetime_fields(r, ["uploaded_at"], [])
            subject_chains = r.get("subject_chains")
            if isinstance(subject_chains, (list, dict)):
                subject_chains = json.dumps(subject_chains)
            await conn.execute("""
                INSERT INTO zlr_entries (
                    id, firm_id, filename, source, jurisdiction, authority_weight,
                    volume_year, zimlii_url, case_name, citation, judgment_number,
                    court, judge, case_type, hearing_date, judgment_date,
                    subject_chains, taxonomy_category, summary, raw_text,
                    word_count, chunk_count, ocr_used, ocr_confidence,
                    needs_review, uploaded_at, legal_source_type, authority_strength
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                          $17::jsonb,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28)
                ON CONFLICT (id) DO NOTHING
            """,
                r["id"], _uuid_mod.UUID(firm_id), r["filename"], r["source"], r["jurisdiction"],
                r["authority_weight"], r["volume_year"], r["zimlii_url"], r["case_name"], r["citation"],
                r["judgment_number"], r["court"], r["judge"], r["case_type"], r["hearing_date"],
                r["judgment_date"], subject_chains or "[]", r["taxonomy_category"], r["summary"],
                r["raw_text"], r["word_count"], r["chunk_count"], r["ocr_used"], r["ocr_confidence"],
                r["needs_review"], r["uploaded_at"], r.get("legal_source_type"), r.get("authority_strength"),
            )

        for row in chunks:
            r = _parse_uuid_fields(row, ["document_id"])
            r = _parse_datetime_fields(r, ["created_at"], [])
            await conn.execute("""
                INSERT INTO chunks (
                    id, firm_id, document_id, matter_id, chunk_source, text,
                    chunk_index, page_number, zlr_item_id, citation, case_name,
                    taxonomy_category, source_type, source_name, reference,
                    validity_flag, created_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
                ON CONFLICT (id) DO NOTHING
            """,
                r["id"], _uuid_mod.UUID(firm_id), r["document_id"], r["matter_id"], r["chunk_source"],
                r["text"], r["chunk_index"], r["page_number"], r["zlr_item_id"], r["citation"],
                r["case_name"], r["taxonomy_category"], r["source_type"], r["source_name"],
                r["reference"], r.get("validity_flag"), r["created_at"],
            )
    finally:
        await conn.close()

    client = chromadb.PersistentClient(path=chroma_path)
    for collection_name, d in chroma_dump.items():
        if not d["ids"]:
            continue
        col = client.get_or_create_collection(collection_name, metadata={"hnsw:space": "cosine"})
        col.upsert(
            ids=d["ids"],
            embeddings=d["embeddings"],
            documents=d["documents"],
            metadatas=d["metadatas"],
        )

    print("\n  APPLIED.")
    print(f"  legal_updates: {len(legal_updates)} rows written")
    print(f"  zlr_entries:   {len(zlr_entries)} rows written")
    print(f"  chunks:        {len(chunks)} rows written")
    for name, d in chroma_dump.items():
        print(f"  chroma[{name}]: {len(d['ids'])} vectors written")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", help="Export the shared corpus from a source firm")
    p_export.add_argument("--database-url", required=True)
    p_export.add_argument("--chroma-path", required=True)
    p_export.add_argument("--firm-id", required=True)
    p_export.add_argument("--out", required=True)

    p_import = sub.add_parser("import", help="Import the shared corpus into a target firm")
    p_import.add_argument("--database-url", required=True)
    p_import.add_argument("--chroma-path", required=True)
    p_import.add_argument("--firm-id", required=True)
    p_import.add_argument("--in", dest="in_path", required=True)
    p_import.add_argument("--apply", action="store_true", help="Actually write. Omit for a dry-run preview.")

    args = parser.parse_args()

    if args.command == "export":
        asyncio.run(do_export(args.database_url, args.chroma_path, args.firm_id, args.out))
    elif args.command == "import":
        asyncio.run(do_import(args.database_url, args.chroma_path, args.firm_id, args.in_path, args.apply))


if __name__ == "__main__":
    main()
