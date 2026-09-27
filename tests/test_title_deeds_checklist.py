"""
Unit tests for the Title/Deeds Digitization Validation checklist
(2026-09-27): backend/title_deeds_checklist.py (the pure config/
completion module) and its wiring in backend/main.py --
matter_checklist_items CRUD (GET/seed/PATCH) and create_matter()'s own
auto-seed-on-creation call.

Confirmed with the user before building: this reuses beneficial_owners/
authorized_representatives' shape (status enum, responsible person,
document evidence FK) for persistence, and case_binder.py's YAML-config
convention for the item catalog -- see both modules' own docstrings.
Not a new tracking mechanism.

Called directly as plain async functions with fake conns/requests, same
convention as tests/test_matter_client_linking.py (whose FakeConnection
this file's create_matter fixtures are modeled on) and
tests/test_client_ownership.py (beneficial_owners/authorized_representatives
CRUD test shape).
"""

import asyncio
import re
import uuid
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException

from backend.title_deeds_checklist import (
    CLIENT_TYPE_TO_CHECKLIST_CATEGORY,
    SPA_CATEGORIES,
    category_display_label,
    checklist_items_for_category,
    compute_checklist_completion,
    known_categories,
)
from backend.main import (
    FIRM_ID,
    ChecklistItemUpdate,
    ChecklistSeedBody,
    MatterCreate,
    create_matter,
    get_matter_checklist,
    list_matters,
    seed_matter_checklist,
    update_checklist_item,
)


# ── backend/title_deeds_checklist.py: the pure module ───────────────────────

def test_known_categories_includes_all_five():
    assert known_categories() == sorted(
        ["individual", "company", "trust", "spa_principal", "spa_representative"]
    )


def test_individual_checklist_matches_the_real_document_list():
    keys = {i["item_key"] for i in checklist_items_for_category("individual")}
    assert keys == {
        "original_title_deed", "previous_deed_of_transfer", "passport_photo",
        "contact_details", "email", "physical_address",
        "notarized_affidavit_thumbprint", "biometrics", "fees",
    }


def test_company_checklist_includes_cr5_cr6_and_resolution():
    items = checklist_items_for_category("company")
    keys = {i["item_key"] for i in items}
    assert {"certificate_of_incorporation", "registration_number", "cr5", "cr6",
            "company_resolution", "representative_passport_photo"} <= keys
    assert len(items) == 12


def test_trust_checklist_includes_deed_of_trust_and_resolution():
    items = checklist_items_for_category("trust")
    keys = {i["item_key"] for i in items}
    assert {"deed_of_trust", "trust_resolution", "registration_number"} <= keys
    assert len(items) == 12


def test_spa_principal_checklist_is_the_notarized_spa_path():
    keys = {i["item_key"] for i in checklist_items_for_category("spa_principal")}
    assert keys == {
        "notarized_spa", "principal_fingerprints", "principal_passport_photos",
        "certified_passport_biodata", "title_deed",
    }


def test_spa_representative_checklist_is_the_affidavit_path():
    keys = {i["item_key"] for i in checklist_items_for_category("spa_representative")}
    assert keys == {"sworn_affidavit_fingerprints", "biometric_capture", "certified_national_id"}


def test_unknown_category_returns_empty_list_not_an_error():
    assert checklist_items_for_category("nonexistent") == []


def test_client_type_map_only_covers_individual_company_trust():
    """Deliberate: Partnership/Estate/NonProfit/Government/Other have no
    defined document list, so they must never silently map to one --
    see the module's own comment for why."""
    assert CLIENT_TYPE_TO_CHECKLIST_CATEGORY == {
        "Individual": "individual", "Company": "company", "Trust": "trust",
    }
    assert CLIENT_TYPE_TO_CHECKLIST_CATEGORY.get("Partnership") is None
    assert CLIENT_TYPE_TO_CHECKLIST_CATEGORY.get("Estate") is None


def test_spa_categories_are_the_two_sub_checklist_variants():
    assert set(SPA_CATEGORIES) == {"spa_principal", "spa_representative"}


