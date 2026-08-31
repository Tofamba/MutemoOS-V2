"""
Unit tests for _send_otp_code's channel fallback chain in backend/main.py:
WhatsApp -> Africa's Talking SMS -> Twilio SMS -> email -> None.
Africa's Talking is the PRIMARY SMS channel (2026-08-30) -- Twilio has
never actually been configured for this firm, so it's checked second,
purely as a zero-cost legacy fallback, not a real second working provider.

Mocks the channel-send functions and the module-level config flags
directly rather than setting real env vars (those are read once at import
time into module-level constants, so patching os.environ after import
wouldn't have any effect) — same direct-monkeypatch-of-module-attributes
approach used throughout this repo's other backend tests (see
tests/test_clients_api.py, tests/test_matter_client_linking.py).
"""

import json

import httpx

from backend.main import _send_otp_code, _send_sms_otp, _send_sms_via_africastalking


def _configure(monkeypatch, *, whatsapp=False, africastalking=False, twilio=False, email=False):
    import backend.main as m
    monkeypatch.setattr(m, "WHATSAPP_ACCESS_TOKEN", "token" if whatsapp else None)
    monkeypatch.setattr(m, "WHATSAPP_PHONE_NUMBER_ID", "phone-id" if whatsapp else None)
    monkeypatch.setattr(m, "_AFRICAS_TALKING_CONFIGURED", africastalking)
    monkeypatch.setattr(m, "_TWILIO_SMS_CONFIGURED", twilio)
    monkeypatch.setattr(m, "_EMAIL_OTP_CONFIGURED", email)


def test_whatsapp_configured_is_preferred_over_sms_and_email(monkeypatch):
    import backend.main as m
    _configure(monkeypatch, whatsapp=True, africastalking=True, twilio=True, email=True)

    calls = []
    monkeypatch.setattr(m, "_send_whatsapp_otp", lambda phone, code: (calls.append("whatsapp"), True)[1])
    monkeypatch.setattr(m, "_send_sms_via_africastalking", lambda phone, code, result_detail=None: (calls.append("africastalking"), True)[1])
    monkeypatch.setattr(m, "_send_sms_otp", lambda phone, code: (calls.append("sms"), True)[1])
    monkeypatch.setattr(m, "_send_email_otp", lambda email, code: (calls.append("email"), True)[1])

    channel = _send_otp_code("+263771234567", "user@example.com", "123456")

    assert channel == "whatsapp"
    assert calls == ["whatsapp"]  # Africa's Talking, Twilio, and email never attempted


def test_africastalking_preferred_over_twilio_when_whatsapp_not_configured(monkeypatch):
    """The actual point of tonight's change: Africa's Talking is checked
    before Twilio, not the other way round."""
    import backend.main as m
    _configure(monkeypatch, whatsapp=False, africastalking=True, twilio=True, email=True)

    calls = []
    monkeypatch.setattr(m, "_send_whatsapp_otp", lambda phone, code: (calls.append("whatsapp"), True)[1])
    monkeypatch.setattr(m, "_send_sms_via_africastalking", lambda phone, code, result_detail=None: (calls.append("africastalking"), True)[1])
    monkeypatch.setattr(m, "_send_sms_otp", lambda phone, code: (calls.append("sms"), True)[1])
    monkeypatch.setattr(m, "_send_email_otp", lambda email, code: (calls.append("email"), True)[1])

    channel = _send_otp_code("+263771234567", "user@example.com", "123456")

    assert channel == "sms"
    assert calls == ["africastalking"]  # Twilio never attempted -- Africa's Talking succeeded first


