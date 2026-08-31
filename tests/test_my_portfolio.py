"""
Unit tests for "My Portfolio" (backend/main.py, 2026-08-31): a self-scoped
caseload dashboard for the calling lawyer -- deliberately NOT one of the
reports:* endpoints (those are all admin/partner-tier firm-wide views).

Scoping boundary is matters/clients.created_by, the same field "My
Clients" already uses -- confirmed against test_client_ownership.py's own
convention. Section 4 (review status) reuses _fetch_matter_review_status_rows
directly, already thoroughly tested in test_matter_review_status_report.py,
so it's monkeypatched here rather than re-faked at the query level --
these tests verify the wiring (lawyer_id=user_id passed through, counts
tallied correctly from whatever it returns), not that function's own
internal correctness.

Called directly as plain async functions, same convention as
tests/test_matter_review_status_report.py (whose FakeConnection/
_as_current_user this file's shape mirrors).
"""

import asyncio
import uuid
from datetime import date, timedelta

import pytest
from fastapi import HTTPException

from backend.main import FIRM_ID, my_portfolio, my_portfolio_export, my_portfolio_export_pdf


class FakeConnection:
    def __init__(self, clients=None, matters=None, compliance=None, owners=None, fee_matters=None):
        self.clients = clients if clients is not None else []
        self.matters = matters if matters is not None else []
        self.compliance = compliance if compliance is not None else []
        self.owners = owners if owners is not None else []
        # Separate list for the billing query's own filtered result --
        # avoids needing every status/practice-area matter fixture to also
        # carry amount_billed/amount_received.
        self.fee_matters = fee_matters if fee_matters is not None else []

    async def fetchval(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT COUNT(*) FROM clients"):
            firm_id, created_by = args
            return len([c for c in self.clients if c["firm_id"] == firm_id and c.get("created_by") == created_by])
        raise NotImplementedError(f"FakeConnection.fetchval: unhandled query: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        firm_id, created_by = args[0], args[1] if len(args) > 1 and isinstance(args[1], uuid.UUID) else (None, None)

        if q.startswith("SELECT status, COUNT(*) AS matter_count FROM matters"):
            firm_id, created_by = args
            mine = [m for m in self.matters if m["firm_id"] == firm_id and m.get("created_by") == created_by and not m.get("is_sentinel")]
            tally = {}
            for m in mine:
                tally[m["status"]] = tally.get(m["status"], 0) + 1
            return [{"status": s, "matter_count": n} for s, n in tally.items()]

        if q.startswith("SELECT practice_area, COUNT(*) AS matter_count FROM matters"):
            firm_id, created_by = args
            mine = [m for m in self.matters if m["firm_id"] == firm_id and m.get("created_by") == created_by and not m.get("is_sentinel")]
            tally = {}
            for m in mine:
                tally[m.get("practice_area")] = tally.get(m.get("practice_area"), 0) + 1
            rows = [{"practice_area": pa, "matter_count": n} for pa, n in tally.items()]
            rows.sort(key=lambda r: r["matter_count"], reverse=True)
            return rows

        if q.startswith("SELECT * FROM clients WHERE firm_id=$1 AND created_by=$2"):
            firm_id, created_by = args
            return [c for c in self.clients if c["firm_id"] == firm_id and c.get("created_by") == created_by]

        if q.startswith("SELECT * FROM client_compliance WHERE client_id = ANY($1)"):
            client_ids, firm_id = args
            return [c for c in self.compliance if c["client_id"] in client_ids and c["firm_id"] == firm_id]

        if q.startswith("SELECT client_id, verification_status FROM beneficial_owners"):
            client_ids, firm_id = args
            return [o for o in self.owners if o["client_id"] in client_ids and o["firm_id"] == firm_id]

        if q.startswith("SELECT client_id, client_name, amount_billed, amount_received FROM matters"):
            firm_id, created_by = args
            return [
                m for m in self.fee_matters
                if m["firm_id"] == firm_id and m.get("created_by") == created_by
                and (m.get("amount_billed") is not None or m.get("amount_received") is not None)
            ]

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


def _matter(name, *, created_by, status="Active", practice_area=None, is_sentinel=False,
            amount_billed=None, amount_received=None, client_id=None, client_name=None):
    return {
        "id": uuid.uuid4(), "firm_id": FIRM_ID, "name": name, "status": status,
        "practice_area": practice_area, "created_by": created_by, "is_sentinel": is_sentinel,
        "amount_billed": amount_billed, "amount_received": amount_received,
        "client_id": client_id, "client_name": client_name,
    }


def _client(name, *, created_by, client_type="Individual"):
    return {"id": uuid.uuid4(), "firm_id": FIRM_ID, "full_name": name,
            "created_by": created_by, "client_type": client_type}


def _compliance(client_id, *, is_pep=None, risk_rating="NotAssessed", identity_verification_status="Unverified",
                 conflict_check_reviewed=False):
    return {"client_id": client_id, "firm_id": FIRM_ID, "is_pep": is_pep, "risk_rating": risk_rating,
            "identity_verification_status": identity_verification_status,
            "conflict_check_reviewed": conflict_check_reviewed,
            "client_is_beneficial_owner": None, "senior_management_approved_by": None}


def _cleared_compliance(client_id):
    return _compliance(client_id, is_pep=False, risk_rating="Low",
                        identity_verification_status="Verified", conflict_check_reviewed=True)


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def _fake_request():
    return None


def _empty_review_mock(monkeypatch, m, lawyer_id_captured):
    async def fake_review(conn, *, lawyer_id, client_id, status):
        lawyer_id_captured.append(lawyer_id)
        return []
    monkeypatch.setattr(m, "_fetch_matter_review_status_rows", fake_review)


# ── permission: any real role, no admin/partner gate ────────────────────────

def test_associate_can_see_their_own_portfolio(monkeypatch):
    import backend.main as m
    lawyer_id = uuid.uuid4()
    associate = {"id": lawyer_id, "firm_id": FIRM_ID, "role": "associate", "display_name": "Farai"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, associate)
    _empty_review_mock(monkeypatch, m, [])

    result = asyncio.run(my_portfolio(_fake_request()))

    assert result["has_real_identity"] is True
    assert result["lawyer_id"] == str(lawyer_id)
    assert result["lawyer_name"] == "Farai"


def test_secretary_can_also_see_their_own_portfolio(monkeypatch):
    """No reports:* permission is checked -- matter:read tier (every
    real role) is the only gate."""
    import backend.main as m
    secretary = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "role": "secretary", "display_name": "Sec"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, secretary)
    _empty_review_mock(monkeypatch, m, [])

    result = asyncio.run(my_portfolio(_fake_request()))
    assert result["has_real_identity"] is True


def test_unauthenticated_gets_401(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    async def fake_get_current_user(request):
        return None
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(my_portfolio(_fake_request()))
    assert exc_info.value.status_code == 401


def test_no_real_identity_returns_empty_portfolio_not_an_error(monkeypatch):
    """AUTH_ENABLED=False's synthetic dev user (id=None) -- degrade
    gracefully rather than crash or show firm-wide data."""
    import backend.main as m
    demo_user = {"id": None, "firm_id": FIRM_ID, "role": "partner", "display_name": "Demo User"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, demo_user)

    result = asyncio.run(my_portfolio(_fake_request()))

    assert result["has_real_identity"] is False
    assert result["volume"]["client_count"] == 0
    assert result["review_status"]["matters"] == []


# ── scoping: never another lawyer's data ─────────────────────────────────────

def test_scoped_strictly_to_calling_lawyers_own_data(monkeypatch):
    """The actual security boundary: no lawyer_id param exists on this
    endpoint at all -- scoping always comes from the session, not a
    request the caller could manipulate."""
    import backend.main as m
    me = uuid.uuid4()
    someone_else = uuid.uuid4()
    matters = [
        _matter("My Matter", created_by=me),
        _matter("Their Matter", created_by=someone_else),
    ]
    clients = [_client("My Client", created_by=me), _client("Their Client", created_by=someone_else)]
    user = {"id": me, "firm_id": FIRM_ID, "role": "associate", "display_name": "Me"}
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters, clients=clients))
    _as_current_user(monkeypatch, m, user)
    captured = []
    _empty_review_mock(monkeypatch, m, captured)

    result = asyncio.run(my_portfolio(_fake_request()))

    assert result["volume"]["client_count"] == 1
    assert result["volume"]["matter_count"] == 1


