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
import uuid
from datetime import date, datetime, timezone

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

        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")

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


# ── Compliance History (Part C, 2026-09-03) ────────────────────────────────
# Real event log via audit_logs -- see tests/test_compliance_event_logging.py
# for the logging-side tests (create_beneficial_owner/update_beneficial_
# owner/update_client_compliance/update_matter each calling
# _log_compliance_event() correctly). These tests cover only the reading
# side: _fetch_compliance_history()'s composition into this report.

def _audit_log(target_type, target_id, action, details=None, actor_name="J. Moyo", created_at=None):
    return {
        "firm_id": FIRM_ID, "target_type": target_type, "target_id": target_id,
        "action": action, "actor_name": actor_name,
        "details": details or {}, "created_at": created_at or datetime.now(timezone.utc),
    }


def test_compliance_history_enabled_with_no_events_yet(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert result["compliance_history"]["enabled"] is True
    assert result["compliance_history"]["events"] == []


def test_compliance_history_includes_client_level_events(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    log = _audit_log("CLIENT", client_id, "BO_VERIFIED", {"owner_name": "Tendai Moyo"})
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], audit_logs=[log]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    events = result["compliance_history"]["events"]
    assert len(events) == 1
    assert events[0]["event"] == "Beneficial owner verified"
    assert events[0]["user"] == "J. Moyo"
    assert events[0]["result"] == "Tendai Moyo"


def test_compliance_history_includes_matter_level_events_for_this_clients_matters(monkeypatch):
    """Mirrors the sample report's own mixed timeline -- a matter-level
    event ("Matter risk reviewed") appears alongside client-level events
    for the same client."""
    import backend.main as m
    client_id = uuid.uuid4()
    matter = _matter(client_id)
    client = _client_row(client_id)
    log = _audit_log("MATTER", matter["id"], "MATTER_RISK_SET", {"old": "NotAssessed", "new": "High"},
                      actor_name="Compliance Officer")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], matters=[matter], audit_logs=[log]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    events = result["compliance_history"]["events"]
    assert len(events) == 1
    assert events[0]["event"] == "Matter risk set"
    assert events[0]["user"] == "Compliance Officer"
    assert events[0]["result"] == "Not Assessed → High"


def test_compliance_history_excludes_another_clients_events(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    other_client_id = uuid.uuid4()
    client = _client_row(client_id)
    log = _audit_log("CLIENT", other_client_id, "PEP_FLAGGED")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], audit_logs=[log]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    assert result["compliance_history"]["events"] == []


def test_compliance_history_sorted_chronologically(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    later = _audit_log("CLIENT", client_id, "CONFLICT_CHECK_COMPLETED", created_at=datetime(2026, 9, 2, tzinfo=timezone.utc))
    earlier = _audit_log("CLIENT", client_id, "PEP_FLAGGED", created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], audit_logs=[later, earlier]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    events = result["compliance_history"]["events"]
    assert [e["event"] for e in events] == ["PEP flagged", "Conflict check completed"]


# ── CDD Review events merged into the same timeline (2026-09-04) ──────────
# Per the user's own instruction: CDD Review events join this ONE
# timeline, not a separate section -- see _row_to_cdd_review_event() and
# _fetch_compliance_history()'s own merge.

def _cdd_review_row(client_id, **overrides):
    row = {
        "client_id": client_id, "firm_id": FIRM_ID, "review_date": date(2026, 9, 4),
        "status": "Complete", "risk_rating": "Medium", "changes_identified": None,
        "reviewed_by": None,
    }
    row.update(overrides)
    return row


def test_cdd_review_event_appears_in_compliance_history(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    reviewer_id = uuid.uuid4()
    reviewer = _user("P. Chademana", user_id=reviewer_id)
    review = _cdd_review_row(client_id, reviewed_by=reviewer_id, risk_rating="High",
                              changes_identified="Client relocated")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], users=[reviewer], cdd_reviews=[review]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    events = result["compliance_history"]["events"]
    assert len(events) == 1
    assert events[0]["event"] == "CDD Review"
    assert events[0]["date"] == "2026-09-04"
    assert events[0]["user"] == "P. Chademana"
    assert "High" in events[0]["result"]
    assert "Client relocated" in events[0]["result"]


def test_cdd_review_events_interleave_chronologically_with_audit_log_events(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    client = _client_row(client_id)
    review = _cdd_review_row(client_id, review_date=date(2026, 9, 2))
    log = _audit_log("CLIENT", client_id, "PEP_FLAGGED", created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], audit_logs=[log], cdd_reviews=[review]))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(client_aml_cdd_report(str(client_id), _fake_request()))

    events = result["compliance_history"]["events"]
    assert [e["event"] for e in events] == ["PEP flagged", "CDD Review"]


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


def test_csv_export_includes_all_nine_sections(monkeypatch):
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
        "7. Matters for this Client", "8. Supporting Document Index", "9. Compliance History",
    ]:
        assert expected in section_titles


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
