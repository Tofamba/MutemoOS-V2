"""
Unit tests for the Compliance Exception Resolution Workflow (backend/main.py,
2026-09-07):
  GET   /api/clients/{client_id}/exceptions
  PATCH /api/clients/{client_id}/exceptions/{exception_id}
  _compute_compliance_status() -- missing/missing_codes now built together
  _is_exception_issue_resolved() -- the one canonical resolution check
  _sync_compliance_exceptions_for_client() -- the one create/auto-reopen path

Core guarantee this whole feature exists to enforce: Exception -> issue_code
-> look up the REAL, current compliance state for that code -> only if it's
actually satisfied right now may the exception move to Resolved. Causality
only ever flows real-compliance-state -> exception; there is no code path
anywhere that lets an exception's own status change client_compliance/
beneficial_owners/cdd_reviews back.

Same FakeConnection/FakePool/_as_current_user/_fake_request convention as
tests/test_client_compliance.py and tests/test_cdd_review.py.
"""

import asyncio
import json
import re
import uuid
from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException

from backend.main import (
    FIRM_ID,
    EXCEPTION_ISSUE_CODES,
    EXCEPTION_STATUSES,
    ComplianceExceptionUpdate,
    list_compliance_exceptions,
    update_compliance_exception,
    _compute_compliance_status,
)


class FakeConnection:
    def __init__(self, clients, compliance=None, owners=None, cdd_reviews=None, users=None,
                 compliance_exceptions=None):
        self.clients = clients
        self.compliance = compliance or {}  # client_id -> dict
        self.owners = owners or []
        self.cdd_reviews = cdd_reviews or []
        self.users = users or []
        self.compliance_exceptions = compliance_exceptions or []
        self.audit_logs = []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT * FROM clients WHERE id=$1 AND firm_id=$2"):
            cid, firm_id = args
            for c in self.clients:
                if c["id"] == cid and c["firm_id"] == firm_id:
                    return dict(c)
            return None

        if q.startswith("SELECT * FROM client_compliance WHERE client_id=$1 AND firm_id=$2"):
            cid, firm_id = args
            return dict(self.compliance[cid]) if cid in self.compliance else None

        if q.startswith("SELECT review_date, status, changes_identified FROM cdd_reviews"):
            firm_id, cid = args
            matching = [r for r in self.cdd_reviews if r["firm_id"] == firm_id and r["client_id"] == cid]
            if not matching:
                return None
            latest = max(matching, key=lambda r: (r["review_date"], r.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)))
            return {k: latest[k] for k in ("review_date", "status", "changes_identified")}

        if q.startswith("SELECT * FROM compliance_exceptions WHERE id=$1 AND client_id=$2 AND firm_id=$3"):
            eid, cid, firm_id = args
            for e in self.compliance_exceptions:
                if e["id"] == eid and e["client_id"] == cid and e["firm_id"] == firm_id:
                    return dict(e)
            return None

        if q.startswith("SELECT display_name FROM users WHERE id=$1 AND firm_id=$2"):
            uid, firm_id = args
            for u in self.users:
                if u["id"] == uid and u["firm_id"] == firm_id:
                    return {"display_name": u["display_name"]}
            return None

        if q.startswith("UPDATE compliance_exceptions SET"):
            m_ = re.search(r"SET (.+) WHERE id=\$1", q)
            cols = re.findall(r"(\w+)=\$\d+", m_.group(1))
            eid = args[0]
            values = args[1:1 + len(cols)]
            for e in self.compliance_exceptions:
                if e["id"] == eid:
                    for col, val in zip(cols, values):
                        e[col] = val
                    return dict(e)
            return None

        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT verification_status FROM beneficial_owners WHERE client_id=$1 AND firm_id=$2"):
            cid, firm_id = args
            return [{"verification_status": o["verification_status"]} for o in self.owners if o["client_id"] == cid]

        if q.startswith("SELECT * FROM compliance_exceptions WHERE firm_id=$1 AND client_id=$2"):
            firm_id, cid = args
            rows = [dict(e) for e in self.compliance_exceptions if e["firm_id"] == firm_id and e["client_id"] == cid]
            return sorted(rows, key=lambda r: r["created_at"])

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
        elif q.startswith("INSERT INTO compliance_exceptions"):
            firm_id, cid, issue_code, issue_label, responsible_user_id = args
            self.compliance_exceptions.append({
                "id": uuid.uuid4(), "firm_id": firm_id, "client_id": cid,
                "issue_code": issue_code, "issue_label": issue_label, "status": "Open",
                "responsible_user_id": responsible_user_id, "due_date": None, "notes": None,
                "closed_reason": None, "resolved_at": None, "closed_at": None,
                "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
            })
        elif q.startswith("UPDATE compliance_exceptions SET status='Open'"):
            issue_label, eid = args
            for e in self.compliance_exceptions:
                if e["id"] == eid:
                    e["status"] = "Open"
                    e["issue_label"] = issue_label
                    e["resolved_at"] = None
        elif q.startswith("UPDATE compliance_exceptions SET issue_label=$1"):
            issue_label, eid = args
            for e in self.compliance_exceptions:
                if e["id"] == eid:
                    e["issue_label"] = issue_label
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


