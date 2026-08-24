"""
Unit tests for the Matter Progress Tracker (visual stepper) —
backend/matter_stages.py + backend/main.py's update_matter()/_row_to_matter().

Before this was built: confirmed (by search across the whole codebase,
config, and the one existing plan file) that no "Procedural State
Machine" module, stage taxonomy, or per-stage SLA/duration concept
exists anywhere. The only prior art is backend/conveyancing.py's
CONVEYANCING_MILESTONES, already live as a plain <select> — reused here,
not duplicated. matter_type is the primary lookup key (matching
config/case_binder_templates.yml); a matter with
practice_area == 'Conveyancing/Property' also resolves to the
conveyancing sequence even when matter_type isn't exactly 'conveyancing',
bridging the two independent taxonomies per the confirmed design.

Called directly as plain async functions, same convention as
tests/test_conveyancing_section.py — this file's FakeConnection mirrors
that one plus the new "SELECT matter_type, practice_area" lookup
update_matter() needs to validate a bare `stage` PATCH.
"""
import asyncio
import re
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from backend.main import FIRM_ID, MatterUpdate, _row_to_matter, update_matter
from backend.matter_stages import (
    CONVEYANCING_MILESTONES,
    DEBT_COLLECTION_STAGES,
    LITIGATION_GENERAL_STAGES,
    resolve_stage_sequence,
    stage_storage_field,
)


class FakeConnection:
    def __init__(self, matters):
        self.matters = matters

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT matter_type, practice_area FROM matters WHERE id=$1 AND firm_id=$2"):
            matter_id, firm_id = args
            for row in self.matters:
                if row["id"] == matter_id and row["firm_id"] == firm_id:
                    return {"matter_type": row["matter_type"], "practice_area": row["practice_area"]}
            return None

        if q.startswith("UPDATE matters SET"):
            match = re.search(r"SET (.+) WHERE id=\$1", q)
            cols = re.findall(r"(\w+)=\$\d+", match.group(1))
            matter_id, firm_id = args[0], args[-1]
            values = args[1:1 + len(cols)]
            for row in self.matters:
                if row["id"] == matter_id and row["firm_id"] == firm_id:
                    for col, val in zip(cols, values):
                        row[col] = val
                    return dict(row)
            return None

        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT * FROM progress_notes"):
            return []
        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")

    async def execute(self, query, *args):
        return "OK"


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, matters=None):
        self.conn = FakeConnection(matters if matters is not None else [])

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _matter_row(matter_id, matter_type=None, practice_area=None, stage=None,
                 conveyancing_milestone=None, stage_updated_at=None, firm_id=FIRM_ID):
    return {
        "id": matter_id, "firm_id": firm_id, "name": "Test Matter", "number": None,
        "internal_ref": None, "external_ref": None, "client_name": None, "client_id": None,
        "case_parties": None, "matter_type": matter_type, "practice_area": practice_area,
        "status": "Active", "custom_status": None, "document_count": 0, "last_activity": None,
        "created_at": datetime.now(timezone.utc), "created_by": None,
        "next_deadline": None, "next_deadline_note": None,
        "conveyancing_milestone": conveyancing_milestone, "conveyancing_property_address": None,
        "conveyancing_title_deed_number": None, "conveyancing_purchase_price": None,
        "conveyancing_other_conveyancer_contact": None, "conveyancing_transfer_date": None,
        "conveyancing_rates_clearance_expiry": None, "conveyancing_bond_registration_deadline": None,
        "amount_billed": None, "amount_received": None,
        "stage": stage, "stage_updated_at": stage_updated_at,
    }


def _fake_request():
    return None


# ── resolve_stage_sequence: per matter_type, and the conveyancing bridge ──

def test_resolve_stage_sequence_for_each_defined_matter_type():
    assert resolve_stage_sequence("conveyancing", None) == CONVEYANCING_MILESTONES
    assert resolve_stage_sequence("debt_collection", None) == DEBT_COLLECTION_STAGES
    assert resolve_stage_sequence("litigation_general", None) == LITIGATION_GENERAL_STAGES


def test_resolve_stage_sequence_bridges_practice_area_to_conveyancing():
    """A matter classified via practice_area (the existing conveyancing
    milestone feature's real key) still gets the conveyancing sequence
    even when matter_type isn't exactly 'conveyancing'."""
    assert resolve_stage_sequence(None, "Conveyancing/Property") == CONVEYANCING_MILESTONES
    assert resolve_stage_sequence("other", "Conveyancing/Property") == CONVEYANCING_MILESTONES


def test_resolve_stage_sequence_returns_none_for_undefined_matter_type():
    assert resolve_stage_sequence("eviction", None) is None
    assert resolve_stage_sequence(None, None) is None