def test_completion_not_started_when_nothing_seeded():
    assert compute_checklist_completion([]) == {"collected": 0, "total": 0, "status": "not_started"}


def test_completion_not_started_when_seeded_but_none_collected():
    items = [{"status": "Outstanding"}, {"status": "Outstanding"}]
    assert compute_checklist_completion(items) == {"collected": 0, "total": 2, "status": "not_started"}


def test_completion_in_progress_when_some_collected():
    items = [{"status": "Collected"}, {"status": "Outstanding"}, {"status": "Outstanding"}]
    assert compute_checklist_completion(items) == {"collected": 1, "total": 3, "status": "in_progress"}


def test_completion_complete_when_all_collected():
    items = [{"status": "Collected"}, {"status": "Collected"}]
    assert compute_checklist_completion(items) == {"collected": 2, "total": 2, "status": "complete"}


def test_category_display_label_known_and_unknown():
    assert category_display_label("individual") == "Individual"
    assert "SPA" in category_display_label("spa_principal")
    assert category_display_label("made_up") == "made_up"
    assert category_display_label(None) == ""


# ── Endpoint fakes ───────────────────────────────────────────────────────────

class FakeConnection:
    def __init__(self, matters=None, clients=None, checklist_items=None):
        self.matters = matters if matters is not None else []
        self.clients = clients if clients is not None else []
        self.checklist_items = checklist_items if checklist_items is not None else []

    async def fetchval(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT COUNT(*) FROM matter_checklist_items WHERE matter_id=$1 AND category=$2"):
            matter_id, category = args
            return sum(1 for i in self.checklist_items if i["matter_id"] == matter_id and i["category"] == category)
        raise NotImplementedError(f"FakeConnection.fetchval: unhandled query: {q}")

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT id FROM matters WHERE id=$1 AND firm_id=$2"):
            matter_id, firm_id = args
            for m in self.matters:
                if m["id"] == matter_id and m["firm_id"] == firm_id:
                    return {"id": m["id"]}
            return None

        if q.startswith("SELECT full_name, client_number, client_type FROM clients WHERE id=$1 AND firm_id=$2"):
            client_id, firm_id = args
            for c in self.clients:
                if c["id"] == client_id and c["firm_id"] == firm_id:
                    return {"full_name": c["full_name"], "client_number": c.get("client_number"),
                            "client_type": c.get("client_type")}
            return None

        if q.startswith("SELECT role FROM organisation_roles"):
            return None

        if q.startswith("INSERT INTO matters"):
            cols_str = q.split("(", 1)[1].split(")", 1)[0]
            cols = [c.strip() for c in cols_str.split(",")]
            row = dict(zip(cols, args))
            row.setdefault("id", uuid.uuid4())
            self.matters.append(row)
            return dict(row)

        if q.startswith("UPDATE matter_checklist_items SET"):
            m = re.search(r"SET (.+) WHERE id=\$1 AND matter_id=\$2 AND firm_id=\$3", q)
            cols = re.findall(r"(\w+)=\$\d+", m.group(1))
            item_id, matter_id, firm_id = args[0], args[1], args[2]
            values = args[3:3 + len(cols)]
            for row in self.checklist_items:
                if row["id"] == item_id and row["matter_id"] == matter_id and row["firm_id"] == firm_id:
                    for col, val in zip(cols, values):
                        row[col] = val
                    return dict(row)
            return None

        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT * FROM matter_checklist_items WHERE matter_id=$1 AND category=$2"):
            matter_id, category = args
            return [dict(i) for i in self.checklist_items if i["matter_id"] == matter_id and i["category"] == category]

        if q.startswith("SELECT * FROM matter_checklist_items WHERE matter_id=$1 AND firm_id=$2"):
            matter_id, firm_id = args
            return [dict(i) for i in self.checklist_items if i["matter_id"] == matter_id and i["firm_id"] == firm_id]

        if q.startswith("SELECT matter_id, status FROM matter_checklist_items WHERE matter_id = ANY($1) AND firm_id=$2"):
            matter_ids, firm_id = args
            return [{"matter_id": i["matter_id"], "status": i["status"]} for i in self.checklist_items
                    if i["matter_id"] in matter_ids and i["firm_id"] == firm_id]

        if q.startswith("SELECT * FROM progress_notes"):
            return []
        if q.startswith("SELECT id, display_name FROM users"):
            return []
        if q.startswith("SELECT * FROM matters WHERE firm_id=$1 AND NOT is_sentinel"):
            firm_id, = args
            return [dict(m) for m in self.matters if m["firm_id"] == firm_id]

        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO matter_checklist_items"):
            matter_id, firm_id, category, item_key, item_label = args
            if any(i["matter_id"] == matter_id and i["category"] == category and i["item_key"] == item_key
                   for i in self.checklist_items):
                return "INSERT 0 0"  # ON CONFLICT DO NOTHING
            self.checklist_items.append({
                "id": uuid.uuid4(), "matter_id": matter_id, "firm_id": firm_id, "category": category,
                "item_key": item_key, "item_label": item_label, "status": "Outstanding",
                "responsible_user_id": None, "document_id": None, "collected_date": None,
                "created_at": datetime.utcnow(), "updated_at": datetime.utcnow(),
            })
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
    def __init__(self, **kwargs):
        self.conn = FakeConnection(**kwargs)

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


class FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.cookies = {}


def _matter(matter_type=None, firm_id=FIRM_ID):
    return {"id": uuid.uuid4(), "firm_id": firm_id, "matter_type": matter_type}


def _client(*, client_type, firm_id=FIRM_ID, client_number=None):
    # client_number left None by default -- numbering (_create_matter_row's
    # own concern, already covered by tests/test_backfill_client_matter_
    # numbers.py etc.) is out of scope here; a None client_number keeps
    # create_matter() on its no-numbering path so this file's fake doesn't
    # also need to fake numbering_counters.
    return {"id": uuid.uuid4(), "firm_id": firm_id, "full_name": "Test Client",
            "client_number": client_number, "client_type": client_type}


def _partner_user():
    return {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "Farai"}


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


# ── POST .../checklist/seed ──────────────────────────────────────────────────

def test_seed_creates_items_for_individual_category(monkeypatch):
    import backend.main as m
    matter = _matter()
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter]))
    _as_current_user(monkeypatch, m, _partner_user())

    result = asyncio.run(seed_matter_checklist(
        str(matter["id"]), ChecklistSeedBody(category="individual"), FakeRequest()
    ))

    assert result["seeded"] is True
    assert result["category"] == "individual"
    assert {i["item_key"] for i in result["items"]} == {i["item_key"] for i in checklist_items_for_category("individual")}
    assert all(i["status"] == "Outstanding" for i in result["items"])


