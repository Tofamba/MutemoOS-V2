"""
Unit tests for per-firm SMS usage tracking (backend/main.py, 2026-08-31):
  - sms_usage_log: one row per real Africa's Talking OTP send attempt,
    success or failure, so a firm's share of the shared AT account's
    real cost can be attributed back to it over time.
  - _log_sms_usage(): the actual INSERT.
  - request_otp(): wires _send_otp_code's collected AT attempt(s) into
    _log_sms_usage, regardless of the eventual channel/outcome.
  - GET /api/reports/sms-usage-by-firm: aggregates by firm + month.
    Operator-only since 2026-09-02 (require_admin_token(), not a firm
    role) -- this is Tofamba's own AT cost-attribution view, pulled out
    of the firm-facing Reports picker.

Called directly as plain async functions, same convention as
tests/test_otp_and_reassignment_firm_scoping.py (request_otp) and
tests/test_admin_backfill_chunk_hashes.py (require_admin_token()
gate / FakeRequest shape).
"""

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from backend.main import (
    FIRM_ID,
    OTPRequestBody,
    _log_sms_usage,
    request_otp,
    sms_usage_by_firm_report,
)


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


class FakeRequest:
    """Same shape as tests/test_admin_backfill_chunk_hashes.py's own
    FakeRequest -- sms_usage_by_firm_report is operator-gated via
    require_admin_token(), not a firm role, since 2026-09-02."""
    def __init__(self, headers=None):
        self.headers = headers or {}


# ── _log_sms_usage: the INSERT itself ───────────────────────────────────────

class _LogConn:
    def __init__(self):
        self.executed = []

    async def execute(self, query, *args):
        self.executed.append((" ".join(query.split()), args))
        return "OK"


def test_log_sms_usage_delivered_attempt():
    conn = _LogConn()
    attempt = {"success": True, "provider_status": "Success", "status_code": 100,
               "message_id": "ATXid_abc", "cost": "USD 0.0500"}

    asyncio.run(_log_sms_usage(conn, phone="+263771234567", provider="africas_talking", attempt=attempt))

    assert len(conn.executed) == 1
    query, args = conn.executed[0]
    assert query.startswith("INSERT INTO sms_usage_log")
    firm_id, phone, provider, status, provider_status, status_code, cost_amount, cost_currency, message_id, error = args
    assert firm_id == FIRM_ID
    assert phone == "+263771234567"
    assert provider == "africas_talking"
    assert status == "delivered"
    assert provider_status == "Success"
    assert status_code == 100
    assert cost_amount == 0.05
    assert cost_currency == "USD"
    assert message_id == "ATXid_abc"
    assert error is None


def test_log_sms_usage_failed_attempt_insufficient_balance():
    """The real production case, 2026-08-30: no cost is returned on a
    provider-rejected send."""
    conn = _LogConn()
    attempt = {"success": False, "provider_status": "InsufficientBalance", "status_code": 405,
               "message_id": None, "cost": None}

    asyncio.run(_log_sms_usage(conn, phone="+263785023897", provider="africas_talking", attempt=attempt))

    query, args = conn.executed[0]
    firm_id, phone, provider, status, provider_status, status_code, cost_amount, cost_currency, message_id, error = args
    assert status == "failed"
    assert provider_status == "InsufficientBalance"
    assert status_code == 405
    assert cost_amount is None
    assert cost_currency is None


def test_log_sms_usage_http_level_failure_has_no_provider_status():
    """No per-recipient AT response existed (e.g. a 401 on the API call
    itself) -- error_detail carries the failure instead."""
    conn = _LogConn()
    attempt = {"success": False, "error": "HTTP 401: Invalid apiKey"}

    asyncio.run(_log_sms_usage(conn, phone="+263771234567", provider="africas_talking", attempt=attempt))

    query, args = conn.executed[0]
    firm_id, phone, provider, status, provider_status, status_code, cost_amount, cost_currency, message_id, error = args
    assert status == "failed"
    assert provider_status is None
    assert status_code is None
    assert error == "HTTP 401: Invalid apiKey"


# ── request_otp: logs the AT attempt(s) regardless of eventual outcome ─────

class _RequestOtpConn:
    def __init__(self):
        self.executed = []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if "SELECT email FROM users" in q:
            return {"email": "lawyer@example.com"}
        raise NotImplementedError(f"unhandled query: {q}")

    async def execute(self, query, *args):
        self.executed.append((" ".join(query.split()), args))
        return "OK"


def test_request_otp_logs_successful_africastalking_attempt(monkeypatch):
    import backend.main as m
    conn = _RequestOtpConn()
    monkeypatch.setattr(m, "_db_pool", FakePool(conn))
    monkeypatch.setattr(m, "AUTH_ENABLED", True)

    def fake_send_otp_code(phone, email, code, sms_attempt_log=None):
        if sms_attempt_log is not None:
            sms_attempt_log.append({"success": True, "provider_status": "Success", "status_code": 100,
                                     "message_id": "ATXid_test", "cost": "USD 0.0500"})
        return "sms"
    monkeypatch.setattr(m, "_send_otp_code", fake_send_otp_code)

    asyncio.run(request_otp(OTPRequestBody(phone="+263771234567")))

    usage_inserts = [c for c in conn.executed if c[0].startswith("INSERT INTO sms_usage_log")]
    assert len(usage_inserts) == 1
    args = usage_inserts[0][1]
    assert args[3] == "delivered"  # status
    assert args[8] == "ATXid_test"  # message_id


