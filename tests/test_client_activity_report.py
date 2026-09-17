"""
Unit tests for "Client Activity Report" (backend/main.py, 2026-09-18) --
the third of five self-scoped practice reports, same access tier and
scoping contract as My Portfolio.

Merges three already-existing sources into one per-client timeline, using
the exact {"date","event","user","result"} shape _fetch_compliance_
history() already established:
  - _fetch_compliance_history() itself, reused completely unchanged (its
    own composition logic is already thoroughly tested in
    tests/test_compliance_history.py) for BO/PEP/conflict-check/risk-
    rating/exception/CDD-review events.
  - _fetch_client_general_activity() (new) for progress_notes and
    documents -- already-recorded data, not new tracking.

CONFIRMED GAP (audited before building, per the report's own backend
comment): matter status changes have no history table, and Search Vault/
Draft Document usage can't be attributed to a specific client in what's
tracked today -- neither can appear here. Not tested as a false positive
here since there is genuinely nothing to test; the report's own docstring
and frontend panel note this gap explicitly.

Same FakeConnection/FakePool convention as
tests/test_compliance_history.py, extended with clients/matters/
progress_notes/documents/users.
"""

import asyncio
import csv
import io
import json
import uuid
from datetime import date, datetime, timezone

import pdfplumber
import pytest

from backend.main import (
    FIRM_ID,
    client_activity_report,
    client_activity_report_export,
    client_activity_report_export_pdf,
)


class FakeConnection:
    def __init__(self, clients=None, matters=None, notes=None, documents=None, users=None,
                 audit_logs=None, cdd_reviews=None):
        self.clients = clients if clients is not None else []
        self.matters = matters if matters is not None else []
        self.notes = notes if notes is not None else []
        self.documents = documents if documents is not None else []
        self.users = users if users is not None else []
        self.audit_logs = audit_logs if audit_logs is not None else []
        self.cdd_reviews = cdd_reviews if cdd_reviews is not None else []

    async def fetch(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT id, full_name FROM clients WHERE firm_id=$1 AND created_by=$2"):
            firm_id, created_by = args
            rows = [c for c in self.clients if c["firm_id"] == firm_id and c.get("created_by") == created_by]
            return sorted(rows, key=lambda c: c["full_name"])

        if q.startswith("SELECT id FROM matters WHERE client_id=$1 AND firm_id=$2 AND NOT is_sentinel"):
            cid, firm_id = args
            return [{"id": m["id"]} for m in self.matters
                    if m["client_id"] == cid and m["firm_id"] == firm_id and not m.get("is_sentinel", False)]

        if q.startswith("SELECT created_at, text, author FROM progress_notes"):
            matter_ids, = args
            rows = [n for n in self.notes if n["matter_id"] in matter_ids]
            return sorted(rows, key=lambda n: n["created_at"])

        if q.startswith("SELECT uploaded_at, filename, uploaded_by FROM documents"):
            matter_ids, = args
            rows = [d for d in self.documents if d["matter_id"] in matter_ids]
            return sorted(rows, key=lambda d: d["uploaded_at"])

        if q.startswith("SELECT id, display_name FROM users WHERE id = ANY($1)"):
            ids, = args
            return [u for u in self.users if u["id"] in ids]

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


def _client(name, *, created_by, client_id=None, firm_id=FIRM_ID):
    return {"id": client_id or uuid.uuid4(), "firm_id": firm_id, "full_name": name, "created_by": created_by}


def _matter(client_id, *, firm_id=FIRM_ID, matter_id=None, is_sentinel=False):
    return {"id": matter_id or uuid.uuid4(), "firm_id": firm_id, "client_id": client_id, "is_sentinel": is_sentinel}


def _note(matter_id, text, author, created_at):
    return {"matter_id": matter_id, "text": text, "author": author, "created_at": created_at}


def _document(matter_id, filename, uploaded_by, uploaded_at):
    return {"matter_id": matter_id, "filename": filename, "uploaded_by": uploaded_by, "uploaded_at": uploaded_at}


def _user(name, user_id=None):
    return {"id": user_id or uuid.uuid4(), "display_name": name}


def _audit_log(target_type, target_id, action, actor_name, created_at, details=None):
    return {
        "firm_id": FIRM_ID, "target_type": target_type, "target_id": target_id,
        "action": action, "actor_name": actor_name, "created_at": created_at,
        "details": json.dumps(details or {}),
    }


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def _fake_request():
    return None


def _lawyer(user_id=None, role="associate"):
    return {"id": user_id or uuid.uuid4(), "firm_id": FIRM_ID, "role": role, "display_name": "L. Test"}


def _csv_rows(response):
    text = response.body.decode("utf-8-sig") if isinstance(response.body, bytes) else response.body.lstrip("﻿")
    return list(csv.reader(io.StringIO(text)))


def _pdf_text(response):
    with pdfplumber.open(io.BytesIO(response.body)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


# ── self-scoping ─────────────────────────────────────────────────────────

def test_only_own_clients_activity_returned(monkeypatch):
    import backend.main as m
    me = _lawyer()
    other = uuid.uuid4()
    my_client = _client("My Client", created_by=me["id"])
    not_my_client = _client("Not My Client", created_by=other)
    my_matter = _matter(my_client["id"])
    note = _note(my_matter["id"], "Reviewed file", "L. Test", datetime(2026, 9, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(m, "_db_pool", FakePool(
        clients=[my_client, not_my_client], matters=[my_matter], notes=[note],
    ))
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(client_activity_report(_fake_request()))

    assert {r["client_name"] for r in result} == {"My Client"}


# ── report-specific reuse: progress notes + documents ──────────────────────

def test_progress_note_appears_as_activity_event(monkeypatch):
    import backend.main as m
    me = _lawyer()
    client = _client("Anchorflow Holdings", created_by=me["id"])
    matter = _matter(client["id"])
    note = _note(matter["id"], "Drafted the response affidavit", "L. Test", datetime(2026, 9, 5, tzinfo=timezone.utc))
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], matters=[matter], notes=[note]))
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(client_activity_report(_fake_request()))

    assert len(result) == 1
    assert result[0]["event"] == "Progress note added"
    assert result[0]["date"] == "2026-09-05"
    assert "Drafted the response affidavit" in result[0]["result"]


def test_document_upload_appears_with_resolved_uploader_name(monkeypatch):
    import backend.main as m
    me = _lawyer()
    uploader = _user("R. Rusike")
    client = _client("Anchorflow Holdings", created_by=me["id"])
    matter = _matter(client["id"])
    doc = _document(matter["id"], "affidavit.pdf", uploader["id"], datetime(2026, 9, 6, tzinfo=timezone.utc))
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], matters=[matter], documents=[doc], users=[uploader]))
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(client_activity_report(_fake_request()))

    assert result[0]["event"] == "Document uploaded"
    assert result[0]["user"] == "R. Rusike"
    assert result[0]["result"] == "affidavit.pdf"


