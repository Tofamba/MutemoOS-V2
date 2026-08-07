"""
Unit tests for POST /api/onboarding/bulk-upload — onboards a lawyer plus
their client base from a filled copy of the firm's Client Database Excel
form (fixed layout: B3-B6 lawyer info, row 11 headers, row 12+ client/
matter rows — see backend/main.py::bulk_onboard_from_excel's docstring).

Builds real .xlsx fixtures via openpyxl matching the exact template layout
(same technique as tests/test_bulk_import_clients.py) and calls the
endpoint function directly with a fake DB pool, same convention as this
repo's other backend tests — see tests/test_docx_export.py's docstring for
why (AUTH_ENABLED is False by default, so get_current_user() never
touches `request`).
"""

import asyncio
import io

import openpyxl
import pytest
from fastapi import HTTPException

from backend.main import FIRM_ID, bulk_onboard_from_excel


class FakeUploadFile:
    def __init__(self, filename, content: bytes):
        self.filename = filename
        self._content = content

    async def read(self):
        return self._content


def _build_onboarding_xlsx(lawyer, blocks):
    """
    lawyer: {"name", "phone", "email", "role"}
    blocks: list of {"name", "phone", "email", "contact_person", "matters": [text, ...]}

    Column layout: A=Client Name, B=Phone, C=Email, D=Contact Person
    (companies/entities only), E=Matter description.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A3"] = "Your Name:"
    ws["B3"] = lawyer.get("name")
    ws["A4"] = "Your Phone Number:"
    ws["B4"] = lawyer.get("phone")
    ws["A5"] = "Your Email Address:"
    ws["B5"] = lawyer.get("email")
    ws["A6"] = "Your Role:"
    ws["B6"] = lawyer.get("role")
    ws["A11"] = "Client Name (Surname first)"
    ws["B11"] = "Telephone Number"
    ws["C11"] = "Email Address (optional)"
    ws["D11"] = "Contact Person (companies only)"
    ws["E11"] = "Matter (Reference No. — description)"

    row = 12
    for block in blocks:
        matters = block.get("matters") or [None]
        first = True
        for matter_text in matters:
            if first:
                ws[f"A{row}"] = block["name"]
                ws[f"B{row}"] = block.get("phone")
                ws[f"C{row}"] = block.get("email")
                ws[f"D{row}"] = block.get("contact_person")
                first = False
            if matter_text:
                ws[f"E{row}"] = matter_text
            row += 1

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, users=None, clients=None, matters=None):
        self.users = users if users is not None else []
        self.clients = clients if clients is not None else []
        self.matters = matters if matters is not None else []

    def transaction(self):
        return _FakeTransaction()

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT * FROM users WHERE firm_id=$1 AND phone=$2"):
            firm_id, phone = args
            for u in self.users:
                if u["firm_id"] == firm_id and u["phone"] == phone:
                    return dict(u)
            return None
        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT id, full_name FROM clients WHERE firm_id=$1"):
            firm_id, = args
            return [dict(c) for c in self.clients if c["firm_id"] == firm_id]
        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")

    async def execute(self, query, *args):
        q = " ".join(query.split())
        cols_str = q.split("(", 1)[1].split(")", 1)[0] if "(" in q.split("VALUES")[0] else None
        if q.startswith("INSERT INTO users"):
            cols = [c.strip() for c in q.split("(", 1)[1].split(")", 1)[0].split(",")]
            self.users.append(dict(zip(cols, args)))
        elif q.startswith("INSERT INTO clients"):
            cols = [c.strip() for c in q.split("(", 1)[1].split(")", 1)[0].split(",")]
            self.clients.append(dict(zip(cols, args)))
        elif q.startswith("INSERT INTO matters"):
            cols = [c.strip() for c in q.split("(", 1)[1].split(")", 1)[0].split(",")]
            self.matters.append(dict(zip(cols, args)))
        else:
            raise NotImplementedError(f"FakeConnection.execute: unhandled query: {q}")
        return "OK"


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, users=None, clients=None, matters=None):
        self.conn = FakeConnection(users, clients, matters)

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


LAWYER = {"name": "Tendai Moyo", "phone": "+263771112222", "email": "tendai@sm.co.zw", "role": "Associate"}


# ── clean single-client single-matter ───────────────────────────────────────

def test_clean_single_client_single_matter(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    content = _build_onboarding_xlsx(LAWYER, [{
        "name": "Huang Li Qiang", "phone": "+263771234567", "email": "huang@example.com",
        "matters": ["HC 1234/26 — Debt collection"],
    }])
    result = asyncio.run(bulk_onboard_from_excel(None, FakeUploadFile("form.xlsx", content), commit=True))

    assert result["lawyer"]["action"] == "created"
    assert len(result["clients"]["created"]) == 1
    assert result["clients"]["created"][0]["name"] == "Huang Li Qiang"
    assert len(result["matters"]["created"]) == 1
    assert result["matters"]["created"][0]["name"] == "HC 1234/26 — Debt collection"
    assert result["clients"]["review"] == []

    assert len(pool.conn.users) == 1
    assert len(pool.conn.clients) == 1
    assert len(pool.conn.matters) == 1
    assert pool.conn.matters[0]["client_id"] == pool.conn.clients[0]["id"]


# ── multi-matter client block ────────────────────────────────────────────

def test_multi_matter_client_block(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    content = _build_onboarding_xlsx(LAWYER, [{
        "name": "Huang Li Qiang", "phone": "+263771234567", "email": "huang@example.com",
        "matters": [
            "Mukweva and Paswa Civil — Debt collection/fraud, replicated to special plea",
            "Mukweva Criminal — Criminal fraud, following up constitutional application",
            "Kuvimba Mining Corporation — Debt collection, settlement in place",
        ],
    }])
    result = asyncio.run(bulk_onboard_from_excel(None, FakeUploadFile("form.xlsx", content), commit=True))

    assert len(result["clients"]["created"]) == 1  # still one client
    assert len(result["matters"]["created"]) == 3   # three matters, same client
    client_id = result["clients"]["created"][0]["client_id"]
    assert all(mm["row"] == result["clients"]["created"][0]["row"] for mm in result["matters"]["created"])
    assert len(pool.conn.matters) == 3
    assert all(str(row["client_id"]) == client_id for row in pool.conn.matters)


# ── existing-user-phone match (no duplicate) ────────────────────────────

def test_existing_user_phone_matches_no_duplicate(monkeypatch):
    import backend.main as m
    existing_users = [{
        "id": "existing-user-id", "firm_id": FIRM_ID, "phone": LAWYER["phone"],
        "display_name": "Tendai T. Moyo", "role": "partner", "email": "t.moyo@sm.co.zw",
    }]
    pool = FakePool(users=existing_users)
    monkeypatch.setattr(m, "_db_pool", pool)

    content = _build_onboarding_xlsx(LAWYER, [{
        "name": "Vengesai Enterprises", "phone": "+263772223333", "email": None, "matters": ["HC 99/26 — Contract dispute"],
    }])
    result = asyncio.run(bulk_onboard_from_excel(None, FakeUploadFile("form.xlsx", content), commit=True))

    assert result["lawyer"]["action"] == "matched"
    assert result["lawyer"]["user_id"] == "existing-user-id"
    assert result["lawyer"]["display_name"] == "Tendai T. Moyo"  # existing record, not overwritten
    assert len(pool.conn.users) == 1  # no duplicate created
    assert pool.conn.matters[0]["created_by"] == "existing-user-id"


# ── ambiguous client name lands in review, not auto-merged ──────────────

def test_ambiguous_client_name_lands_in_review_not_auto_merged(monkeypatch):
    import backend.main as m
    existing_clients = [
        {"id": "c1", "firm_id": FIRM_ID, "full_name": "John Moyo"},
        {"id": "c2", "firm_id": FIRM_ID, "full_name": "Jon Moyo"},
    ]
    pool = FakePool(clients=existing_clients)
    monkeypatch.setattr(m, "_db_pool", pool)

    content = _build_onboarding_xlsx(LAWYER, [{
        "name": "J. Moyo", "phone": "+263773334444", "email": None, "matters": ["HC 55/26 — Eviction"],
    }])
    result = asyncio.run(bulk_onboard_from_excel(None, FakeUploadFile("form.xlsx", content), commit=True))

    assert result["clients"]["created"] == []
    assert result["clients"]["matched"] == []
    assert len(result["clients"]["review"]) == 1
    review_entry = result["clients"]["review"][0]
    assert review_entry["name"] == "J. Moyo"
    assert {c["id"] for c in review_entry["candidates"]} == {"c1", "c2"}

    # No new client was created for the ambiguous name.
    assert len(pool.conn.clients) == 2

    # The matter still gets created (nothing silently dropped) but with no
    # client_id — the legacy fallback state — and shows up in the
    # unlinked/pending-review bucket, not the normal "created" list.
    assert result["matters"]["created"] == []
    assert len(result["matters"]["created_unlinked_pending_review"]) == 1
    assert len(pool.conn.matters) == 1
    assert pool.conn.matters[0]["client_id"] is None
    assert pool.conn.matters[0]["client_name"] == "J. Moyo"


# ── corporate/entity name parsed as-is (not surname-flipped) ────────────

def test_corporate_entity_name_stored_as_is(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    content = _build_onboarding_xlsx(LAWYER, [{
        "name": "Vengesai Enterprises", "phone": "+263774445555", "email": "info@vengesai.co.zw",
        "matters": ["HC 200/26 — Commercial lease dispute"],
    }])
    result = asyncio.run(bulk_onboard_from_excel(None, FakeUploadFile("form.xlsx", content), commit=True))

    assert result["clients"]["created"][0]["name"] == "Vengesai Enterprises"
    assert pool.conn.clients[0]["full_name"] == "Vengesai Enterprises"


# ── contact_person: corporate client sets it, individual leaves it blank ──

def test_corporate_client_with_contact_person(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    content = _build_onboarding_xlsx(LAWYER, [{
        "name": "Vengesai Enterprises", "phone": "+263774445555", "email": "info@vengesai.co.zw",
        "contact_person": "Jane Muzenda (Company Secretary)",
        "matters": ["HC 200/26 — Commercial lease dispute"],
    }])
    result = asyncio.run(bulk_onboard_from_excel(None, FakeUploadFile("form.xlsx", content), commit=True))

    assert result["clients"]["created"][0]["contact_person"] == "Jane Muzenda (Company Secretary)"
    assert pool.conn.clients[0]["contact_person"] == "Jane Muzenda (Company Secretary)"


def test_individual_client_leaves_contact_person_blank(monkeypatch):
    """The common case — no Contact Person column entry for a person."""
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    content = _build_onboarding_xlsx(LAWYER, [{
        "name": "Huang Li Qiang", "phone": "+263771234567", "email": "huang@example.com",
        "matters": ["HC 1234/26 — Debt collection"],
    }])
    result = asyncio.run(bulk_onboard_from_excel(None, FakeUploadFile("form.xlsx", content), commit=True))

    assert result["clients"]["created"][0]["contact_person"] is None
    assert pool.conn.clients[0]["contact_person"] is None


# ── preview (commit=False) writes nothing ────────────────────────────────

def test_preview_does_not_write(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    content = _build_onboarding_xlsx(LAWYER, [{
        "name": "Huang Li Qiang", "phone": "+263771234567", "email": None, "matters": ["HC 1/26 — Test"],
    }])
    result = asyncio.run(bulk_onboard_from_excel(None, FakeUploadFile("form.xlsx", content), commit=False))

    assert result["committed"] is False
    assert result["lawyer"]["action"] == "would_create"
    assert len(result["clients"]["created"]) == 1
    assert len(result["matters"]["created"]) == 1
    # Nothing actually written to the fake DB.
    assert pool.conn.users == []
    assert pool.conn.clients == []
    assert pool.conn.matters == []


# ── header validation ────────────────────────────────────────────────────

def test_missing_lawyer_phone_rejected(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    bad_lawyer = dict(LAWYER)
    bad_lawyer["phone"] = None
    content = _build_onboarding_xlsx(bad_lawyer, [{"name": "Huang Li Qiang", "matters": ["HC 1/26 — Test"]}])

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(bulk_onboard_from_excel(None, FakeUploadFile("form.xlsx", content), commit=False))
    assert exc_info.value.status_code == 422
    assert "phone" in exc_info.value.detail.lower()


def test_rejects_non_xlsx_file():
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(bulk_onboard_from_excel(None, FakeUploadFile("form.docx", b"not really xlsx"), commit=False))
    assert exc_info.value.status_code == 422
