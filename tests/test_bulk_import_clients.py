"""
Unit tests for the client_id/case_parties handling added to
POST /api/matters/bulk-import in backend/main.py.

Design decision under test: bulk_import_matters does NOT accept a per-row
client_id. The import is a raw file upload (one .docx/.xlsx per request,
many rows, no client-picker UI in that flow) and the import templates have
no client_id column — there's nowhere for a caller to supply one. Matters
created this way keep the legacy client_name-only path (client_id stays
NULL), the same state every pre-Client matter is already in, and are
expected to be linked up later via scripts/migrate_clients.py, same as any
other existing matter.

case_parties DOES get populated — from the "Opposing Party" column, which
already existed in the Excel template's alias map (COL_ALIASES) but was
previously only used as a matter-name fallback and otherwise discarded.

Called directly as a plain async function, same convention as the other
new test files in this session — see tests/test_docx_export.py's docstring
for why (AUTH_ENABLED is False by default, and bulk_import_matters treats
a None `request` as "skip auth" by its own `if request:` guard).
"""

import asyncio
import io

import openpyxl
import pytest
from docx import Document as DocxWriter

from backend.main import FIRM_ID, bulk_import_matters


class FakeUploadFile:
    def __init__(self, filename, content: bytes):
        self.filename = filename
        self._content = content

    async def read(self):
        return self._content