def _client_row(client_id=None, **overrides):
    row = {
        "id": client_id or uuid.uuid4(), "firm_id": FIRM_ID, "full_name": "Test Client",
        "client_type": "Individual", "created_by": None,
    }
    row.update(overrides)
    return row


def _compliance(client_id, **kwargs):
    row = {
        "client_id": client_id, "firm_id": FIRM_ID,
        "identity_verification_status": "Verified",
        "client_is_beneficial_owner": None,
        "is_pep": False,
        "senior_management_approved_by": None,
        "risk_rating": "NotAssessed",
        "conflict_check_reviewed": True,
    }
    row.update(kwargs)
    return row


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def _fake_request():
    return None


def _partner(user_id=None):
    return {"id": user_id or uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P. Chademana"}


def _exception_row(client_id, issue_code, **overrides):
    row = {
        "id": uuid.uuid4(), "firm_id": FIRM_ID, "client_id": client_id,
        "issue_code": issue_code, "issue_label": issue_code.replace("_", " ").title(),
        "status": "Open", "responsible_user_id": None, "due_date": None, "notes": None,
        "closed_reason": None, "resolved_at": None, "closed_at": None,
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
    }
    row.update(overrides)
    return row


# ── issue_code <-> missing[] label mapping (requirement: must match production) ──

def test_every_missing_code_maps_to_the_real_production_label():
    """The exact test the workflow's own spec calls for: issue_code must
    correctly map to the same missing[] labels already in production, for
    every existing case -- not a hand-maintained parallel list that could
    drift, but the literal output of _compute_compliance_status() itself."""
    client = _client_row(client_type="Company")
    compliance = _compliance(
        client["id"], identity_verification_status="Unverified",
        client_is_beneficial_owner=None, is_pep=None, conflict_check_reviewed=False,
    )
    status = _compute_compliance_status(client, compliance, [])

    expected = {
        "IDENTITY_NOT_VERIFIED": "Identity not verified",
        "BENEFICIAL_OWNER_NOT_ASSESSED": "Beneficial ownership not assessed",
        "PEP_SCREENING_INCOMPLETE": "PEP screening not completed",
        "CONFLICT_CHECK_REQUIRED": "Conflict check not reviewed",
    }
    label_by_code = dict(zip(status["missing_codes"], status["missing"]))
    for code, label in expected.items():
        assert label_by_code[code] == label


def test_beneficial_owner_not_verified_code_maps_correctly():
    client = _client_row(client_type="Company")
    compliance = _compliance(client["id"], client_is_beneficial_owner="No")
    status = _compute_compliance_status(client, compliance, [])
    assert dict(zip(status["missing_codes"], status["missing"]))["BENEFICIAL_OWNER_NOT_VERIFIED"] \
        == "Beneficial ownership not verified"


def test_pep_approval_and_risk_rating_codes_map_correctly():
    client = _client_row(client_type="Individual")
    compliance = _compliance(client["id"], is_pep=True, risk_rating="NotAssessed")
    status = _compute_compliance_status(client, compliance, [])
    label_by_code = dict(zip(status["missing_codes"], status["missing"]))
    assert label_by_code["PEP_APPROVAL_REQUIRED"] == "Senior management approval required (PEP)"
    assert label_by_code["RISK_RATING_REQUIRED"] == "Risk rating required for PEP client"


def test_client_type_not_recorded_code_maps_correctly():
    client = _client_row(client_type=None)
    status = _compute_compliance_status(client, None, [])
    assert status["missing"] == ["Client type not recorded"]
    assert status["missing_codes"] == ["CLIENT_TYPE_NOT_RECORDED"]


def test_fully_cleared_client_has_no_missing_codes():
    client = _client_row(client_type="Individual")
    compliance = _compliance(client["id"])
    status = _compute_compliance_status(client, compliance, [])
    assert status["compliance_status"] == "Cleared"
    assert status["missing"] == []
    assert status["missing_codes"] == []


def test_every_issue_code_constant_is_covered():
    """EXCEPTION_ISSUE_CODES must cover the full real missing[] set plus
    CDD_REVIEW_OUTSTANDING -- not more, not fewer."""
    assert set(EXCEPTION_ISSUE_CODES) == {
        "CLIENT_TYPE_NOT_RECORDED", "IDENTITY_NOT_VERIFIED",
        "BENEFICIAL_OWNER_NOT_ASSESSED", "BENEFICIAL_OWNER_NOT_VERIFIED",
        "PEP_SCREENING_INCOMPLETE", "PEP_APPROVAL_REQUIRED", "RISK_RATING_REQUIRED",
        "CONFLICT_CHECK_REQUIRED", "CDD_REVIEW_OUTSTANDING",
    }


# ── GET .../exceptions -- sync creates real rows from real compliance state ──

def test_listing_opens_a_real_exception_for_each_genuinely_missing_item(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual", created_by=creator_id)
    compliance = _compliance(client_id, identity_verification_status="Unverified", is_pep=None, conflict_check_reviewed=False)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance={client_id: compliance}))
    _as_current_user(monkeypatch, m, _partner())

    rows = asyncio.run(list_compliance_exceptions(str(client_id), _fake_request()))

    codes = {r["issue_code"] for r in rows}
    assert codes == {"IDENTITY_NOT_VERIFIED", "PEP_SCREENING_INCOMPLETE", "CONFLICT_CHECK_REQUIRED"}
    assert all(r["status"] == "Open" for r in rows)
    # existing responsible-person default: the client's own created_by
    assert all(r["responsible_user_id"] == str(creator_id) for r in rows)


