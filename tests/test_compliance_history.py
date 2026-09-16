"""
Unit tests for the Compliance History drill-down (backend/main.py,
2026-09-16, adversarial-audit follow-up):
  GET /api/clients/{client_id}/compliance-history

This closes the WHO/WHEN gap the AML/Client Compliance Register, AML
Exceptions, and Matter AML Status reports all share, via one shared
drill-down rather than three separate report-column fixes.

No new tracking is added here -- _fetch_compliance_history() and
_row_to_compliance_event() (both in backend/main.py) already existed,
built 2026-09-03 for the Individual Client AML/CDD Report's own
Compliance History section, then left with zero callers when that
section was removed 2026-09-09 per partner feedback. Its own
composition logic (event ordering, CDD-review merging, cross-client
exclusion) was flagged at the time as untested since nothing exercised
it -- these are those tests, now that a real caller exists again.

Same FakeConnection/FakePool/_as_current_user/_fake_request convention
as tests/test_client_aml_cdd_report.py.
"""

import asyncio
import json
import uuid
from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException

from backend.main import FIRM_ID, list_client_compliance_history


class FakeConnection:
    def __init__(self, clients=None, matters=None, audit_logs=None, cdd_reviews=None, users=None):
        self.clients = clients if clients is not None else []
        self.matters = matters if matters is not None else []
        self.audit_logs = audit_logs if audit_logs is not None else []
        self.cdd_reviews = cdd_reviews if cdd_reviews is not None else []
        self.users = users if users is not None else []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT * FROM clients WHERE id=$1 AND firm_id=$2"):
            cid, firm_id = args
            for c in self.clients:
                if c["id"] == cid and c["firm_id"] == firm_id:
                    return dict(c)
            return None

        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT id FROM matters WHERE client_id=$1 AND firm_id=$2 AND NOT is_sentinel"):
            cid, firm_id = args
            return [
                {"id": m["id"]} for m in self.matters
                if m["client_id"] == cid and m["firm_id"] == firm_id and not m.get("is_sentinel", False)
            ]

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


def _client(name="Anchorflow Holdings", client_id=None):
    return {"id": client_id or uuid.uuid4(), "firm_id": FIRM_ID, "full_name": name}


def _matter(client_id, matter_id=None, is_sentinel=False):
    return {"id": matter_id or uuid.uuid4(), "firm_id": FIRM_ID, "client_id": client_id, "is_sentinel": is_sentinel}


def _audit_log(target_type, target_id, action, actor_name, created_at, details=None):
    return {
        "firm_id": FIRM_ID, "target_type": target_type, "target_id": target_id,
        "action": action, "actor_name": actor_name, "created_at": created_at,
        "details": json.dumps(details or {}),
    }


def _cdd_review(client_id, *, review_date, status="Complete", risk_rating="Low",
                changes_identified=None, reviewed_by=None):
    return {
        "firm_id": FIRM_ID, "client_id": client_id, "review_date": review_date,
        "status": status, "risk_rating": risk_rating, "changes_identified": changes_identified,
        "reviewed_by": reviewed_by,
    }


def _user(display_name, user_id=None):
    return {"id": user_id or uuid.uuid4(), "firm_id": FIRM_ID, "display_name": display_name}


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def _fake_request():
    return None


# ── permission / not-found ────────────────────────────────────────────────

def test_secretary_can_read_same_tier_as_other_client_compliance_data(monkeypatch):
    import backend.main as m
    client = _client()
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "secretary", "display_name": "S"})

    result = asyncio.run(list_client_compliance_history(str(client["id"]), _fake_request()))

    assert result == []


