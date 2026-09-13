"""
Unit tests for the AI Action Queue, Phase 1 (backend/main.py, 2026-09-13):
  AI_ACTION_QUEUE_LADDERS / AI_ACTION_QUEUE_GENERAL_LADDER / AI_ACTION_QUEUE_STAGES
  _escalation_ladder_for() -- firm-override-then-hardcoded-default resolution
  _ai_action_anchor_date() -- due_date-then-created_at fallback
  _run_ai_action_queue_scan() -- the core, directly-testable scheduler logic
  GET   /api/clients/{client_id}/ai-action-queue
  PATCH /api/ai-action-queue/{item_id}

THE guarantee this whole feature exists to enforce, called out explicitly
in the spec and re-stated as a literal comment above ai_action_queue's own
CREATE TABLE in run_migrations(): compliance_exceptions is the sole
authoritative record of compliance status. The scan (and the review
endpoint) may create/read/update ai_action_queue rows freely, but must
NEVER write to compliance_exceptions in any way, at any point, across any
number of escalation-threshold crossings. test_scheduler_never_mutates_
compliance_exceptions below is the specific test proving that.

Same FakeConnection/FakePool/_as_current_user/_fake_request convention as
tests/test_compliance_exceptions.py.
"""

import asyncio
import copy
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from backend.main import (
    FIRM_ID,
    AI_ACTION_QUEUE_LADDERS,
    AI_ACTION_QUEUE_GENERAL_LADDER,
    AI_ACTION_QUEUE_STAGES,
    AiActionQueueReview,
    _escalation_ladder_for,
    _ai_action_anchor_date,
    _run_ai_action_queue_scan,
    list_ai_action_queue_for_client,
    review_ai_action_queue_item,
)


class FakeConnection:
    def __init__(self, clients=None, compliance_exceptions=None, firm=None, ai_action_queue=None):
        self.clients = clients or []
        self.compliance_exceptions = compliance_exceptions or []
        self.firm = firm if firm is not None else {"id": FIRM_ID, "escalation_config": None,
                                                     "ai_action_queue_last_run_date": None}
        self.ai_action_queue = ai_action_queue or []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT escalation_config FROM firms WHERE id=$1"):
            return {"escalation_config": self.firm.get("escalation_config")}

        if q.startswith("SELECT ai_action_queue_last_run_date FROM firms WHERE id=$1"):
            return {"ai_action_queue_last_run_date": self.firm.get("ai_action_queue_last_run_date")}

        if q.startswith("SELECT full_name FROM clients WHERE id=$1 AND firm_id=$2"):
            cid, firm_id = args
            for c in self.clients:
                if c["id"] == cid and c["firm_id"] == firm_id:
                    return {"full_name": c["full_name"]}
            return None

        if q.startswith("SELECT * FROM clients WHERE id=$1 AND firm_id=$2"):
            cid, firm_id = args
            for c in self.clients:
                if c["id"] == cid and c["firm_id"] == firm_id:
                    return dict(c)
            return None

        if q.startswith("SELECT id FROM ai_action_queue WHERE compliance_exception_id=$1"):
            eid, action_type, level = args
            for a in self.ai_action_queue:
                if (a["compliance_exception_id"] == eid and a["action_type"] == action_type
                        and a["escalation_level"] == level and a.get("superseded_by") is None):
                    return {"id": a["id"]}
            return None

        if q.startswith("INSERT INTO ai_action_queue"):
            (firm_id, eid, cid, action_type, level, recommended_action, draft_text,
             draft_grounding_payload) = args
            row = {
                "id": uuid.uuid4(), "firm_id": firm_id, "compliance_exception_id": eid, "client_id": cid,
                "action_type": action_type, "escalation_level": level,
                "recommended_action": recommended_action, "draft_text": draft_text,
                "draft_grounding_payload": draft_grounding_payload, "status": "pending_review",
                "superseded_by": None, "created_at": datetime.now(timezone.utc),
                "reviewed_at": None, "reviewed_by": None,
            }
            self.ai_action_queue.append(row)
            return dict(row)

        if q.startswith("SELECT * FROM ai_action_queue WHERE id=$1 AND firm_id=$2"):
            iid, firm_id = args
            for a in self.ai_action_queue:
                if a["id"] == iid and a["firm_id"] == firm_id:
                    return dict(a)
            return None

        if q.startswith("UPDATE ai_action_queue SET") and "WHERE id=$1 AND firm_id=$" in q:
            m_ = re.search(r"SET (.+) WHERE id=\$1 AND firm_id=\$\d+", q)
            cols = re.findall(r"(\w+)=\$\d+", m_.group(1))
            iid = args[0]
            values = args[1:1 + len(cols)]
            firm_id = args[-1]
            for a in self.ai_action_queue:
                if a["id"] == iid and a["firm_id"] == firm_id:
                    for col, val in zip(cols, values):
                        a[col] = val
                    return dict(a)
            return None

        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT * FROM compliance_exceptions WHERE firm_id=$1 AND status IN"):
            firm_id, = args
            statuses = {"Open", "InProgress", "AwaitingClient"}
            return [dict(e) for e in self.compliance_exceptions
                    if e["firm_id"] == firm_id and e["status"] in statuses]

        if q.startswith("SELECT * FROM ai_action_queue WHERE client_id=$1 AND firm_id=$2"):
            cid, firm_id = args
            rows = [dict(a) for a in self.ai_action_queue if a["client_id"] == cid and a["firm_id"] == firm_id]
            return sorted(rows, key=lambda r: r["created_at"], reverse=True)

        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("UPDATE firms SET ai_action_queue_last_run_date=$1 WHERE id=$2"):
            run_date, firm_id = args
            if self.firm.get("id") == firm_id:
                self.firm["ai_action_queue_last_run_date"] = run_date
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
    row = {"id": client_id or uuid.uuid4(), "firm_id": FIRM_ID, "full_name": "Test Client"}
    row.update(overrides)
    return row


