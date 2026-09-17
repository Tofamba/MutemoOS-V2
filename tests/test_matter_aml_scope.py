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
import json
import re
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from backend.main import FIRM_ID, MatterUpdate, update_matter


class FakeConnection:
    def __init__(self, matters):
        self.matters = matters
        self.audit_logs = []  # captured INSERT INTO audit_logs calls -- see Part C tests below

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT aml_scope, matter_risk, matter_risk_reason FROM matters WHERE id=$1"):
            matter_id, firm_id = args
            for row in self.matters:
                if row["id"] == matter_id and row["firm_id"] == firm_id:
                    return {
                        "aml_scope": row["aml_scope"], "matter_risk": row["matter_risk"],
                        "matter_risk_reason": row.get("matter_risk_reason"),
                    }
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
        q = " ".join(query.split())
        if q.startswith("INSERT INTO audit_logs"):
            (firm_id, user_id, actor_name, actor_role, action, target_type, target_id, details) = args
            self.audit_logs.append({
                "firm_id": firm_id, "user_id": user_id, "actor_name": actor_name, "actor_role": actor_role,
                "action": action, "target_type": target_type, "target_id": target_id,
                "details": json.loads(details) if details else {},
            })
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
        "matter_risk_reason": None,
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
    """A reason is supplied for every real level (Low/Medium/High) since
    2026-09-17 requires one on a genuine transition -- see the dedicated
    'risk rating reason' section below for that requirement's own tests.
    NotAssessed needs none, matching 'required only when a rating is
    actually set.'"""
    import backend.main as m
    for value in m.RISK_RATINGS:
        matter_id = uuid.uuid4()
        monkeypatch.setattr(m, "_db_pool", FakePool(matters=[_matter_row(matter_id)]))

        reason = None if value == "NotAssessed" else "Cash-intensive business."
        result = asyncio.run(update_matter(
            str(matter_id), MatterUpdate(matter_risk=value, matter_risk_reason=reason), _fake_request()
        ))
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
        matter_risk="High", matter_risk_reason="High-value immovable property transaction.",
    ), _fake_request()))
    asyncio.run(update_matter(str(divorce_matter), MatterUpdate(
        aml_scope="OutOfScope", matter_risk="Low", matter_risk_reason="No AML nexus, personal matter.",
    ), _fake_request()))

    property_result = asyncio.run(update_matter(str(property_matter), MatterUpdate(custom_status="X"), _fake_request()))
    divorce_result = asyncio.run(update_matter(str(divorce_matter), MatterUpdate(custom_status="Y"), _fake_request()))

    assert property_result["aml_scope"] == "InScope"
    assert property_result["matter_risk"] == "High"
    assert divorce_result["aml_scope"] == "OutOfScope"
    assert divorce_result["matter_risk"] == "Low"


# ── Part C: compliance event logging (2026-09-03) ────────────────────────
# Same _log_compliance_event() call, MATTER-scoped side -- see
# tests/test_client_compliance.py's own "Part C" section for the
# CLIENT-scoped call sites (create_beneficial_owner/update_beneficial_
# owner/update_client_compliance). Confirms a real value change logs
# correctly with old/new values, and a no-op PATCH (same value re-set,
# or a PATCH that never touches aml_scope/matter_risk at all) logs
# nothing.

def test_aml_scope_change_logs_matter_aml_scope_set_with_old_and_new(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id)])
    monkeypatch.setattr(m, "_db_pool", pool)

    asyncio.run(update_matter(str(matter_id), MatterUpdate(aml_scope="InScope"), _fake_request()))

    logs = [l for l in pool.conn.audit_logs if l["action"] == "MATTER_AML_SCOPE_SET"]
    assert len(logs) == 1
    assert logs[0]["target_type"] == "MATTER"
    assert logs[0]["details"] == {"old": "NotAssessed", "new": "InScope"}


def test_matter_risk_change_logs_matter_risk_set_with_old_and_new(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id)])
    monkeypatch.setattr(m, "_db_pool", pool)

    asyncio.run(update_matter(
        str(matter_id),
        MatterUpdate(matter_risk="High", matter_risk_reason="Cash-intensive business."),
        _fake_request(),
    ))

    logs = [l for l in pool.conn.audit_logs if l["action"] == "MATTER_RISK_SET"]
    assert len(logs) == 1
    assert logs[0]["details"] == {"old": "NotAssessed", "new": "High", "reason": "Cash-intensive business."}


def test_no_op_repatch_same_aml_scope_logs_nothing(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id, aml_scope="InScope")])
    monkeypatch.setattr(m, "_db_pool", pool)

    asyncio.run(update_matter(str(matter_id), MatterUpdate(aml_scope="InScope"), _fake_request()))

    assert [l for l in pool.conn.audit_logs if l["action"] == "MATTER_AML_SCOPE_SET"] == []


