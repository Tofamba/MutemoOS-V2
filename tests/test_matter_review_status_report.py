"""
Unit tests for the Matter Review Status report (backend/main.py, 2026-08-30):
  - GET /api/reports/matter-review-status — JSON, every matter's review
    state + last real activity, sorted next_review_date ASC NULLS FIRST.
  - GET /api/reports/matter-review-status-export — same data/filters/
    permission, CSV download (same convention as the RBZ export).

Complements _get_review_matters_for_digest (tested in
test_matter_review_safety_net.py) -- that's the due/overdue-only nudge
digest; this report shows EVERY matter regardless of status, for an
on-demand full-caseload audit.

Called directly as plain async functions, same convention as
tests/test_rbz_compliance_export.py (whose FakeConnection/FakePool/
_as_current_user/_fake_request this file mirrors) and
tests/test_matter_review_safety_net.py (whose matter-row shape this
file mirrors).
"""

import asyncio
import csv
import io
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from backend.main import (
    FIRM_ID,
    matter_review_status_report,
    matter_review_status_report_export,
)


class FakeConnection:
    def __init__(self, matters=None, notes=None, documents=None, clients=None, users=None):
        self.matters = matters if matters is not None else []
        self.notes = notes if notes is not None else []
        self.documents = documents if documents is not None else []
        self.clients = clients if clients is not None else []
        self.users = users if users is not None else []

    async def fetch(self, query, *args):
        q = " ".join(query.split())

        if q.startswith("SELECT m.id, m.name, m.number, m.matter_number"):
            # args[0] is always firm_id; any further args are the WHERE
            # filters in the order _fetch_matter_review_status_rows added
            # them (lawyer_id, then client_id, then status) -- read them
            # back out positionally the same way the real WHERE clause
            # would apply them, rather than re-parsing SQL.
            firm_id = args[0]
            filters = list(args[1:])
            rows = [m for m in self.matters if m["firm_id"] == firm_id and not m.get("is_sentinel")]
            # Filters are applied conditionally based on which WHERE fragments
            # the real query actually built, consuming `filters` positionally
            # in the same order _fetch_matter_review_status_rows appends them.
            if "m.created_by=$" in q:
                lawyer_id = filters.pop(0)
                rows = [m for m in rows if m.get("created_by") == lawyer_id]
            if "m.client_id=$" in q:
                client_id = filters.pop(0)
                rows = [m for m in rows if m.get("client_id") == client_id]
            if "m.status=$" in q:
                status = filters.pop(0)
                rows = [m for m in rows if m.get("status") == status]

            clients_by_id = {c["id"]: c for c in self.clients}
            users_by_id = {u["id"]: u for u in self.users}
            out = []
            for m in rows:
                client = clients_by_id.get(m.get("client_id"))
                creator = users_by_id.get(m.get("created_by"))
                out.append({
                    "id": m["id"], "name": m["name"], "number": m.get("number"),
                    "matter_number": m.get("matter_number"), "client_id": m.get("client_id"),
                    "client_name": m.get("client_name"), "status": m.get("status"),
                    "next_review_date": m.get("next_review_date"),
                    "last_reviewed_date": m.get("last_reviewed_date"),
                    "last_activity": m.get("last_activity"), "created_at": m.get("created_at"),
                    "created_by": m.get("created_by"),
                    "client_full_name": client["full_name"] if client else None,
                    "created_by_name": creator["display_name"] if creator else None,
                })
            # Mirrors the real ORDER BY: next_review_date ASC NULLS FIRST, name ASC
            out.sort(key=lambda r: (r["next_review_date"] is not None, r["next_review_date"] or date.min, r["name"]))
            return out

        if q.startswith("SELECT matter_id, text, created_at FROM progress_notes"):
            matter_ids, = args
            rows = [n for n in self.notes if n["matter_id"] in matter_ids]
            rows.sort(key=lambda n: (n["matter_id"], n["created_at"]), reverse=True)
            return rows

        if q.startswith("SELECT matter_id, filename, uploaded_at FROM documents"):
            matter_ids, = args
            rows = [d for d in self.documents if d["matter_id"] in matter_ids and d.get("status", "complete") == "complete"]
            rows.sort(key=lambda d: (d["matter_id"], d["uploaded_at"]), reverse=True)
            return rows

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