class FakeConnection:
    def __init__(self):
        self.matters = []
        self.notes = []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO matters"):
            cols_str = q.split("(", 1)[1].split(")", 1)[0]
            cols = [c.strip() for c in cols_str.split(",")]
            row = dict(zip(cols, args))
            self.matters.append(row)
            return dict(row)
        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {query}")

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO progress_notes"):
            self.notes.append(args)
        return "OK"


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self):
        self.conn = FakeConnection()

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _build_xlsx(rows):
    """rows: list of dicts with keys internal_ref, client_name, subject,
    opposing, law_type, external_ref, status, action_done, next_action, latest_comm."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([
        "Internal Ref", "Client Name", "Matter Description", "Opposing Party",
        "Area of Law", "External Ref", "Status", "Action Done", "Next Action", "Latest Communication",
    ])
    # bulk_import_matters always skips the row immediately after the header
    # (min_row=header_row+2) — real templates ship an "EXAMPLE" row there for
    # the user's reference. Match that structure or the first real data row
    # silently gets skipped too.
    ws.append(["EXAMPLE", "Example Client", "Example matter", "", "", "", "", "", "", ""])
    for r in rows:
        ws.append([
            r.get("internal_ref", ""), r.get("client_name", ""), r.get("subject", ""),
            r.get("opposing", ""), r.get("law_type", ""), r.get("external_ref", ""),
            r.get("status", ""), r.get("action_done", ""), r.get("next_action", ""),
            r.get("latest_comm", ""),
        ])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_docx(rows):
    """rows: list of dicts with keys internal_ref, client_name, subject,
    law_type, external_ref, status, action_done, next_action, latest_communication.
    Each row becomes its own 2-column table, matching how bulk_import_matters
    reads one matter per table."""
    doc = DocxWriter()
    field_labels = {
        "internal_ref": "File Name", "client_name": "Name of Client", "subject": "Re",
        "law_type": "Area of Law", "external_ref": "External Reference",
        "action_done": "Action Done", "next_action": "Next Action", "status": "Status",
        "latest_communication": "Latest Communication",
    }
    for r in rows:
        table = doc.add_table(rows=0, cols=2)
        for key, label in field_labels.items():
            if key in r:
                cells = table.add_row().cells
                cells[0].text = label
                cells[1].text = r[key]
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ── xlsx: case_parties populated from Opposing Party column ────────────────

def test_bulk_import_xlsx_populates_case_parties_from_opposing_party(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    content = _build_xlsx([{
        "internal_ref": "NGM 5", "client_name": "Tendai Chikwanha",
        "subject": "Employment dispute", "opposing": "Zenith Pvt Ltd (Chikwanha's employer)",
        "law_type": "employment", "external_ref": "LC 55/26", "status": "Active",
    }])
    result = asyncio.run(bulk_import_matters(FakeUploadFile("import.xlsx", content), None))

    assert result["created"] == 1
    assert len(pool.conn.matters) == 1
    row = pool.conn.matters[0]
    assert row["case_parties"] == "Zenith Pvt Ltd (Chikwanha's employer)"
    assert row["client_name"] == "Tendai Chikwanha"
    assert row["client_id"] is None  # never linked — see module docstring


def test_bulk_import_xlsx_case_parties_null_when_no_opposing_party_given(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    content = _build_xlsx([{
        "internal_ref": "NGM 6", "client_name": "Jane Sithole",
        "subject": "Debt collection", "law_type": "debt", "external_ref": "LC 56/26", "status": "Active",
    }])
    result = asyncio.run(bulk_import_matters(FakeUploadFile("import.xlsx", content), None))

    assert result["created"] == 1
    assert pool.conn.matters[0]["case_parties"] is None


def test_bulk_import_xlsx_multiple_rows_all_get_correct_case_parties(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    content = _build_xlsx([
        {"internal_ref": "NGM 7", "client_name": "Client A", "opposing": "Respondent A", "external_ref": "LC 57/26"},
        {"internal_ref": "NGM 8", "client_name": "Client B", "external_ref": "LC 58/26"},
        {"internal_ref": "NGM 9", "client_name": "Client C", "opposing": "Respondent C", "external_ref": "LC 59/26"},
    ])
    result = asyncio.run(bulk_import_matters(FakeUploadFile("import.xlsx", content), None))

    assert result["created"] == 3
    by_ref = {row["internal_ref"]: row for row in pool.conn.matters}
    assert by_ref["NGM 7"]["case_parties"] == "Respondent A"
    assert by_ref["NGM 8"]["case_parties"] is None
    assert by_ref["NGM 9"]["case_parties"] == "Respondent C"


# ── docx: legacy path unaffected (no Opposing Party field in this template) ─

def test_bulk_import_docx_still_works_case_parties_null(monkeypatch):
    """Regression check: the Word template has no Opposing Party field, so
    case_parties is expected to stay NULL there — but the row must still
    import correctly with the new nullable columns present in the INSERT."""
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    content = _build_docx([{
        "internal_ref": "NGM 10", "client_name": "Peter Ndlovu", "subject": "Lease dispute",
        "law_type": "property", "external_ref": "LC 60/26", "status": "Active",
    }])
    result = asyncio.run(bulk_import_matters(FakeUploadFile("import.docx", content), None))

    assert result["created"] == 1
    row = pool.conn.matters[0]
    assert row["client_name"] == "Peter Ndlovu"
    assert row["case_parties"] is None
    assert row["client_id"] is None


# ── practice_area reuses the already-computed matter_type (fix, 2026-09-01) ─
# detect_matter_type() was already classifying "Area of Law" text into a
# real matter_type, then discarding that signal instead of also setting
# practice_area -- every bulk-imported matter landed as "Uncategorized"
# regardless of how specific the source law_type text was. See
# backend/practice_areas.py's INTAKE_MATTER_TYPE_TO_PRACTICE_AREA (shared
# with the client_intake fix -- same enum vocabulary).

def test_bulk_import_xlsx_sets_practice_area_from_law_type(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    content = _build_xlsx([{
        "internal_ref": "NGM 11", "client_name": "Tendai Chikwanha",
        "subject": "Unfair dismissal claim", "law_type": "employment",
        "external_ref": "LC 61/26", "status": "Active",
    }])
    result = asyncio.run(bulk_import_matters(FakeUploadFile("import.xlsx", content), None))

    assert result["matters"][0]["matter_type"] == "employment"
    assert result["matters"][0]["practice_area"] == "Labour"
    assert pool.conn.matters[0]["practice_area"] == "Labour"


def test_bulk_import_xlsx_unrecognized_law_type_maps_to_other(monkeypatch):
    """detect_matter_type() falls back to "other" when nothing in
    LAW_TYPE_MAP matches -- mapped to the real "Other" practice area
    bucket, not left NULL, same choice made for the intake path."""
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    content = _build_xlsx([{
        "internal_ref": "NGM 12", "client_name": "Jane Sithole",
        "subject": "General consultation", "law_type": "something unrecognized",
        "external_ref": "LC 62/26", "status": "Active",
    }])
    result = asyncio.run(bulk_import_matters(FakeUploadFile("import.xlsx", content), None))

    assert result["matters"][0]["matter_type"] == "other"
    assert result["matters"][0]["practice_area"] == "Other"


def test_bulk_import_xlsx_multiple_rows_get_independently_correct_practice_areas(monkeypatch):
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    content = _build_xlsx([
        {"internal_ref": "NGM 13", "client_name": "Client A", "law_type": "trust", "external_ref": "LC 63/26"},
        {"internal_ref": "NGM 14", "client_name": "Client B", "law_type": "debt collection", "external_ref": "LC 64/26"},
        {"internal_ref": "NGM 15", "client_name": "Client C", "law_type": "conveyancing", "external_ref": "LC 65/26"},
    ])
    result = asyncio.run(bulk_import_matters(FakeUploadFile("import.xlsx", content), None))

    by_ref = {row["internal_ref"]: row for row in pool.conn.matters}
    assert by_ref["NGM 13"]["practice_area"] == "Trust"
    assert by_ref["NGM 14"]["practice_area"] == "Debt Collection"
    assert by_ref["NGM 15"]["practice_area"] == "Conveyancing/Property"


def test_bulk_import_docx_also_sets_practice_area(monkeypatch):
    """Regression check across both file formats -- build_matter_dict() is
    shared by the xlsx and docx branches, so the docx path must get the
    same fix for free."""
    import backend.main as m
    pool = FakePool()
    monkeypatch.setattr(m, "_db_pool", pool)

    content = _build_docx([{
        "internal_ref": "NGM 16", "client_name": "Peter Ndlovu", "subject": "Lease dispute",
        "law_type": "property", "external_ref": "LC 66/26", "status": "Active",
    }])
    result = asyncio.run(bulk_import_matters(FakeUploadFile("import.docx", content), None))

    assert pool.conn.matters[0]["matter_type"] == "commercial_property"
    assert pool.conn.matters[0]["practice_area"] == "Conveyancing/Property"


def test_bulk_import_rejects_unsupported_file_type():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(bulk_import_matters(FakeUploadFile("import.pdf", b"not a real file"), None))
    assert exc_info.value.status_code == 422
