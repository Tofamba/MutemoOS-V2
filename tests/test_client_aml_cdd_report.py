"""
Unit tests for the Individual Client AML/CDD Report
(backend/main.py, 2026-09-03, partner design review, built from a
sample report PDF -- "Sample 2"):
  GET /api/clients/{client_id}/aml-cdd-report

Part A (composition, not new logic): every section reuses data/
functions that already exist elsewhere --
_compute_compliance_status()/_compute_bo_status() (already thoroughly
tested in tests/test_client_compliance.py and tests/
test_client_compliance_status_report.py), _row_to_beneficial_owner()/
_row_to_authorized_representative()/_row_to_doc()/_row_to_matter(). These
tests check the composition (right sections, right fields, right
degrade-gracefully behavior for missing/dangling data), not those
functions' own internal correctness.

Part B (matter-level AML, genuinely new): each matter's own aml_scope/
aml_scope_reason/matter_risk, listed per-matter -- see
tests/test_matter_aml_scope.py for the PATCH-level validation/
persistence tests this file doesn't repeat. The multi-matter,
differently-scoped scenario here mirrors the sample report's own Blue
Ridge Traders example (property acquisition In Scope/High risk vs. a
divorce Out of Scope/Low risk, same client).

Part C (compliance history) is a deliberate placeholder -- not built
this pass; these tests pin the honest placeholder shape.

Gated at client:read (not the stricter reports:client_compliance_status
the firm-wide Register/Exceptions reports use) -- every real role has
client:read, so there's no 403 case to test the way the firm-wide
reports have one.

Called directly as plain async functions, same convention as
tests/test_client_compliance.py, whose FakeConnection/_client_row shape
this file mirrors closely (adding matters/documents/users tables that
function's FakeConnection doesn't need).
"""

import asyncio
import io
import json
import uuid
from datetime import date, datetime, timezone

import pdfplumber
import pytest
from fastapi import HTTPException

from backend.main import (
    FIRM_ID,
    client_aml_cdd_report,
    client_aml_cdd_report_export,
    client_aml_cdd_report_export_pdf,
)


