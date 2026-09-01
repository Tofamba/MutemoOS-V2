"""
Unit tests for scripts/corpus_snapshot.py.

Three things get tested:
1. compute_consistency_gate() -- the hard requirement (per instruction: not
   optional, not skippable). Pure function over an in-memory export dict,
   no DB/R2 needed -- thoroughly exercised here since this is the whole
   point of the tool.
2. do_publish()/do_restore() -- the R2 transport wrapper. do_export()/
   do_import() themselves are NOT re-tested here (already covered in
   test_copy_shared_corpus_to_new_firm.py and unchanged by this file);
   they're monkeypatched so these tests focus purely on: does a gate
   failure really refuse to upload anything, does a clean gate really
   publish to both the timestamped and 'latest' keys, does dry-run really
   upload nothing while still running the real gate.
3. _r2_client() -- refuses cleanly (no boto3 call at all) when any of the
   four required env vars is missing, rather than constructing a client
   against a blank endpoint and failing confusingly later.

boto3 itself is real (installed, matches requirements.txt) -- only the S3
client instance is faked, so put_object/download_file calls are captured
and asserted on directly rather than requiring real R2 credentials for
this test tier. The real end-to-end R2 round trip against a live bucket
is a separate, manual verification step (see the corpus-snapshot-tooling
project memory).
"""
import json
import uuid

import pytest

from scripts import corpus_snapshot as cs


# ── compute_consistency_gate() ──────────────────────────────────────────────

def _lu_row(chunk_count=1, status="complete", **overrides):
    row = {"id": str(uuid.uuid4()), "filename": "Some Act.pdf", "chunk_count": chunk_count, "status": status}
    row.update(overrides)
    return row


def _zlr_row(chunk_count=1, **overrides):
    row = {"id": str(uuid.uuid4()), "filename": "S v Test.pdf", "chunk_count": chunk_count}
    row.update(overrides)
    return row


def _chunk(document_id):
    return {"id": str(uuid.uuid4()), "document_id": document_id}


def _chroma_meta(document_id):
    return {"document_id": document_id, "chunk_index": 0}


def _empty_chroma():
    return {"legal_updates": {"ids": [], "metadatas": []}, "zlr_index": {"ids": [], "metadatas": []}}


def test_gate_clean_when_every_complete_doc_has_real_chunks_and_vectors():
    lu = _lu_row(chunk_count=2)
    chroma = _empty_chroma()
    chroma["legal_updates"]["metadatas"] = [_chroma_meta(lu["id"]), _chroma_meta(lu["id"])]
    export = {
        "legal_updates": [lu], "zlr_entries": [],
        "chunks": [_chunk(lu["id"]), _chunk(lu["id"])],
        "chroma": chroma,
    }
    result = cs.compute_consistency_gate(export)
    assert result["clean"] is True
    assert result["gaps"] == []
    assert result["checked_legal_updates"] == 1


def test_gate_catches_zero_postgres_chunks():
    lu = _lu_row(chunk_count=137)  # exactly the Constitution's own real shape
    export = {
        "legal_updates": [lu], "zlr_entries": [], "chunks": [],  # no chunks at all
        "chroma": _empty_chroma(),
    }
    result = cs.compute_consistency_gate(export)
    assert result["clean"] is False
    assert len(result["gaps"]) == 1
    assert result["gaps"][0]["id"] == lu["id"]
    assert result["gaps"][0]["actual_chunks_rows"] == 0


def test_gate_catches_zero_chroma_vectors_even_with_real_postgres_chunks():
    """The exact shape of the original v1-migrated gap this tool exists to
    catch: chunks rows exist but somehow never made it into Chroma (or a
    genuinely corrupted export) -- claimed metadata alone is not enough."""
    lu = _lu_row(chunk_count=1)
    export = {
        "legal_updates": [lu], "zlr_entries": [],
        "chunks": [_chunk(lu["id"])],  # a real chunks row exists
        "chroma": _empty_chroma(),  # but zero vectors
    }
    result = cs.compute_consistency_gate(export)
    assert result["clean"] is False
    assert result["gaps"][0]["actual_chunks_rows"] == 1
    assert result["gaps"][0]["actual_chroma_vectors"] == 0


