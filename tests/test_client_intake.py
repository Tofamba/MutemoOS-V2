"""
Unit tests for POST /api/onboarding/intake (backend/main.py) — the
single-client version of bulk_onboard_from_excel: one new/existing client,
one new matter, Case Binder starter documents provisioned via
backend/case_binder.py.

Called directly as a plain async function, same convention as
tests/test_clients_api.py (see that file's docstring for why). _db_pool is
swapped for a small in-memory fake that understands exactly the queries
this endpoint issues, extending test_clients_api.py's proven FakeConnection
design (same numbering_counters/_resolve_user_initials machinery
create_client already relies on) with a no-op transaction() context
manager and the extra query shapes this endpoint adds (lawyer lookup,
existing_client_id lookup, the name-matching candidate pool, matters/
documents/audit_logs inserts).

R2 is never configured in this test environment (no R2_* env vars), so
R2_ENABLED is False and no network call happens for the case-binder
documents' "upload" — r2_uploaded is always False here, which is fine;
that path's resilience (upload failure still creates the row) is exactly
what main.py's R2_ENABLED branch guards against needing at all.
"""
import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from backend.main import FIRM_ID, ClientIntakeRequest, client_intake


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, clients=None, matters=None, users=None, numbering_counters=None, idempotency_keys=None):
        self.clients = clients if clients is not None else []
        self.matters = matters if matters is not None else []
        self.users = users if users is not None else []
        self.numbering_counters = numbering_counters if numbering_counters is not None else []
        self.idempotency_keys = idempotency_keys if idempotency_keys is not None else []
        self.documents = []
        self.audit_logs = []

    def transaction(self):
        return _FakeTransaction()

    async def fetchval(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT 1 FROM numbering_counters WHERE firm_id=$1 AND prefix=$2"):
            for c in self.numbering_counters:
                if c["firm_id"] == args[0] and c["prefix"] == args[1]:
                    return 1
            return None
        raise NotImplementedError(f"FakeConnection.fetchval: unhandled query: {q}")

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT response_body FROM intake_idempotency_keys WHERE firm_id=$1 AND key=$2"):
            firm_id, key = args
            for row in self.idempotency_keys:
                if row["firm_id"] == firm_id and row["key"] == key:
                    return {"response_body": row["response_body"]}
            return None

        if q.startswith("SELECT id, display_name FROM users WHERE firm_id=$1 AND id=$2"):
            for u in self.users:
                if u["firm_id"] == args[0] and u["id"] == args[1]:
                    return {"id": u["id"], "display_name": u["display_name"]}
            return None

        if q.startswith("SELECT id, full_name, client_number FROM clients WHERE firm_id=$1 AND id=$2"):
            for c in self.clients:
                if c["firm_id"] == args[0] and c["id"] == args[1]:
                    return {"id": c["id"], "full_name": c["full_name"], "client_number": c.get("client_number")}
            return None

        if q.startswith("UPDATE numbering_counters SET next_seq = next_seq + 1"):
            firm_id, prefix = args
            for c in self.numbering_counters:
                if c["firm_id"] == firm_id and c["prefix"] == prefix:
                    allocated = c["next_seq"]
                    c["next_seq"] += 1
                    return {"allocated": allocated}
            return None

        if q.startswith("SELECT initials FROM users WHERE id=$1"):
            for u in self.users:
                if u["id"] == args[0]:
                    return {"initials": u.get("initials")}
            return None

        if q.startswith("INSERT INTO clients"):
            cols = [c.strip() for c in q.split("(", 1)[1].split(")", 1)[0].split(",")]
            row = dict(zip(cols, args))
            self.clients.append(row)
            return dict(row)

        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT id, full_name, client_number FROM clients WHERE firm_id=$1"):
            return [{"id": c["id"], "full_name": c["full_name"], "client_number": c.get("client_number")}
                    for c in self.clients if c["firm_id"] == args[0]]

        if q.startswith("SELECT client_number FROM clients WHERE firm_id=$1 AND client_number LIKE $2"):
            prefix = args[1][:-1]  # strip trailing '%'
            return [{"client_number": c["client_number"]} for c in self.clients
                    if c["firm_id"] == args[0] and (c.get("client_number") or "").startswith(prefix)]

        if q.startswith("SELECT matter_number FROM matters WHERE firm_id=$1 AND matter_number LIKE $2"):
            prefix = args[1][:-1]
            return [{"matter_number": m["matter_number"]} for m in self.matters
                    if m["firm_id"] == args[0] and (m.get("matter_number") or "").startswith(prefix)]

        if q.startswith("SELECT initials FROM users WHERE firm_id=$1 AND initials IS NOT NULL"):
            return [{"initials": u["initials"]} for u in self.users if u.get("initials")]

        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")

    async def execute(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("INSERT INTO numbering_counters"):
            firm_id, prefix, seed = args
            if not any(c["firm_id"] == firm_id and c["prefix"] == prefix for c in self.numbering_counters):
                self.numbering_counters.append({"firm_id": firm_id, "prefix": prefix, "next_seq": seed})
            return "INSERT 0 1"

        if q.startswith("INSERT INTO clients"):
            # Column list read straight out of the query, same trick
            # test_clients_api.py's FakeConnection uses -- note $9 is
            # reused for both created_at/updated_at in the real INSERT, so
            # this zip() naturally drops the last column; harmless since
            # nothing here asserts on updated_at.
            cols = [c.strip() for c in q.split("(", 1)[1].split(")", 1)[0].split(",")]
            row = dict(zip(cols, args))
            self.clients.append(row)
            return "INSERT 0 1"

        if q.startswith("INSERT INTO matters"):
            cols = [c.strip() for c in q.split("(", 1)[1].split(")", 1)[0].split(",")]
            row = dict(zip(cols, args))
            self.matters.append(row)
            return "INSERT 0 1"

        if q.startswith("INSERT INTO documents"):
            # Not the generic column/arg zip trick -- like audit_logs above,
            # this real query embeds 'auto_provisioned'/'complete'/'Draft'/1
            # as SQL literals interleaved with placeholders, so column count
            # (13) != arg count (9) and a naive zip() silently misaligns
            # every column from "source" onward.
            doc_id, matter_id, firm_id, filename, word_count, r2_key, uploaded_at, uploaded_by, provenance_document_type = args
            row = {
                "id": doc_id, "matter_id": matter_id, "firm_id": firm_id, "filename": filename,
                "source": "auto_provisioned", "status": "complete", "word_count": word_count,
                "page_count": 1, "r2_key": r2_key, "uploaded_at": uploaded_at, "uploaded_by": uploaded_by,
                "document_status": "Draft", "provenance_document_type": provenance_document_type,
            }
            self.documents.append(row)
            return "INSERT 0 1"

        if q.startswith("INSERT INTO audit_logs"):
            # Not the generic column/arg zip trick here -- the real query
            # embeds 'CLIENT_INTAKE'/'MATTER' as SQL literals rather than
            # placeholders (matching this codebase's one existing
            # audit_logs call site), so column count != arg count.
            firm_id, user_id, actor_name, actor_role, target_id, details = args
            self.audit_logs.append({
                "firm_id": firm_id, "user_id": user_id, "actor_name": actor_name,
                "actor_role": actor_role, "action": "CLIENT_INTAKE", "target_type": "MATTER",
                "target_id": target_id, "details": details,
            })
            return "INSERT 0 1"

        if q.startswith("INSERT INTO intake_idempotency_keys"):
            firm_id, key, response_body = args
            if not any(r["firm_id"] == firm_id and r["key"] == key for r in self.idempotency_keys):
                self.idempotency_keys.append({"firm_id": firm_id, "key": key, "response_body": response_body})
            return "INSERT 0 1"

        return "OK"


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, clients=None, matters=None, users=None, numbering_counters=None, idempotency_keys=None):
        self.conn = FakeConnection(clients, matters, users, numbering_counters, idempotency_keys)

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _lawyer(display_name="Tanaka Chademana", initials="TC"):
    return {"id": uuid.uuid4(), "firm_id": FIRM_ID, "display_name": display_name, "initials": initials}


def _client_row(full_name, client_number, id_=None):
    return {
        "id": id_ or uuid.uuid4(), "firm_id": FIRM_ID, "full_name": full_name,
        "client_number": client_number,
    }


def _fake_request():
    return None


# ── Happy path: new client + new matter ──────────────────────────────────

def test_new_client_new_matter_commit_creates_everything(monkeypatch):
    import backend.main as m
    lawyer = _lawyer()
    pool = FakePool(users=[lawyer])
    monkeypatch.setattr(m, "_db_pool", pool)

    req = ClientIntakeRequest(
        client_name="Kudakwashe Marufu", phone="+263772100003",
        matter_type="conveyancing", matter_description="Stand 88 Hillside — Sale agreement and transfer",
        assigned_lawyer_id=str(lawyer["id"]), commit=True,
    )
    result = asyncio.run(client_intake(req, _fake_request()))

    assert result["committed"] is True
    assert result["client"]["action"] == "created"
    assert result["client"]["client_number"] == "TC-001"
    assert result["matter"]["action"] == "created"
    assert result["matter"]["matter_number"] == "TC-001-01"
    assert result["matter"]["matter_type"] == "conveyancing"
    assert len(result["case_binder"]) == 3  # conveyancing's 3 seeded starter docs
    assert all(d["action"] == "created" and d["document_id"] for d in result["case_binder"])

    # Actually persisted, not just reflected in the response.
    assert len(pool.conn.clients) == 1
    assert len(pool.conn.matters) == 1
    assert len(pool.conn.documents) == 3
    assert len(pool.conn.audit_logs) == 1
    assert pool.conn.audit_logs[0]["action"] == "CLIENT_INTAKE"
    assert pool.conn.audit_logs[0]["target_type"] == "MATTER"

    # Every auto-provisioned case-binder document defaults to Draft --
    # they're untouched shells until a lawyer edits them.
    assert all(d["document_status"] == "Draft" for d in pool.conn.documents)
    assert {d["provenance_document_type"] for d in pool.conn.documents} == {
        "Correspondence", "Contract", "General",
    }  # conveyancing's 3 seeded docs, per config/case_binder_templates.yml


# ── Existing client via existing_client_id ───────────────────────────────

def test_existing_client_id_skips_client_creation(monkeypatch):
    import backend.main as m
    lawyer = _lawyer()
    existing = _client_row("Chiedza Bvumbe", "TC-002")
    pool = FakePool(users=[lawyer], clients=[existing])
    monkeypatch.setattr(m, "_db_pool", pool)

    req = ClientIntakeRequest(
        client_name="Chiedza Bvumbe (ignored — existing_client_id wins)", phone="+263772100002",
        matter_type="litigation_general", matter_description="Second matter for an existing client",
        assigned_lawyer_id=str(lawyer["id"]), existing_client_id=str(existing["id"]), commit=True,
    )
    result = asyncio.run(client_intake(req, _fake_request()))

    assert result["client"]["action"] == "matched_explicit"
    assert result["client"]["client_id"] == str(existing["id"])
    assert result["client"]["client_number"] == "TC-002"
    assert result["matter"]["matter_number"] == "TC-002-01"
    # Only the matter was created — the client row itself was never inserted.
    assert len(pool.conn.clients) == 1  # unchanged from the fixture
    assert len(pool.conn.matters) == 1


# ── Ambiguous match: review flag, never a guess ──────────────────────────

def test_ambiguous_name_match_returns_review_flag_and_writes_nothing(monkeypatch):
    import backend.main as m
    lawyer = _lawyer()
    candidates = [_client_row("Kudakwashe Marufu", "TC-001"), _client_row("Kudakwashe Muranda", "TC-002")]
    pool = FakePool(users=[lawyer], clients=candidates)
    monkeypatch.setattr(m, "_db_pool", pool)

    req = ClientIntakeRequest(
        client_name="Kudakwashe M.", phone="+263772100099",
        matter_type="conveyancing", matter_description="New matter for an ambiguous client",
        assigned_lawyer_id=str(lawyer["id"]), commit=True,  # even with commit=True, must not guess
    )
    result = asyncio.run(client_intake(req, _fake_request()))

    assert result["committed"] is False
    assert result["client"]["action"] == "review_required"
    assert len(result["client"]["candidates"]) == 2
    assert result["matter"] is None
    assert result["case_binder"] == []
    assert result["errors"]

    # Nothing written at all — not the client, not a matter.
    assert len(pool.conn.clients) == 2  # unchanged from the fixture
    assert len(pool.conn.matters) == 0
    assert len(pool.conn.documents) == 0
    assert len(pool.conn.audit_logs) == 0


# ── Preview mode: zero writes ─────────────────────────────────────────────

def test_preview_mode_makes_zero_db_writes(monkeypatch):
    import backend.main as m
    lawyer = _lawyer()
    pool = FakePool(users=[lawyer])
    monkeypatch.setattr(m, "_db_pool", pool)

    req = ClientIntakeRequest(
        client_name="Believe Mangoma", phone="+263772100008",
        matter_type="debt_collection", matter_description="Recovery of loan advance",
        assigned_lawyer_id=str(lawyer["id"]), commit=False,
    )
    result = asyncio.run(client_intake(req, _fake_request()))

    assert result["committed"] is False
    assert result["client"]["action"] == "would_create"
    assert result["client"]["client_id"] is None
    assert result["client"]["client_number"] == "TC-001"  # the would-be number
    assert result["matter"]["action"] == "would_create"
    assert result["matter"]["matter_id"] is None
    assert result["matter"]["matter_number"] == "TC-001-01"
    assert len(result["case_binder"]) == 2  # debt_collection's 2 seeded starter docs
    assert all(d["action"] == "would_create" and d["document_id"] is None for d in result["case_binder"])

    # Nothing persisted anywhere, and the numbering counter was never
    # touched either — a real commit later must still get TC-001, not
    # TC-002, proving the preview didn't burn a sequence number.
    assert pool.conn.clients == []
    assert pool.conn.matters == []
    assert pool.conn.documents == []
    assert pool.conn.audit_logs == []
    assert pool.conn.numbering_counters == []


def test_preview_then_commit_both_land_on_the_same_number(monkeypatch):
    """Proves preview truly never allocates: two previews in a row still
    both compute TC-001, and the real commit that follows also gets
    TC-001 -- if preview had called the atomic allocator, the counter
    would already be past 1 by the time commit runs."""
    import backend.main as m
    lawyer = _lawyer()
    pool = FakePool(users=[lawyer])
    monkeypatch.setattr(m, "_db_pool", pool)

    def make_req(commit):
        return ClientIntakeRequest(
            client_name="Tapiwa Nyoni", phone="+263772100006",
            matter_type="debt_collection", matter_description="Recovery of loan advance",
            assigned_lawyer_id=str(lawyer["id"]), commit=commit,
        )

    preview1 = asyncio.run(client_intake(make_req(False), _fake_request()))
    preview2 = asyncio.run(client_intake(make_req(False), _fake_request()))
    committed = asyncio.run(client_intake(make_req(True), _fake_request()))

    assert preview1["client"]["client_number"] == "TC-001"
    assert preview2["client"]["client_number"] == "TC-001"
    assert committed["client"]["client_number"] == "TC-001"


# ── Zero-document matter_type (valid enum value, no yml entry) ──────────

def test_matter_type_with_no_case_binder_entry_still_creates_matter(monkeypatch):
    import backend.main as m
    lawyer = _lawyer()
    pool = FakePool(users=[lawyer])
    monkeypatch.setattr(m, "_db_pool", pool)

    req = ClientIntakeRequest(
        client_name="Tinashe Museba", phone="+263772100010",
        matter_type="mining",  # a real INTAKE_MATTER_TYPES value with no case_binder_templates.yml entry
        matter_description="Mining claim dispute", assigned_lawyer_id=str(lawyer["id"]), commit=True,
    )
    result = asyncio.run(client_intake(req, _fake_request()))

    assert result["committed"] is True
    assert result["matter"]["action"] == "created"
    assert result["matter"]["matter_type"] == "mining"
    assert result["case_binder"] == []
    assert len(pool.conn.matters) == 1
    assert len(pool.conn.documents) == 0
    # Audit log still records the intake even with zero starter documents.
    assert len(pool.conn.audit_logs) == 1
    logged_details = json.loads(pool.conn.audit_logs[0]["details"])
    assert logged_details["case_binder_documents_created"] == 0
    assert logged_details["matter_type"] == "mining"


# ── Validation ─────────────────────────────────────────────────────────

def test_unknown_matter_type_is_rejected_with_422(monkeypatch):
    import backend.main as m
    lawyer = _lawyer()
    pool = FakePool(users=[lawyer])
    monkeypatch.setattr(m, "_db_pool", pool)

    req = ClientIntakeRequest(
        client_name="Someone", phone="+263770000000",
        matter_type="not_a_real_matter_type", matter_description="x",
        assigned_lawyer_id=str(lawyer["id"]), commit=False,
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(client_intake(req, _fake_request()))
    assert exc_info.value.status_code == 422


def test_assigned_lawyer_not_found_returns_404(monkeypatch):
    import backend.main as m
    pool = FakePool(users=[])
    monkeypatch.setattr(m, "_db_pool", pool)

    req = ClientIntakeRequest(
        client_name="Someone", phone="+263770000000",
        matter_type="conveyancing", matter_description="x",
        assigned_lawyer_id=str(uuid.uuid4()), commit=False,
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(client_intake(req, _fake_request()))
    assert exc_info.value.status_code == 404


def test_existing_client_id_not_found_returns_404(monkeypatch):
    import backend.main as m
    lawyer = _lawyer()
    pool = FakePool(users=[lawyer])
    monkeypatch.setattr(m, "_db_pool", pool)

    req = ClientIntakeRequest(
        client_name="Someone", phone="+263770000000",
        matter_type="conveyancing", matter_description="x",
        assigned_lawyer_id=str(lawyer["id"]), existing_client_id=str(uuid.uuid4()), commit=False,
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(client_intake(req, _fake_request()))
    assert exc_info.value.status_code == 404


# ── Idempotency guard ──────────────────────────────────────────────────

def test_same_idempotency_key_twice_returns_original_response_zero_new_writes(monkeypatch):
    import backend.main as m
    lawyer = _lawyer()
    pool = FakePool(users=[lawyer])
    monkeypatch.setattr(m, "_db_pool", pool)

    def make_req():
        return ClientIntakeRequest(
            client_name="Kudakwashe Marufu", phone="+263772100003",
            matter_type="conveyancing", matter_description="Stand 88 Hillside — Sale agreement and transfer",
            assigned_lawyer_id=str(lawyer["id"]), commit=True, idempotency_key="fixed-retry-key-001",
        )

    first = asyncio.run(client_intake(make_req(), _fake_request()))
    assert first["client"]["action"] == "created"
    assert len(pool.conn.clients) == 1
    assert len(pool.conn.matters) == 1
    assert len(pool.conn.documents) == 3
    assert len(pool.conn.audit_logs) == 1
    assert len(pool.conn.idempotency_keys) == 1

    second = asyncio.run(client_intake(make_req(), _fake_request()))

    # Byte-for-byte the same response as the first call, not a fresh
    # recomputation (a fresh one would report client action="matched"
    # rather than "created", and a different/incremented matter_number).
    assert second == first

    # Zero new writes anywhere -- still exactly what the first call produced.
    assert len(pool.conn.clients) == 1
    assert len(pool.conn.matters) == 1
    assert len(pool.conn.documents) == 3
    assert len(pool.conn.audit_logs) == 1
    assert len(pool.conn.idempotency_keys) == 1


def test_two_different_idempotency_keys_both_process_as_separate_intakes(monkeypatch):
    import backend.main as m
    lawyer = _lawyer()
    pool = FakePool(users=[lawyer])
    monkeypatch.setattr(m, "_db_pool", pool)

    def make_req(key):
        return ClientIntakeRequest(
            client_name="Kudakwashe Marufu", phone="+263772100003",
            matter_type="conveyancing", matter_description="Stand 88 Hillside — Sale agreement and transfer",
            assigned_lawyer_id=str(lawyer["id"]), commit=True, idempotency_key=key,
        )

    first = asyncio.run(client_intake(make_req("key-a"), _fake_request()))
    second = asyncio.run(client_intake(make_req("key-b"), _fake_request()))

    # Same client reused the second time (real match_client_name behaviour,
    # not idempotency short-circuiting) -- but a genuinely NEW matter each
    # time, since these are two distinct keys, i.e. two distinct legitimate
    # requests as far as the guard is concerned.
    assert first["client"]["action"] == "created"
    assert second["client"]["action"] == "matched"
    assert first["matter"]["matter_number"] != second["matter"]["matter_number"]
    assert len(pool.conn.clients) == 1  # one client, reused correctly
    assert len(pool.conn.matters) == 2  # two separate matters
    assert len(pool.conn.documents) == 6  # 3 case-binder docs each
    assert len(pool.conn.audit_logs) == 2
    assert len(pool.conn.idempotency_keys) == 2


def test_no_idempotency_key_behaves_exactly_as_before(monkeypatch):
    """Omitting idempotency_key entirely -- the existing, pre-guard
    behaviour -- must be completely unaffected: no idempotency_keys row
    ever gets written, and nothing about client/matter/document creation
    changes. (The other 9 tests in this file already exercise this
    behaviour without ever setting idempotency_key; this test makes the
    "no key stored" property explicit rather than just implicit.)"""
    import backend.main as m
    lawyer = _lawyer()
    pool = FakePool(users=[lawyer])
    monkeypatch.setattr(m, "_db_pool", pool)

    req = ClientIntakeRequest(
        client_name="Kudakwashe Marufu", phone="+263772100003",
        matter_type="conveyancing", matter_description="Stand 88 Hillside — Sale agreement and transfer",
        assigned_lawyer_id=str(lawyer["id"]), commit=True,  # idempotency_key omitted
    )
    result = asyncio.run(client_intake(req, _fake_request()))

    assert result["client"]["action"] == "created"
    assert len(pool.conn.clients) == 1
    assert len(pool.conn.matters) == 1
    assert len(pool.conn.documents) == 3
    assert len(pool.conn.audit_logs) == 1
    assert pool.conn.idempotency_keys == []  # no guard applied, nothing stored


def test_same_key_for_a_different_firm_is_not_treated_as_a_duplicate(monkeypatch):
    import backend.main as m
    lawyer = _lawyer()
    other_firm_id = uuid.uuid4()
    # A row already exists for this exact key string, but scoped to a
    # different firm -- must not short-circuit a request for FIRM_ID.
    pool = FakePool(
        users=[lawyer],
        idempotency_keys=[{"firm_id": other_firm_id, "key": "shared-key-string", "response_body": '{"fake": "other firm response"}'}],
    )
    monkeypatch.setattr(m, "_db_pool", pool)

    req = ClientIntakeRequest(
        client_name="Kudakwashe Marufu", phone="+263772100003",
        matter_type="conveyancing", matter_description="Stand 88 Hillside — Sale agreement and transfer",
        assigned_lawyer_id=str(lawyer["id"]), commit=True, idempotency_key="shared-key-string",
    )
    result = asyncio.run(client_intake(req, _fake_request()))

    # Processed for real -- not the other firm's stored fake response.
    assert result["client"]["action"] == "created"
    assert len(pool.conn.clients) == 1
    assert len(pool.conn.matters) == 1
    assert len(pool.conn.documents) == 3
    # A new row for THIS firm was stored, alongside the pre-existing
    # other-firm row -- both present, correctly scoped.
    assert len(pool.conn.idempotency_keys) == 2
    this_firm_rows = [r for r in pool.conn.idempotency_keys if r["firm_id"] == FIRM_ID]
    assert len(this_firm_rows) == 1