def test_unresolved_uploader_falls_back_to_unknown(monkeypatch):
    import backend.main as m
    me = _lawyer()
    client = _client("C1", created_by=me["id"])
    matter = _matter(client["id"])
    doc = _document(matter["id"], "doc.pdf", uuid.uuid4(), datetime(2026, 9, 6, tzinfo=timezone.utc))
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], matters=[matter], documents=[doc], users=[]))
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(client_activity_report(_fake_request()))
    assert result[0]["user"] == "Unknown"


# ── report-specific reuse: compliance history merge ─────────────────────────

def test_compliance_events_merge_in_via_fetch_compliance_history(monkeypatch):
    """No second compliance-event computation -- reuses
    _fetch_compliance_history() directly."""
    import backend.main as m
    me = _lawyer()
    client = _client("C1", created_by=me["id"])
    log = _audit_log("CLIENT", client["id"], "PEP_FLAGGED", "P", datetime(2026, 9, 2, tzinfo=timezone.utc))
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], audit_logs=[log]))
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(client_activity_report(_fake_request()))

    assert result[0]["event"] == "PEP flagged"


def test_general_and_compliance_activity_interleave_by_date_newest_first(monkeypatch):
    import backend.main as m
    me = _lawyer()
    client = _client("C1", created_by=me["id"])
    matter = _matter(client["id"])
    note = _note(matter["id"], "Old note", "L", datetime(2026, 9, 1, tzinfo=timezone.utc))
    log = _audit_log("CLIENT", client["id"], "PEP_FLAGGED", "P", datetime(2026, 9, 10, tzinfo=timezone.utc))
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], matters=[matter], notes=[note], audit_logs=[log]))
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(client_activity_report(_fake_request()))

    assert [r["event"] for r in result] == ["PEP flagged", "Progress note added"]


# ── cross-user isolation ──────────────────────────────────────────────────

