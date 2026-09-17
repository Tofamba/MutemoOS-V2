"""
Unit tests for Compliance Actions queue items' Investigator Context
(backend/main.py, 2026-09-18):
  GET /api/ai-action-queue/{item_id}/investigator-context

An item's detail view previously showed only what triggered it (the
underlying exception + recommended action), with no surrounding history
for someone actually investigating rather than just approving/dismissing.
This endpoint reuses _fetch_compliance_history() completely unchanged
(same function GET .../compliance-history already calls -- see
tests/test_compliance_history.py for that function's own composition
tests) -- no new tracking, purely composing data that's already being
recorded, capped to the 5 most recent events (newest first) plus a count
of any OTHER open exceptions on the same client (excluding this item's
own underlying one).

Same FakeConnection/FakePool/_as_current_user/_fake_request convention as
tests/test_compliance_history.py, extended with ai_action_queue and a
fetchval() for the open-exceptions count.
"""

import asyncio
import json
import uuid
from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException

from backend.main import FIRM_ID, ai_action_queue_investigator_context


class FakeConnection:
    def __init__(self, ai_action_queue=None, clients=None, matters=None, audit_logs=None,
                 cdd_reviews=None, users=None, compliance_exceptions=None):
        self.ai_action_queue = ai_action_queue if ai_action_queue is not None else []
        self.clients = clients if clients is not None else []
        self.matters = matters if matters is not None else []
        self.audit_logs = audit_logs if audit_logs is not None else []
        self.cdd_reviews = cdd_reviews if cdd_reviews is not None else []
        self.users = users if users is not None else []
        self.compliance_exceptions = compliance_exceptions if compliance_exceptions is not None else []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT client_id, compliance_exception_id FROM ai_action_queue WHERE id=$1"):
            iid, firm_id = args
            for a in self.ai_action_queue:
                if a["id"] == iid and a["firm_id"] == firm_id:
                    return {"client_id": a["client_id"], "compliance_exception_id": a["compliance_exception_id"]}
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

    async def fetchval(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT COUNT(*) FROM compliance_exceptions WHERE firm_id=$1 AND client_id=$2"):
            firm_id, cid, exclude_id = args
            statuses = {"Open", "InProgress", "AwaitingClient"}
            return len([
                e for e in self.compliance_exceptions
                if e["firm_id"] == firm_id and e["client_id"] == cid
                and e["status"] in statuses and e["id"] != exclude_id
            ])

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


def _exception(client_id, exception_id=None, status="Open"):
    return {"id": exception_id or uuid.uuid4(), "firm_id": FIRM_ID, "client_id": client_id, "status": status}


def _caq_item(client_id, exception_id, item_id=None):
    return {
        "id": item_id or uuid.uuid4(), "firm_id": FIRM_ID,
        "client_id": client_id, "compliance_exception_id": exception_id,
    }


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def _fake_request():
    return None


def _partner():
    return {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P"}


# ── not-found / validation ────────────────────────────────────────────────

def test_unknown_item_id_is_404(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, _partner())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(ai_action_queue_investigator_context(str(uuid.uuid4()), _fake_request()))
    assert exc_info.value.status_code == 404


def test_malformed_item_id_is_400(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, _partner())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(ai_action_queue_investigator_context("not-a-uuid", _fake_request()))
    assert exc_info.value.status_code == 400


# ── empty state -- no prior history, not an error ──────────────────────────

def test_no_prior_history_returns_empty_events_not_an_error(monkeypatch):
    import backend.main as m
    client = _client()
    exc = _exception(client["id"])
    item = _caq_item(client["id"], exc["id"])
    monkeypatch.setattr(m, "_db_pool", FakePool(
        ai_action_queue=[item], clients=[client], compliance_exceptions=[exc],
    ))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(ai_action_queue_investigator_context(str(item["id"]), _fake_request()))

    assert result["client_id"] == str(client["id"])
    assert result["recent_events"] == []
    # The item's own underlying exception is excluded from the "other" count.
    assert result["other_open_exceptions_count"] == 0


# ── real history reflected correctly ────────────────────────────────────────

def test_recent_events_reflect_real_compliance_history(monkeypatch):
    import backend.main as m
    client = _client()
    exc = _exception(client["id"])
    item = _caq_item(client["id"], exc["id"])
    logs = [
        _audit_log("CLIENT", client["id"], "PEP_FLAGGED", "P1",
                   datetime(2026, 9, 1, tzinfo=timezone.utc)),
        _audit_log("CLIENT", client["id"], "CONFLICT_CHECK_COMPLETED", "P2",
                   datetime(2026, 9, 10, tzinfo=timezone.utc)),
    ]
    monkeypatch.setattr(m, "_db_pool", FakePool(
        ai_action_queue=[item], clients=[client], compliance_exceptions=[exc], audit_logs=logs,
    ))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(ai_action_queue_investigator_context(str(item["id"]), _fake_request()))

    # Newest first -- the reverse of _fetch_compliance_history()'s own
    # chronological (oldest-first) order.
    assert [e["date"] for e in result["recent_events"]] == ["2026-09-10", "2026-09-01"]
    assert result["recent_events"][0]["event"] == "Conflict check completed"


def test_capped_at_five_most_recent_events(monkeypatch):
    import backend.main as m
    client = _client()
    exc = _exception(client["id"])
    item = _caq_item(client["id"], exc["id"])
    logs = [
        _audit_log("CLIENT", client["id"], "PEP_FLAGGED", "P",
                   datetime(2026, 9, day, tzinfo=timezone.utc))
        for day in range(1, 8)
    ]
    monkeypatch.setattr(m, "_db_pool", FakePool(
        ai_action_queue=[item], clients=[client], compliance_exceptions=[exc], audit_logs=logs,
    ))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(ai_action_queue_investigator_context(str(item["id"]), _fake_request()))

    assert len(result["recent_events"]) == 5
    # Newest 5 of the 7 (days 3-7), newest first.
    assert [e["date"] for e in result["recent_events"]] == [
        "2026-09-07", "2026-09-06", "2026-09-05", "2026-09-04", "2026-09-03",
    ]


def test_matter_events_and_cdd_reviews_included_same_as_full_history(monkeypatch):
    """No second, parallel history computation -- this endpoint reuses
    _fetch_compliance_history() exactly, so matter events and CDD reviews
    (already covered end-to-end in tests/test_compliance_history.py) show
    up here too."""
    import backend.main as m
    client = _client()
    matter = _matter(client["id"])
    exc = _exception(client["id"])
    item = _caq_item(client["id"], exc["id"])
    log = _audit_log("MATTER", matter["id"], "MATTER_RISK_SET", "P",
                      datetime(2026, 9, 3, tzinfo=timezone.utc), {"old": "Low", "new": "High"})
    review = _cdd_review(client["id"], review_date=date(2026, 9, 5), status="Complete")
    monkeypatch.setattr(m, "_db_pool", FakePool(
        ai_action_queue=[item], clients=[client], compliance_exceptions=[exc],
        matters=[matter], audit_logs=[log], cdd_reviews=[review],
    ))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(ai_action_queue_investigator_context(str(item["id"]), _fake_request()))

    assert [e["event"] for e in result["recent_events"]] == ["CDD Review", "Matter risk set"]


# ── other-open-exceptions count ─────────────────────────────────────────────

def test_other_open_exceptions_counted_excluding_this_items_own(monkeypatch):
    import backend.main as m
    client = _client()
    this_exc = _exception(client["id"], status="Open")
    other_open_1 = _exception(client["id"], status="Open")
    other_open_2 = _exception(client["id"], status="InProgress")
    other_resolved = _exception(client["id"], status="Resolved")
    item = _caq_item(client["id"], this_exc["id"])
    monkeypatch.setattr(m, "_db_pool", FakePool(
        ai_action_queue=[item], clients=[client],
        compliance_exceptions=[this_exc, other_open_1, other_open_2, other_resolved],
    ))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(ai_action_queue_investigator_context(str(item["id"]), _fake_request()))

    # 2 other open/in-progress -- this item's own exception and the
    # resolved one are both excluded.
    assert result["other_open_exceptions_count"] == 2


def test_zero_other_open_exceptions_when_none_exist(monkeypatch):
    import backend.main as m
    client = _client()
    exc = _exception(client["id"])
    item = _caq_item(client["id"], exc["id"])
    monkeypatch.setattr(m, "_db_pool", FakePool(
        ai_action_queue=[item], clients=[client], compliance_exceptions=[exc],
    ))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(ai_action_queue_investigator_context(str(item["id"]), _fake_request()))

    assert result["other_open_exceptions_count"] == 0


def test_other_clients_exceptions_never_counted(monkeypatch):
    import backend.main as m
    client = _client("This Client")
    other_client = _client("Other Client")
    exc = _exception(client["id"])
    other_clients_exc = _exception(other_client["id"], status="Open")
    item = _caq_item(client["id"], exc["id"])
    monkeypatch.setattr(m, "_db_pool", FakePool(
        ai_action_queue=[item], clients=[client, other_client],
        compliance_exceptions=[exc, other_clients_exc],
    ))
    _as_current_user(monkeypatch, m, _partner())

    result = asyncio.run(ai_action_queue_investigator_context(str(item["id"]), _fake_request()))

    assert result["other_open_exceptions_count"] == 0