def test_gate_ignores_non_complete_legal_updates_rows():
    lu = _lu_row(chunk_count=5, status="processing")
    export = {"legal_updates": [lu], "zlr_entries": [], "chunks": [], "chroma": _empty_chroma()}
    result = cs.compute_consistency_gate(export)
    assert result["clean"] is True
    assert result["checked_legal_updates"] == 0


def test_gate_ignores_docs_with_zero_claimed_chunks():
    lu = _lu_row(chunk_count=0, status="complete")
    export = {"legal_updates": [lu], "zlr_entries": [], "chunks": [], "chroma": _empty_chroma()}
    result = cs.compute_consistency_gate(export)
    assert result["clean"] is True


def test_gate_checks_zlr_entries_despite_no_status_column():
    zlr = _zlr_row(chunk_count=3)
    export = {"legal_updates": [], "zlr_entries": [zlr], "chunks": [], "chroma": _empty_chroma()}
    result = cs.compute_consistency_gate(export)
    assert result["clean"] is False
    assert result["gaps"][0]["source"] == "zlr_entries"
    assert result["checked_zlr_entries"] == 1


def test_gate_reports_multiple_independent_gaps():
    lu1 = _lu_row(chunk_count=137, filename="Constitution.pdf")
    lu2 = _lu_row(chunk_count=79, filename="Some Regulations.pdf")
    lu_ok = _lu_row(chunk_count=1, filename="Fine Act.pdf")
    chroma = _empty_chroma()
    chroma["legal_updates"]["metadatas"] = [_chroma_meta(lu_ok["id"])]
    export = {
        "legal_updates": [lu1, lu2, lu_ok], "zlr_entries": [],
        "chunks": [_chunk(lu_ok["id"])],
        "chroma": chroma,
    }
    result = cs.compute_consistency_gate(export)
    assert result["clean"] is False
    assert len(result["gaps"]) == 2
    assert {g["id"] for g in result["gaps"]} == {lu1["id"], lu2["id"]}


# ── build_manifest() ─────────────────────────────────────────────────────────

def test_build_manifest_shape():
    export = {
        "source_firm_id": "firm-1", "legal_updates": [_lu_row()], "zlr_entries": [],
        "chunks": [], "chroma": {"legal_updates": {"ids": ["a", "b"]}, "zlr_index": {"ids": []}},
    }
    gate = {"clean": True, "gaps": []}
    manifest = cs.build_manifest(export, gate, "2026-09-01T12-00-00Z")
    assert manifest["published_at"] == "2026-09-01T12-00-00Z"
    assert manifest["source_firm_id"] == "firm-1"
    assert manifest["counts"]["legal_updates"] == 1
    assert manifest["chroma_vector_counts"]["legal_updates"] == 2
    assert manifest["consistency_gate"] is gate


# ── _r2_client() env var guard ───────────────────────────────────────────────

def test_r2_client_refuses_when_bucket_env_var_missing(monkeypatch):
    monkeypatch.setenv("R2_ENDPOINT", "https://example.r2.cloudflarestorage.com")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("CORPUS_SNAPSHOT_BUCKET", raising=False)
    with pytest.raises(SystemExit):
        cs._r2_client()


# ── do_publish() / do_restore() -- R2 transport, do_export/do_import faked ──

class _FakeR2Client:
    def __init__(self):
        self.put_calls = []
        self.downloaded = {}  # key -> bytes, pre-seeded by the test

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.put_calls.append((Bucket, Key, Body))

    def download_file(self, Bucket, Key, local_path):
        if Key not in self.downloaded:
            raise Exception(f"NoSuchKey: {Key}")
        with open(local_path, "wb") as f:
            f.write(self.downloaded[Key])


@pytest.fixture
def fake_r2(monkeypatch):
    fake = _FakeR2Client()
    monkeypatch.setenv("R2_ENDPOINT", "https://example.r2.cloudflarestorage.com")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("CORPUS_SNAPSHOT_BUCKET", "test-corpus-snapshots")
    monkeypatch.setattr(cs, "_r2_client", lambda: (fake, "test-corpus-snapshots"))
    return fake


