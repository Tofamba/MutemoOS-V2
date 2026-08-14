"""
Unit tests for scripts/split_ngm_merged_clients.py — fixing 5 clients that
import_ngm_case_list.py's apply run either wrongly fuzzy-merged (4 cases)
or correctly left unlinked pending review (1 case).

Source data for these tests is copied directly from the real
ngm_case_list_import_review.json produced by that run (table_index 43, 62,
81, 82, 83) — not re-derived from conversation memory, matching the same
"reference the actual report" instruction the script itself follows.

Called directly as plain async functions against a FakeConnection, same
convention as this repo's other backend/script tests.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest

from scripts.split_ngm_merged_clients import (
    FIRM_ID,
    build_split_plan,
    _resolve_db_actions,
)

FIRM_UUID = uuid.UUID(FIRM_ID)

# Exact entries from ngm_case_list_import_review.json for the 5 records in
# question — copied verbatim, not retyped from memory.
REVIEW = {
    "clients": {
        "fuzzy_merged_within_run": [
            {"table_index": 43, "name": "Evelyn Kudzotsa", "merged_into_name": "Evelyn Gondo", "client_number": "NGM-025"},
            {"table_index": 62, "name": "Kudzai Ndanga", "merged_into_name": "Kudzai Madzingira", "client_number": "NGM-015"},
            {"table_index": 81, "name": "Mutungwe Trust", "merged_into_name": "Muza Trust", "client_number": "NGM-048"},
            {"table_index": 83, "name": "Vongai Maroyi", "merged_into_name": "Vongai Murigo", "client_number": "NGM-041"},
        ],
        "review": [
            {"table_index": 82, "name": "Pastor Linda", "candidates": [
                {"id": None, "full_name": "Pastor Charlotte", "client_number": "NGM-027"},
                {"id": None, "full_name": "Linda Mpofu", "client_number": "NGM-030"},
            ]},
        ],
    },
    "matters": {
        "created": [
            {"table_index": 43, "client_name": "Evelyn Kudzotsa", "matter_name": "Chamunorwa chivhunga — Eviction",
             "matter_number": "NGM-025-02", "client_id": None, "client_number": "NGM-025"},
            {"table_index": 62, "client_name": "Kudzai Ndanga", "matter_name": "Matrimonial / divorce",
             "matter_number": "NGM-015-02", "client_id": None, "client_number": "NGM-015"},
            {"table_index": 81, "client_name": "Mutungwe Trust", "matter_name": "Family Trust — Trust",
             "matter_number": "NGM-048-02", "client_id": None, "client_number": "NGM-048"},
            {"table_index": 83, "client_name": "Vongai Maroyi", "matter_name": "Arosume — Land /property",
             "matter_number": "NGM-041-03", "client_id": None, "client_number": "NGM-041"},
        ],
        "created_unlinked_pending_review": [
            {"table_index": 82, "client_name": "Pastor Linda", "matter_name": "Agreement of sale \nLand / property"},
        ],
    },
}


class FakeConnection:
    def __init__(self, clients, matters):
        self.clients = clients
        self.matters = matters

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT client_number FROM clients WHERE firm_id=$1 AND client_number LIKE 'NGM-%'"):
            firm_id, = args
            return [{"client_number": c["client_number"]} for c in self.clients
                    if c["firm_id"] == firm_id and (c["client_number"] or "").startswith("NGM-")]
        if q.startswith("SELECT id FROM matters WHERE firm_id=$1 AND client_id IS NULL AND client_name=$2 AND name=$3"):
            firm_id, client_name, name = args
            return [{"id": m["id"]} for m in self.matters
                    if m["firm_id"] == firm_id and m["client_id"] is None
                    and m["client_name"] == client_name and m["name"] == name]
        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT id, name, client_id FROM matters WHERE firm_id=$1 AND matter_number=$2"):
            firm_id, matter_number = args
            for m in self.matters:
                if m["firm_id"] == firm_id and m.get("matter_number") == matter_number:
                    return dict(m)
            return None
        if q.startswith("SELECT client_number, full_name FROM clients WHERE id=$1"):
            client_id, = args
            for c in self.clients:
                if c["id"] == client_id:
                    return dict(c)
            return None
        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO clients"):
            cols = [c.strip() for c in q.split("(", 1)[1].split(")", 1)[0].split(",")]
            self.clients.append(dict(zip(cols, args)))
        elif q.startswith("UPDATE matters SET client_id=$1, client_name=$2, matter_number=$3, last_activity=$4"):
            client_id, client_name, matter_number, last_activity, matter_id = args
            for m in self.matters:
                if m["id"] == matter_id:
                    m["client_id"] = client_id
                    m["client_name"] = client_name
                    m["matter_number"] = matter_number
                    m["last_activity"] = last_activity
        else:
            raise NotImplementedError(f"FakeConnection.execute: unhandled query: {q}")
        return "OK"

    def transaction(self):
        return _FakeTransaction()


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _client(client_id, name, client_number):
    return {"id": client_id, "firm_id": FIRM_UUID, "full_name": name, "client_number": client_number}


def _matter(matter_id, name, client_id, client_name, matter_number=None):
    return {
        "id": matter_id, "firm_id": FIRM_UUID, "name": name, "client_id": client_id,
        "client_name": client_name, "matter_number": matter_number,
    }


def _build_fixture():
    """One wrongly-merged-into client per case, each with the misfiled
    matter PLUS one other matter that should stay put — so "the original
    client no longer has it" is a meaningful assertion, not just "has
    nothing." Client_numbers run up to NGM-050 so next_sequence continues
    realistically from there."""
    evelyn_gondo = _client(uuid.uuid4(), "Evelyn Gondo", "NGM-025")
    kudzai_madzingira = _client(uuid.uuid4(), "Kudzai Madzingira", "NGM-015")
    muza_trust = _client(uuid.uuid4(), "Muza Trust", "NGM-048")
    vongai_murigo = _client(uuid.uuid4(), "Vongai Murigo", "NGM-041")
    other_high_numbered = _client(uuid.uuid4(), "Some Other Client", "NGM-050")

    clients = [evelyn_gondo, kudzai_madzingira, muza_trust, vongai_murigo, other_high_numbered]

    misfiled_evelyn = _matter(uuid.uuid4(), "Chamunorwa chivhunga — Eviction",
                               evelyn_gondo["id"], "Evelyn Kudzotsa", "NGM-025-02")
    evelyn_gondo_own = _matter(uuid.uuid4(), "Something else", evelyn_gondo["id"], "Evelyn Gondo", "NGM-025-01")

    misfiled_kudzai = _matter(uuid.uuid4(), "Matrimonial / divorce",
                               kudzai_madzingira["id"], "Kudzai Ndanga", "NGM-015-02")
    kudzai_own = _matter(uuid.uuid4(), "Alexio Mungate and Another",
                          kudzai_madzingira["id"], "Kudzai Madzingira", "NGM-015-01")

    misfiled_mutungwe = _matter(uuid.uuid4(), "Family Trust — Trust",
                                 muza_trust["id"], "Mutungwe Trust", "NGM-048-02")
    muza_own = _matter(uuid.uuid4(), "Rainbow Trust", muza_trust["id"], "Muza Trust", "NGM-048-01")

    misfiled_vongai = _matter(uuid.uuid4(), "Arosume — Land /property",
                               vongai_murigo["id"], "Vongai Maroyi", "NGM-041-03")
    vongai_own_1 = _matter(uuid.uuid4(), "Vongai and Children Trust",
                            vongai_murigo["id"], "Vongai Murigo", "NGM-041-01")
    vongai_own_2 = _matter(uuid.uuid4(), "Charewa", vongai_murigo["id"], "Vongai Murigo", "NGM-041-02")

    pastor_linda_matter = _matter(uuid.uuid4(), "Agreement of sale \nLand / property",
                                   None, "Pastor Linda", None)

    matters = [
        misfiled_evelyn, evelyn_gondo_own, misfiled_kudzai, kudzai_own,
        misfiled_mutungwe, muza_own, misfiled_vongai, vongai_own_1, vongai_own_2,
        pastor_linda_matter,
    ]
    return clients, matters, {
        "evelyn_gondo": evelyn_gondo, "misfiled_evelyn": misfiled_evelyn,
        "kudzai_madzingira": kudzai_madzingira, "misfiled_kudzai": misfiled_kudzai,
        "muza_trust": muza_trust, "misfiled_mutungwe": misfiled_mutungwe,
        "vongai_murigo": vongai_murigo, "misfiled_vongai": misfiled_vongai,
        "pastor_linda_matter": pastor_linda_matter,
    }


def _resolve_and_apply(conn, items):
    resolved = asyncio.run(_resolve_db_actions(conn, items))
    now = datetime.now(timezone.utc)
    for item in resolved:
        new_id = uuid.uuid4()
        asyncio.run(conn.execute(
            "INSERT INTO clients (id, firm_id, full_name, client_number, created_at, updated_at) "
            "VALUES ($1,$2,$3,$4,$5,$6)",
            new_id, FIRM_UUID, item["new_client_name"], item["new_client_number"], now, now,
        ))
        asyncio.run(conn.execute(
            "UPDATE matters SET client_id=$1, client_name=$2, matter_number=$3, last_activity=$4 WHERE id=$5",
            new_id, item["new_client_name"], item["new_matter_number"], now, uuid.UUID(item["matter_id"]),
        ))
        item["_new_client_id"] = new_id
    return resolved


def test_build_split_plan_matches_real_review_json_data():
    items = build_split_plan(REVIEW)
    assert len(items) == 5
    by_ti = {i["table_index"]: i for i in items}
    assert by_ti[43]["new_client_name"] == "Evelyn Kudzotsa"
    assert by_ti[43]["current_matter_number"] == "NGM-025-02"
    assert by_ti[82]["kind"] == "unlinked"
    assert by_ti[82]["new_client_name"] == "Pastor Linda"


# ── one test per case ─────────────────────────────────────────────────────

def test_case_1_evelyn_kudzotsa_split_from_evelyn_gondo():
    clients, matters, f = _build_fixture()
    conn = FakeConnection(clients, matters)
    items = [i for i in build_split_plan(REVIEW) if i["table_index"] == 43]

    resolved = _resolve_and_apply(conn, items)

    moved = next(m for m in conn.matters if m["id"] == f["misfiled_evelyn"]["id"])
    assert moved["client_id"] == resolved[0]["_new_client_id"]
    assert moved["client_name"] == "Evelyn Kudzotsa"
    assert moved["matter_number"] == f"{resolved[0]['new_client_number']}-01"

    new_client = next(c for c in conn.clients if c["id"] == resolved[0]["_new_client_id"])
    assert new_client["full_name"] == "Evelyn Kudzotsa"

    # The original client keeps its own matter, no longer has the misfiled one.
    gondo_matters = [m for m in conn.matters if m["client_id"] == f["evelyn_gondo"]["id"]]
    assert len(gondo_matters) == 1
    assert all(m["id"] != f["misfiled_evelyn"]["id"] for m in gondo_matters)


def test_case_2_kudzai_ndanga_split_from_kudzai_madzingira():
    clients, matters, f = _build_fixture()
    conn = FakeConnection(clients, matters)
    items = [i for i in build_split_plan(REVIEW) if i["table_index"] == 62]

    resolved = _resolve_and_apply(conn, items)

    moved = next(m for m in conn.matters if m["id"] == f["misfiled_kudzai"]["id"])
    assert moved["client_id"] == resolved[0]["_new_client_id"]
    assert moved["client_name"] == "Kudzai Ndanga"

    madzingira_matters = [m for m in conn.matters if m["client_id"] == f["kudzai_madzingira"]["id"]]
    assert all(m["id"] != f["misfiled_kudzai"]["id"] for m in madzingira_matters)
    assert len(madzingira_matters) == 1


def test_case_3_mutungwe_trust_split_from_muza_trust():
    clients, matters, f = _build_fixture()
    conn = FakeConnection(clients, matters)
    items = [i for i in build_split_plan(REVIEW) if i["table_index"] == 81]

    resolved = _resolve_and_apply(conn, items)

    moved = next(m for m in conn.matters if m["id"] == f["misfiled_mutungwe"]["id"])
    assert moved["client_id"] == resolved[0]["_new_client_id"]
    assert moved["client_name"] == "Mutungwe Trust"

    muza_matters = [m for m in conn.matters if m["client_id"] == f["muza_trust"]["id"]]
    assert all(m["id"] != f["misfiled_mutungwe"]["id"] for m in muza_matters)
    assert len(muza_matters) == 1


def test_case_4_vongai_maroyi_split_from_vongai_murigo():
    clients, matters, f = _build_fixture()
    conn = FakeConnection(clients, matters)
    items = [i for i in build_split_plan(REVIEW) if i["table_index"] == 83]

    resolved = _resolve_and_apply(conn, items)

    moved = next(m for m in conn.matters if m["id"] == f["misfiled_vongai"]["id"])
    assert moved["client_id"] == resolved[0]["_new_client_id"]
    assert moved["client_name"] == "Vongai Maroyi"

    # Vongai Murigo keeps BOTH her own matters, loses only the misfiled one.
    murigo_matters = [m for m in conn.matters if m["client_id"] == f["vongai_murigo"]["id"]]
    assert len(murigo_matters) == 2
    assert all(m["id"] != f["misfiled_vongai"]["id"] for m in murigo_matters)


def test_case_5_pastor_linda_created_and_linked():
    clients, matters, f = _build_fixture()
    conn = FakeConnection(clients, matters)
    items = [i for i in build_split_plan(REVIEW) if i["table_index"] == 82]

    resolved = _resolve_and_apply(conn, items)

    moved = next(m for m in conn.matters if m["id"] == f["pastor_linda_matter"]["id"])
    assert moved["client_id"] == resolved[0]["_new_client_id"]  # was None, now linked
    assert moved["client_name"] == "Pastor Linda"
    assert moved["matter_number"] == f"{resolved[0]['new_client_number']}-01"

    new_client = next(c for c in conn.clients if c["id"] == resolved[0]["_new_client_id"])
    assert new_client["full_name"] == "Pastor Linda"


# ── all 5 together: sequential numbering, nothing collides ──────────────

def test_all_five_together_get_sequential_new_client_numbers():
    clients, matters, f = _build_fixture()
    conn = FakeConnection(clients, matters)
    items = build_split_plan(REVIEW)

    resolved = _resolve_and_apply(conn, items)

    numbers = [item["new_client_number"] for item in resolved]
    assert len(numbers) == len(set(numbers))  # all distinct
    assert numbers == sorted(numbers)  # strictly increasing
    assert numbers[0] == "NGM-051"  # continues from the fixture's highest, NGM-050


# ── safety: verification catches drift rather than guessing ─────────────

def test_resolve_raises_if_matter_number_not_found():
    clients, matters, f = _build_fixture()
    # Simulate the matter having already been fixed/renumbered since the report.
    for m in matters:
        if m["id"] == f["misfiled_evelyn"]["id"]:
            m["matter_number"] = "NGM-025-99"
    conn = FakeConnection(clients, matters)
    items = [i for i in build_split_plan(REVIEW) if i["table_index"] == 43]

    with pytest.raises(ValueError, match="not found"):
        asyncio.run(_resolve_db_actions(conn, items))


def test_resolve_raises_if_matter_name_changed_since_report():
    clients, matters, f = _build_fixture()
    for m in matters:
        if m["id"] == f["misfiled_evelyn"]["id"]:
            m["name"] = "A different matter name entirely"
    conn = FakeConnection(clients, matters)
    items = [i for i in build_split_plan(REVIEW) if i["table_index"] == 43]

    with pytest.raises(ValueError, match="name mismatch"):
        asyncio.run(_resolve_db_actions(conn, items))


def test_resolve_raises_if_unlinked_matter_not_found():
    clients, matters, f = _build_fixture()
    matters = [m for m in matters if m["id"] != f["pastor_linda_matter"]["id"]]  # already fixed/removed
    conn = FakeConnection(clients, matters)
    items = [i for i in build_split_plan(REVIEW) if i["table_index"] == 82]

    with pytest.raises(ValueError, match="expected exactly 1"):
        asyncio.run(_resolve_db_actions(conn, items))