class FakeConnection:
    def __init__(self, clients, compliance=None, owners=None, reps=None, matters=None, documents=None, users=None,
                 audit_logs=None, cdd_reviews=None):
        self.clients = clients
        self.compliance = compliance if compliance is not None else {}  # client_id -> dict
        self.owners = owners if owners is not None else []
        self.reps = reps if reps is not None else []
        self.matters = matters if matters is not None else []
        self.documents = documents if documents is not None else []
        self.users = users if users is not None else []
        self.audit_logs = audit_logs if audit_logs is not None else []
        self.cdd_reviews = cdd_reviews if cdd_reviews is not None else []
        # Compliance Exception Resolution Workflow (2026-09-07, Phase 2) --
        # the report now syncs this client's own exceptions (Part D /
        # "9. Exceptions / Follow-up", renumbered 2026-09-09 when
        # Compliance History was removed) via _sync_compliance_exceptions_
        # for_client(), which issues the real single-client-scoped
        # queries handled below (fetchrow/fetch) and writes via execute().
        self.compliance_exceptions = []

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
            latest = max(
                matching,
                key=lambda r: (r["review_date"], r.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)),
            )
            return {k: latest[k] for k in ("review_date", "status", "changes_identified")}

        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT * FROM beneficial_owners WHERE client_id=$1 AND firm_id=$2"):
            cid, firm_id = args
            return [dict(o) for o in self.owners if o["client_id"] == cid and o["firm_id"] == firm_id]

        if q.startswith("SELECT * FROM authorized_representatives WHERE client_id=$1 AND firm_id=$2"):
            cid, firm_id = args
            return [dict(r) for r in self.reps if r["client_id"] == cid and r["firm_id"] == firm_id]

        if q.startswith("SELECT * FROM matters WHERE client_id=$1 AND firm_id=$2 AND NOT is_sentinel"):
            cid, firm_id = args
            return [dict(mt) for mt in self.matters
                    if mt["client_id"] == cid and mt["firm_id"] == firm_id and not mt.get("is_sentinel", False)]

        if q.startswith("SELECT id, display_name FROM users WHERE id = ANY($1) AND firm_id=$2"):
            user_ids, firm_id = args
            return [dict(u) for u in self.users if u["id"] in user_ids and u["firm_id"] == firm_id]

        if q.startswith("SELECT * FROM documents WHERE id = ANY($1) AND firm_id=$2"):
            doc_ids, firm_id = args
            return [dict(d) for d in self.documents if d["id"] in doc_ids and d["firm_id"] == firm_id]

        if q.startswith("SELECT * FROM documents WHERE matter_id = ANY($1) AND firm_id=$2"):
            matter_ids, firm_id = args
            return [dict(d) for d in self.documents if d.get("matter_id") in matter_ids and d["firm_id"] == firm_id]

        if q.startswith("SELECT created_at, action, actor_name, details FROM audit_logs"):
            firm_id, cid, matter_ids = args
            rows = [
                a for a in self.audit_logs
                if a["firm_id"] == firm_id
                and ((a["target_type"] == "CLIENT" and a["target_id"] == cid)
                     or (a["target_type"] == "MATTER" and a["target_id"] in matter_ids))
            ]
            return sorted((dict(r) for r in rows), key=lambda r: r["created_at"])

        if q.startswith("SELECT cr.review_date, cr.status, cr.risk_rating, cr.changes_identified"):
            firm_id, cid = args
            users_by_id = {u["id"]: u["display_name"] for u in self.users}
            rows = [
                {**r, "reviewer_name": users_by_id.get(r.get("reviewed_by"))}
                for r in self.cdd_reviews if r["firm_id"] == firm_id and r["client_id"] == cid
            ]
            return sorted(rows, key=lambda r: r["review_date"])

        if q.startswith("SELECT verification_status FROM beneficial_owners WHERE client_id=$1 AND firm_id=$2"):
            cid, firm_id = args
            return [
                {"verification_status": o["verification_status"]}
                for o in self.owners if o["client_id"] == cid and o["firm_id"] == firm_id
            ]

        if q.startswith("SELECT * FROM compliance_exceptions WHERE firm_id=$1 AND client_id=$2"):
            firm_id, cid = args
            return [dict(e) for e in self.compliance_exceptions if e["firm_id"] == firm_id and e["client_id"] == cid]

        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")

    async def execute(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("INSERT INTO audit_logs"):
            (firm_id, user_id, actor_name, actor_role, action, target_type, target_id, details) = args
            self.audit_logs.append({
                "firm_id": firm_id, "user_id": user_id, "actor_name": actor_name, "actor_role": actor_role,
                "action": action, "target_type": target_type, "target_id": target_id,
                "details": json.loads(details) if details else {},
                "created_at": datetime.now(timezone.utc),
            })
            return "INSERT 0 1"

        if q.startswith("INSERT INTO compliance_exceptions"):
            firm_id, cid, issue_code, issue_label, responsible_user_id = args
            self.compliance_exceptions.append({
                "id": uuid.uuid4(), "firm_id": firm_id, "client_id": cid,
                "issue_code": issue_code, "issue_label": issue_label, "status": "Open",
                "responsible_user_id": responsible_user_id, "due_date": None, "notes": None,
                "closed_reason": None, "resolved_at": None, "closed_at": None,
                "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
            })
            return "INSERT 0 1"

        if q.startswith("UPDATE compliance_exceptions SET status='Open'"):
            issue_label, eid = args
            for e in self.compliance_exceptions:
                if e["id"] == eid:
                    e["status"] = "Open"
                    e["issue_label"] = issue_label
                    e["resolved_at"] = None
            return "UPDATE 1"

        if q.startswith("UPDATE compliance_exceptions SET issue_label=$1"):
            issue_label, eid = args
            for e in self.compliance_exceptions:
                if e["id"] == eid:
                    e["issue_label"] = issue_label
            return "UPDATE 1"

        raise NotImplementedError(f"FakeConnection.execute: unhandled query: {q}")

    async def fetchval(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT MAX(review_date) FROM cdd_reviews WHERE firm_id=$1 AND client_id=$2"):
            firm_id, cid = args
            dates = [r["review_date"] for r in self.cdd_reviews if r["firm_id"] == firm_id and r["client_id"] == cid]
            return max(dates) if dates else None
        raise NotImplementedError(f"FakeConnection.fetchval: unhandled query: {q}")


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


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def _fake_request():
    return None


def _client_row(client_id, *, client_type="Company", **overrides):
    row = {
        "id": client_id, "firm_id": FIRM_ID, "full_name": "Blue Ridge Traders (Pvt) Ltd",
        "client_type": client_type, "client_number": "BN-001",
        "email": None, "phone": None, "physical_address": None,
        "id_or_registration_number": None, "contact_person": None, "notes": None,
        "created_by": None, "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
        "date_of_birth": None, "place_of_birth": None, "national_id_number": None,
        "passport_number": None, "id_expiry_date": None, "residential_address": None,
        "occupation": None, "employer_or_business": None,
        "registered_name": "Blue Ridge Traders (Pvt) Ltd", "trading_name": "Blue Ridge Traders",
        "registration_number": "123456/2024", "date_incorporated": date(2024, 3, 12),
        "registered_office_address": "Harare, Zimbabwe", "principal_business_address": "Wholesale trading",
        "proof_of_incorporation_document_id": None, "governing_document_id": None,
        "trustees": [], "settlors": [], "beneficiaries": [],
    }
    row.update(overrides)
    return row


def _compliance(client_id, **kwargs):
    row = {
        "client_id": client_id, "firm_id": FIRM_ID,
        "identity_verification_status": "Unverified",
        "client_is_beneficial_owner": None,
        "is_pep": None, "pep_basis": None, "pep_position": None, "pep_country": None,
        "senior_management_approval_required": False,
        "senior_management_approved_by": None, "senior_management_approved_date": None,
        "source_of_wealth": None, "source_of_funds": None,
        "enhanced_monitoring_required": False,
        "risk_rating": "NotAssessed", "aml_scope": "NotAssessed",
        "relationship_ended_date": None, "retained_until": None,
        "conflict_check_reviewed": False, "conflict_check_reviewed_by": None,
        "conflict_check_reviewed_date": None,
        "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
        "id": uuid.uuid4(),
    }
    row.update(kwargs)
    return row


def _cleared_compliance(client_id, **kwargs):
    """A fully "Cleared" compliance row (2026-09-07, Phase 2) -- lets a
    test pin down exactly one real gap (via an override kwarg) rather
    than the default _client_row's Company client_type + no compliance
    row at all, which would leave every one of _compute_compliance_
    status()'s missing[] items open and _sync_compliance_exceptions_
    for_client() (now called by every report fetch) creating a real
    "Compliance exception opened" event for each -- noise unrelated to
    what the Exceptions/Follow-up section tests below are each checking.
    See tests/test_compliance_exceptions.py for that workflow's own
    dedicated tests."""
    defaults = dict(
        identity_verification_status="Verified", client_is_beneficial_owner="Yes",
        is_pep=False, conflict_check_reviewed=True,
    )
    defaults.update(kwargs)
    return _compliance(client_id, **defaults)


def _owner(client_id, **kwargs):
    row = {
        "id": uuid.uuid4(), "client_id": client_id, "firm_id": FIRM_ID,
        "owner_name": "Tendai Moyo", "date_of_birth": None, "nationality": "Zimbabwean",
        "id_or_passport_number": None, "residential_address": None,
        "ownership_or_control_basis": "Shareholding", "ownership_percentage": None,
        "verification_status": "Unverified", "verified_date": None, "verified_by": None,
        "created_at": datetime.now(timezone.utc),
    }
    row.update(kwargs)
    return row


def _rep(client_id, **kwargs):
    row = {
        "id": uuid.uuid4(), "client_id": client_id, "firm_id": FIRM_ID,
        "full_name": "Kelvin Moyo", "position_or_relationship": "Director",
        "id_or_passport_number": None, "contact_details": None,
        "authority_basis": "BoardResolution", "authority_document_id": None,
        "verification_status": "Unverified", "verified_date": None,
        "created_at": datetime.now(timezone.utc),
    }
    row.update(kwargs)
    return row


def _matter(client_id, **kwargs):
    row = {
        "id": uuid.uuid4(), "firm_id": FIRM_ID, "client_id": client_id,
        "name": "Acquisition of commercial property", "number": "BN-001-01",
        "internal_ref": None, "external_ref": None, "client_name": None,
        "case_parties": None, "matter_type": None, "practice_area": None, "status": "Active",
        "custom_status": None, "document_count": 0, "last_activity": None,
        "created_at": datetime.now(timezone.utc), "created_by": None,
        "next_deadline": None, "next_deadline_note": None,
        "amount_billed": None, "amount_received": None,
        "aml_scope": "NotAssessed", "aml_scope_reason": None, "matter_risk": "NotAssessed",
        "is_sentinel": False,
    }
    row.update(kwargs)
    return row


def _document(*, matter_id=None, **kwargs):
    row = {
        "id": uuid.uuid4(), "firm_id": FIRM_ID, "matter_id": matter_id,
        "filename": "Certificate of Incorporation.pdf", "document_type": "Corporate identity",
        "matter_type": None, "parties": None, "doc_date": None, "court": None,
        "word_count": 0, "page_count": 1, "chunk_count": 0, "ocr_used": False,
        "ocr_confidence": None, "needs_review": False, "status": "complete",
        "document_status": "Final", "error_message": None,
        "uploaded_at": datetime.now(timezone.utc), "uploaded_by": None,
    }
    row.update(kwargs)
    return row


def _user(display_name, user_id=None):
    return {"id": user_id or uuid.uuid4(), "firm_id": FIRM_ID, "display_name": display_name}


def _partner():
    return {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}


# ── permission / basic wiring ──────────────────────────────────────────────

def test_every_real_role_can_read_it(monkeypatch):
    """client:read, not the stricter reports:client_compliance_status --
    every role has it."""
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    for role in ("admin", "partner", "associate", "secretary"):
        monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
        _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": role, "display_name": "X"})
        result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))
        assert result["client"]["full_name"] == "Blue Ridge Traders (Pvt) Ltd"