def test_seed_creates_items_for_company_category(monkeypatch):
    import backend.main as m
    matter = _matter()
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter]))
    _as_current_user(monkeypatch, m, _partner_user())

    result = asyncio.run(seed_matter_checklist(
        str(matter["id"]), ChecklistSeedBody(category="company"), FakeRequest()
    ))
    assert len(result["items"]) == 12


def test_seed_creates_items_for_trust_category(monkeypatch):
    import backend.main as m
    matter = _matter()
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter]))
    _as_current_user(monkeypatch, m, _partner_user())

    result = asyncio.run(seed_matter_checklist(
        str(matter["id"]), ChecklistSeedBody(category="trust"), FakeRequest()
    ))
    assert len(result["items"]) == 12


def test_seed_spa_principal_is_additive_to_the_main_checklist(monkeypatch):
    import backend.main as m
    matter = _matter()
    pool = FakePool(matters=[matter])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())

    asyncio.run(seed_matter_checklist(str(matter["id"]), ChecklistSeedBody(category="individual"), FakeRequest()))
    result = asyncio.run(seed_matter_checklist(str(matter["id"]), ChecklistSeedBody(category="spa_principal"), FakeRequest()))

    assert result["seeded"] is True
    assert len(result["items"]) == 5
    # both categories now coexist on the same matter
    all_items = pool.conn.checklist_items
    assert {i["category"] for i in all_items} == {"individual", "spa_principal"}
    assert len(all_items) == 9 + 5