def test_listing_a_fully_cleared_client_opens_nothing(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    compliance = _compliance(client_id)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance={client_id: compliance}))
    _as_current_user(monkeypatch, m, _partner())

    rows = asyncio.run(list_compliance_exceptions(str(client_id), _fake_request()))
    assert rows == []


def test_listing_twice_does_not_duplicate_the_same_open_exception(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    compliance = _compliance(client_id, identity_verification_status="Unverified")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance={client_id: compliance}))
    _as_current_user(monkeypatch, m, _partner())

    first = asyncio.run(list_compliance_exceptions(str(client_id), _fake_request()))
    second = asyncio.run(list_compliance_exceptions(str(client_id), _fake_request()))

    assert len(first) == 1
    assert len(second) == 1
    assert first[0]["id"] == second[0]["id"]


def test_missing_client_returns_404(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[]))
    _as_current_user(monkeypatch, m, _partner())
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(list_compliance_exceptions(str(uuid.uuid4()), _fake_request()))
    assert exc_info.value.status_code == 404


# ── Resolution gate -- the core guarantee ───────────────────────────────────

def test_cannot_mark_resolved_while_condition_is_unsatisfied(monkeypatch):
    """The exact scenario the workflow exists to prevent: a lawyer tries to
    mark an exception Resolved while the client is still genuinely
    Unverified. Must be rejected outright, not silently accepted."""
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    compliance = _compliance(client_id, identity_verification_status="Unverified")
    exception = _exception_row(client_id, "IDENTITY_NOT_VERIFIED", issue_label="Identity not verified")
    monkeypatch.setattr(m, "_db_pool", FakePool(
        clients=[client], compliance={client_id: compliance}, compliance_exceptions=[exception]
    ))
    _as_current_user(monkeypatch, m, _partner())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_compliance_exception(
            str(client_id), str(exception["id"]), ComplianceExceptionUpdate(status="Resolved"), _fake_request()
        ))

    assert exc_info.value.status_code == 409
    assert exception["status"] == "Open"  # unchanged -- rejected, not silently applied