def test_falls_through_to_twilio_when_africastalking_configured_but_send_fails(monkeypatch):
    """Africa's Talking down/misconfigured doesn't strand the firm on SMS —
    the (unconfigured-today, but harmless) Twilio path is still there."""
    import backend.main as m
    _configure(monkeypatch, whatsapp=False, africastalking=True, twilio=True, email=True)

    calls = []
    monkeypatch.setattr(m, "_send_sms_via_africastalking", lambda phone, code, result_detail=None: (calls.append("africastalking"), False)[1])
    monkeypatch.setattr(m, "_send_sms_otp", lambda phone, code: (calls.append("sms"), True)[1])
    monkeypatch.setattr(m, "_send_email_otp", lambda email, code: (calls.append("email"), True)[1])

    channel = _send_otp_code("+263771234567", "user@example.com", "123456")

    assert channel == "sms"
    assert calls == ["africastalking", "sms"]


def test_sms_used_when_whatsapp_not_configured(monkeypatch):
    import backend.main as m
    _configure(monkeypatch, whatsapp=False, africastalking=False, twilio=True, email=True)

    calls = []
    monkeypatch.setattr(m, "_send_whatsapp_otp", lambda phone, code: (calls.append("whatsapp"), True)[1])
    monkeypatch.setattr(m, "_send_sms_otp", lambda phone, code: (calls.append("sms"), True)[1])
    monkeypatch.setattr(m, "_send_email_otp", lambda email, code: (calls.append("email"), True)[1])

    channel = _send_otp_code("+263771234567", "user@example.com", "123456")

    assert channel == "sms"
    assert calls == ["sms"]  # WhatsApp not configured, so never attempted; email never reached


def test_sms_used_when_whatsapp_configured_but_send_fails(monkeypatch):
    """Falls through mid-chain on a send failure, not just missing config."""
    import backend.main as m
    _configure(monkeypatch, whatsapp=True, africastalking=False, twilio=True, email=True)

    calls = []
    monkeypatch.setattr(m, "_send_whatsapp_otp", lambda phone, code: (calls.append("whatsapp"), False)[1])
    monkeypatch.setattr(m, "_send_sms_otp", lambda phone, code: (calls.append("sms"), True)[1])
    monkeypatch.setattr(m, "_send_email_otp", lambda email, code: (calls.append("email"), True)[1])

    channel = _send_otp_code("+263771234567", "user@example.com", "123456")

    assert channel == "sms"
    assert calls == ["whatsapp", "sms"]


def test_falls_back_to_email_when_neither_whatsapp_nor_sms_configured(monkeypatch):
    import backend.main as m
    _configure(monkeypatch, whatsapp=False, africastalking=False, twilio=False, email=True)

    calls = []
    monkeypatch.setattr(m, "_send_whatsapp_otp", lambda phone, code: (calls.append("whatsapp"), True)[1])
    monkeypatch.setattr(m, "_send_sms_otp", lambda phone, code: (calls.append("sms"), True)[1])
    monkeypatch.setattr(m, "_send_email_otp", lambda email, code: (calls.append("email"), True)[1])

    channel = _send_otp_code("+263771234567", "user@example.com", "123456")

    assert channel == "email"
    assert calls == ["email"]


def test_returns_none_when_nothing_configured_and_no_email_on_file(monkeypatch):
    import backend.main as m
    _configure(monkeypatch, whatsapp=False, africastalking=False, twilio=False, email=False)

    monkeypatch.setattr(m, "_send_whatsapp_otp", lambda phone, code: True)
    monkeypatch.setattr(m, "_send_sms_otp", lambda phone, code: True)
    monkeypatch.setattr(m, "_send_email_otp", lambda email, code: True)

    channel = _send_otp_code("+263771234567", None, "123456")

    assert channel is None


def test_returns_none_when_email_configured_but_no_email_on_file(monkeypatch):
    """email=None (no address on record for this user) must not crash — just
    skip the last channel, same as if email delivery weren't configured."""
    import backend.main as m
    _configure(monkeypatch, whatsapp=False, africastalking=False, twilio=False, email=True)

    monkeypatch.setattr(m, "_send_email_otp", lambda email, code: True)

    channel = _send_otp_code("+263771234567", None, "123456")

    assert channel is None


# ── _send_sms_otp itself: Twilio REST call shape ────────────────────────────