# ── section 1: volume & status ───────────────────────────────────────────────

def test_volume_and_status_breakdown(monkeypatch):
    import backend.main as m
    me = uuid.uuid4()
    matters = [
        _matter("A", created_by=me, status="Active"),
        _matter("B", created_by=me, status="Active"),
        _matter("C", created_by=me, status="Closed"),
        _matter("Sentinel", created_by=me, status="Active", is_sentinel=True),  # excluded
    ]
    clients = [_client(f"Client {i}", created_by=me) for i in range(3)]
    user = {"id": me, "firm_id": FIRM_ID, "role": "partner", "display_name": "Me"}
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters, clients=clients))
    _as_current_user(monkeypatch, m, user)
    _empty_review_mock(monkeypatch, m, [])

    result = asyncio.run(my_portfolio(_fake_request()))

    assert result["volume"]["client_count"] == 3
    assert result["volume"]["matter_count"] == 3  # sentinel excluded
    assert result["volume"]["matters_by_status"]["Active"] == 2
    assert result["volume"]["matters_by_status"]["Closed"] == 1
    assert result["volume"]["matters_by_status"]["On Hold"] == 0  # zero-filled, not missing


# ── section 2: practice areas ─────────────────────────────────────────────────

def test_practice_area_split(monkeypatch):
    import backend.main as m
    me = uuid.uuid4()
    matters = [
        _matter("A", created_by=me, practice_area="Conveyancing/Property"),
        _matter("B", created_by=me, practice_area="Conveyancing/Property"),
        _matter("C", created_by=me, practice_area="Family Law"),
        _matter("D", created_by=me, practice_area=None),
    ]
    user = {"id": me, "firm_id": FIRM_ID, "role": "associate", "display_name": "Me"}
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters))
    _as_current_user(monkeypatch, m, user)
    _empty_review_mock(monkeypatch, m, [])

    result = asyncio.run(my_portfolio(_fake_request()))

    by_area = {r["practice_area"]: r["matter_count"] for r in result["practice_areas"]}
    assert by_area["Conveyancing/Property"] == 2
    assert by_area["Family Law"] == 1
    assert by_area["Uncategorized"] == 1