def test_can_mark_resolved_once_condition_is_genuinely_satisfied(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    compliance = _compliance(client_id, identity_verification_status="Verified")  # now genuinely satisfied
    exception = _exception_row(client_id, "IDENTITY_NOT_VERIFIED", issue_label="Identity not verified")
    monkeypatch.setattr(m, "_db_pool", FakePool(
        clients=[client], compliance={client_id: compliance}, compliance_exceptions=[exception]
    ))
    user = _partner()
    _as_current_user(monkeypatch, m, user)

    result = asyncio.run(update_compliance_exception(
        str(client_id), str(exception["id"]), ComplianceExceptionUpdate(status="Resolved"), _fake_request()
    ))

    assert result["status"] == "Resolved"
    assert result["resolved_at"] is not None


def test_resolving_never_writes_back_into_client_compliance(monkeypatch):
    """Causality only flows one direction: marking Resolved must not
    change client_compliance itself in any way."""
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    compliance = _compliance(client_id, identity_verification_status="Verified", risk_rating="NotAssessed")
    exception = _exception_row(client_id, "IDENTITY_NOT_VERIFIED", issue_label="Identity not verified")
    monkeypatch.setattr(m, "_db_pool", FakePool(
        clients=[client], compliance={client_id: compliance}, compliance_exceptions=[exception]
    ))
    _as_current_user(monkeypatch, m, _partner())

    asyncio.run(update_compliance_exception(
        str(client_id), str(exception["id"]), ComplianceExceptionUpdate(status="Resolved"), _fake_request()
    ))

    assert compliance["identity_verification_status"] == "Verified"  # untouched, same object
    assert compliance["risk_rating"] == "NotAssessed"  # untouched


def test_resolved_logs_compliance_exception_resolved_event(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    compliance = _compliance(client_id, identity_verification_status="Verified")
    exception = _exception_row(client_id, "IDENTITY_NOT_VERIFIED", issue_label="Identity not verified")
    pool = FakePool(clients=[client], compliance={client_id: compliance}, compliance_exceptions=[exception])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner())

    asyncio.run(update_compliance_exception(
        str(client_id), str(exception["id"]), ComplianceExceptionUpdate(status="Resolved"), _fake_request()
    ))

    logs = [l for l in pool.conn.audit_logs if l["action"] == "COMPLIANCE_EXCEPTION_RESOLVED"]
    assert len(logs) == 1


# ── Closed -- No Further Action: distinct outcome, requires a reason ───────

def test_closed_no_further_action_without_a_reason_is_rejected(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    compliance = _compliance(client_id, identity_verification_status="Unverified")
    exception = _exception_row(client_id, "IDENTITY_NOT_VERIFIED")
    monkeypatch.setattr(m, "_db_pool", FakePool(
        clients=[client], compliance={client_id: compliance}, compliance_exceptions=[exception]
    ))
    _as_current_user(monkeypatch, m, _partner())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_compliance_exception(
            str(client_id), str(exception["id"]),
            ComplianceExceptionUpdate(status="ClosedNoFurtherAction"), _fake_request()
        ))
    assert exc_info.value.status_code == 422
    assert exception["status"] == "Open"


def test_closed_no_further_action_with_blank_reason_is_rejected(monkeypatch):
    """Whitespace-only counts as blank -- same convention as every other
    required-text field in this codebase (e.g. CDD Review's changes_
    identified)."""
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    compliance = _compliance(client_id, identity_verification_status="Unverified")
    exception = _exception_row(client_id, "IDENTITY_NOT_VERIFIED")
    monkeypatch.setattr(m, "_db_pool", FakePool(
        clients=[client], compliance={client_id: compliance}, compliance_exceptions=[exception]
    ))
    _as_current_user(monkeypatch, m, _partner())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_compliance_exception(
            str(client_id), str(exception["id"]),
            ComplianceExceptionUpdate(status="ClosedNoFurtherAction", closed_reason="   "), _fake_request()
        ))
    assert exc_info.value.status_code == 422