def _exception_row(client_id, issue_code, created_days_ago=0, due_date=None, status="Open", **overrides):
    row = {
        "id": uuid.uuid4(), "firm_id": FIRM_ID, "client_id": client_id,
        "issue_code": issue_code, "issue_label": issue_code.replace("_", " ").title(),
        "status": status, "due_date": due_date,
        "created_at": datetime.now(timezone.utc) - timedelta(days=created_days_ago),
        "updated_at": datetime.now(timezone.utc),
    }
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


# ── Ladder mapping (enumerated + confirmed in chat before this build) ──────

def test_pep_approval_required_ladder():
    assert AI_ACTION_QUEUE_LADDERS["PEP_APPROVAL_REQUIRED"] == {"flag": 1, "draft": 3, "partner_escalation": 5}


def test_cdd_review_outstanding_ladder():
    assert AI_ACTION_QUEUE_LADDERS["CDD_REVIEW_OUTSTANDING"] == {"flag": 3, "draft": 7, "partner_escalation": 14}


def test_general_ladder():
    assert AI_ACTION_QUEUE_GENERAL_LADDER == {"flag": 3, "draft": 7, "partner_escalation": 14}


@pytest.mark.parametrize("issue_code", [
    "CLIENT_TYPE_NOT_RECORDED", "IDENTITY_NOT_VERIFIED", "BENEFICIAL_OWNER_NOT_ASSESSED",
    "BENEFICIAL_OWNER_NOT_VERIFIED", "PEP_SCREENING_INCOMPLETE", "RISK_RATING_REQUIRED",
    "CONFLICT_CHECK_REQUIRED", "PEP_REVIEW_DUE",
])
def test_unmapped_issue_codes_fall_back_to_general_ladder(issue_code):
    """Every issue_code NOT explicitly given its own ladder -- including
    PEP_REVIEW_DUE, confirmed to stay on General rather than sharing
    PEP_APPROVAL_REQUIRED's ladder -- must fall back to General, not get
    silently invented special treatment."""
    assert issue_code not in AI_ACTION_QUEUE_LADDERS
    assert _escalation_ladder_for(issue_code, None) == AI_ACTION_QUEUE_GENERAL_LADDER


def test_stage_order_and_level_mapping():
    assert AI_ACTION_QUEUE_STAGES == [(1, "flag"), (2, "draft"), (3, "partner_escalation")]


# ── Anchor date ──────────────────────────────────────────────────────────

def test_anchor_date_prefers_due_date_when_set():
    exc = _exception_row(uuid.uuid4(), "CDD_REVIEW_OUTSTANDING", due_date=date(2026, 1, 1))
    assert _ai_action_anchor_date(exc) == date(2026, 1, 1)


def test_anchor_date_falls_back_to_created_at_when_due_date_is_null():
    exc = _exception_row(uuid.uuid4(), "CDD_REVIEW_OUTSTANDING", created_days_ago=5, due_date=None)
    assert _ai_action_anchor_date(exc) == exc["created_at"].date()


# ── Firm-level escalation_config override ───────────────────────────────

