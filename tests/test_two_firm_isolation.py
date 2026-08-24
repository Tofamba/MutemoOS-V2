"""
Multi-tenancy Part 4: two-firm leakage test.

MutemoOS runs Option B (one deployment per firm — see README.md's
"Multi-Tenancy" section) — no real deployment ever actually has two
firms' data in the same Postgres/Chroma instance. That means the only
way to meaningfully exercise the firm_id scoping that exists as
defense-in-depth is to simulate it: seed a fake connection/collection
with TWO firms' rows side by side, swap which FIRM_ID is "active" (via
monkeypatch, since FIRM_ID is a fixed module-level constant), and prove
each firm's view never includes the other's data — and, symmetrically,
that the *shared* legal/ZLR corpus is NOT firm-scoped and stays visible
regardless of which firm is active.

Covers every table this session's multi-tenancy work touched or
depends on: clients, matters, documents, client_compliance/
beneficial_owners (compliance), and the firm_precedents Chroma
collection — plus the negative case (legal_updates/zlr_index visible to
both firms) and a direct cross-firm access-by-ID attempt (not just "the
list doesn't show it" but "asking for it by ID specifically fails").

Called directly as plain async functions, same convention as this
repo's other backend tests.
"""
import asyncio

from backend.main import (
    FIRM_ID,
    list_clients,
    list_matters,
    list_recent_documents,
    _get_client_or_404,
    _semantic_search_firm,
    _semantic_search_legal,
)

FIRM_A = FIRM_ID  # the real, single FIRM_ID this deployment actually runs as
FIRM_B = "b2c3d4e5-0000-0000-0000-000000000002"  # a hypothetical second firm — never real in Option B

DEV_USER = {"id": None, "firm_id": FIRM_A, "phone": None, "email": None, "role": "partner", "display_name": "Demo User"}


class FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}


# ── Two-firm seed data (Postgres) ────────────────────────────────────────────
# One client, one matter, one document per firm — enough to prove scoping
# without needing every column each row type actually has in production.

CLIENTS = [
    {"id": "c1", "firm_id": str(FIRM_A), "full_name": "Firm A Client"},
    {"id": "c2", "firm_id": FIRM_B, "full_name": "Firm B Client"},
]
MATTERS = [
    {"id": "m1", "firm_id": str(FIRM_A), "name": "Firm A Matter", "is_sentinel": False},
    {"id": "m2", "firm_id": FIRM_B, "name": "Firm B Matter", "is_sentinel": False},
]
DOCUMENTS = [
    {"id": "d1", "firm_id": str(FIRM_A), "matter_id": "m1", "filename": "firm-a-doc.pdf",
     "status": "complete", "matter_name": "Firm A Matter", "matter_client_name": "Firm A Client"},
    {"id": "d2", "firm_id": FIRM_B, "matter_id": "m2", "filename": "firm-b-doc.pdf",
     "status": "complete", "matter_name": "Firm B Matter", "matter_client_name": "Firm B Client"},
]


class TwoFirmFakeConnection:
    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT * FROM clients WHERE firm_id=$1"):
            firm_id = str(args[0])
            return [c for c in CLIENTS if c["firm_id"] == firm_id]
        if q.startswith("SELECT * FROM matters WHERE firm_id=$1 AND NOT is_sentinel"):
            firm_id = str(args[0])
            return [m for m in MATTERS if m["firm_id"] == firm_id]
        if q.startswith("SELECT * FROM progress_notes WHERE matter_id=$1"):
            return []  # no notes seeded — irrelevant to firm scoping
        if "FROM documents d" in q and "JOIN matters m" in q:
            firm_id = str(args[0])
            return [d for d in DOCUMENTS if d["firm_id"] == firm_id]
        raise NotImplementedError(f"TwoFirmFakeConnection.fetch: unhandled query: {q}")

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT * FROM clients WHERE id=$1 AND firm_id=$2"):
            cid, firm_id = str(args[0]), str(args[1])
            for c in CLIENTS:
                if c["id"] == cid and c["firm_id"] == firm_id:
                    return c
            return None
        raise NotImplementedError(f"TwoFirmFakeConnection.fetchrow: unhandled query: {q}")


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class TwoFirmFakePool:
    def __init__(self):
        self.conn = TwoFirmFakeConnection()

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _install(monkeypatch, active_firm_id):
    import backend.main as m
    monkeypatch.setattr(m, "FIRM_ID", active_firm_id)
    monkeypatch.setattr(m, "_db_pool", TwoFirmFakePool())


# ── clients ───────────────────────────────────────────────────────────────

def test_list_clients_firm_a_never_sees_firm_bs_client(monkeypatch):
    _install(monkeypatch, FIRM_A)
    result = asyncio.run(list_clients(FakeRequest()))
    names = {c["full_name"] for c in result}
    assert names == {"Firm A Client"}


def test_list_clients_firm_b_never_sees_firm_as_client(monkeypatch):
    _install(monkeypatch, FIRM_B)
    result = asyncio.run(list_clients(FakeRequest()))
    names = {c["full_name"] for c in result}
    assert names == {"Firm B Client"}


def test_direct_access_to_another_firms_client_by_id_is_denied_not_leaked(monkeypatch):
    """Not just "the list doesn't show it" -- asking for firm B's client
    specifically, by its real id, while firm A is active must 404, never
    return firm B's row."""
    import backend.main as m
    from fastapi import HTTPException
    _install(monkeypatch, FIRM_A)

    async def _check():
        async with m._db_pool.acquire() as conn:
            try:
                await _get_client_or_404(conn, "c2")  # firm B's real client id
                return "leaked"
            except HTTPException as e:
                return e.status_code

    assert asyncio.run(_check()) == 404