class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def test_send_sms_otp_posts_to_twilio_with_expected_payload(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "TWILIO_ACCOUNT_SID", "ACtestsid")
    monkeypatch.setattr(m, "TWILIO_AUTH_TOKEN", "testtoken")
    monkeypatch.setattr(m, "TWILIO_FROM_NUMBER", "+15005550006")

    captured = {}
    def fake_post(url, auth=None, data=None, timeout=None):
        captured["url"] = url
        captured["auth"] = auth
        captured["data"] = data
        return _FakeResponse(201)
    monkeypatch.setattr(httpx, "post", fake_post)

    result = _send_sms_otp("+263771234567", "654321")

    assert result is True
    assert captured["url"] == "https://api.twilio.com/2010-04-01/Accounts/ACtestsid/Messages.json"
    assert captured["auth"] == ("ACtestsid", "testtoken")
    assert captured["data"]["To"] == "+263771234567"
    assert captured["data"]["From"] == "+15005550006"
    assert captured["data"]["Body"] == "Your Mutemo Desk login code is 654321. It expires in 5 minutes."


def test_send_sms_otp_returns_false_on_non_2xx(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "TWILIO_ACCOUNT_SID", "ACtestsid")
    monkeypatch.setattr(m, "TWILIO_AUTH_TOKEN", "testtoken")
    monkeypatch.setattr(m, "TWILIO_FROM_NUMBER", "+15005550006")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(400, "Invalid 'To' Phone Number"))

    assert _send_sms_otp("+263771234567", "654321") is False


def test_send_sms_otp_returns_false_on_exception(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "TWILIO_ACCOUNT_SID", "ACtestsid")
    monkeypatch.setattr(m, "TWILIO_AUTH_TOKEN", "testtoken")
    monkeypatch.setattr(m, "TWILIO_FROM_NUMBER", "+15005550006")
    def raise_timeout(*a, **k):
        raise httpx.TimeoutException("timed out")
    monkeypatch.setattr(httpx, "post", raise_timeout)

    assert _send_sms_otp("+263771234567", "654321") is False


# ── _send_sms_via_africastalking itself: Africa's Talking REST call shape ──
#
# Genuinely different from the Twilio tests above: Africa's Talking returns
# HTTP 200/201 even when the individual recipient failed (bad number, no
# credit, etc.) -- the real success/failure signal lives in the JSON body at
# SMSMessageData.Recipients[0].status, not the HTTP status code. So unlike
# _FakeResponse above, this fake needs a working .json() method, and there's
# a dedicated test for the "200 but recipient actually failed" case, which
# has no Twilio equivalent.

class _FakeATResponse:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self._body = body or {}
        self.text = text or json.dumps(self._body)

    def json(self):
        return self._body


def _at_success_body(status="Success", status_code=101):
    return {
        "SMSMessageData": {
            "Message": "Sent to 1/1 Total Cost: ZWL 0.5000",
            "Recipients": [
                {"statusCode": status_code, "number": "+263771234567", "status": status, "cost": "ZWL 0.5000", "messageId": "ATXid_test"}
            ],
        }
    }