def test_stage_storage_field_routes_conveyancing_to_its_existing_column():
    assert stage_storage_field("conveyancing", None) == "conveyancing_milestone"
    assert stage_storage_field(None, "Conveyancing/Property") == "conveyancing_milestone"
    assert stage_storage_field("debt_collection", None) == "stage"
    assert stage_storage_field("litigation_general", None) == "stage"


# ── _row_to_matter(): stage_info reflects actual stored state ───────────

def test_stage_info_reflects_current_stage_and_index():
    row = _matter_row(uuid.uuid4(), matter_type="litigation_general", stage="Pending Service")
    m = _row_to_matter(row)

    assert m["stage_info"]["current_stage"] == "Pending Service"
    assert m["stage_info"]["current_index"] == LITIGATION_GENERAL_STAGES.index("Pending Service")
    assert m["stage_info"]["sequence"] == LITIGATION_GENERAL_STAGES


def test_stage_info_uses_conveyancing_milestone_column_for_conveyancing():
    row = _matter_row(uuid.uuid4(), practice_area="Conveyancing/Property",
                       conveyancing_milestone="Deposit Paid")
    m = _row_to_matter(row)

    assert m["stage_info"]["current_stage"] == "Deposit Paid"
    assert m["stage_info"]["current_index"] == 1


def test_stage_info_current_index_is_none_when_no_stage_set_yet():
    row = _matter_row(uuid.uuid4(), matter_type="debt_collection")
    m = _row_to_matter(row)

    assert m["stage_info"]["current_stage"] is None
    assert m["stage_info"]["current_index"] is None
    assert m["stage_info"]["days_in_stage"] is None


def test_days_in_stage_computed_from_stage_updated_at():
    ten_days_ago = datetime.now(timezone.utc) - timedelta(days=10)
    row = _matter_row(uuid.uuid4(), matter_type="debt_collection",
                       stage="Summons Issued", stage_updated_at=ten_days_ago)
    m = _row_to_matter(row)

    assert m["stage_info"]["days_in_stage"] == 10


def test_days_in_stage_handles_a_naive_stage_updated_at_without_crashing():
    """Regression: computing days_in_stage against a naive datetime
    (e.g. datetime.utcnow(), the write-side convention used elsewhere in
    this codebase) previously raised 'can't subtract offset-naive and
    offset-aware datetimes' if the value round-tripped back naive."""
    naive_ten_days_ago = datetime.utcnow() - timedelta(days=10)
    row = _matter_row(uuid.uuid4(), matter_type="debt_collection",
                       stage="Summons Issued", stage_updated_at=naive_ten_days_ago)

    m = _row_to_matter(row)  # must not raise

    assert m["stage_info"]["days_in_stage"] == 10


# ── Graceful fallback: no defined sequence -> stage_info is None ────────

def test_stage_info_is_none_for_a_matter_type_with_no_defined_sequence():
    row = _matter_row(uuid.uuid4(), matter_type="eviction")
    m = _row_to_matter(row)

    assert m["stage_info"] is None
    assert m["matter_type"] == "eviction"  # existing fields still render normally


def test_stage_info_is_none_when_matter_type_and_practice_area_are_both_unset():
    row = _matter_row(uuid.uuid4())
    m = _row_to_matter(row)
    assert m["stage_info"] is None


# ── Manual advancement via update_matter() ───────────────────────────────

def test_manual_advancement_updates_stage_and_stage_updated_at(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id, matter_type="litigation_general", stage="Draft Created")])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_matter(str(matter_id), MatterUpdate(stage="Documents Prepared"), _fake_request()))

    assert result["stage_info"]["current_stage"] == "Documents Prepared"
    assert result["stage_updated_at"] is not None


def test_advancement_rejects_a_stage_not_in_the_matter_types_sequence(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id, matter_type="litigation_general", stage="Draft Created")])
    monkeypatch.setattr(m, "_db_pool", pool)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_matter(str(matter_id), MatterUpdate(stage="Deeds Registered"), _fake_request()))
    assert exc_info.value.status_code == 422


def test_advancement_rejects_stage_for_a_matter_type_with_no_defined_sequence(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id, matter_type="eviction")])
    monkeypatch.setattr(m, "_db_pool", pool)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_matter(str(matter_id), MatterUpdate(stage="Anything"), _fake_request()))
    assert exc_info.value.status_code == 422


def test_advancing_conveyancing_milestone_also_touches_stage_updated_at(monkeypatch):
    """conveyancing_milestone already had its own validation before this
    feature -- confirms it now also updates the shared stage_updated_at
    timestamp used for days-in-stage, without changing its existing
    validation behaviour."""
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id, practice_area="Conveyancing/Property",
                                          conveyancing_milestone="Agreement of Sale Signed")])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_matter(
        str(matter_id), MatterUpdate(conveyancing_milestone="Deposit Paid"), _fake_request()
    ))

    assert result["conveyancing_milestone"] == "Deposit Paid"
    assert result["stage_updated_at"] is not None