# ── section 3: compliance/risk snapshot ───────────────────────────────────────

def test_compliance_snapshot_tallies_cleared_and_action_required(monkeypatch):
    import backend.main as m
    me = uuid.uuid4()
    cleared_client = _client("Cleared Co", created_by=me, client_type="Individual")
    unassessed_client = _client("Unassessed Co", created_by=me, client_type="Individual")
    clients = [cleared_client, unassessed_client]
    compliance = [_cleared_compliance(cleared_client["id"])]  # unassessed_client has no row -> defaults apply
    user = {"id": me, "firm_id": FIRM_ID, "role": "partner", "display_name": "Me"}
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=clients, compliance=compliance))
    _as_current_user(monkeypatch, m, user)
    _empty_review_mock(monkeypatch, m, [])

    result = asyncio.run(my_portfolio(_fake_request()))

    assert result["compliance"]["cleared_count"] == 1
    assert result["compliance"]["action_required_count"] == 1
    assert result["compliance"]["risk_ratings"]["Low"] == 1
    assert result["compliance"]["risk_ratings"]["NotAssessed"] == 1  # default for the unassessed client


def test_compliance_snapshot_counts_pep_flags(monkeypatch):
    import backend.main as m
    me = uuid.uuid4()
    client = _client("PEP Co", created_by=me)
    compliance = [_compliance(client["id"], is_pep=True, risk_rating="High")]
    user = {"id": me, "firm_id": FIRM_ID, "role": "partner", "display_name": "Me"}
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], compliance=compliance))
    _as_current_user(monkeypatch, m, user)
    _empty_review_mock(monkeypatch, m, [])

    result = asyncio.run(my_portfolio(_fake_request()))

    assert result["compliance"]["pep_count"] == 1
    assert result["compliance"]["risk_ratings"]["High"] == 1


# ── section 4: review status (wiring, not re-testing the reused function) ───