def test_send_sms_via_africastalking_posts_with_expected_payload(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "AFRICAS_TALKING_USERNAME", "tofambatech")
    monkeypatch.setattr(m, "AFRICAS_TALKING_API_KEY", "testkey")
    monkeypatch.setattr(m, "AFRICAS_TALKING_FROM", None)

    captured = {}
    def fake_post(url, headers=None, data=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["data"] = data
        return _FakeATResponse(201, _at_success_body())
    monkeypatch.setattr(httpx, "post", fake_post)

    result = _send_sms_via_africastalking("+263771234567", "654321")

    assert result is True
    assert captured["url"] == "https://api.africastalking.com/version1/messaging"
    assert captured["headers"]["apiKey"] == "testkey"
    assert captured["data"]["username"] == "tofambatech"
    assert captured["data"]["to"] == "+263771234567"
    assert captured["data"]["message"] == "Your Mutemo Desk login code is 654321. It expires in 5 minutes."
    assert "from" not in captured["data"]  # optional sender ID omitted when unset


def test_send_sms_via_africastalking_includes_optional_from_when_set(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "AFRICAS_TALKING_USERNAME", "tofambatech")
    monkeypatch.setattr(m, "AFRICAS_TALKING_API_KEY", "testkey")
    monkeypatch.setattr(m, "AFRICAS_TALKING_FROM", "MUTEMO")

    captured = {}
    def fake_post(url, headers=None, data=None, timeout=None):
        captured["data"] = data
        return _FakeATResponse(200, _at_success_body())
    monkeypatch.setattr(httpx, "post", fake_post)

    assert _send_sms_via_africastalking("+263771234567", "654321") is True
    assert captured["data"]["from"] == "MUTEMO"


def test_send_sms_via_africastalking_returns_false_on_non_2xx(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "AFRICAS_TALKING_USERNAME", "tofambatech")
    monkeypatch.setattr(m, "AFRICAS_TALKING_API_KEY", "testkey")
    monkeypatch.setattr(m, "AFRICAS_TALKING_FROM", None)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeATResponse(401, text="Invalid apiKey"))

    assert _send_sms_via_africastalking("+263771234567", "654321") is False


def test_send_sms_via_africastalking_returns_false_when_recipient_failed_despite_200(monkeypatch):
    """The Africa's Talking-specific case with no Twilio equivalent: HTTP
    200 does NOT mean the SMS was actually sent -- must check the
    per-recipient status inside the JSON body."""
    import backend.main as m
    monkeypatch.setattr(m, "AFRICAS_TALKING_USERNAME", "tofambatech")
    monkeypatch.setattr(m, "AFRICAS_TALKING_API_KEY", "testkey")
    monkeypatch.setattr(m, "AFRICAS_TALKING_FROM", None)
    failed_body = _at_success_body(status="InsufficientBalance", status_code=401)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeATResponse(200, failed_body))

    assert _send_sms_via_africastalking("+263771234567", "654321") is False


def test_send_sms_via_africastalking_returns_false_on_empty_recipients(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "AFRICAS_TALKING_USERNAME", "tofambatech")
    monkeypatch.setattr(m, "AFRICAS_TALKING_API_KEY", "testkey")
    monkeypatch.setattr(m, "AFRICAS_TALKING_FROM", None)
    empty_body = {"SMSMessageData": {"Message": "error", "Recipients": []}}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeATResponse(200, empty_body))

    assert _send_sms_via_africastalking("+263771234567", "654321") is False


def test_send_sms_via_africastalking_returns_false_on_exception(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "AFRICAS_TALKING_USERNAME", "tofambatech")
    monkeypatch.setattr(m, "AFRICAS_TALKING_API_KEY", "testkey")
    monkeypatch.setattr(m, "AFRICAS_TALKING_FROM", None)
    def raise_timeout(*a, **k):
        raise httpx.TimeoutException("timed out")
    monkeypatch.setattr(httpx, "post", raise_timeout)

    assert _send_sms_via_africastalking("+263771234567", "654321") is False


# ── result_detail out-param: feeds sms_usage_log without a second real send ─

def test_result_detail_populated_on_success(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "AFRICAS_TALKING_USERNAME", "tofambatech")
    monkeypatch.setattr(m, "AFRICAS_TALKING_API_KEY", "testkey")
    monkeypatch.setattr(m, "AFRICAS_TALKING_FROM", None)
    body = _at_success_body(status="Success", status_code=100)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeATResponse(200, body))

    detail = {}
    result = _send_sms_via_africastalking("+263771234567", "654321", result_detail=detail)

    assert result is True
    assert detail == {
        "success": True, "provider_status": "Success", "status_code": 100,
        "message_id": "ATXid_test", "cost": "ZWL 0.5000",
    }