def test_closed_no_further_action_with_a_reason_succeeds_even_while_unsatisfied(monkeypatch):
    """The key distinction from Resolved: Closed-NFA does NOT require the
    underlying condition to be satisfied -- it's the firm documenting a
    decision not to pursue it further, not a claim it's been fixed."""
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    compliance = _compliance(client_id, identity_verification_status="Unverified")  # still unsatisfied
    exception = _exception_row(client_id, "IDENTITY_NOT_VERIFIED")
    pool = FakePool(clients=[client], compliance={client_id: compliance}, compliance_exceptions=[exception])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(update_compliance_exception(
        str(client_id), str(exception["id"]),
        ComplianceExceptionUpdate(status="ClosedNoFurtherAction", closed_reason="Client relationship ending; no further CDD warranted."),
        _fake_request()
    ))

    assert result["status"] == "ClosedNoFurtherAction"
    assert result["closed_reason"] == "Client relationship ending; no further CDD warranted."
    assert result["closed_at"] is not None
    logs = [l for l in pool.conn.audit_logs if l["action"] == "COMPLIANCE_EXCEPTION_CLOSED_NO_FURTHER_ACTION"]
    assert len(logs) == 1
    assert logs[0]["details"]["reason"] == "Client relationship ending; no further CDD warranted."


def test_closed_no_further_action_and_resolved_log_distinct_event_types(monkeypatch):
    """Preserve as genuinely distinct, non-interchangeable outcomes -- not
    the same event under two names."""
    import backend.main as m
    c1, c2 = uuid.uuid4(), uuid.uuid4()
    client1 = _client_row(c1, client_type="Individual")
    client2 = _client_row(c2, client_type="Individual")
    compliance1 = _compliance(c1, identity_verification_status="Verified")  # satisfied -> can Resolve
    compliance2 = _compliance(c2, identity_verification_status="Unverified")  # not satisfied
    e1 = _exception_row(c1, "IDENTITY_NOT_VERIFIED")
    e2 = _exception_row(c2, "IDENTITY_NOT_VERIFIED")
    pool = FakePool(
        clients=[client1, client2], compliance={c1: compliance1, c2: compliance2},
        compliance_exceptions=[e1, e2],
    )
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner())

    asyncio.run(update_compliance_exception(str(c1), str(e1["id"]), ComplianceExceptionUpdate(status="Resolved"), _fake_request()))
    asyncio.run(update_compliance_exception(
        str(c2), str(e2["id"]), ComplianceExceptionUpdate(status="ClosedNoFurtherAction", closed_reason="Decided not to pursue."),
        _fake_request()
    ))

    actions = {l["action"] for l in pool.conn.audit_logs}
    assert "COMPLIANCE_EXCEPTION_RESOLVED" in actions
    assert "COMPLIANCE_EXCEPTION_CLOSED_NO_FURTHER_ACTION" in actions
    assert e1["status"] == "Resolved"
    assert e2["status"] == "ClosedNoFurtherAction"


# ── Auto-reopening ───────────────────────────────────────────────────────────

def test_resolved_exception_auto_reopens_when_condition_becomes_unsatisfied_again(monkeypatch):
    """The exact scenario requirement #4 describes: a Resolved exception's
    underlying condition breaks again -> the system reopens it itself,
    with an explicit event and the fixed reason string -- never a silent
    status flip."""
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    exception = _exception_row(
        client_id, "IDENTITY_NOT_VERIFIED", issue_label="Identity not verified",
        status="Resolved", resolved_at=datetime.now(timezone.utc),
    )
    # Real state has since regressed: identity is Unverified again.
    compliance = _compliance(client_id, identity_verification_status="Unverified")
    pool = FakePool(clients=[client], compliance={client_id: compliance}, compliance_exceptions=[exception])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner())

    rows = asyncio.run(list_compliance_exceptions(str(client_id), _fake_request()))

    assert len(rows) == 1
    assert rows[0]["status"] == "Open"
    assert rows[0]["resolved_at"] is None
    reopen_logs = [l for l in pool.conn.audit_logs if l["action"] == "COMPLIANCE_EXCEPTION_REOPENED"]
    assert len(reopen_logs) == 1
    assert reopen_logs[0]["details"]["reason"] == "Underlying compliance condition is no longer satisfied"
    assert reopen_logs[0]["details"]["issue_code"] == "IDENTITY_NOT_VERIFIED"