def _matter(name, *, status="Active", next_review_date=None, last_reviewed_date=None,
            last_activity=None, created_at=None, client_id=None, client_name=None,
            created_by=None, matter_number=None, is_sentinel=False):
    return {
        "id": uuid.uuid4(), "firm_id": FIRM_ID, "name": name, "number": None,
        "matter_number": matter_number, "status": status,
        "next_review_date": next_review_date, "last_reviewed_date": last_reviewed_date,
        "last_activity": last_activity, "created_at": created_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
        "client_id": client_id, "client_name": client_name, "created_by": created_by,
        "is_sentinel": is_sentinel,
    }


def _note(matter_id, text, created_at):
    return {"matter_id": matter_id, "text": text, "created_at": created_at}


def _doc(matter_id, filename, uploaded_at, status="complete"):
    return {"matter_id": matter_id, "filename": filename, "uploaded_at": uploaded_at, "status": status}


def _client(name):
    return {"id": uuid.uuid4(), "full_name": name}


def _user(name):
    return {"id": uuid.uuid4(), "display_name": name}


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def _fake_request():
    return None


def _csv_rows(response):
    text = response.body.decode("utf-8") if isinstance(response.body, bytes) else response.body
    return list(csv.reader(io.StringIO(text)))


# ── permission gate ──────────────────────────────────────────────────────

def test_associate_gets_403(monkeypatch):
    import backend.main as m
    associate = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "associate", "display_name": "Assoc"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, associate)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(matter_review_status_report(_fake_request()))
    assert exc_info.value.status_code == 403


def test_secretary_gets_403(monkeypatch):
    import backend.main as m
    secretary = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "secretary", "display_name": "Sec"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, secretary)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(matter_review_status_report(_fake_request()))
    assert exc_info.value.status_code == 403


def test_export_associate_gets_403(monkeypatch):
    import backend.main as m
    associate = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "associate", "display_name": "Assoc"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, associate)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(matter_review_status_report_export(_fake_request()))
    assert exc_info.value.status_code == 403


# ── next_review_date / last_reviewed_date reporting ─────────────────────
# "AUTH_ENABLED=False's synthetic dev user is role=partner by default" —
# same as test_rbz_compliance_export.py — no monkeypatch needed for these.

def test_never_reviewed_matter_reports_none_set(monkeypatch):
    import backend.main as m
    matter = _matter("Estate of Chikafu", next_review_date=None, last_reviewed_date=None)
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter]))

    rows = asyncio.run(matter_review_status_report(_fake_request()))

    assert len(rows) == 1
    assert rows[0]["next_review_date"] is None
    assert rows[0]["last_reviewed_date"] is None


def test_recently_reviewed_matter_reports_correct_dates(monkeypatch):
    import backend.main as m
    today = date.today()
    matter = _matter("Vengesai Lease Dispute", next_review_date=today + timedelta(days=30),
                      last_reviewed_date=today)
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter]))

    rows = asyncio.run(matter_review_status_report(_fake_request()))

    assert rows[0]["next_review_date"] == (today + timedelta(days=30)).isoformat()
    assert rows[0]["last_reviewed_date"] == today.isoformat()


def test_overdue_and_future_matters_both_reported_correctly(monkeypatch):
    import backend.main as m
    today = date.today()
    overdue = _matter("Overdue Matter", next_review_date=today - timedelta(days=10))
    future = _matter("Future Matter", next_review_date=today + timedelta(days=60))
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[future, overdue]))  # insertion order deliberately reversed

    rows = asyncio.run(matter_review_status_report(_fake_request()))
    by_name = {r["matter_name"]: r for r in rows}

    assert by_name["Overdue Matter"]["next_review_date"] == (today - timedelta(days=10)).isoformat()
    assert by_name["Future Matter"]["next_review_date"] == (today + timedelta(days=60)).isoformat()


# ── sort order: NULLS FIRST, then ascending ──────────────────────────────