def test_missing_client_returns_404(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[]))
    _as_current_user(monkeypatch, m, _partner())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(client_aml_cdd_report(str(uuid.uuid4()), _fake_request()))
    assert exc_info.value.status_code == 404


# ── Overall Compliance Position -- reuses _compute_compliance_status ───────

def test_overall_position_matches_compute_compliance_status(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert result["overall"]["compliance_status"] == "Action Required"
    assert "Identity not verified" in result["overall"]["missing"]
    assert result["overall"]["outstanding_count"] == len(result["overall"]["missing"])
    assert result["overall"]["aml_scope"] == "NotAssessed"
    assert result["overall"]["bo_status"] == "N/A"  # Individual


def test_overall_position_reflects_a_cleared_client(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    compliance = _compliance(
        client_id, identity_verification_status="Verified",
        is_pep=False, conflict_check_reviewed=True, risk_rating="Low", aml_scope="OutOfScope",
    )
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance={client_id: compliance}))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert result["overall"]["compliance_status"] == "Cleared"
    assert result["overall"]["missing"] == []
    assert result["overall"]["risk_rating"] == "Low"
    assert result["overall"]["aml_scope"] == "OutOfScope"
    assert result["overall"]["conflict_check_status"] == "Completed"


def test_person_acting_status_none_recorded_with_no_representatives(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert result["overall"]["person_acting_status"] == "None recorded"


def test_person_acting_status_verified_when_a_rep_is_verified(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    rep = _rep(client_id, verification_status="Verified")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], reps=[rep]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert result["overall"]["person_acting_status"] == "Verified"


# ── Client Identification ──────────────────────────────────────────────────

def test_client_identification_fields_present(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    c = result["client"]
    assert c["registered_name"] == "Blue Ridge Traders (Pvt) Ltd"
    assert c["trading_name"] == "Blue Ridge Traders"
    assert c["registration_number"] == "123456/2024"
    assert c["registered_office_address"] == "Harare, Zimbabwe"


# ── Beneficial Ownership ────────────────────────────────────────────────────

def test_beneficial_owners_table_includes_every_owner_with_full_detail(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    owner1 = _owner(client_id, owner_name="Tendai Moyo", ownership_percentage=60, verification_status="Verified")
    owner2 = _owner(client_id, owner_name="Sarah Ncube", ownership_percentage=40, verification_status="Verified")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], owners=[owner1, owner2]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert len(result["beneficial_owners"]) == 2
    names = {o["owner_name"] for o in result["beneficial_owners"]}
    assert names == {"Tendai Moyo", "Sarah Ncube"}
    by_name = {o["owner_name"]: o for o in result["beneficial_owners"]}
    assert by_name["Tendai Moyo"]["ownership_percentage"] == 60.0
    assert by_name["Tendai Moyo"]["ownership_or_control_basis"] == "Shareholding"
    assert by_name["Tendai Moyo"]["verification_status"] == "Verified"


def test_no_beneficial_owners_gives_an_empty_list_not_an_error(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert result["beneficial_owners"] == []


# ── Person Acting for Client ────────────────────────────────────────────────

def test_authorized_representatives_full_detail(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    rep = _rep(client_id, full_name="Kelvin Moyo", authority_basis="BoardResolution", verification_status="Verified")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], reps=[rep]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert len(result["authorized_representatives"]) == 1
    r = result["authorized_representatives"][0]
    assert r["full_name"] == "Kelvin Moyo"
    assert r["authority_basis"] == "BoardResolution"
    assert r["verification_status"] == "Verified"


# ── PEP / Risk ──────────────────────────────────────────────────────────────

def test_pep_risk_section_resolves_approver_name(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    approver_id = uuid.uuid4()
    compliance = _compliance(
        client_id, is_pep=True, senior_management_approved_by=approver_id,
        senior_management_approved_date=date(2026, 9, 1), risk_rating="High",
    )
    approver = _user("Tanaka Chademana", user_id=approver_id)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance={client_id: compliance}, users=[approver]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert result["pep_risk"]["is_pep"] is True
    assert result["pep_risk"]["risk_rating"] == "High"
    assert result["pep_risk"]["senior_management_approved_by_name"] == "Tanaka Chademana"
    assert result["pep_risk"]["senior_management_approved_date"] == "2026-09-01"


def test_pep_risk_section_no_approver_recorded(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert result["pep_risk"]["senior_management_approved_by_name"] is None


# ── Conflict Check ──────────────────────────────────────────────────────────

def test_conflict_check_section_resolves_reviewer_name(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    reviewer_id = uuid.uuid4()
    compliance = _compliance(
        client_id, conflict_check_reviewed=True, conflict_check_reviewed_by=reviewer_id,
        conflict_check_reviewed_date=date(2026, 9, 1),
    )
    reviewer = _user("J. Moyo", user_id=reviewer_id)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance={client_id: compliance}, users=[reviewer]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert result["conflict_check"]["reviewed"] is True
    assert result["conflict_check"]["reviewed_by_name"] == "J. Moyo"
    assert result["conflict_check"]["reviewed_date"] == "2026-09-01"


def test_conflict_check_not_reviewed(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert result["conflict_check"]["reviewed"] is False
    assert result["conflict_check"]["reviewed_by_name"] is None


# ── Matters (Part B) -- the sample's Blue Ridge Traders example ───────────

def test_multiple_matters_show_their_own_differing_aml_scope_and_risk(monkeypatch):
    """Mirrors the sample report exactly: a property acquisition (In
    Scope, High risk) and a divorce (Out of Scope, Low risk) for the
    same client, each with its own AML scope -- not one scope for the
    whole client."""
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    property_matter = _matter(
        client_id, name="Acquisition of commercial property", number="BN-001-01",
        aml_scope="InScope", aml_scope_reason="Transaction involves acquisition of immovable property.",
        matter_risk="High",
    )
    divorce_matter = _matter(
        client_id, name="Divorce proceedings", number="BN-001-02",
        aml_scope="OutOfScope", matter_risk="Low",
    )
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], matters=[property_matter, divorce_matter]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert len(result["matters"]) == 2
    by_number = {mt["number"]: mt for mt in result["matters"]}
    assert by_number["BN-001-01"]["aml_scope"] == "InScope"
    assert by_number["BN-001-01"]["aml_scope_reason"] == "Transaction involves acquisition of immovable property."
    assert by_number["BN-001-01"]["matter_risk"] == "High"
    assert by_number["BN-001-02"]["aml_scope"] == "OutOfScope"
    assert by_number["BN-001-02"]["matter_risk"] == "Low"


def test_sentinel_matter_excluded_from_the_list(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    real_matter = _matter(client_id, is_sentinel=False)
    sentinel_matter = _matter(client_id, is_sentinel=True)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], matters=[real_matter, sentinel_matter]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert len(result["matters"]) == 1
    assert result["matters"][0]["id"] == str(real_matter["id"])


def test_no_matters_gives_an_empty_list(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert result["matters"] == []


# ── Supporting Document Index ──────────────────────────────────────────────

def test_document_index_includes_client_level_docs(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    poi_doc_id = uuid.uuid4()
    gov_doc_id = uuid.uuid4()
    client = _client_row(client_id, proof_of_incorporation_document_id=poi_doc_id, governing_document_id=gov_doc_id)
    poi_doc = _document(id=poi_doc_id, filename="Certificate of Incorporation.pdf")
    gov_doc = _document(id=gov_doc_id, filename="Articles of Association.pdf")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], documents=[poi_doc, gov_doc]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    labels = {d["label"]: d for d in result["documents"]}
    assert "Proof of Incorporation" in labels
    assert labels["Proof of Incorporation"]["filename"] == "Certificate of Incorporation.pdf"
    assert labels["Proof of Incorporation"]["category"] == "Client Identification"
    assert "Governing Document" in labels


def test_document_index_includes_representative_authority_doc(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    client = _client_row(client_id)
    rep = _rep(client_id, full_name="Kelvin Moyo", authority_document_id=doc_id)
    doc = _document(id=doc_id, filename="Board Resolution.pdf")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], reps=[rep], documents=[doc]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    matching = [d for d in result["documents"] if d["category"] == "Authority"]
    assert len(matching) == 1
    assert matching[0]["label"] == "Authority — Kelvin Moyo"
    assert matching[0]["filename"] == "Board Resolution.pdf"


def test_document_index_includes_matter_linked_documents(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    matter = _matter(client_id)
    doc = _document(matter_id=matter["id"], filename="Source of funds documentation.pdf", document_type="Matter")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[_client_row(client_id)], matters=[matter], documents=[doc]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    matching = [d for d in result["documents"] if d["category"] == "Matter"]
    assert len(matching) == 1
    assert matching[0]["filename"] == "Source of funds documentation.pdf"


def test_dangling_document_reference_is_skipped_not_an_error(monkeypatch):
    """proof_of_incorporation_document_id is set but no matching document
    row exists (e.g. it was deleted) -- the report degrades gracefully,
    it doesn't crash or fabricate a row."""
    import backend.main as m
    client_id = uuid.uuid4()
    dangling_id = uuid.uuid4()
    client = _client_row(client_id, proof_of_incorporation_document_id=dangling_id)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], documents=[]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert result["documents"] == []


def test_no_documents_at_all_gives_an_empty_index(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert result["documents"] == []


# ── Compliance History (Part C, 2026-09-03; REMOVED from this report
# 2026-09-09 per direct partner feedback) ──────────────────────────────────
# This report no longer returns a "compliance_history" key or renders a
# Compliance History section in any format. The underlying logging
# (_log_compliance_event(), see tests/test_compliance_event_logging.py)
# is untouched -- audit_logs keeps recording exactly as before, this
# report just doesn't read it back. _fetch_compliance_history() itself
# is left in place but is no longer exercised by any test in this file
# (it has no other caller either); its own composition logic (event
# ordering, CDD-review merging, cross-client exclusion) is consequently
# untested for now -- flagged, not silently dropped, in case it's
# resurfaced somewhere later.

def _cdd_review_row(client_id, **overrides):
    row = {
        "client_id": client_id, "firm_id": FIRM_ID, "review_date": date(2026, 9, 4),
        "status": "Complete", "risk_rating": "Medium", "changes_identified": None,
        "reviewed_by": None,
    }
    row.update(overrides)
    return row


# ── Last CDD Review (2026-09-04) ────────────────────────────────────────────

def test_overall_last_cdd_review_date_none_when_never_reviewed(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert result["overall"]["last_cdd_review_date"] is None


def test_overall_last_cdd_review_date_reflects_most_recent_review(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    older = _cdd_review_row(client_id, review_date=date(2026, 1, 1))
    newer = _cdd_review_row(client_id, review_date=date(2026, 9, 4))
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], cdd_reviews=[older, newer]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert result["overall"]["last_cdd_review_date"] == "2026-09-04"


# ── CSV / PDF export (2026-09-04) ──────────────────────────────────────────
# Reuses the exact same data _fetch_client_aml_cdd_report() already
# produces -- these tests check the export wiring (all 9 sections
# present, real content surfaces, valid file bytes), not a second
# implementation of the report's own composition logic (already covered
# above).

def _csv_rows(response):
    import csv, io
    text = response.body.decode("utf-8-sig") if isinstance(response.body, bytes) else response.body.lstrip("﻿")
    return list(csv.reader(io.StringIO(text)))


def _pdf_tables_by_header(response):
    """Every section of this report is its own separate bordered table
    (own heading, own header row, own column widths) -- not one big
    table spanning the page the way Matter AML Status's report is, so
    pdfplumber's extract_tables() row-clustering (which broke down on
    that report's 22-row scale) is reliable here: each section typically
    has only a few rows. Keyed by the header row tuple -- but that
    alone doesn't uniquely identify a section: Sections 1 (Overall
    Compliance Position), 5 (PEP/Risk) and 6 (Conflict Check) all use
    the identical ("Item", "Status") header, so this returns a LIST of
    tables per header (use _table_with_item() below to pick out the
    right one by its actual row content, not by header alone)."""
    tables = {}
    with pdfplumber.open(io.BytesIO(response.body)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table:
                    continue
                header = tuple(table[0])
                rows = [
                    [" ".join((cell or "").split()) for cell in row]
                    for row in table[1:]
                ]
                tables.setdefault(header, []).append(rows)
    return tables


def _table_with_item(tables_for_header, item_label):
    """Given the list of same-header tables from _pdf_tables_by_header(),
    return the one row list that actually contains a row whose first
    cell is item_label -- disambiguates Sections 1/5/6's shared
    ("Item", "Status") header by content instead."""
    for rows in tables_for_header:
        if any(row[0] == item_label for row in rows):
            return rows
    raise AssertionError(f"No ('Item', 'Status') table contains a row for {item_label!r}")


# ── Exceptions / Follow-up (Part D, 2026-09-07, Phase 2) ──────────────────
# Read-only surfacing of this client's own Compliance Exception Resolution
# Workflow rows -- see tests/test_compliance_exceptions.py for the
# workflow's own lifecycle/resolution-validation/auto-reopen tests; these
# only check this report's composition of already-synced rows.

def test_exceptions_section_surfaces_a_real_outstanding_item(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    # Unverified identity is the one real gap -- everything else Cleared.
    compliance = _cleared_compliance(client_id, identity_verification_status="Unverified")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance={client_id: compliance}))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    exceptions = result["exceptions"]
    assert len(exceptions) == 1
    assert exceptions[0]["issue_label"] == "Identity not verified"
    assert exceptions[0]["status_label"] == "Open"
    assert exceptions[0]["responsible_person_name"] == "Compliance Officer"


def test_exceptions_section_empty_for_a_fully_cleared_client(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    monkeypatch.setattr(
        m, "_db_pool", FakePool(clients=[client], compliance={client_id: _cleared_compliance(client_id)}),
    )
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert result["exceptions"] == []


def test_exceptions_section_resolves_responsible_person_name(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    lawyer_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual", created_by=lawyer_id)
    lawyer = _user("J. Moyo", user_id=lawyer_id)
    compliance = _cleared_compliance(client_id, identity_verification_status="Unverified")
    monkeypatch.setattr(
        m, "_db_pool", FakePool(clients=[client], compliance={client_id: compliance}, users=[lawyer]),
    )
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert result["exceptions"][0]["responsible_person_name"] == "J. Moyo"


def test_exceptions_section_in_csv_export(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    compliance = _cleared_compliance(client_id, identity_verification_status="Unverified")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance={client_id: compliance}))
    _as_current_user(monkeypatch, m, _partner())

    response = asyncio.run(client_aml_cdd_report_export(str(client_id), _fake_request()))
    rows = _csv_rows(response)
    flat_rows = [row for row in rows if row]

    idx = next(i for i, row in enumerate(flat_rows) if row[0] == "9. Exceptions / Follow-up")
    assert flat_rows[idx + 1] == ["Issue", "Status", "Responsible Person", "Due"]
    assert flat_rows[idx + 2] == ["Identity not verified", "Open", "Compliance Officer", "—"]


def test_pdf_export_wraps_long_exception_issue_label(monkeypatch):
    """CDD_REVIEW_OUTSTANDING's issue_label can carry a lawyer's own
    unbounded changes_identified text appended to it -- same free-text
    truncation risk as every other wrapped column in this report."""
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    compliance = _cleared_compliance(client_id)
    long_note = (
        "Client's registered address changed and a new director was appointed, "
        "requiring a full re-verification of identity documents and source of funds"
    )
    review = _cdd_review_row(client_id, status="Outstanding", changes_identified=long_note)
    monkeypatch.setattr(
        m, "_db_pool", FakePool(clients=[client], compliance={client_id: compliance}, cdd_reviews=[review]),
    )
    _as_current_user(monkeypatch, m, _partner())

    response = asyncio.run(client_aml_cdd_report_export_pdf(str(client_id), _fake_request()))
    tables = _pdf_tables_by_header(response)
    exception_rows = tables[("Issue", "Status", "Responsible Person", "Due")][0]

    assert len(exception_rows) == 1
    assert long_note in exception_rows[0][0]
    assert not exception_rows[0][0].endswith("...")


def test_csv_export_includes_all_nine_sections(monkeypatch):
    """Compliance History (formerly Section 9) was removed 2026-09-09
    per direct partner feedback -- Exceptions/Follow-up renumbered from
    10 down to 9, nothing else shifted."""
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, _partner())

    response = asyncio.run(client_aml_cdd_report_export(str(client_id), _fake_request()))
    rows = _csv_rows(response)
    section_titles = {row[0] for row in rows if row}

    for expected in [
        "1. Overall Compliance Position", "2. Client Identification", "3. Beneficial Ownership",
        "4. Person Acting for Client", "5. PEP / Risk Assessment", "6. Conflict Check",
        "7. Matters for this Client", "8. Supporting Document Index",
        "9. Exceptions / Follow-up",
    ]:
        assert expected in section_titles
    assert "Compliance History" not in "".join(section_titles)


def test_csv_export_reflects_real_data(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Company")
    owner = _owner(client_id, owner_name="Tendai Moyo", ownership_percentage=60, verification_status="Verified")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], owners=[owner]))
    _as_current_user(monkeypatch, m, _partner())

    response = asyncio.run(client_aml_cdd_report_export(str(client_id), _fake_request()))
    rows = _csv_rows(response)
    flat = [cell for row in rows for cell in row]

    assert "Blue Ridge Traders (Pvt) Ltd" in flat  # client name, header block
    assert "Tendai Moyo" in flat  # real beneficial owner
    assert "60.0%" in flat


def test_pdf_export_associate_and_secretary_succeed(monkeypatch):
    """client:read, not the stricter reports:* permission -- every role
    (including associate/secretary) can export their own client's CDD
    report, same as viewing it."""
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    for role in ("associate", "secretary"):
        monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
        _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": role, "display_name": "X"})
        response = asyncio.run(client_aml_cdd_report_export_pdf(str(client_id), _fake_request()))
        assert response.media_type == "application/pdf"
        assert response.body.startswith(b"%PDF")


def test_pdf_export_produces_a_real_pdf(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, _partner())

    response = asyncio.run(client_aml_cdd_report_export_pdf(str(client_id), _fake_request()))

    assert response.media_type == "application/pdf"
    assert "client_aml_cdd_report" in response.headers["content-disposition"]
    assert response.body.startswith(b"%PDF")


def test_pdf_export_handles_empty_sections_without_crashing(monkeypatch):
    """A client with no owners/reps/matters/documents/history at all --
    every table section is legitimately empty; the PDF must still render
    a friendly "None recorded." rather than crash."""
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id, client_type="Individual")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, _partner())

    response = asyncio.run(client_aml_cdd_report_export_pdf(str(client_id), _fake_request()))

    assert response.media_type == "application/pdf"
    assert response.body.startswith(b"%PDF")


# ── PDF formatting fixes (2026-09-07, full-report review; Section 9 was
# Compliance History at review time, removed 2026-09-09 and its number
# reused by Exceptions/Follow-up -- see test_pdf_export_wraps_long_
# exception_issue_label below for that section's own, separate wrap
# coverage, added with the section itself in Phase 2) ─────────────────────
# Sections 3, 5, 7 and 8 carried genuinely open-ended free text squeezed
# into equal-width, unwrapped _mp_pdf_table() columns -- confirmed
# truncating with an ellipsis on real staging data (20 real clients), not
# just theoretically. Each now wraps its own free-text column(s) via
# _CDD_SECTION_LAYOUT instead. Sections 1, 2, 4 are a known, deferred
# follow-up (see _CDD_SECTION_LAYOUT's own comment) -- not covered here
# since they weren't touched.

def test_pdf_export_wraps_long_beneficial_ownership_basis(monkeypatch):
    """Real bug: Basis (ownership_or_control_basis) is unbounded free
    text squeezed into one of 5 equal-width columns -- confirmed
    truncating on real staging data even at ~40-50 chars, well under
    what a naive length-based check would flag (Section 3's columns are
    narrower than a 2-column section's)."""
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    long_basis = "70% shareholding and executive director with day-to-day control of operations"
    owner = _owner(client_id, owner_name="Gershom Sabri", ownership_percentage=70,
                    ownership_or_control_basis=long_basis, verification_status="Verified")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], owners=[owner]))
    _as_current_user(monkeypatch, m, _partner())

    response = asyncio.run(client_aml_cdd_report_export_pdf(str(client_id), _fake_request()))
    tables = _pdf_tables_by_header(response)
    bo_rows = tables[("Name", "Nationality", "Ownership", "Basis", "Verification")][0]

    assert len(bo_rows) == 1
    assert bo_rows[0][3] == long_basis
    assert not bo_rows[0][3].endswith("...")


def test_pdf_export_wraps_long_source_of_wealth_and_funds(monkeypatch):
    """The originally-reported bug: Source of Wealth/Source of Funds,
    both unbounded TEXT, share Section 5's ("Item", "Status") table with
    Sections 1 and 6 -- disambiguated by row content since all three
    sections render an identical header."""
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    long_wealth = (
        "Company profits accumulated from investment holdings and "
        "portfolio management activities since incorporation"
    )
    long_funds = "Client payments from investment advisory and portfolio management contracts"
    compliance = _compliance(client_id, source_of_wealth=long_wealth, source_of_funds=long_funds)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance={client_id: compliance}))
    _as_current_user(monkeypatch, m, _partner())

    response = asyncio.run(client_aml_cdd_report_export_pdf(str(client_id), _fake_request()))
    tables = _pdf_tables_by_header(response)[("Item", "Status")]
    pep_rows = _table_with_item(tables, "Source of Wealth")
    by_item = {row[0]: row[1] for row in pep_rows}

    assert by_item["Source of Wealth"] == long_wealth
    assert by_item["Source of Funds"] == long_funds
    assert not by_item["Source of Wealth"].endswith("...")
    assert not by_item["Source of Funds"].endswith("...")