def test_firm_override_for_a_specific_issue_code():
    cfg = {"CDD_REVIEW_OUTSTANDING": {"flag": 1, "draft": 2, "partner_escalation": 3}}
    assert _escalation_ladder_for("CDD_REVIEW_OUTSTANDING", cfg) == cfg["CDD_REVIEW_OUTSTANDING"]
    # An issue_code the firm didn't override still gets the hardcoded default
    assert _escalation_ladder_for("PEP_APPROVAL_REQUIRED", cfg) == AI_ACTION_QUEUE_LADDERS["PEP_APPROVAL_REQUIRED"]


def test_firm_default_override_applies_only_to_unmapped_codes():
    cfg = {"_default": {"flag": 10, "draft": 20, "partner_escalation": 30}}
    assert _escalation_ladder_for("RISK_RATING_REQUIRED", cfg) == cfg["_default"]
    # A code with its own hardcoded ladder is untouched by a firm's "_default" override
    assert _escalation_ladder_for("CDD_REVIEW_OUTSTANDING", cfg) == AI_ACTION_QUEUE_LADDERS["CDD_REVIEW_OUTSTANDING"]


def test_partial_firm_override_is_ignored_not_merged():
    """A firm override missing any of the three stages doesn't get
    field-by-field merged with the hardcoded default -- that would let a
    firm's config silently revert one stage without them noticing."""
    cfg = {"CDD_REVIEW_OUTSTANDING": {"flag": 1}}  # missing draft/partner_escalation
    assert _escalation_ladder_for("CDD_REVIEW_OUTSTANDING", cfg) == AI_ACTION_QUEUE_LADDERS["CDD_REVIEW_OUTSTANDING"]


# ── The scan itself ──────────────────────────────────────────────────────

def test_no_action_before_threshold_is_reached(monkeypatch):
    import backend.main as m
    client = _client_row()
    exc = _exception_row(client["id"], "CDD_REVIEW_OUTSTANDING", created_days_ago=2)  # flag threshold is 3
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance_exceptions=[exc]))

    created = asyncio.run(_run_ai_action_queue_scan())
    assert created == []


def test_flag_action_created_exactly_on_threshold_day(monkeypatch):
    import backend.main as m
    client = _client_row()
    exc = _exception_row(client["id"], "CDD_REVIEW_OUTSTANDING", created_days_ago=3)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance_exceptions=[exc]))

    created = asyncio.run(_run_ai_action_queue_scan())
    assert len(created) == 1
    assert created[0]["action_type"] == "flag"
    assert created[0]["escalation_level"] == 1
    assert created[0]["draft_text"] is None  # nothing drafted at flag stage
    assert created[0]["status"] == "pending_review"


def test_old_exception_creates_every_stage_reached_at_once(monkeypatch):
    """An exception old enough to have skipped straight past flag/draft
    still gets a row for EACH stage -- each is independent evidence, not
    just the highest one reached."""
    import backend.main as m
    client = _client_row()
    exc = _exception_row(client["id"], "CDD_REVIEW_OUTSTANDING", created_days_ago=20)  # past all 3 thresholds
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance_exceptions=[exc]))

    created = asyncio.run(_run_ai_action_queue_scan())
    assert sorted(a["action_type"] for a in created) == ["draft", "flag", "partner_escalation"]
    assert created[1]["draft_text"] is not None  # 'draft' stage IS drafted
    assert created[2]["draft_text"] is not None  # so is partner_escalation


def test_pep_ladder_is_faster_than_cdd_ladder(monkeypatch):
    import backend.main as m
    client = _client_row()
    exc = _exception_row(client["id"], "PEP_APPROVAL_REQUIRED", created_days_ago=1)  # PEP flag threshold is 1
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance_exceptions=[exc]))

    created = asyncio.run(_run_ai_action_queue_scan())
    assert len(created) == 1 and created[0]["action_type"] == "flag"


def test_future_due_date_produces_no_action(monkeypatch):
    import backend.main as m
    client = _client_row()
    exc = _exception_row(client["id"], "CDD_REVIEW_OUTSTANDING",
                          due_date=date.today() + timedelta(days=30))
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance_exceptions=[exc]))

    created = asyncio.run(_run_ai_action_queue_scan())
    assert created == []


def test_resolved_exception_is_never_scanned(monkeypatch):
    import backend.main as m
    client = _client_row()
    exc = _exception_row(client["id"], "CDD_REVIEW_OUTSTANDING", created_days_ago=100, status="Resolved")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance_exceptions=[exc]))

    created = asyncio.run(_run_ai_action_queue_scan())
    assert created == []