def test_unknown_client_id_is_404(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"})

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(list_client_compliance_history(str(uuid.uuid4()), _fake_request()))
    assert exc_info.value.status_code == 404


def test_malformed_client_id_is_400(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"})

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(list_client_compliance_history("not-a-uuid", _fake_request()))
    assert exc_info.value.status_code == 400


def test_no_history_returns_empty_list_not_an_error(monkeypatch):
    import backend.main as m
    client = _client()
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"})

    result = asyncio.run(list_client_compliance_history(str(client["id"]), _fake_request()))

    assert result == []


# ── shape: {"date", "event", "user", "result"} ────────────────────────────

def test_audit_log_event_shape_and_who_when(monkeypatch):
    import backend.main as m
    client = _client()
    log = _audit_log(
        "CLIENT", client["id"], "BO_VERIFIED", "Rufaro Rusike",
        datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc), {"owner_name": "Tendai Moyo"},
    )
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], audit_logs=[log]))
    _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"})

    result = asyncio.run(list_client_compliance_history(str(client["id"]), _fake_request()))

    assert result == [{"date": "2026-09-05", "event": "Beneficial owner verified", "user": "Rufaro Rusike", "result": "Tendai Moyo"}]


def test_risk_rating_changed_shows_old_arrow_new(monkeypatch):
    import backend.main as m
    client = _client()
    log = _audit_log(
        "CLIENT", client["id"], "RISK_RATING_CHANGED", "Farirai Gwenzi",
        datetime(2026, 9, 5, tzinfo=timezone.utc), {"old": "Low", "new": "High"},
    )
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], audit_logs=[log]))
    _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"})

    result = asyncio.run(list_client_compliance_history(str(client["id"]), _fake_request()))

    assert result[0]["result"] == "Low → High"


def test_compliance_exception_closed_shows_issue_and_reason(monkeypatch):
    import backend.main as m
    client = _client()
    log = _audit_log(
        "CLIENT", client["id"], "COMPLIANCE_EXCEPTION_CLOSED_NO_FURTHER_ACTION", "Tanaka Chademana",
        datetime(2026, 9, 10, tzinfo=timezone.utc),
        {"issue_code": "IDENTITY_NOT_VERIFIED", "issue": "Identity not verified", "reason": "Client is deceased"},
    )
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], audit_logs=[log]))
    _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"})

    result = asyncio.run(list_client_compliance_history(str(client["id"]), _fake_request()))

    assert result[0]["event"] == "Compliance exception closed — no further action"
    assert result[0]["result"] == "Identity not verified — Client is deceased"


# ── event ordering ─────────────────────────────────────────────────────────

def test_events_are_returned_in_chronological_order(monkeypatch):
    import backend.main as m
    client = _client()
    logs = [
        _audit_log("CLIENT", client["id"], "CONFLICT_CHECK_COMPLETED", "P1",
                   datetime(2026, 9, 10, tzinfo=timezone.utc)),
        _audit_log("CLIENT", client["id"], "PEP_FLAGGED", "P2",
                   datetime(2026, 9, 1, tzinfo=timezone.utc)),
        _audit_log("CLIENT", client["id"], "BO_ADDED", "P3",
                   datetime(2026, 9, 5, tzinfo=timezone.utc), {"owner_name": "X"}),
    ]
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], audit_logs=logs))
    _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"})

    result = asyncio.run(list_client_compliance_history(str(client["id"]), _fake_request()))

    assert [r["date"] for r in result] == ["2026-09-01", "2026-09-05", "2026-09-10"]


# ── cross-client / cross-matter exclusion ─────────────────────────────────

def test_other_clients_audit_logs_never_leak_in(monkeypatch):
    import backend.main as m
    client = _client("This Client")
    other_client = _client("Other Client")
    logs = [
        _audit_log("CLIENT", client["id"], "PEP_FLAGGED", "P",
                   datetime(2026, 9, 1, tzinfo=timezone.utc)),
        _audit_log("CLIENT", other_client["id"], "PEP_FLAGGED", "P",
                   datetime(2026, 9, 2, tzinfo=timezone.utc)),
    ]
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client, other_client], audit_logs=logs))
    _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"})

    result = asyncio.run(list_client_compliance_history(str(client["id"]), _fake_request()))

    assert len(result) == 1
    assert result[0]["date"] == "2026-09-01"