def test_resolved_exception_stays_resolved_while_condition_remains_satisfied(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    exception = _exception_row(
        client_id, "IDENTITY_NOT_VERIFIED", status="Resolved", resolved_at=datetime.now(timezone.utc)
    )
    compliance = _compliance(client_id, identity_verification_status="Verified")  # still satisfied
    pool = FakePool(clients=[client], compliance={client_id: compliance}, compliance_exceptions=[exception])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner())

    rows = asyncio.run(list_compliance_exceptions(str(client_id), _fake_request()))

    assert len(rows) == 1
    assert rows[0]["status"] == "Resolved"
    assert not any(l["action"] == "COMPLIANCE_EXCEPTION_REOPENED" for l in pool.conn.audit_logs)


def test_closed_no_further_action_never_auto_reopens(monkeypatch):
    """Distinct from Resolved -- a ClosedNoFurtherAction row is a
    deliberate firm decision the system doesn't re-litigate on its own,
    even though the condition is (and may have always been) unsatisfied."""
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    exception = _exception_row(
        client_id, "IDENTITY_NOT_VERIFIED", status="ClosedNoFurtherAction",
        closed_reason="Relationship ending.", closed_at=datetime.now(timezone.utc),
    )
    compliance = _compliance(client_id, identity_verification_status="Unverified")
    pool = FakePool(clients=[client], compliance={client_id: compliance}, compliance_exceptions=[exception])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner())

    rows = asyncio.run(list_compliance_exceptions(str(client_id), _fake_request()))

    assert len(rows) == 1
    assert rows[0]["status"] == "ClosedNoFurtherAction"  # untouched
    assert not any(l["action"] in ("COMPLIANCE_EXCEPTION_REOPENED", "COMPLIANCE_EXCEPTION_OPENED")
                   for l in pool.conn.audit_logs)


def test_cdd_review_outstanding_resolves_and_reopens_correctly(monkeypatch):
    """CDD_REVIEW_OUTSTANDING reuses the AML Exceptions report's own
    existing signal (the client's most recent cdd_reviews row having
    status='Outstanding'), not a new invented policy."""
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    compliance = _compliance(client_id)  # otherwise fully cleared
    outstanding_review = {
        "firm_id": FIRM_ID, "client_id": client_id, "review_date": date(2026, 9, 1),
        "status": "Outstanding", "changes_identified": "Client relocated abroad",
        "created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
    }
    pool = FakePool(clients=[client], compliance={client_id: compliance}, cdd_reviews=[outstanding_review])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner())

    rows = asyncio.run(list_compliance_exceptions(str(client_id), _fake_request()))
    assert len(rows) == 1
    assert rows[0]["issue_code"] == "CDD_REVIEW_OUTSTANDING"
    assert "Client relocated abroad" in rows[0]["issue_label"]

    # Cannot resolve while still Outstanding.
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_compliance_exception(
            str(client_id), rows[0]["id"], ComplianceExceptionUpdate(status="Resolved"), _fake_request()
        ))
    assert exc_info.value.status_code == 409

    # A later Complete review resolves the real signal -- now Resolved is allowed.
    pool.conn.cdd_reviews.append({
        "firm_id": FIRM_ID, "client_id": client_id, "review_date": date(2026, 9, 5),
        "status": "Complete", "changes_identified": None,
        "created_at": datetime(2026, 9, 5, tzinfo=timezone.utc),
    })
    result = asyncio.run(update_compliance_exception(
        str(client_id), rows[0]["id"], ComplianceExceptionUpdate(status="Resolved"), _fake_request()
    ))
    assert result["status"] == "Resolved"

    # Reality regresses again (a new Outstanding review) -> auto-reopens.
    pool.conn.cdd_reviews.append({
        "firm_id": FIRM_ID, "client_id": client_id, "review_date": date(2026, 9, 10),
        "status": "Outstanding", "changes_identified": "New PEP link discovered",
        "created_at": datetime(2026, 9, 10, tzinfo=timezone.utc),
    })
    reopened_rows = asyncio.run(list_compliance_exceptions(str(client_id), _fake_request()))
    assert reopened_rows[0]["status"] == "Open"
    assert "New PEP link discovered" in reopened_rows[0]["issue_label"]
    assert any(l["action"] == "COMPLIANCE_EXCEPTION_REOPENED" for l in pool.conn.audit_logs)