def test_patch_not_touching_aml_fields_logs_nothing(monkeypatch):
    """The common case: an ordinary matter PATCH (e.g. custom_status)
    that never mentions aml_scope/matter_risk at all must not even
    attempt to log a compliance event."""
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id)])
    monkeypatch.setattr(m, "_db_pool", pool)

    asyncio.run(update_matter(str(matter_id), MatterUpdate(custom_status="Awaiting docs"), _fake_request()))

    assert pool.conn.audit_logs == []


# ── Risk rating rationale (2026-09-17, closing the confirmed adversarial-
# audit gap) ──────────────────────────────────────────────────────────────
# matter_risk_reason is required ONLY when matter_risk is genuinely
# TRANSITIONING to a real level (Low/Medium/High) different from what's
# currently stored -- checked via before/after comparison, not "matter_risk
# present => reason required", so an existing High/Medium/Low matter set
# before this fix is never retroactively blocked from an unrelated save.
# See tests/test_client_compliance.py's own equivalent section for the
# client-level (risk_rating/risk_rating_reason) counterpart.

def test_matter_risk_reason_required_when_setting_a_real_level(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[_matter_row(matter_id)]))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_matter(str(matter_id), MatterUpdate(matter_risk="High"), _fake_request()))
    assert exc_info.value.status_code == 422
    assert "matter_risk_reason" in exc_info.value.detail


def test_matter_risk_reason_not_required_when_setting_not_assessed(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[_matter_row(matter_id, matter_risk="High", matter_risk_reason="Prior reason")]))

    result = asyncio.run(update_matter(str(matter_id), MatterUpdate(matter_risk="NotAssessed"), _fake_request()))
    assert result["matter_risk"] == "NotAssessed"


def test_matter_risk_reason_required_when_changing_between_two_real_levels(monkeypatch):
    """Low -> High (not just NotAssessed -> a real level) still counts as
    a genuine transition and still requires a reason."""
    import backend.main as m
    matter_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[_matter_row(matter_id, matter_risk="Low", matter_risk_reason="Existing low-risk reason")]))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_matter(str(matter_id), MatterUpdate(matter_risk="High"), _fake_request()))
    assert exc_info.value.status_code == 422


def test_matter_risk_reason_accepted_on_a_genuine_transition(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[_matter_row(matter_id)]))

    result = asyncio.run(update_matter(
        str(matter_id),
        MatterUpdate(matter_risk="High", matter_risk_reason="Cash-intensive business, cross-border transactions."),
        _fake_request(),
    ))
    assert result["matter_risk"] == "High"
    assert result["matter_risk_reason"] == "Cash-intensive business, cross-border transactions."


def test_existing_no_reason_matter_risk_not_retroactively_blocked(monkeypatch):
    """A matter already at High risk with no reason (set before this fix)
    must NOT be blocked from an unrelated field save -- this is a
    going-forward requirement, not a punitive backfill."""
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id, matter_risk="High", matter_risk_reason=None)])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_matter(str(matter_id), MatterUpdate(custom_status="Awaiting docs"), _fake_request()))
    assert result["custom_status"] == "Awaiting docs"
    assert result["matter_risk"] == "High"


def test_resending_same_matter_risk_with_no_reason_does_not_error(monkeypatch):
    """The matter panel's risk-select now bundles matter_risk +
    matter_risk_reason into one PATCH (see updateMatterRisk() in
    index.html), but re-sending the SAME already-stored value (no real
    transition) must never require a reason, even with none on file."""
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id, matter_risk="High", matter_risk_reason=None)])
    monkeypatch.setattr(m, "_db_pool", pool)

    result = asyncio.run(update_matter(str(matter_id), MatterUpdate(matter_risk="High"), _fake_request()))
    assert result["matter_risk"] == "High"


def test_matter_risk_set_log_includes_reason_when_provided(monkeypatch):
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id)])
    monkeypatch.setattr(m, "_db_pool", pool)

    asyncio.run(update_matter(
        str(matter_id),
        MatterUpdate(matter_risk="Medium", matter_risk_reason="Foreign beneficial owner."),
        _fake_request(),
    ))

    logs = [l for l in pool.conn.audit_logs if l["action"] == "MATTER_RISK_SET"]
    assert len(logs) == 1
    assert logs[0]["details"]["reason"] == "Foreign beneficial owner."


def test_matter_risk_set_log_has_no_reason_key_when_none_on_file(monkeypatch):
    """A matter with no stored reason at all (e.g. a grandfathered
    pre-fix record) transitioning down to NotAssessed -- 'reason' should
    be entirely absent from the log details, not an empty string."""
    import backend.main as m
    matter_id = uuid.uuid4()
    pool = FakePool(matters=[_matter_row(matter_id, matter_risk="High", matter_risk_reason=None)])
    monkeypatch.setattr(m, "_db_pool", pool)

    asyncio.run(update_matter(str(matter_id), MatterUpdate(matter_risk="NotAssessed"), _fake_request()))

    logs = [l for l in pool.conn.audit_logs if l["action"] == "MATTER_RISK_SET"]
    assert len(logs) == 1
    assert "reason" not in logs[0]["details"]