def test_seed_is_idempotent_second_call_is_a_safe_noop(monkeypatch):
    import backend.main as m
    matter = _matter()
    pool = FakePool(matters=[matter])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())

    first = asyncio.run(seed_matter_checklist(str(matter["id"]), ChecklistSeedBody(category="individual"), FakeRequest()))
    second = asyncio.run(seed_matter_checklist(str(matter["id"]), ChecklistSeedBody(category="individual"), FakeRequest()))

    assert first["seeded"] is True
    assert second["seeded"] is False
    assert len(pool.conn.checklist_items) == 9  # no duplicates


def test_seed_rejects_unknown_category(monkeypatch):
    import backend.main as m
    matter = _matter()
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter]))
    _as_current_user(monkeypatch, m, _partner_user())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(seed_matter_checklist(str(matter["id"]), ChecklistSeedBody(category="nonexistent"), FakeRequest()))
    assert exc.value.status_code == 422


def test_seed_404s_on_unknown_matter(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, _partner_user())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(seed_matter_checklist(str(uuid.uuid4()), ChecklistSeedBody(category="individual"), FakeRequest()))
    assert exc.value.status_code == 404


def test_seed_404s_on_matter_from_another_firm(monkeypatch):
    import backend.main as m
    other_firm_matter = _matter(firm_id=uuid.uuid4())
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[other_firm_matter]))
    _as_current_user(monkeypatch, m, _partner_user())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(seed_matter_checklist(str(other_firm_matter["id"]), ChecklistSeedBody(category="individual"), FakeRequest()))
    assert exc.value.status_code == 404


# ── GET .../checklist ────────────────────────────────────────────────────────

def test_get_checklist_groups_by_category_with_completion(monkeypatch):
    import backend.main as m
    matter = _matter()
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter]))
    _as_current_user(monkeypatch, m, _partner_user())
    asyncio.run(seed_matter_checklist(str(matter["id"]), ChecklistSeedBody(category="individual"), FakeRequest()))
    asyncio.run(seed_matter_checklist(str(matter["id"]), ChecklistSeedBody(category="spa_representative"), FakeRequest()))

    result = asyncio.run(get_matter_checklist(str(matter["id"]), FakeRequest()))

    cats = {c["category"] for c in result["categories"]}
    assert cats == {"individual", "spa_representative"}
    individual = next(c for c in result["categories"] if c["category"] == "individual")
    assert individual["completion"] == {"collected": 0, "total": 9, "status": "not_started"}
    assert result["overall_completion"] == {"collected": 0, "total": 9 + 3, "status": "not_started"}


def test_get_checklist_empty_when_nothing_seeded_yet(monkeypatch):
    import backend.main as m
    matter = _matter()
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter]))
    _as_current_user(monkeypatch, m, _partner_user())

    result = asyncio.run(get_matter_checklist(str(matter["id"]), FakeRequest()))

    assert result["categories"] == []
    assert result["overall_completion"] == {"collected": 0, "total": 0, "status": "not_started"}


# ── PATCH .../checklist/{item_id} ────────────────────────────────────────────

def test_patch_marks_item_collected_and_defaults_collected_date_to_today(monkeypatch):
    import backend.main as m
    matter = _matter()
    pool = FakePool(matters=[matter])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())
    asyncio.run(seed_matter_checklist(str(matter["id"]), ChecklistSeedBody(category="individual"), FakeRequest()))
    item = pool.conn.checklist_items[0]

    result = asyncio.run(update_checklist_item(
        str(matter["id"]), str(item["id"]), ChecklistItemUpdate(status="Collected"), FakeRequest()
    ))

    assert result["status"] == "Collected"
    assert result["collected_date"] == date.today().isoformat()


def test_patch_respects_an_explicit_collected_date(monkeypatch):
    import backend.main as m
    matter = _matter()
    pool = FakePool(matters=[matter])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())
    asyncio.run(seed_matter_checklist(str(matter["id"]), ChecklistSeedBody(category="individual"), FakeRequest()))
    item = pool.conn.checklist_items[0]
    backdated = (date.today() - timedelta(days=10)).isoformat()

    result = asyncio.run(update_checklist_item(
        str(matter["id"]), str(item["id"]),
        ChecklistItemUpdate(status="Collected", collected_date=backdated), FakeRequest()
    ))
    assert result["collected_date"] == backdated