# ── Reassignment ─────────────────────────────────────────────────────────────

def test_reassignment_changes_responsible_person_and_logs_it(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    old_user_id, new_user_id = uuid.uuid4(), uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    compliance = _compliance(client_id, identity_verification_status="Unverified")
    exception = _exception_row(client_id, "IDENTITY_NOT_VERIFIED", responsible_user_id=old_user_id)
    old_user = {"id": old_user_id, "firm_id": FIRM_ID, "display_name": "Old Owner"}
    new_user = {"id": new_user_id, "firm_id": FIRM_ID, "display_name": "New Owner"}
    pool = FakePool(
        clients=[client], compliance={client_id: compliance}, compliance_exceptions=[exception],
        users=[old_user, new_user],
    )
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(update_compliance_exception(
        str(client_id), str(exception["id"]),
        ComplianceExceptionUpdate(responsible_user_id=str(new_user_id)), _fake_request()
    ))

    assert result["responsible_user_id"] == str(new_user_id)
    logs = [l for l in pool.conn.audit_logs if l["action"] == "COMPLIANCE_EXCEPTION_REASSIGNED"]
    assert len(logs) == 1
    assert logs[0]["details"]["old"] == "Old Owner"
    assert logs[0]["details"]["new"] == "New Owner"


def test_reassignment_to_unknown_user_is_rejected(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    compliance = _compliance(client_id, identity_verification_status="Unverified")
    exception = _exception_row(client_id, "IDENTITY_NOT_VERIFIED")
    monkeypatch.setattr(m, "_db_pool", FakePool(
        clients=[client], compliance={client_id: compliance}, compliance_exceptions=[exception]
    ))
    _as_current_user(monkeypatch, m, _partner())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_compliance_exception(
            str(client_id), str(exception["id"]),
            ComplianceExceptionUpdate(responsible_user_id=str(uuid.uuid4())), _fake_request()
        ))
    assert exc_info.value.status_code == 422


# ── Plain status transitions / notes / due date ─────────────────────────────

def test_moving_through_working_states_logs_status_changed(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    compliance = _compliance(client_id, identity_verification_status="Unverified")
    exception = _exception_row(client_id, "IDENTITY_NOT_VERIFIED")
    pool = FakePool(clients=[client], compliance={client_id: compliance}, compliance_exceptions=[exception])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(update_compliance_exception(
        str(client_id), str(exception["id"]), ComplianceExceptionUpdate(status="InProgress"), _fake_request()
    ))

    assert result["status"] == "InProgress"
    logs = [l for l in pool.conn.audit_logs if l["action"] == "COMPLIANCE_EXCEPTION_STATUS_CHANGED"]
    assert len(logs) == 1


def test_due_date_and_notes_can_be_set_without_a_status_change(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    compliance = _compliance(client_id, identity_verification_status="Unverified")
    exception = _exception_row(client_id, "IDENTITY_NOT_VERIFIED")
    monkeypatch.setattr(m, "_db_pool", FakePool(
        clients=[client], compliance={client_id: compliance}, compliance_exceptions=[exception]
    ))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(update_compliance_exception(
        str(client_id), str(exception["id"]),
        ComplianceExceptionUpdate(due_date="2026-10-01", notes="Chasing client for ID documents."),
        _fake_request()
    ))

    assert result["due_date"] == "2026-10-01"
    assert result["notes"] == "Chasing client for ID documents."
    assert result["status"] == "Open"  # unchanged


def test_invalid_status_value_rejected(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    compliance = _compliance(client_id, identity_verification_status="Unverified")
    exception = _exception_row(client_id, "IDENTITY_NOT_VERIFIED")
    monkeypatch.setattr(m, "_db_pool", FakePool(
        clients=[client], compliance={client_id: compliance}, compliance_exceptions=[exception]
    ))
    _as_current_user(monkeypatch, m, _partner())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_compliance_exception(
            str(client_id), str(exception["id"]), ComplianceExceptionUpdate(status="Waived"), _fake_request()
        ))
    assert exc_info.value.status_code == 422


def test_missing_exception_returns_404(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, _partner())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(update_compliance_exception(
            str(client_id), str(uuid.uuid4()), ComplianceExceptionUpdate(status="InProgress"), _fake_request()
        ))
    assert exc_info.value.status_code == 404