def test_pdf_export_wraps_long_matter_name_and_reason_for_aml_scope(monkeypatch):
    """Same aml_scope_reason field, and the same number+name Matter
    combination, already fixed once in the Matter AML Status report --
    this report renders its own separate Section 7 table and had the
    identical unfixed bug."""
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    long_name = "Estate Late Tendekai Mafuta — Winding up estate, letters of administration"
    long_reason = "Purchase of mining claims from PEP who wants payment offshore, requires enhanced due diligence"
    matter = _matter(client_id, name=long_name, number="TC-001-01",
                      aml_scope="InScope", aml_scope_reason=long_reason, matter_risk="High")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], matters=[matter]))
    _as_current_user(monkeypatch, m, _partner())

    response = asyncio.run(client_aml_cdd_report_export_pdf(str(client_id), _fake_request()))
    tables = _pdf_tables_by_header(response)
    matter_rows = tables[("Matter", "Status", "AML Scope", "Reason for AML Scope", "Matter Risk")][0]

    assert len(matter_rows) == 1
    assert matter_rows[0][0] == f"TC-001-01 - {long_name.replace(chr(8212), '-')}"
    assert matter_rows[0][3] == long_reason
    assert not matter_rows[0][0].endswith("...")
    assert not matter_rows[0][3].endswith("...")