def test_request_otp_logs_failed_africastalking_attempt_even_though_email_fallback_succeeded(monkeypatch):
    """The real weekend scenario: AT rejects with InsufficientBalance, the
    chain falls through to email, request_otp still returns 200 -- but the
    failed AT attempt must still be logged, not silently lost."""
    import backend.main as m
    conn = _RequestOtpConn()
    monkeypatch.setattr(m, "_db_pool", FakePool(conn))
    monkeypatch.setattr(m, "AUTH_ENABLED", True)

    def fake_send_otp_code(phone, email, code, sms_attempt_log=None):
        if sms_attempt_log is not None:
            sms_attempt_log.append({"success": False, "provider_status": "InsufficientBalance",
                                     "status_code": 405, "message_id": None, "cost": None})
        return "email"  # fell all the way through
    monkeypatch.setattr(m, "_send_otp_code", fake_send_otp_code)

    result = asyncio.run(request_otp(OTPRequestBody(phone="+263771234567")))

    assert result["channel"] == "email"
    usage_inserts = [c for c in conn.executed if c[0].startswith("INSERT INTO sms_usage_log")]
    assert len(usage_inserts) == 1
    assert usage_inserts[0][1][3] == "failed"  # status
    assert usage_inserts[0][1][4] == "InsufficientBalance"  # provider_status


def test_request_otp_logs_nothing_when_africastalking_never_attempted(monkeypatch):
    """No log entry at all when Africa's Talking isn't configured/reached --
    e.g. WhatsApp succeeded first, or nothing but email is configured."""
    import backend.main as m
    conn = _RequestOtpConn()
    monkeypatch.setattr(m, "_db_pool", FakePool(conn))
    monkeypatch.setattr(m, "AUTH_ENABLED", True)
    monkeypatch.setattr(m, "_send_otp_code", lambda *a, **k: "whatsapp")  # sms_attempt_log left untouched

    asyncio.run(request_otp(OTPRequestBody(phone="+263771234567")))

    usage_inserts = [c for c in conn.executed if c[0].startswith("INSERT INTO sms_usage_log")]
    assert usage_inserts == []


# ── GET /api/reports/sms-usage-by-firm ──────────────────────────────────────

class _ReportConn:
    def __init__(self, usage_rows):
        self.usage_rows = usage_rows

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT f.id AS firm_id, f.name AS firm_name"):
            firm_id, = args
            return [r for r in self.usage_rows if r["firm_id"] == firm_id]
        raise NotImplementedError(f"unhandled query: {q}")


def _usage_row(month, status, cost_amount=None, cost_currency=None, firm_id=FIRM_ID, firm_name="Sawyer & Mkushi"):
    return {"firm_id": firm_id, "firm_name": firm_name, "month": month,
            "status": status, "cost_amount": cost_amount, "cost_currency": cost_currency}


def test_rejects_without_a_valid_admin_token(monkeypatch):
    """Operator-only report (2026-09-02): no firm role, admin or partner
    included, gets in without the shared admin token."""
    import backend.main as m
    monkeypatch.setattr(m, "ADMIN_TOKEN", "real-admin-token")
    monkeypatch.setattr(m, "_db_pool", FakePool(_ReportConn([])))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(sms_usage_by_firm_report(FakeRequest(headers={})))
    assert exc_info.value.status_code == 403


def test_aggregates_counts_and_cost_for_one_month(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "ADMIN_TOKEN", "real-admin-token")
    aug = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = [
        _usage_row(aug, "delivered", 0.05, "USD"),
        _usage_row(aug, "delivered", 0.05, "USD"),
        _usage_row(aug, "failed"),  # InsufficientBalance-style: no cost
    ]
    monkeypatch.setattr(m, "_db_pool", FakePool(_ReportConn(rows)))

    result = asyncio.run(sms_usage_by_firm_report(FakeRequest(headers={"X-Admin-Token": "real-admin-token"})))

    assert len(result) == 1
    entry = result[0]
    assert entry["month"] == "2026-08-01"
    assert entry["total_sends"] == 3
    assert entry["delivered_count"] == 2
    assert entry["failed_count"] == 1
    assert entry["cost_by_currency"] == [{"currency": "USD", "total_cost": 0.10}]


def test_separates_months(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "ADMIN_TOKEN", "real-admin-token")
    aug = datetime(2026, 8, 1, tzinfo=timezone.utc)
    sep = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = [_usage_row(aug, "delivered", 0.05, "USD"), _usage_row(sep, "delivered", 0.05, "USD")]
    monkeypatch.setattr(m, "_db_pool", FakePool(_ReportConn(rows)))

    result = asyncio.run(sms_usage_by_firm_report(FakeRequest(headers={"X-Admin-Token": "real-admin-token"})))

    months = {r["month"] for r in result}
    assert months == {"2026-08-01", "2026-09-01"}
    assert result[0]["month"] > result[1]["month"]  # newest first


def test_separates_currencies_rather_than_blending_them(monkeypatch):
    """A real, deliberate correctness guard: summing different currencies
    into one number would be meaningless."""
    import backend.main as m
    monkeypatch.setattr(m, "ADMIN_TOKEN", "real-admin-token")
    aug = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = [_usage_row(aug, "delivered", 0.05, "USD"), _usage_row(aug, "delivered", 0.5, "ZWL")]
    monkeypatch.setattr(m, "_db_pool", FakePool(_ReportConn(rows)))

    result = asyncio.run(sms_usage_by_firm_report(FakeRequest(headers={"X-Admin-Token": "real-admin-token"})))

    assert result[0]["cost_by_currency"] == [
        {"currency": "USD", "total_cost": 0.05},
        {"currency": "ZWL", "total_cost": 0.5},
    ]


def test_no_usage_returns_empty_list(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "ADMIN_TOKEN", "real-admin-token")
    monkeypatch.setattr(m, "_db_pool", FakePool(_ReportConn([])))

    assert asyncio.run(sms_usage_by_firm_report(FakeRequest(headers={"X-Admin-Token": "real-admin-token"}))) == []
