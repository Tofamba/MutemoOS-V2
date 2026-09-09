"""
Unit tests for the Africa's Talking delivery-report (DLR) callback in
backend/main.py (2026-09-08):
  - _record_sms_delivery_report(): the UPDATE that applies one delivery
    report to its sms_usage_log row, matched by message_id, with the
    "don't walk a final status backwards" guard.
  - POST /api/webhooks/africas-talking/delivery-report: parses AT's
    form-encoded callback, records it, and ALWAYS 200s (a non-200 makes
    AT retry for hours) -- except when the optional ?token= shared secret
    is configured and wrong.

Called directly as plain async functions with fake conns/requests, same
convention as tests/test_sms_usage_tracking.py and
tests/test_legal_feed_service_token.py.
"""

import asyncio

import pytest
from fastapi import HTTPException

from backend.main import (
    _record_sms_delivery_report,
    africas_talking_delivery_report,
)


# ── _record_sms_delivery_report: the UPDATE ────────────────────────────────

class _RecorderConn:
    def __init__(self, update_result="UPDATE 1"):
        self.update_result = update_result
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((" ".join(query.split()), args))
        return self.update_result


def test_record_delivery_report_updates_matching_row():
    conn = _RecorderConn("UPDATE 1")
    n = asyncio.run(_record_sms_delivery_report(
        conn, message_id="ATXid_abc", status="Success",
        failure_reason=None, network_code="64001",
    ))
    assert n == 1
    query, args = conn.calls[0]
    assert query.startswith("UPDATE sms_usage_log")
    assert "dlr_status = $1" in query
    assert "message_id = $4" in query
    # the "already final" guard is present
    assert "dlr_status IS NULL OR dlr_status <> ALL($5" in query
    assert args[0] == "Success"
    assert args[1] is None
    assert args[2] == "64001"
    assert args[3] == "ATXid_abc"
    assert args[4] == ["Success", "Failed", "Rejected"]


def test_record_delivery_report_returns_zero_when_no_row_matched():
    conn = _RecorderConn("UPDATE 0")
    n = asyncio.run(_record_sms_delivery_report(
        conn, message_id="ATXid_unknown", status="Failed",
        failure_reason="UserInBlackList", network_code=None,
    ))
    assert n == 0


def test_record_delivery_report_skips_empty_message_id():
    conn = _RecorderConn()
    n = asyncio.run(_record_sms_delivery_report(
        conn, message_id="", status="Success", failure_reason=None, network_code=None,
    ))
    assert n == 0
    assert conn.calls == []  # no query issued at all


def test_record_delivery_report_tolerates_odd_status_string():
    conn = _RecorderConn("MERGE 1")  # some unexpected command tag shape
    n = asyncio.run(_record_sms_delivery_report(
        conn, message_id="ATXid_abc", status="Buffered", failure_reason=None, network_code=None,
    ))
    assert n == 1  # last token "1" still parses


def test_record_delivery_report_unparseable_result_is_zero():
    conn = _RecorderConn("weird")
    n = asyncio.run(_record_sms_delivery_report(
        conn, message_id="ATXid_abc", status="Sent", failure_reason=None, network_code=None,
    ))
    assert n == 0


# ── POST /api/webhooks/africas-talking/delivery-report ─────────────────────

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
    def __init__(self, *, form=None, json_body=None, query=None, form_raises=False):
        self._form = form
        self._json = json_body
        self._form_raises = form_raises
        self.query_params = query or {}

    async def form(self):
        if self._form_raises or self._form is None:
            raise RuntimeError("no form body")
        return self._form

    async def json(self):
        if self._json is None:
            raise RuntimeError("no json body")
        return self._json


def _patch_pool(monkeypatch, update_result="UPDATE 1"):
    import backend.main as m
    conn = _RecorderConn(update_result)
    monkeypatch.setattr(m, "_db_pool", FakePool(conn))
    return conn