def test_result_detail_populated_on_provider_rejection(monkeypatch):
    """The real InsufficientBalance case from production, 2026-08-30."""
    import backend.main as m
    monkeypatch.setattr(m, "AFRICAS_TALKING_USERNAME", "tofambatech")
    monkeypatch.setattr(m, "AFRICAS_TALKING_API_KEY", "testkey")
    monkeypatch.setattr(m, "AFRICAS_TALKING_FROM", None)
    body = _at_success_body(status="InsufficientBalance", status_code=405)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeATResponse(200, body))

    detail = {}
    result = _send_sms_via_africastalking("+263771234567", "654321", result_detail=detail)

    assert result is False
    assert detail["success"] is False
    assert detail["provider_status"] == "InsufficientBalance"
    assert detail["status_code"] == 405


def test_result_detail_populated_on_http_failure(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "AFRICAS_TALKING_USERNAME", "tofambatech")
    monkeypatch.setattr(m, "AFRICAS_TALKING_API_KEY", "testkey")
    monkeypatch.setattr(m, "AFRICAS_TALKING_FROM", None)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeATResponse(401, text="Invalid apiKey"))

    detail = {}
    result = _send_sms_via_africastalking("+263771234567", "654321", result_detail=detail)

    assert result is False
    assert detail["success"] is False
    assert "401" in detail["error"]
    assert "provider_status" not in detail  # no per-recipient AT response existed to read one from


def test_result_detail_populated_on_exception(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "AFRICAS_TALKING_USERNAME", "tofambatech")
    monkeypatch.setattr(m, "AFRICAS_TALKING_API_KEY", "testkey")
    monkeypatch.setattr(m, "AFRICAS_TALKING_FROM", None)
    def raise_timeout(*a, **k):
        raise httpx.TimeoutException("timed out")
    monkeypatch.setattr(httpx, "post", raise_timeout)

    detail = {}
    result = _send_sms_via_africastalking("+263771234567", "654321", result_detail=detail)

    assert result is False
    assert detail == {"success": False, "error": "timed out"}


def test_result_detail_left_untouched_when_not_passed(monkeypatch):
    """Every existing caller that doesn't pass result_detail is unaffected --
    the parameter is purely additive."""
    import backend.main as m
    monkeypatch.setattr(m, "AFRICAS_TALKING_USERNAME", "tofambatech")
    monkeypatch.setattr(m, "AFRICAS_TALKING_API_KEY", "testkey")
    monkeypatch.setattr(m, "AFRICAS_TALKING_FROM", None)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeATResponse(200, _at_success_body()))

    assert _send_sms_via_africastalking("+263771234567", "654321") is True  # no crash, no 3rd positional arg needed


def test_send_otp_code_collects_africastalking_attempt_into_sms_attempt_log(monkeypatch):
    """The actual wiring _log_sms_usage relies on: _send_otp_code appends
    one dict per real Africa's Talking attempt into the caller-supplied list."""
    import backend.main as m
    _configure(monkeypatch, whatsapp=False, africastalking=True, twilio=False, email=False)
    monkeypatch.setattr(m, "AFRICAS_TALKING_USERNAME", "tofambatech")
    monkeypatch.setattr(m, "AFRICAS_TALKING_API_KEY", "testkey")
    monkeypatch.setattr(m, "AFRICAS_TALKING_FROM", None)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeATResponse(200, _at_success_body(status_code=100)))

    log: list = []
    channel = _send_otp_code("+263771234567", None, "654321", sms_attempt_log=log)

    assert channel == "sms"
    assert len(log) == 1
    assert log[0]["success"] is True
    assert log[0]["status_code"] == 100


# ── _parse_at_cost: splits Africa's Talking's "CUR 0.0000"-style string ────

def test_parse_at_cost_splits_currency_and_amount():
    from backend.main import _parse_at_cost
    amount, currency = _parse_at_cost("USD 0.0500")
    assert amount == 0.05
    assert currency == "USD"


def test_parse_at_cost_handles_none():
    from backend.main import _parse_at_cost
    assert _parse_at_cost(None) == (None, None)


def test_parse_at_cost_handles_malformed_string():
    from backend.main import _parse_at_cost
    assert _parse_at_cost("garbage") == (None, None)
    assert _parse_at_cost("") == (None, None)
    assert _parse_at_cost("USD not-a-number") == (None, None)