def test_patch_sets_responsible_user_id(monkeypatch):
    import backend.main as m
    matter = _matter()
    pool = FakePool(matters=[matter])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())
    asyncio.run(seed_matter_checklist(str(matter["id"]), ChecklistSeedBody(category="individual"), FakeRequest()))
    item = pool.conn.checklist_items[0]
    responsible = uuid.uuid4()

    result = asyncio.run(update_checklist_item(
        str(matter["id"]), str(item["id"]), ChecklistItemUpdate(responsible_user_id=str(responsible)), FakeRequest()
    ))
    assert result["responsible_user_id"] == str(responsible)


def test_patch_sets_document_id_as_the_evidence_link(monkeypatch):
    import backend.main as m
    matter = _matter()
    pool = FakePool(matters=[matter])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())
    asyncio.run(seed_matter_checklist(str(matter["id"]), ChecklistSeedBody(category="individual"), FakeRequest()))
    item = pool.conn.checklist_items[0]
    doc_id = uuid.uuid4()

    result = asyncio.run(update_checklist_item(
        str(matter["id"]), str(item["id"]), ChecklistItemUpdate(document_id=str(doc_id)), FakeRequest()
    ))
    assert result["document_id"] == str(doc_id)


def test_patch_rejects_invalid_status(monkeypatch):
    import backend.main as m
    matter = _matter()
    pool = FakePool(matters=[matter])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())
    asyncio.run(seed_matter_checklist(str(matter["id"]), ChecklistSeedBody(category="individual"), FakeRequest()))
    item = pool.conn.checklist_items[0]

    with pytest.raises(HTTPException) as exc:
        asyncio.run(update_checklist_item(
            str(matter["id"]), str(item["id"]), ChecklistItemUpdate(status="Verified"), FakeRequest()
        ))
    assert exc.value.status_code == 422


def test_patch_rejects_malformed_responsible_user_id(monkeypatch):
    import backend.main as m
    matter = _matter()
    pool = FakePool(matters=[matter])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())
    asyncio.run(seed_matter_checklist(str(matter["id"]), ChecklistSeedBody(category="individual"), FakeRequest()))
    item = pool.conn.checklist_items[0]

    with pytest.raises(HTTPException) as exc:
        asyncio.run(update_checklist_item(
            str(matter["id"]), str(item["id"]), ChecklistItemUpdate(responsible_user_id="not-a-uuid"), FakeRequest()
        ))
    assert exc.value.status_code == 400


def test_patch_404s_when_item_belongs_to_a_different_matter(monkeypatch):
    import backend.main as m
    matter_a = _matter()
    matter_b = _matter()
    pool = FakePool(matters=[matter_a, matter_b])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())
    asyncio.run(seed_matter_checklist(str(matter_a["id"]), ChecklistSeedBody(category="individual"), FakeRequest()))
    item = pool.conn.checklist_items[0]

    with pytest.raises(HTTPException) as exc:
        asyncio.run(update_checklist_item(
            str(matter_b["id"]), str(item["id"]), ChecklistItemUpdate(status="Collected"), FakeRequest()
        ))
    assert exc.value.status_code == 404


def test_patch_with_no_fields_400s(monkeypatch):
    import backend.main as m
    matter = _matter()
    pool = FakePool(matters=[matter])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())
    asyncio.run(seed_matter_checklist(str(matter["id"]), ChecklistSeedBody(category="individual"), FakeRequest()))
    item = pool.conn.checklist_items[0]

    with pytest.raises(HTTPException) as exc:
        asyncio.run(update_checklist_item(str(matter["id"]), str(item["id"]), ChecklistItemUpdate(), FakeRequest()))
    assert exc.value.status_code == 400


# ── create_matter(): auto-seed on creation ──────────────────────────────────