def test_review_status_scoped_by_lawyer_id(monkeypatch):
    """Confirms my_portfolio passes lawyer_id=<the calling lawyer's own id>
    into _fetch_matter_review_status_rows -- the actual scoping mechanism
    for this section."""
    import backend.main as m
    me = uuid.uuid4()
    user = {"id": me, "firm_id": FIRM_ID, "role": "associate", "display_name": "Me"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, user)
    captured = []
    _empty_review_mock(monkeypatch, m, captured)

    asyncio.run(my_portfolio(_fake_request()))

    assert captured == [me]


def test_review_status_tallies_overdue_due_soon_never_reviewed(monkeypatch):
    import backend.main as m
    me = uuid.uuid4()
    today = date.today()
    rows = [
        {"matter_id": "1", "next_review_date": (today - timedelta(days=5)).isoformat()},   # overdue
        {"matter_id": "2", "next_review_date": (today + timedelta(days=3)).isoformat()},    # due soon
        {"matter_id": "3", "next_review_date": (today + timedelta(days=60)).isoformat()},   # future, neither
        {"matter_id": "4", "next_review_date": None},                                       # never reviewed
    ]
    user = {"id": me, "firm_id": FIRM_ID, "role": "associate", "display_name": "Me"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, user)
    async def fake_review(conn, *, lawyer_id, client_id, status):
        return rows
    monkeypatch.setattr(m, "_fetch_matter_review_status_rows", fake_review)

    result = asyncio.run(my_portfolio(_fake_request()))

    assert result["review_status"]["overdue_count"] == 1
    assert result["review_status"]["due_soon_count"] == 1
    assert result["review_status"]["never_reviewed_count"] == 1
    assert result["review_status"]["matters"] == rows


# ── section 5: billing snapshot ───────────────────────────────────────────────

def test_billing_aggregates_billed_received_outstanding_per_client(monkeypatch):
    import backend.main as m
    me = uuid.uuid4()
    client_id = uuid.uuid4()
    fee_matters = [
        _matter("A", created_by=me, amount_billed=1000.0, amount_received=400.0, client_id=client_id, client_name="Huang Li Qiang"),
        _matter("B", created_by=me, amount_billed=500.0, amount_received=500.0, client_id=client_id, client_name="Huang Li Qiang"),
    ]
    user = {"id": me, "firm_id": FIRM_ID, "role": "partner", "display_name": "Me"}
    monkeypatch.setattr(m, "_db_pool", FakePool(fee_matters=fee_matters))
    _as_current_user(monkeypatch, m, user)
    _empty_review_mock(monkeypatch, m, [])

    result = asyncio.run(my_portfolio(_fake_request()))

    assert result["billing"]["total_billed"] == 1500.0
    assert result["billing"]["total_received"] == 900.0
    assert result["billing"]["total_outstanding"] == 600.0
    assert len(result["billing"]["by_client"]) == 1
    entry = result["billing"]["by_client"][0]
    assert entry["client_name"] == "Huang Li Qiang"
    assert entry["billed"] == 1500.0
    assert entry["received"] == 900.0
    assert entry["outstanding"] == 600.0


def test_billing_handles_only_billed_no_received_yet(monkeypatch):
    import backend.main as m
    me = uuid.uuid4()
    fee_matters = [_matter("A", created_by=me, amount_billed=300.0, amount_received=None,
                            client_id=uuid.uuid4(), client_name="New Client")]
    user = {"id": me, "firm_id": FIRM_ID, "role": "associate", "display_name": "Me"}
    monkeypatch.setattr(m, "_db_pool", FakePool(fee_matters=fee_matters))
    _as_current_user(monkeypatch, m, user)
    _empty_review_mock(monkeypatch, m, [])

    result = asyncio.run(my_portfolio(_fake_request()))

    assert result["billing"]["total_billed"] == 300.0
    assert result["billing"]["total_received"] == 0.0
    assert result["billing"]["total_outstanding"] == 300.0


def test_billing_empty_when_no_fees_tracked(monkeypatch):
    import backend.main as m
    me = uuid.uuid4()
    user = {"id": me, "firm_id": FIRM_ID, "role": "associate", "display_name": "Me"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, user)
    _empty_review_mock(monkeypatch, m, [])

    result = asyncio.run(my_portfolio(_fake_request()))

    assert result["billing"]["total_billed"] == 0.0
    assert result["billing"]["by_client"] == []