def test_pdf_export_wraps_long_document_filename(monkeypatch):
    """Confirmed on real data (Mould Enterprises): a real matter-linked
    document's filename truncated in the "File" column. A filename has
    no natural word-break points, so fpdf2's wrap force-splits it
    mid-word with no separator (confirmed: every character survives,
    just split across two lines) -- _pdf_tables_by_header()'s
    space-joining collapse can't tell that artifact apart from a real
    space, so the comparison below strips spaces from both sides rather
    than asserting exact equality (a real filename never legitimately
    contains one anyway)."""
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    matter = _matter(client_id)
    long_filename = "Mould_Enterprises_Court_Order_Boundary_Dispute_Ruling_2026.docx"
    doc = _document(matter_id=matter["id"], filename=long_filename, document_type="Court Order")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], matters=[matter], documents=[doc]))
    _as_current_user(monkeypatch, m, _partner())

    response = asyncio.run(client_aml_cdd_report_export_pdf(str(client_id), _fake_request()))
    tables = _pdf_tables_by_header(response)
    doc_rows = tables[("Document", "Category", "Status", "File")][0]

    assert len(doc_rows) == 1
    assert doc_rows[0][3].replace(" ", "") == long_filename
    assert not doc_rows[0][3].endswith("...")

# test_pdf_export_wraps_long_compliance_history_review_note and
# test_pdf_export_renders_arrow_as_plain_ascii_not_garbled (Compliance
# History PDF wrapping/arrow-substitution coverage) were removed
# 2026-09-09 along with the report section they exercised. No other
# live code path currently builds a "old → new" string that reaches
# _pdf_safe() (that string shape came only from _row_to_compliance_
# event(), which nothing calls anymore), so _pdf_safe()'s own arrow
# substitution is untested for now too -- flagged, not silently dropped.