def test_create_matter_auto_seeds_individual_checklist(monkeypatch):
    import backend.main as m
    client = _client(client_type="Individual")
    pool = FakePool(clients=[client])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())

    result = asyncio.run(create_matter(
        MatterCreate(name="Title Validation — X", matter_type="title_deeds_validation", client_id=str(client["id"])),
        FakeRequest(),
    ))

    assert result["title_deeds_checklist"]["category"] == "individual"
    assert result["title_deeds_checklist"]["completion"]["total"] == 9
    assert len(pool.conn.checklist_items) == 9


def test_create_matter_auto_seeds_company_checklist(monkeypatch):
    import backend.main as m
    client = _client(client_type="Company")
    pool = FakePool(clients=[client])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())

    result = asyncio.run(create_matter(
        MatterCreate(name="Title Validation — Co", matter_type="title_deeds_validation", client_id=str(client["id"])),
        FakeRequest(),
    ))
    assert result["title_deeds_checklist"]["category"] == "company"


def test_create_matter_auto_seeds_trust_checklist(monkeypatch):
    import backend.main as m
    client = _client(client_type="Trust")
    pool = FakePool(clients=[client])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())

    result = asyncio.run(create_matter(
        MatterCreate(name="Title Validation — Trust", matter_type="title_deeds_validation", client_id=str(client["id"])),
        FakeRequest(),
    ))
    assert result["title_deeds_checklist"]["category"] == "trust"


def test_create_matter_does_not_seed_for_unmapped_client_type(monkeypatch):
    """Partnership has no defined document list -- left unseeded rather
    than guessed, per the confirmed design."""
    import backend.main as m
    client = _client(client_type="Partnership")
    pool = FakePool(clients=[client])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())

    result = asyncio.run(create_matter(
        MatterCreate(name="Title Validation — Ptnr", matter_type="title_deeds_validation", client_id=str(client["id"])),
        FakeRequest(),
    ))
    assert result["title_deeds_checklist"] is None
    assert pool.conn.checklist_items == []


def test_create_matter_does_not_seed_without_a_client_id(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())

    result = asyncio.run(create_matter(
        MatterCreate(name="Title Validation — No Client", matter_type="title_deeds_validation",
                     client_name="Walk-in"),
        FakeRequest(),
    ))
    assert result["title_deeds_checklist"] is None
    assert pool.conn.checklist_items == []


def test_create_matter_does_not_seed_for_a_different_matter_type(monkeypatch):
    """The workflow flag is what gates this, not just having a client_id
    -- an ordinary conveyancing matter must never get a checklist."""
    import backend.main as m
    client = _client(client_type="Individual")
    pool = FakePool(clients=[client])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner_user())

    result = asyncio.run(create_matter(
        MatterCreate(name="Ordinary Sale", matter_type="conveyancing", client_id=str(client["id"])),
        FakeRequest(),
    ))
    assert result["title_deeds_checklist"] is None
    assert pool.conn.checklist_items == []


# ── list_matters(): batched completion summary ──────────────────────────────

def test_list_matters_attaches_checklist_completion_for_title_deeds_matters(monkeypatch):
    import backend.main as m
    matter = _matter(matter_type="title_deeds_validation")
    monkeypatch.setattr(m, "_db_pool", FakePool(
        matters=[matter],
        checklist_items=[
            {"matter_id": matter["id"], "firm_id": FIRM_ID, "category": "individual",
             "item_key": "fees", "status": "Collected"},
            {"matter_id": matter["id"], "firm_id": FIRM_ID, "category": "individual",
             "item_key": "email", "status": "Outstanding"},
        ],
    ))
    _as_current_user(monkeypatch, m, _partner_user())

    result = asyncio.run(list_matters(FakeRequest()))

    assert result[0]["title_deeds_checklist"]["completion"] == {"collected": 1, "total": 2, "status": "in_progress"}


def test_list_matters_omits_checklist_key_for_non_title_deeds_matters(monkeypatch):
    import backend.main as m
    matter = _matter(matter_type="conveyancing")
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter]))
    _as_current_user(monkeypatch, m, _partner_user())

    result = asyncio.run(list_matters(FakeRequest()))

    assert "title_deeds_checklist" not in result[0]