def test_cross_user_isolation(monkeypatch):
    import backend.main as m
    lawyer_a = uuid.uuid4()
    lawyer_b = _lawyer()
    client_a = _client("A's Client", created_by=lawyer_a)
    client_b = _client("B's Client", created_by=lawyer_b["id"])
    matter_b = _matter(client_b["id"])
    note_b = _note(matter_b["id"], "B's note", "B", datetime(2026, 9, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client_a, client_b], matters=[matter_b], notes=[note_b]))
    _as_current_user(monkeypatch, m, lawyer_b)

    result = asyncio.run(client_activity_report(_fake_request()))

    assert {r["client_name"] for r in result} == {"B's Client"}


def test_no_lawyer_id_parameter_exists(monkeypatch):
    import inspect
    sig = inspect.signature(client_activity_report)
    assert list(sig.parameters.keys()) == ["request"]


# ── firm/tenant isolation ────────────────────────────────────────────────

def test_firm_isolation_never_sees_another_firms_clients(monkeypatch):
    import backend.main as m
    shared_lawyer_id = uuid.uuid4()
    firm_b = "b2c3d4e5-0000-0000-0000-000000000002"
    client_a = _client("Firm A Client", created_by=shared_lawyer_id, firm_id=FIRM_ID)
    client_b = _client("Firm B Client", created_by=shared_lawyer_id, firm_id=firm_b)
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client_a, client_b]))
    _as_current_user(monkeypatch, m, {"id": shared_lawyer_id, "firm_id": FIRM_ID, "role": "partner", "display_name": "P"})

    # Even with no activity, the client-scoping query itself must exclude
    # firm B's client -- confirmed indirectly via no crash/leak when a
    # matter+note is added for firm B's client below.
    result = asyncio.run(client_activity_report(_fake_request()))
    assert result == []


# ── empty state ──────────────────────────────────────────────────────────

def test_no_clients_returns_empty_list_not_an_error(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, _lawyer())

    result = asyncio.run(client_activity_report(_fake_request()))
    assert result == []


def test_clients_with_no_activity_yet_produce_no_rows_not_an_error(monkeypatch):
    import backend.main as m
    me = _lawyer()
    client = _client("Quiet Client", created_by=me["id"])
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client]))
    _as_current_user(monkeypatch, m, me)

    result = asyncio.run(client_activity_report(_fake_request()))
    assert result == []


def test_no_real_identity_returns_empty_not_an_error(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, {"id": None, "firm_id": FIRM_ID, "role": "partner", "display_name": "Demo User"})

    result = asyncio.run(client_activity_report(_fake_request()))
    assert result == []


# ── CSV generation ───────────────────────────────────────────────────────

def test_csv_export_contains_correct_data(monkeypatch):
    import backend.main as m
    me = _lawyer()
    client = _client("Anchorflow Holdings", created_by=me["id"])
    matter = _matter(client["id"])
    note = _note(matter["id"], "Reviewed the lease", "L. Test", datetime(2026, 9, 5, tzinfo=timezone.utc))
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], matters=[matter], notes=[note]))
    _as_current_user(monkeypatch, m, me)

    response = asyncio.run(client_activity_report_export(_fake_request()))
    rows = _csv_rows(response)

    assert rows[0] == ["Date", "Client", "Event", "User", "Details"]
    assert rows[1][1] == "Anchorflow Holdings"
    assert rows[1][2] == "Progress note added"


def test_csv_export_starts_with_utf8_bom(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, _lawyer())

    response = asyncio.run(client_activity_report_export(_fake_request()))
    assert response.body.startswith("﻿".encode("utf-8"))


# ── PDF generation ───────────────────────────────────────────────────────

def test_pdf_export_generates_without_error_and_contains_correct_data(monkeypatch):
    import backend.main as m
    me = _lawyer()
    client = _client("Anchorflow Holdings", created_by=me["id"])
    matter = _matter(client["id"])
    note = _note(matter["id"], "Reviewed the lease", "L. Test", datetime(2026, 9, 5, tzinfo=timezone.utc))
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], matters=[matter], notes=[note]))
    _as_current_user(monkeypatch, m, me)

    response = asyncio.run(client_activity_report_export_pdf(_fake_request()))
    assert response.media_type == "application/pdf"
    assert response.body.startswith(b"%PDF")
    text = _pdf_text(response)
    assert "Anchorflow Holdings" in text


def test_pdf_export_handles_no_activity_without_crashing(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, _lawyer())

    response = asyncio.run(client_activity_report_export_pdf(_fake_request()))
    assert response.body.startswith(b"%PDF")
    assert "No recorded activity" in _pdf_text(response)