def test_endpoint_records_form_encoded_callback_and_200s(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "AFRICAS_TALKING_DLR_TOKEN", None)
    conn = _patch_pool(monkeypatch, "UPDATE 1")

    req = FakeRequest(form={
        "id": "ATXid_df0dda39ece1321f73810f9c95eb6d5c",
        "status": "Success",
        "phoneNumber": "+263785023897",
        "networkCode": "64001",
        "failureReason": "",
        "retryCount": "0",
    })
    resp = asyncio.run(africas_talking_delivery_report(req))

    assert resp.status_code == 200
    assert len(conn.calls) == 1
    _, args = conn.calls[0]
    assert args[0] == "Success"
    assert args[1] is None          # empty failureReason normalised to None
    assert args[2] == "64001"
    assert args[3] == "ATXid_df0dda39ece1321f73810f9c95eb6d5c"


def test_endpoint_200s_on_unknown_message_id(monkeypatch):
    """A send from another deployment sharing the same AT app -- no row
    here to match. Must still 200 so AT stops retrying."""
    import backend.main as m
    monkeypatch.setattr(m, "AFRICAS_TALKING_DLR_TOKEN", None)
    _patch_pool(monkeypatch, "UPDATE 0")

    req = FakeRequest(form={"id": "ATXid_from_prod", "status": "Failed",
                            "failureReason": "DeliveryFailure"})
    resp = asyncio.run(africas_talking_delivery_report(req))
    assert resp.status_code == 200


def test_endpoint_falls_back_to_json_body(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "AFRICAS_TALKING_DLR_TOKEN", None)
    conn = _patch_pool(monkeypatch, "UPDATE 1")

    req = FakeRequest(form_raises=True, json_body={
        "id": "ATXid_json", "status": "Rejected", "failureReason": "InvalidSenderId",
    })
    resp = asyncio.run(africas_talking_delivery_report(req))
    assert resp.status_code == 200
    _, args = conn.calls[0]
    assert args[0] == "Rejected"
    assert args[1] == "InvalidSenderId"
    assert args[3] == "ATXid_json"


def test_endpoint_200s_and_records_nothing_on_empty_body(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "AFRICAS_TALKING_DLR_TOKEN", None)
    conn = _patch_pool(monkeypatch, "UPDATE 1")

    req = FakeRequest(form_raises=True)  # no form, no json, no query
    resp = asyncio.run(africas_talking_delivery_report(req))
    assert resp.status_code == 200
    assert conn.calls == []  # no id -> no DB write attempted


def test_endpoint_200s_when_db_raises(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "AFRICAS_TALKING_DLR_TOKEN", None)

    class _BoomConn:
        async def execute(self, *a, **k):
            raise RuntimeError("connection reset")
    monkeypatch.setattr(m, "_db_pool", FakePool(_BoomConn()))

    req = FakeRequest(form={"id": "ATXid_abc", "status": "Success"})
    resp = asyncio.run(africas_talking_delivery_report(req))
    assert resp.status_code == 200  # DB blip must not become an AT retry storm


def test_endpoint_rejects_missing_token_when_secret_configured(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "AFRICAS_TALKING_DLR_TOKEN", "s3cret")
    _patch_pool(monkeypatch)

    req = FakeRequest(form={"id": "ATXid_abc", "status": "Success"}, query={})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(africas_talking_delivery_report(req))
    assert exc.value.status_code == 403


def test_endpoint_rejects_wrong_token_when_secret_configured(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "AFRICAS_TALKING_DLR_TOKEN", "s3cret")
    _patch_pool(monkeypatch)

    req = FakeRequest(form={"id": "ATXid_abc", "status": "Success"}, query={"token": "nope"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(africas_talking_delivery_report(req))
    assert exc.value.status_code == 403


def test_endpoint_accepts_correct_token_when_secret_configured(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "AFRICAS_TALKING_DLR_TOKEN", "s3cret")
    conn = _patch_pool(monkeypatch, "UPDATE 1")

    req = FakeRequest(form={"id": "ATXid_abc", "status": "Success"}, query={"token": "s3cret"})
    resp = asyncio.run(africas_talking_delivery_report(req))
    assert resp.status_code == 200
    assert len(conn.calls) == 1