def test_sorted_never_set_first_then_overdue_then_future(monkeypatch):
    import backend.main as m
    today = date.today()
    never_set = _matter("Never Set", next_review_date=None)
    future = _matter("Future", next_review_date=today + timedelta(days=60))
    overdue = _matter("Overdue", next_review_date=today - timedelta(days=5))
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[future, never_set, overdue]))

    rows = asyncio.run(matter_review_status_report(_fake_request()))

    assert [r["matter_name"] for r in rows] == ["Never Set", "Overdue", "Future"]


# ── last-activity fallback chain ──────────────────────────────────────────

def test_last_note_used_when_notes_exist(monkeypatch):
    import backend.main as m
    matter = _matter("Estate of Chikafu")
    note_old = _note(matter["id"], "Filed initial papers", datetime(2026, 1, 1, tzinfo=timezone.utc))
    note_new = _note(matter["id"], "Client confirmed instructions to proceed", datetime(2026, 2, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter], notes=[note_old, note_new]))

    rows = asyncio.run(matter_review_status_report(_fake_request()))

    assert rows[0]["last_activity_kind"] == "note"
    assert rows[0]["last_activity_text"] == "Client confirmed instructions to proceed"
    assert rows[0]["last_activity_date"] == "2026-02-01T00:00:00+00:00"


def test_note_preview_truncated_when_long():
    from backend.main import _truncate_preview
    long_text = "A" * 200
    result = _truncate_preview(long_text)
    assert len(result) == 161  # 160 chars + ellipsis
    assert result.endswith("…")


def test_falls_back_to_most_recent_document_when_no_notes(monkeypatch):
    import backend.main as m
    matter = _matter("Vengesai Lease Dispute")
    doc_old = _doc(matter["id"], "draft_lease.docx", datetime(2026, 1, 5, tzinfo=timezone.utc))
    doc_new = _doc(matter["id"], "signed_lease_final.pdf", datetime(2026, 1, 20, tzinfo=timezone.utc))
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter], documents=[doc_old, doc_new]))

    rows = asyncio.run(matter_review_status_report(_fake_request()))

    assert rows[0]["last_activity_kind"] == "document"
    assert rows[0]["last_activity_text"] == "Document uploaded: signed_lease_final.pdf"
    assert rows[0]["last_activity_date"] == "2026-01-20T00:00:00+00:00"


def test_incomplete_documents_excluded_from_fallback(monkeypatch):
    import backend.main as m
    matter = _matter("Vengesai Lease Dispute")
    still_processing = _doc(matter["id"], "uploading.pdf", datetime(2026, 1, 20, tzinfo=timezone.utc), status="processing")
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter], documents=[still_processing]))

    rows = asyncio.run(matter_review_status_report(_fake_request()))

    assert rows[0]["last_activity_kind"] != "document"


def test_falls_back_to_bare_last_activity_when_no_notes_or_documents(monkeypatch):
    import backend.main as m
    touched = datetime(2026, 2, 10, tzinfo=timezone.utc)
    matter = _matter("Quiet Matter", last_activity=touched)
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter]))

    rows = asyncio.run(matter_review_status_report(_fake_request()))

    assert rows[0]["last_activity_kind"] == "touched"
    assert rows[0]["last_activity_text"] == "Touched (no note or document recorded)"
    assert rows[0]["last_activity_date"] == touched.isoformat()


def test_falls_back_to_created_at_when_never_touched_at_all(monkeypatch):
    import backend.main as m
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    matter = _matter("Untouched Matter", created_at=created, last_activity=None)
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter]))

    rows = asyncio.run(matter_review_status_report(_fake_request()))

    assert rows[0]["last_activity_kind"] == "created"
    assert rows[0]["last_activity_text"] == "Matter created, no activity since"
    assert rows[0]["last_activity_date"] == created.isoformat()


# ── client name resolution ────────────────────────────────────────────────

def test_client_name_resolved_from_linked_client(monkeypatch):
    import backend.main as m
    client = _client("Huang Li Qiang")
    matter = _matter("Huang Estate Matter", client_id=client["id"], client_name="stale cached name")
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter], clients=[client]))

    rows = asyncio.run(matter_review_status_report(_fake_request()))

    assert rows[0]["client_name"] == "Huang Li Qiang"  # live client.full_name wins over matters.client_name