def test_this_clients_matter_events_are_included(monkeypatch):
    import backend.main as m
    client = _client()
    matter = _matter(client["id"])
    log = _audit_log(
        "MATTER", matter["id"], "MATTER_RISK_SET", "Blessing Nyathi",
        datetime(2026, 9, 3, tzinfo=timezone.utc), {"old": "Low", "new": "High"},
    )
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], matters=[matter], audit_logs=[log]))
    _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"})

    result = asyncio.run(list_client_compliance_history(str(client["id"]), _fake_request()))

    assert len(result) == 1
    assert result[0]["user"] == "Blessing Nyathi"


def test_other_clients_matter_events_never_leak_in(monkeypatch):
    """The whole reason this endpoint fetches matter_ids scoped to
    NOT is_sentinel AND this client's own id first -- a matter event
    belonging to some other client's matter must never appear here."""
    import backend.main as m
    client = _client("This Client")
    other_client = _client("Other Client")
    other_matter = _matter(other_client["id"])
    log = _audit_log(
        "MATTER", other_matter["id"], "MATTER_RISK_SET", "P",
        datetime(2026, 9, 3, tzinfo=timezone.utc), {"old": "Low", "new": "High"},
    )
    monkeypatch.setattr(
        m, "_db_pool", FakePool(clients=[client, other_client], matters=[other_matter], audit_logs=[log]),
    )
    _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"})

    result = asyncio.run(list_client_compliance_history(str(client["id"]), _fake_request()))

    assert result == []


def test_sentinel_matter_events_are_excluded(monkeypatch):
    import backend.main as m
    client = _client()
    sentinel = _matter(client["id"], is_sentinel=True)
    log = _audit_log(
        "MATTER", sentinel["id"], "MATTER_RISK_SET", "P",
        datetime(2026, 9, 3, tzinfo=timezone.utc), {"old": "Low", "new": "High"},
    )
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], matters=[sentinel], audit_logs=[log]))
    _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"})

    result = asyncio.run(list_client_compliance_history(str(client["id"]), _fake_request()))

    assert result == []


# ── CDD review merging ─────────────────────────────────────────────────────

def test_cdd_review_merges_into_the_same_timeline(monkeypatch):
    import backend.main as m
    client = _client()
    reviewer = _user("J. Moyo")
    review = _cdd_review(
        client["id"], review_date=date(2026, 9, 4), status="Outstanding",
        risk_rating="Medium", changes_identified="Client relocated", reviewed_by=reviewer["id"],
    )
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], cdd_reviews=[review], users=[reviewer]))
    _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"})

    result = asyncio.run(list_client_compliance_history(str(client["id"]), _fake_request()))

    assert len(result) == 1
    assert result[0] == {
        "date": "2026-09-04", "event": "CDD Review", "user": "J. Moyo",
        "result": "Outstanding — risk Medium; Client relocated",
    }


def test_cdd_review_and_audit_log_events_interleave_by_date(monkeypatch):
    import backend.main as m
    client = _client()
    review = _cdd_review(client["id"], review_date=date(2026, 9, 5), status="Complete")
    log = _audit_log("CLIENT", client["id"], "PEP_FLAGGED", "P",
                      datetime(2026, 9, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], cdd_reviews=[review], audit_logs=[log]))
    _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"})

    result = asyncio.run(list_client_compliance_history(str(client["id"]), _fake_request()))

    assert [r["event"] for r in result] == ["PEP flagged", "CDD Review"]


def test_no_reviewer_falls_back_to_unknown(monkeypatch):
    import backend.main as m
    client = _client()
    review = _cdd_review(client["id"], review_date=date(2026, 9, 4), reviewed_by=None)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], cdd_reviews=[review]))
    _as_current_user(monkeypatch, m, {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"})

    result = asyncio.run(list_client_compliance_history(str(client["id"]), _fake_request()))

    assert result[0]["user"] == "Unknown"