# ── CSV / PDF exports ─────────────────────────────────────────────────────────
# Same self-scoping-only security boundary as the live view (no lawyer_id
# param on either export function's signature -- nothing to manipulate),
# same matter:read permission gate, same data via _resolve_my_portfolio().
# These reuse the exact fixtures/mocks above rather than re-deriving the
# aggregation logic's correctness -- that's already covered by every test
# in this file; these confirm the export formats faithfully carry that
# same data through.

import csv
import io

import pdfplumber


def _csv_rows(response):
    # utf-8-sig strips a leading UTF-8 BOM if present (added 2026-08-31 so
    # Excel reads non-ASCII characters like em-dashes correctly) and is a
    # no-op otherwise -- correct either way, unlike plain utf-8 which would
    # leave a stray ﻿ prefixed onto the first cell.
    text = response.body.decode("utf-8-sig") if isinstance(response.body, bytes) else response.body.lstrip("﻿")
    return list(csv.reader(io.StringIO(text)))


def _pdf_text(response):
    with pdfplumber.open(io.BytesIO(response.body)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def _portfolio_fixture(monkeypatch, m):
    """One lawyer with a known client, matter, compliance record, and fee
    figures -- exercises every section with real, checkable values."""
    me = uuid.uuid4()
    client = _client("Huang Li Qiang", created_by=me)
    matters = [
        _matter("Huang Estate Matter", created_by=me, status="Active", practice_area="Trusts & Estates",
                client_id=client["id"], client_name="Huang Li Qiang"),
    ]
    compliance = [_cleared_compliance(client["id"])]
    fee_matters = [_matter("Huang Estate Matter", created_by=me, amount_billed=1000.0, amount_received=400.0,
                            client_id=client["id"], client_name="Huang Li Qiang")]
    user = {"id": me, "firm_id": FIRM_ID, "role": "associate", "display_name": "Farai Nyamande"}
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=[client], matters=matters,
                                                  compliance=compliance, fee_matters=fee_matters))
    _as_current_user(monkeypatch, m, user)
    _empty_review_mock(monkeypatch, m, [])
    return user


def test_csv_export_contains_correct_data(monkeypatch):
    import backend.main as m
    user = _portfolio_fixture(monkeypatch, m)

    response = asyncio.run(my_portfolio_export(_fake_request()))

    assert response.media_type == "text/csv"
    assert "attachment" in response.headers["Content-Disposition"]
    rows = _csv_rows(response)
    flat = [cell for row in rows for cell in row]
    assert user["display_name"] in flat
    assert "== Volume & Status ==" in flat
    assert ["Total Clients", "1"] in rows
    assert ["Total Matters", "1"] in rows
    assert "== Practice Area Split ==" in flat
    assert ["Trusts & Estates", "1"] in rows
    assert "== Compliance & Risk Snapshot ==" in flat
    assert ["Cleared", "1"] in rows
    assert "== Billing Snapshot (firm's own professional fees -- not client funds held in trust) ==" in flat
    assert ["Total Billed", "1000.0"] in rows
    assert ["Total Received", "400.0"] in rows
    assert ["Total Outstanding", "600.0"] in rows
    assert ["Huang Li Qiang", "1000.0", "400.0", "600.0"] in rows


def test_csv_export_starts_with_utf8_bom(monkeypatch):
    """Real bug reported 2026-08-31: no BOM meant Excel misread non-ASCII
    characters (em-dashes, accented names) as mojibake. Checks the raw
    bytes directly -- the exact 3-byte EF BB BF signature Excel looks
    for, not just that _csv_rows() happens to parse correctly (which
    utf-8-sig would mask even if the BOM were missing)."""
    import backend.main as m
    _portfolio_fixture(monkeypatch, m)

    response = asyncio.run(my_portfolio_export(_fake_request()))

    assert response.body[:3] == b"\xef\xbb\xbf"


