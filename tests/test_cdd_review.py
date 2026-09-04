"""
Unit tests for CDD Review (backend/main.py, 2026-09-04):
  POST /api/clients/{client_id}/cdd-review
  _cdd_review_status() -- the Complete/Outstanding rule
  _fetch_last_cdd_review_date() -- surfaced on GET .../compliance and the
    Individual Client AML/CDD Report's overall section

A CDD Review is an EVENT (a real, dated, retained record of an actual
compliance re-check), not a field -- see cdd_reviews' own migration
comment in backend/main.py. Deliberately distinct from
matters.last_reviewed_date (30-day operational safety net) and
client_compliance.risk_rating (the client's current, live rating) -- a
review's own risk_rating is a point-in-time snapshot.

Same FakeConnection/FakePool/_as_current_user/_fake_request convention as
tests/test_client_compliance.py.
"""

import asyncio
import uuid
from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException

from backend.main import (
    FIRM_ID,
    CDDReviewCreate,
    create_cdd_review,
    _cdd_review_status,
)


class FakeConnection:
    def __init__(self, clients, cdd_reviews=None):
        self.clients = clients
        self.cdd_reviews = cdd_reviews if cdd_reviews is not None else []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT * FROM clients WHERE id=$1 AND firm_id=$2"):
            cid, firm_id = args
            for c in self.clients:
                if c["id"] == cid and c["firm_id"] == firm_id:
                    return dict(c)
            return None

        if q.startswith("INSERT INTO cdd_reviews"):
            cols = [c.strip() for c in q.split("(", 1)[1].split(")", 1)[0].split(",")]
            row = dict(zip(cols, args))
            row["id"] = uuid.uuid4()
            row["created_at"] = datetime.now(timezone.utc)
            self.cdd_reviews.append(row)
            return dict(row)

        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

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


def _client_row(client_id=None, **overrides):
    row = {"id": client_id or uuid.uuid4(), "firm_id": FIRM_ID, "full_name": "Test Client"}
    row.update(overrides)
    return row


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def _fake_request():
    return None


def _partner(user_id=None):
    return {"id": user_id or uuid.uuid4(), "firm_id": FIRM_ID, "role": "partner", "display_name": "P. Chademana"}


# ── _cdd_review_status() -- the Complete/Outstanding rule, pure ────────────

def test_status_complete_when_no_changes_identified():
    assert _cdd_review_status(None, None) == "Complete"


def test_status_complete_when_changes_identified_but_no_further_action_is_still_outstanding():
    assert _cdd_review_status("Client moved address", None) == "Outstanding"


def test_status_complete_when_changes_identified_and_further_action_recorded():
    """The user's own "requires resolution before clearing" rule: changes
    alone are outstanding; recording what's being done about them is what
    clears it."""
    assert _cdd_review_status("Client moved address", "Updated file, re-verified new address") == "Complete"


def test_status_complete_when_further_action_set_but_no_changes_identified():
    """No real problem was ever flagged -- further_action alone (e.g. a
    routine note) doesn't manufacture an Outstanding review."""
    assert _cdd_review_status(None, "Nothing further needed") == "Complete"


# ── POST /api/clients/{id}/cdd-review ───────────────────────────────────────

def test_creating_a_review_writes_correctly(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    user = _partner()
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[_client_row(client_id)]))
    _as_current_user(monkeypatch, m, user)

    review = CDDReviewCreate(
        review_date="2026-09-04", info_current=True, bo_current=True,
        matter_activity_consistent=True, risk_rating="Medium",
    )
    result = asyncio.run(create_cdd_review(str(client_id), review, _fake_request()))

    assert result["client_id"] == str(client_id)
    assert result["review_date"] == "2026-09-04"
    assert result["info_current"] is True
    assert result["bo_current"] is True
    assert result["matter_activity_consistent"] is True
    assert result["risk_rating"] == "Medium"
    assert result["changes_identified"] is None
    assert result["further_action"] is None
    assert result["status"] == "Complete"
    assert result["reviewed_by"] == str(user["id"])


def test_creating_a_review_with_unresolved_changes_is_outstanding(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[_client_row(client_id)]))
    _as_current_user(monkeypatch, m, _partner())

    review = CDDReviewCreate(
        info_current=False, bo_current=True, matter_activity_consistent=True,
        risk_rating="High", changes_identified="Client is now a director of a new company",
    )
    result = asyncio.run(create_cdd_review(str(client_id), review, _fake_request()))

    assert result["status"] == "Outstanding"
    assert result["changes_identified"] == "Client is now a director of a new company"
    assert result["further_action"] is None


def test_creating_a_review_with_changes_and_recorded_action_is_complete(monkeypatch):
    """Outstanding status correctly requires resolution before clearing --
    the same changes_identified text, but WITH a further_action recorded,
    clears to Complete at creation."""
    import backend.main as m
    client_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[_client_row(client_id)]))
    _as_current_user(monkeypatch, m, _partner())

    review = CDDReviewCreate(
        info_current=False, bo_current=True, matter_activity_consistent=True,
        risk_rating="High", changes_identified="Client is now a director of a new company",
        further_action="Updated beneficial ownership records, re-verified.",
    )
    result = asyncio.run(create_cdd_review(str(client_id), review, _fake_request()))

    assert result["status"] == "Complete"


def test_review_date_defaults_to_today_when_omitted(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[_client_row(client_id)]))
    _as_current_user(monkeypatch, m, _partner())

    review = CDDReviewCreate(info_current=True, bo_current=True, matter_activity_consistent=True, risk_rating="Low")
    result = asyncio.run(create_cdd_review(str(client_id), review, _fake_request()))

    assert result["review_date"] == date.today().isoformat()


def test_blank_changes_identified_string_is_treated_as_none(monkeypatch):
    """Whitespace-only text from the form shouldn't count as "changes
    identified" and force an Outstanding status."""
    import backend.main as m
    client_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[_client_row(client_id)]))
    _as_current_user(monkeypatch, m, _partner())

    review = CDDReviewCreate(
        info_current=True, bo_current=True, matter_activity_consistent=True,
        risk_rating="Low", changes_identified="   ",
    )
    result = asyncio.run(create_cdd_review(str(client_id), review, _fake_request()))

    assert result["changes_identified"] is None
    assert result["status"] == "Complete"


def test_invalid_risk_rating_rejected(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[_client_row(client_id)]))
    _as_current_user(monkeypatch, m, _partner())

    review = CDDReviewCreate(
        info_current=True, bo_current=True, matter_activity_consistent=True, risk_rating="Severe",
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(create_cdd_review(str(client_id), review, _fake_request()))
    assert exc_info.value.status_code == 422


def test_invalid_review_date_format_rejected(monkeypatch):
    import backend.main as m
    client_id = uuid.uuid4()
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[_client_row(client_id)]))
    _as_current_user(monkeypatch, m, _partner())

    review = CDDReviewCreate(
        review_date="04/09/2026", info_current=True, bo_current=True,
        matter_activity_consistent=True, risk_rating="Low",
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(create_cdd_review(str(client_id), review, _fake_request()))
    assert exc_info.value.status_code == 400


def test_missing_client_returns_404(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[]))
    _as_current_user(monkeypatch, m, _partner())

    review = CDDReviewCreate(info_current=True, bo_current=True, matter_activity_consistent=True, risk_rating="Low")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(create_cdd_review(str(uuid.uuid4()), review, _fake_request()))
    assert exc_info.value.status_code == 404
