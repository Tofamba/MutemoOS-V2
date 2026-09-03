"""
Unit tests for matter-level AML fields (backend/main.py, 2026-09-03,
Individual Client AML/CDD Report Part B): matters.aml_scope,
matters.aml_scope_reason, matters.matter_risk.

Same honest framing as client_compliance.aml_scope (tests/
test_client_compliance.py's own "AML Scope" section) -- manually set by
a lawyer, NOT auto-derived from matter type. The same client can have
differently-scoped matters (a property acquisition vs. a divorce, the
sample report's own example) -- that's the whole reason this lives on
the matter, not just the client; see tests/test_client_aml_cdd_report.py
for that multi-matter scenario end-to-end.

Called directly as plain async functions, same convention as
tests/test_matter_fees.py, whose FakeConnection/FakePool/_matter_row
this file mirrors.
"""

import asyncio
import re
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from backend.main import FIRM_ID, MatterUpdate, update_matter


class FakeConnection:
    def __init__(self, matters):
        self.matters = matters

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

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


def _matter_row(matter_id, firm_id=FIRM_ID, **overrides):
    row = {
        "id": matter_id, "firm_id": firm_id, "name": "Acquisition of commercial property",
        "number": None, "internal_ref": None, "external_ref": None,
        "client_name": None, "client_id": None, "case_parties": None,
        "matter_type": None, "practice_area": None, "status": "Active",
        "custom_status": None, "document_count": 0, "last_activity": None,
        "created_at": datetime.now(timezone.utc), "created_by": None,
        "next_deadline": None, "next_deadline_note": None,
        "amount_billed": None, "amount_received": None,
        "aml_scope": "NotAssessed", "aml_scope_reason": None, "matter_risk": "NotAssessed",
    }
    row.update(overrides)
    return row


def _fake_request():
    return None


def test_aml_scope_rejects_invalid_value(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[_matter_row(matter_id)]))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_matter(str(matter_id), MatterUpdate(aml_scope="Somewhere"), _fake_request()))
    assert exc_info.value.status_code == 422


def test_aml_scope_accepts_every_valid_value(monkeypatch):
    import backend.main as m
    for value in m.AML_SCOPE_VALUES:
        matter_id = uuid.uuid4()
        monkeypatch.setattr(m, "_db_pool", FakePool(matters=[_matter_row(matter_id)]))

        result = asyncio.run(update_matter(str(matter_id), MatterUpdate(aml_scope=value), _fake_request()))
        assert result["aml_scope"] == value


def test_matter_risk_rejects_invalid_value(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[_matter_row(matter_id)]))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_matter(str(matter_id), MatterUpdate(matter_risk="Severe"), _fake_request()))
    assert exc_info.value.status_code == 422


def test_matter_risk_accepts_every_valid_value(monkeypatch):
    import backend.main as m
    for value in m.RISK_RATINGS:
        matter_id = uuid.uuid4()
        monkeypatch.setattr(m, "_db_pool", FakePool(matters=[_matter_row(matter_id)]))

        result = asyncio.run(update_matter(str(matter_id), MatterUpdate(matter_risk=value), _fake_request()))
        assert result["matter_risk"] == value


def test_aml_scope_reason_is_free_text_no_validation(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[_matter_row(matter_id)]))

    result = asyncio.run(update_matter(
        str(matter_id),
        MatterUpdate(aml_scope="InScope", aml_scope_reason="Transaction involves acquisition of immovable property."),
        _fake_request(),
    ))

    assert result["aml_scope"] == "InScope"
    assert result["aml_scope_reason"] == "Transaction involves acquisition of immovable property."


def test_two_matters_for_the_same_client_can_have_different_aml_scope(monkeypatch):
    """The whole reason this lives on the matter, not just the client --
    mirrors the sample report's Blue Ridge Traders example: a property
    acquisition (In Scope, High risk) vs. a divorce (Out of Scope, Low
    risk) for the same client."""
    import backend.main as m
    client_id = uuid.uuid4()
    property_matter = uuid.uuid4()
    divorce_matter = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[
        _matter_row(property_matter, client_id=client_id, name="Acquisition of commercial property"),
        _matter_row(divorce_matter, client_id=client_id, name="Divorce proceedings"),
    ]))

    asyncio.run(update_matter(str(property_matter), MatterUpdate(
        aml_scope="InScope", aml_scope_reason="Transaction involves acquisition of immovable property.",
        matter_risk="High",
    ), _fake_request()))
    asyncio.run(update_matter(str(divorce_matter), MatterUpdate(
        aml_scope="OutOfScope", matter_risk="Low",
    ), _fake_request()))

    property_result = asyncio.run(update_matter(str(property_matter), MatterUpdate(custom_status="X"), _fake_request()))
    divorce_result = asyncio.run(update_matter(str(divorce_matter), MatterUpdate(custom_status="Y"), _fake_request()))

    assert property_result["aml_scope"] == "InScope"
    assert property_result["matter_risk"] == "High"
    assert divorce_result["aml_scope"] == "OutOfScope"
    assert divorce_result["matter_risk"] == "Low"