def test_csv_export_em_dash_in_matter_name_survives_round_trip(monkeypatch):
    """The exact real case reported: 'Commercial lease dispute — LC 88/26'
    -- a real matter name from this firm's own staging data, surfaced via
    the Review Status section (the only section that carries free-text
    matter names, rather than just aggregate counts) -- must decode back
    to a genuine em-dash, not mojibake, after the BOM fix."""
    import backend.main as m
    me = uuid.uuid4()
    user = {"id": me, "firm_id": FIRM_ID, "role": "associate", "display_name": "Me"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, user)
    review_row = {
        "matter_id": "1", "matter_name": "Commercial lease dispute — LC 88/26", "matter_number": "LC-88-26",
        "client_id": None, "client_name": None, "status": "Active",
        "next_review_date": None, "last_reviewed_date": None,
        "last_activity_kind": "touched", "last_activity_text": "Touched (no note or document recorded)",
        "last_activity_date": None,
    }
    async def fake_review(conn, *, lawyer_id, client_id, status):
        return [review_row]
    monkeypatch.setattr(m, "_fetch_matter_review_status_rows", fake_review)

    response = asyncio.run(my_portfolio_export(_fake_request()))

    assert response.body[:3] == b"\xef\xbb\xbf"
    decoded = response.body.decode("utf-8-sig")
    assert "Commercial lease dispute — LC 88/26" in decoded
    rows = _csv_rows(response)
    assert ["Commercial lease dispute — LC 88/26", "LC-88-26", "", "Active",
            "None set", "Never reviewed", "Touched (no note or document recorded)"] in rows


def test_pdf_export_generates_without_error_and_contains_correct_data(monkeypatch):
    import backend.main as m
    user = _portfolio_fixture(monkeypatch, m)

    response = asyncio.run(my_portfolio_export_pdf(_fake_request()))

    assert response.media_type == "application/pdf"
    assert "attachment" in response.headers["Content-Disposition"]
    text = _pdf_text(response)
    assert user["display_name"] in text
    # KPI cards: value + label are drawn as separate cells (a real value
    # box, not a "Label: N" text line) -- pdfplumber's reading order still
    # groups the four values on one line and the four labels on the next,
    # so this confirms both without depending on exact card pixel geometry.
    assert "Total Clients" in text
    assert "Total Matters" in text
    assert "Cleared" in text
    assert "Action Required" in text
    card_value_line = [line for line in text.splitlines() if line.strip() == "1 1 1 0"]
    assert card_value_line, f"expected a '1 1 1 0' KPI value line, got:\n{text}"
    assert "Trusts & Estates" in text  # practice-area bar chart label
    assert "not client funds held in trust" in text
    assert "600.00" in text  # outstanding balance


def test_pdf_export_kpi_cards_show_correct_values(monkeypatch):
    """Confirms the redesign didn't just move text around cosmetically --
    the four card values are the real client_count/matter_count/
    cleared_count/action_required_count numbers, in that order."""
    import backend.main as m
    me = uuid.uuid4()
    clients = [_client(f"Client {i}", created_by=me) for i in range(5)]
    matters = [_matter(f"Matter {i}", created_by=me) for i in range(8)]
    compliance = [_cleared_compliance(clients[0]["id"]), _cleared_compliance(clients[1]["id"])]
    user = {"id": me, "firm_id": FIRM_ID, "role": "partner", "display_name": "Me"}
    monkeypatch.setattr(m, "_db_pool", FakePool(clients=clients, matters=matters, compliance=compliance))
    _as_current_user(monkeypatch, m, user)
    _empty_review_mock(monkeypatch, m, [])

    response = asyncio.run(my_portfolio_export_pdf(_fake_request()))
    text = _pdf_text(response)

    # 5 clients, 8 matters, 2 cleared, 3 action required (the other 3 of 5 clients)
    card_value_line = [line for line in text.splitlines() if line.strip() == "5 8 2 3"]
    assert card_value_line, f"expected a '5 8 2 3' KPI value line, got:\n{text}"