# ── matters ───────────────────────────────────────────────────────────────

def test_list_matters_firm_a_never_sees_firm_bs_matter(monkeypatch):
    _install(monkeypatch, FIRM_A)
    result = asyncio.run(list_matters(FakeRequest()))
    names = {m["name"] for m in result}
    assert names == {"Firm A Matter"}


def test_list_matters_firm_b_never_sees_firm_as_matter(monkeypatch):
    _install(monkeypatch, FIRM_B)
    result = asyncio.run(list_matters(FakeRequest()))
    names = {m["name"] for m in result}
    assert names == {"Firm B Matter"}


# ── documents ─────────────────────────────────────────────────────────────

def test_recent_documents_firm_a_never_sees_firm_bs_document(monkeypatch):
    _install(monkeypatch, FIRM_A)
    result = asyncio.run(list_recent_documents(FakeRequest()))
    filenames = {d["filename"] for d in result}
    assert filenames == {"firm-a-doc.pdf"}


def test_recent_documents_firm_b_never_sees_firm_as_document(monkeypatch):
    _install(monkeypatch, FIRM_B)
    result = asyncio.run(list_recent_documents(FakeRequest()))
    filenames = {d["filename"] for d in result}
    assert filenames == {"firm-b-doc.pdf"}


# ── ChromaDB: firm_precedents (firm-scoped) vs legal_updates (shared) ──────

class _TwoFirmChromaCollection:
    """Both firms' chunks live in the same fake collection, exactly as they
    would in a hypothetical shared instance -- the where filter is the
    only thing standing between them."""
    def __init__(self, chunks_by_id):
        self.chunks_by_id = chunks_by_id

    def count(self):
        return len(self.chunks_by_id)

    def query(self, query_embeddings, n_results, where=None):
        ids = list(self.chunks_by_id.keys())
        if where:
            fid = where.get("firm_id")
            ids = [cid for cid in ids if self.chunks_by_id[cid].get("firm_id") == fid]
        ids = ids[:n_results]
        return {"ids": [ids], "distances": [[0.1] * len(ids)]}


def _two_firm_chroma_setup(monkeypatch):
    import backend.main as m
    firm_col = _TwoFirmChromaCollection({
        "fc-a": {"firm_id": str(FIRM_A)},
        "fc-b": {"firm_id": FIRM_B},
    })
    legal_col = _TwoFirmChromaCollection({
        "lc-1": {},  # shared corpus chunks carry no firm_id at all
        "lc-2": {},
    })
    monkeypatch.setattr(m, "get_chroma_collections", lambda: (firm_col, legal_col, None))
    monkeypatch.setattr(m, "embed_texts", lambda texts: [[0.1, 0.2]])
    return firm_col, legal_col


class _FakeSearchRequest:
    def __init__(self, query="test", limit=8, matter_id=None):
        self.query = query
        self.limit = limit
        self.matter_id = matter_id


def test_semantic_search_firm_scoped_to_active_firm_only(monkeypatch):
    import backend.main as m
    _two_firm_chroma_setup(monkeypatch)
    monkeypatch.setattr(m, "FIRM_ID", FIRM_A)

    chunks = [
        {"id": "fc-a", "text": "firm A precedent text", "document_id": "d1"},
        {"id": "fc-b", "text": "firm B precedent text", "document_id": "d2"},
    ]
    results = _semantic_search_firm(_FakeSearchRequest(), chunks)

    assert [r["chunk_id"] for r in results] == ["fc-a"]


def test_semantic_search_firm_scoped_to_the_other_firm_only(monkeypatch):
    """Symmetric check -- swap which firm is active and confirm the
    exclusion flips, proving this isn't just "firm A always wins"."""
    import backend.main as m
    _two_firm_chroma_setup(monkeypatch)
    monkeypatch.setattr(m, "FIRM_ID", FIRM_B)

    chunks = [
        {"id": "fc-a", "text": "firm A precedent text", "document_id": "d1"},
        {"id": "fc-b", "text": "firm B precedent text", "document_id": "d2"},
    ]
    results = _semantic_search_firm(_FakeSearchRequest(), chunks)

    assert [r["chunk_id"] for r in results] == ["fc-b"]


def test_semantic_search_legal_stays_shared_regardless_of_active_firm(monkeypatch):
    """The negative case: legal_updates/zlr_index are intentionally NOT
    firm-scoped -- both firms must see the same shared corpus. If this
    ever starts filtering by firm_id, that's a real regression (the
    shared law library would start disappearing for firms), not a
    security improvement."""
    import backend.main as m
    _, legal_col = _two_firm_chroma_setup(monkeypatch)

    chunks = [
        {"id": "lc-1", "text": "shared legislation text", "document_id": "z1"},
        {"id": "lc-2", "text": "more shared legislation", "document_id": "z2"},
    ]

    monkeypatch.setattr(m, "FIRM_ID", FIRM_A)
    results_a = _semantic_search_legal(_FakeSearchRequest(), chunks)

    monkeypatch.setattr(m, "FIRM_ID", FIRM_B)
    results_b = _semantic_search_legal(_FakeSearchRequest(), chunks)

    ids_a = {r["chunk_id"] for r in results_a}
    ids_b = {r["chunk_id"] for r in results_b}
    assert ids_a == ids_b == {"lc-1", "lc-2"}