def test_client_name_falls_back_to_cached_matter_field_when_unlinked(monkeypatch):
    import backend.main as m
    matter = _matter("Legacy Matter", client_id=None, client_name="Old Cached Client Name")
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter]))

    rows = asyncio.run(matter_review_status_report(_fake_request()))

    assert rows[0]["client_name"] == "Old Cached Client Name"


# ── filters ────────────────────────────────────────────────────────────────

def test_lawyer_filter_matches_created_by(monkeypatch):
    import backend.main as m
    lawyer = _user("Nyaradzo Maphosa")
    mine = _matter("My Matter", created_by=lawyer["id"])
    theirs = _matter("Someone Else's Matter", created_by=uuid.uuid4())
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[mine, theirs], users=[lawyer]))

    rows = asyncio.run(matter_review_status_report(_fake_request(), lawyer_id=str(lawyer["id"])))

    assert len(rows) == 1
    assert rows[0]["matter_name"] == "My Matter"
    assert rows[0]["created_by_name"] == "Nyaradzo Maphosa"


def test_client_filter_matches_client_id(monkeypatch):
    import backend.main as m
    client = _client("Huang Li Qiang")
    theirs = _matter("Huang Matter", client_id=client["id"])
    other = _matter("Other Matter", client_id=uuid.uuid4())
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[theirs, other], clients=[client]))

    rows = asyncio.run(matter_review_status_report(_fake_request(), client_id=str(client["id"])))

    assert len(rows) == 1
    assert rows[0]["matter_name"] == "Huang Matter"


def test_status_filter_matches_exact_status(monkeypatch):
    import backend.main as m
    active = _matter("Active Matter", status="Active")
    closed = _matter("Closed Matter", status="Closed")
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[active, closed]))

    rows = asyncio.run(matter_review_status_report(_fake_request(), status="Closed"))

    assert len(rows) == 1
    assert rows[0]["matter_name"] == "Closed Matter"


def test_invalid_status_filter_rejected(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(matter_review_status_report(_fake_request(), status="Not A Real Status"))
    assert exc_info.value.status_code == 422


def test_invalid_lawyer_id_rejected(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(matter_review_status_report(_fake_request(), lawyer_id="not-a-uuid"))
    assert exc_info.value.status_code == 400


def test_sentinel_matter_excluded(monkeypatch):
    import backend.main as m
    sentinel = _matter("General / Firm Precedents", is_sentinel=True)
    real = _matter("Real Matter")
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[sentinel, real]))

    rows = asyncio.run(matter_review_status_report(_fake_request()))

    assert len(rows) == 1
    assert rows[0]["matter_name"] == "Real Matter"


def test_no_matters_does_not_error(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())

    rows = asyncio.run(matter_review_status_report(_fake_request()))

    assert rows == []


# ── CSV export ─────────────────────────────────────────────────────────────

def test_csv_export_partner_succeeds_with_expected_content(monkeypatch):
    import backend.main as m
    today = date.today()
    matter = _matter("Estate of Chikafu", next_review_date=today, last_reviewed_date=today - timedelta(days=30),
                      status="Active", matter_number="NGM-001-01")
    note = _note(matter["id"], "Reviewed with client", datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc))
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter], notes=[note]))

    response = asyncio.run(matter_review_status_report_export(_fake_request()))

    assert response.media_type == "text/csv"
    assert "attachment" in response.headers["Content-Disposition"]

    rows = _csv_rows(response)
    assert rows[0] == ["Matter Name", "Matter Number", "Client Name", "Status", "Created By",
                        "Next Review Date", "Last Reviewed Date", "Last Activity", "Last Activity Date"]
    assert len(rows) == 2
    data_row = rows[1]
    assert data_row[0] == "Estate of Chikafu"
    assert data_row[1] == "NGM-001-01"
    assert data_row[3] == "Active"
    assert data_row[5] == today.isoformat()
    assert data_row[7] == "Reviewed with client"


def test_csv_export_shows_placeholder_text_for_never_reviewed(monkeypatch):
    import backend.main as m
    matter = _matter("Never Touched Matter", next_review_date=None, last_reviewed_date=None)
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=[matter]))

    response = asyncio.run(matter_review_status_report_export(_fake_request()))
    rows = _csv_rows(response)

    assert rows[1][5] == "None set"
    assert rows[1][6] == "Never reviewed"