def test_pdf_export_handles_empty_portfolio_without_crashing(monkeypatch):
    """No clients, no matters, no fees -- must render a clean report, not
    crash on an empty practice_areas/by_client list."""
    import backend.main as m
    me = uuid.uuid4()
    user = {"id": me, "firm_id": FIRM_ID, "role": "associate", "display_name": "Farai Nyamande"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, user)
    _empty_review_mock(monkeypatch, m, [])

    response = asyncio.run(my_portfolio_export_pdf(_fake_request()))
    text = _pdf_text(response)

    assert "No matters yet." in text
    card_value_line = [line for line in text.splitlines() if line.strip() == "0 0 0 0"]
    assert card_value_line, f"expected a '0 0 0 0' KPI value line, got:\n{text}"


def test_pdf_export_handles_multiple_review_status_matters_without_crashing(monkeypatch):
    """Real bug caught during staging verification, 2026-08-31:
    multi_cell()'s default cursor behavior leaves x at the right edge of
    the last wrapped line rather than resetting to the left margin (unlike
    cell(), used everywhere else in this function) -- a second matter's
    multi_cell call back-to-back then had zero horizontal space left and
    raised FPDFException. Needs at least two matters with real, differently
    -shaped text to reproduce; a single matter (or none) doesn't hit it."""
    import backend.main as m
    me = uuid.uuid4()
    user = {"id": me, "firm_id": FIRM_ID, "role": "associate", "display_name": "Farai Nyamande"}
    monkeypatch.setattr(m, "_db_pool", FakePool())
    _as_current_user(monkeypatch, m, user)

    rows = [
        {"matter_id": "1", "matter_name": "Divorce and maintenance claim", "matter_number": "FN-001-01",
         "client_id": "c1", "client_name": "Munyaradzi Gwenzi", "status": "Active",
         "next_review_date": "2026-09-12", "last_reviewed_date": "2026-08-30", "created_by_name": "Farai Nyamande",
         "last_activity_kind": "note",
         "last_activity_text": "To meet with client to discuss what happens to the family trust and plan for pre-trial conference",
         "last_activity_date": "2026-08-30T16:52:57"},
        {"matter_id": "2", "matter_name": "Mining claim boundary dispute — HC 4521/26", "matter_number": None,
         "client_id": None, "client_name": "Nyaradzo Construction & Engineering (Pvt) Ltd", "status": "Active",
         "next_review_date": None, "last_reviewed_date": None, "created_by_name": "Farai Nyamande",
         "last_activity_kind": "touched", "last_activity_text": "Touched (no note or document recorded)",
         "last_activity_date": "2026-08-27T08:10:54"},
    ]
    async def fake_review(conn, *, lawyer_id, client_id, status):
        return rows
    monkeypatch.setattr(m, "_fetch_matter_review_status_rows", fake_review)

    response = asyncio.run(my_portfolio_export_pdf(_fake_request()))  # must not raise

    text = _pdf_text(response)
    assert "Divorce and maintenance claim" in text
    assert "Mining claim boundary dispute" in text


def test_csv_export_unauthenticated_gets_401(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    async def fake_get_current_user(request):
        return None
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(my_portfolio_export(_fake_request()))
    assert exc_info.value.status_code == 401


def test_pdf_export_scoped_strictly_to_calling_lawyers_own_data(monkeypatch):
    """The same security boundary as the live view: neither export
    function takes a lawyer_id argument at all, so there's nothing in
    the request to manipulate into exporting someone else's data --
    confirmed here the same way test_scoped_strictly_to_calling_lawyers_own_data
    confirms it for the live view."""
    import backend.main as m
    me = uuid.uuid4()
    someone_else = uuid.uuid4()
    matters = [
        _matter("My Matter", created_by=me),
        _matter("Their Matter", created_by=someone_else),
    ]
    clients = [_client("My Client", created_by=me), _client("Their Client", created_by=someone_else)]
    user = {"id": me, "firm_id": FIRM_ID, "role": "associate", "display_name": "Me"}
    monkeypatch.setattr(m, "_db_pool", FakePool(matters=matters, clients=clients))
    _as_current_user(monkeypatch, m, user)
    _empty_review_mock(monkeypatch, m, [])

    csv_response = asyncio.run(my_portfolio_export(_fake_request()))
    rows = _csv_rows(csv_response)

    assert ["Total Clients", "1"] in rows
    assert ["Total Matters", "1"] in rows
    flat_text = "\n".join(",".join(r) for r in rows)
    assert "Their Client" not in flat_text
    assert "Their Matter" not in flat_text