def _clean_export(source_firm_id="firm-1"):
    lu = _lu_row(chunk_count=1)
    chroma = _empty_chroma()
    chroma["legal_updates"]["metadatas"] = [_chroma_meta(lu["id"])]
    chroma["legal_updates"]["ids"] = ["v1"]
    return {
        "source_firm_id": source_firm_id, "legal_updates": [lu], "zlr_entries": [],
        "chunks": [_chunk(lu["id"])], "chroma": chroma,
    }


def _dirty_export():
    lu = _lu_row(chunk_count=137)  # claims chunks it doesn't have
    return {
        "source_firm_id": "firm-1", "legal_updates": [lu], "zlr_entries": [],
        "chunks": [], "chroma": _empty_chroma(),
    }


async def _fake_do_export_factory(export_dict):
    async def _fake_do_export(database_url, chroma_path, firm_id, out_path):
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(export_dict, f)
    return _fake_do_export


def test_publish_uploads_to_both_timestamped_and_latest_when_clean(fake_r2, monkeypatch):
    import asyncio

    async def fake_do_export(database_url, chroma_path, firm_id, out_path):
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(_clean_export(), f)
    monkeypatch.setattr(cs, "do_export", fake_do_export)

    asyncio.run(cs.do_publish("postgres://fake", "/tmp/chroma", "firm-1", dry_run=False))

    keys = [k for (_bucket, k, _body) in fake_r2.put_calls]
    assert any(k.startswith("corpus-snapshots/") and k.endswith("/corpus.json") and "latest" not in k for k in keys)
    assert "corpus-snapshots/latest/corpus.json" in keys
    assert "corpus-snapshots/latest/manifest.json" in keys
    assert len(fake_r2.put_calls) == 4  # corpus.json + manifest.json, x2 destinations


def test_publish_refuses_to_upload_anything_when_gate_finds_a_gap(fake_r2, monkeypatch):
    """The hard requirement, end to end: a dirty export must result in
    ZERO R2 uploads, not a partial or 'best effort' publish."""
    import asyncio

    async def fake_do_export(database_url, chroma_path, firm_id, out_path):
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(_dirty_export(), f)
    monkeypatch.setattr(cs, "do_export", fake_do_export)

    with pytest.raises(SystemExit):
        asyncio.run(cs.do_publish("postgres://fake", "/tmp/chroma", "firm-1", dry_run=False))

    assert fake_r2.put_calls == []


def test_publish_dry_run_uploads_nothing_even_when_clean(fake_r2, monkeypatch):
    import asyncio

    async def fake_do_export(database_url, chroma_path, firm_id, out_path):
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(_clean_export(), f)
    monkeypatch.setattr(cs, "do_export", fake_do_export)

    asyncio.run(cs.do_publish("postgres://fake", "/tmp/chroma", "firm-1", dry_run=True))

    assert fake_r2.put_calls == []


def test_restore_downloads_snapshot_and_calls_do_import(fake_r2, monkeypatch):
    import asyncio

    export = _clean_export()
    manifest = cs.build_manifest(export, cs.compute_consistency_gate(export), "2026-09-01T12-00-00Z")
    fake_r2.downloaded["corpus-snapshots/latest/corpus.json"] = json.dumps(export).encode("utf-8")
    fake_r2.downloaded["corpus-snapshots/latest/manifest.json"] = json.dumps(manifest).encode("utf-8")

    captured = {}

    async def fake_do_import(database_url, chroma_path, firm_id, in_path, apply):
        captured["args"] = (database_url, chroma_path, firm_id, apply)
        with open(in_path, "r", encoding="utf-8") as f:
            captured["imported"] = json.load(f)
    monkeypatch.setattr(cs, "do_import", fake_do_import)

    asyncio.run(cs.do_restore("postgres://fake", "/tmp/chroma", "new-firm-id", "latest", apply=True))

    assert captured["args"] == ("postgres://fake", "/tmp/chroma", "new-firm-id", True)
    assert captured["imported"]["source_firm_id"] == "firm-1"


def test_restore_exits_cleanly_when_snapshot_not_found(fake_r2, monkeypatch):
    import asyncio

    async def fake_do_import(*a, **kw):
        raise AssertionError("do_import should never be reached if the snapshot download fails")
    monkeypatch.setattr(cs, "do_import", fake_do_import)

    with pytest.raises(SystemExit):
        asyncio.run(cs.do_restore("postgres://fake", "/tmp/chroma", "new-firm-id", "does-not-exist", apply=True))