def test_closed_no_further_action_exception_is_never_scanned(monkeypatch):
    import backend.main as m
    client = _client_row()
    exc = _exception_row(client["id"], "CDD_REVIEW_OUTSTANDING", created_days_ago=100,
                          status="ClosedNoFurtherAction")
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance_exceptions=[exc]))

    created = asyncio.run(_run_ai_action_queue_scan())
    assert created == []


def test_scan_is_idempotent_on_rerun_same_day(monkeypatch):
    import backend.main as m
    client = _client_row()
    exc = _exception_row(client["id"], "CDD_REVIEW_OUTSTANDING", created_days_ago=3)
    pool = FakePool(clients=[client], compliance_exceptions=[exc])
    monkeypatch.setattr(m, "_db_pool", pool)

    first = asyncio.run(_run_ai_action_queue_scan())
    second = asyncio.run(_run_ai_action_queue_scan())
    assert len(first) == 1
    assert second == []  # the real DB partial-unique-index would also reject this; app logic catches it first
    assert len(pool.conn.ai_action_queue) == 1


def test_superseded_row_allows_a_fresh_one_for_the_same_stage(monkeypatch):
    """A dismissed-then-superseded row must not permanently block that
    exact (exception, action_type, level) combination from ever getting a
    new row -- only a LIVE (superseded_by IS NULL) row does."""
    import backend.main as m
    client = _client_row()
    exc = _exception_row(client["id"], "CDD_REVIEW_OUTSTANDING", created_days_ago=3)
    pool = FakePool(clients=[client], compliance_exceptions=[exc])
    monkeypatch.setattr(m, "_db_pool", pool)

    first = asyncio.run(_run_ai_action_queue_scan())
    assert len(first) == 1
    pool.conn.ai_action_queue[0]["superseded_by"] = uuid.uuid4()  # simulate a re-draft superseding it

    second = asyncio.run(_run_ai_action_queue_scan())
    assert len(second) == 1  # a fresh live row for the same stage is allowed again
    assert len(pool.conn.ai_action_queue) == 2


def test_grounding_payload_preserves_due_date_and_calendar_day_age(monkeypatch):
    import backend.main as m
    client = _client_row(full_name="Anchorflow Holdings")
    due = date.today() - timedelta(days=10)
    exc = _exception_row(client["id"], "CDD_REVIEW_OUTSTANDING", due_date=due)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance_exceptions=[exc]))

    created = asyncio.run(_run_ai_action_queue_scan())
    payload = created[0]["draft_grounding_payload"]
    assert payload["due_date"] == due.isoformat()
    assert payload["anchor_date"] == due.isoformat()
    assert payload["anchor_source"] == "due_date"
    assert payload["days_overdue"] == 10
    assert payload["day_basis"] == "calendar"  # not business days, per the 2026-09-13 correction
    assert payload["client_name"] == "Anchorflow Holdings"
    assert payload["issue_code"] == "CDD_REVIEW_OUTSTANDING"
    assert payload["requested_items"] == [exc["issue_label"]]


# ── THE guardrail: escalation must never mutate compliance_exceptions ──────

def test_scheduler_never_mutates_compliance_exceptions(monkeypatch):
    """Runs the scan across multiple escalation-threshold crossings for the
    SAME exception (flag -> draft -> partner_escalation, one simulated
    'today' per crossing) and asserts compliance_exceptions is
    byte-for-byte unchanged after every single one. Escalation may only
    ever create/read ai_action_queue rows -- compliance_exceptions itself
    must never be touched, not even its updated_at."""
    import backend.main as m
    client = _client_row()
    exc = _exception_row(client["id"], "CDD_REVIEW_OUTSTANDING", created_days_ago=0)
    pool = FakePool(clients=[client], compliance_exceptions=[exc])
    monkeypatch.setattr(m, "_db_pool", pool)

    anchor = exc["created_at"].date()
    snapshot_before_any_scan = copy.deepcopy(pool.conn.compliance_exceptions)

    # One simulated day per crossing: day 3 (flag), day 7 (draft), day 14
    # (partner_escalation), and a few in-between/after days for good measure.
    for offset in (1, 2, 3, 4, 6, 7, 8, 13, 14, 15, 30):
        simulated_today = anchor + timedelta(days=offset)
        asyncio.run(_run_ai_action_queue_scan(simulated_today))
        assert pool.conn.compliance_exceptions == snapshot_before_any_scan, (
            f"compliance_exceptions mutated after scanning at day {offset}"
        )

    # Sanity: the scan actually did something across those crossings (a
    # guardrail test that passes only because nothing ever ran proves
    # nothing) -- all three stages should have been created by day 30.
    assert len(pool.conn.ai_action_queue) == 3


def test_review_endpoint_never_mutates_compliance_exceptions(monkeypatch):
    """Same guardrail, exercised through the human-review endpoint this
    time rather than the scheduler -- approving/editing/dismissing an
    ai_action_queue item must not touch compliance_exceptions either."""
    import backend.main as m
    client = _client_row()
    exc = _exception_row(client["id"], "CDD_REVIEW_OUTSTANDING", created_days_ago=3)
    pool = FakePool(clients=[client], compliance_exceptions=[exc])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner())

    created = asyncio.run(_run_ai_action_queue_scan())
    snapshot = copy.deepcopy(pool.conn.compliance_exceptions)

    asyncio.run(review_ai_action_queue_item(
        str(created[0]["id"]), AiActionQueueReview(status="approved"), _fake_request()
    ))
    assert pool.conn.compliance_exceptions == snapshot


# ── Human review endpoint ────────────────────────────────────────────────

def test_review_approves_and_records_reviewer(monkeypatch):
    import backend.main as m
    client = _client_row()
    exc = _exception_row(client["id"], "CDD_REVIEW_OUTSTANDING", created_days_ago=3)
    pool = FakePool(clients=[client], compliance_exceptions=[exc])
    monkeypatch.setattr(m, "_db_pool", pool)
    user = _partner()
    _as_current_user(monkeypatch, m, user)

    created = asyncio.run(_run_ai_action_queue_scan())
    result = asyncio.run(review_ai_action_queue_item(
        str(created[0]["id"]), AiActionQueueReview(status="approved"), _fake_request()
    ))

    assert result["status"] == "approved"
    assert result["reviewed_at"] is not None
    assert result["reviewed_by"] == str(user["id"])


def test_review_edited_updates_draft_text(monkeypatch):
    import backend.main as m
    client = _client_row()
    exc = _exception_row(client["id"], "CDD_REVIEW_OUTSTANDING", created_days_ago=20)  # reaches 'draft' stage
    pool = FakePool(clients=[client], compliance_exceptions=[exc])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner())

    created = asyncio.run(_run_ai_action_queue_scan())
    draft_item = next(a for a in created if a["action_type"] == "draft")

    result = asyncio.run(review_ai_action_queue_item(
        str(draft_item["id"]), AiActionQueueReview(status="edited", draft_text="Edited by a human"),
        _fake_request()
    ))
    assert result["status"] == "edited"
    assert result["draft_text"] == "Edited by a human"


def test_review_rejects_invalid_status(monkeypatch):
    import backend.main as m
    client = _client_row()
    exc = _exception_row(client["id"], "CDD_REVIEW_OUTSTANDING", created_days_ago=3)
    pool = FakePool(clients=[client], compliance_exceptions=[exc])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner())

    created = asyncio.run(_run_ai_action_queue_scan())
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(review_ai_action_queue_item(
            str(created[0]["id"]), AiActionQueueReview(status="not_a_real_status"), _fake_request()
        ))
    assert exc_info.value.status_code == 422


def test_review_missing_item_returns_404(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, _partner())
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(review_ai_action_queue_item(
            str(uuid.uuid4()), AiActionQueueReview(status="approved"), _fake_request()
        ))
    assert exc_info.value.status_code == 404


# ── List endpoint ────────────────────────────────────────────────────────

def test_list_returns_items_for_the_right_client_only(monkeypatch):
    import backend.main as m
    client_a = _client_row()
    client_b = _client_row()
    exc_a = _exception_row(client_a["id"], "CDD_REVIEW_OUTSTANDING", created_days_ago=3)
    exc_b = _exception_row(client_b["id"], "PEP_APPROVAL_REQUIRED", created_days_ago=1)
    pool = FakePool(clients=[client_a, client_b], compliance_exceptions=[exc_a, exc_b])
    monkeypatch.setattr(m, "_db_pool", pool)
    _as_current_user(monkeypatch, m, _partner())

    asyncio.run(_run_ai_action_queue_scan())
    result = asyncio.run(list_ai_action_queue_for_client(str(client_a["id"]), _fake_request()))

    assert len(result) == 1
    assert result[0]["client_id"] == str(client_a["id"])


def test_list_missing_client_returns_404(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[]))
    _as_current_user(monkeypatch, m, _partner())
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(list_ai_action_queue_for_client(str(uuid.uuid4()), _fake_request()))
    assert exc_info.value.status_code == 404
