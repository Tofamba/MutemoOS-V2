"""
Unit tests for _send_otp_code's channel fallback chain in backend/main.py:
WhatsApp -> Twilio SMS -> email -> None.

Mocks the three channel-send functions and the module-level config flags
directly rather than setting real env vars (those are read once at import
time into module-level constants, so patching os.environ after import
wouldn't have any effect) — same direct-monkeypatch-of-module-attributes
approach used throughout this repo's other backend tests (see
tests/test_clients_api.py, tests/test_matter_client_linking.py).
"""

import httpx

from backend.main import _send_otp_code, _send_sms_otp


def _configure(monkeypatch, *, whatsapp=False, twilio=False, email=False):
    import backend.main as m
    monkeypatch.setattr(m, "WHATSAPP_ACCESS_TOKEN", "token" if whatsapp else None)
    monkeypatch.setattr(m, "WHATSAPP_PHONE_NUMBER_ID", "phone-id" if whatsapp else None)
    monkeypatch.setattr(m, "_TWILIO_SMS_CONFIGURED", twilio)
    monkeypatch.setattr(m, "_EMAIL_OTP_CONFIGURED", email)


def test_whatsapp_configured_is_preferred_over_sms_and_email(monkeypatch):
    import backend.main as m
    _configure(monkeypatch, whatsapp=True, twilio=True, email=True)

    calls = []
    monkeypatch.setattr(m, "_send_whatsapp_otp", lambda phone, code: (calls.append("whatsapp"), True)[1])
    monkeypatch.setattr(m, "_send_sms_otp", lambda phone, code: (calls.append("sms"), True)[1])
    monkeypatch.setattr(m, "_send_email_otp", lambda email, code: (calls.append("email"), True)[1])

    channel = _send_otp_code("+263771234567", "user@example.com", "123456")

    assert channel == "whatsapp"
    assert calls == ["whatsapp"]  # SMS and email never attempted


def test_sms_used_when_whatsapp_not_configured(monkeypatch):
    import backend.main as m
    _configure(monkeypatch, whatsapp=False, twilio=True, email=True)

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
    _configure(monkeypatch, whatsapp=True, twilio=True, email=True)

    calls = []
    monkeypatch.setattr(m, "_send_whatsapp_otp", lambda phone, code: (calls.append("whatsapp"), False)[1])
    monkeypatch.setattr(m, "_send_sms_otp", lambda phone, code: (calls.append("sms"), True)[1])
    monkeypatch.setattr(m, "_send_email_otp", lambda email, code: (calls.append("email"), True)[1])

    channel = _send_otp_code("+263771234567", "user@example.com", "123456")

    assert channel == "sms"
    assert calls == ["whatsapp", "sms"]


def test_falls_back_to_email_when_neither_whatsapp_nor_sms_configured(monkeypatch):
    import backend.main as m
    _configure(monkeypatch, whatsapp=False, twilio=False, email=True)

    calls = []
    monkeypatch.setattr(m, "_send_whatsapp_otp", lambda phone, code: (calls.append("whatsapp"), True)[1])
    monkeypatch.setattr(m, "_send_sms_otp", lambda phone, code: (calls.append("sms"), True)[1])
    monkeypatch.setattr(m, "_send_email_otp", lambda email, code: (calls.append("email"), True)[1])

    channel = _send_otp_code("+263771234567", "user@example.com", "123456")

    assert channel == "email"
    assert calls == ["email"]


def test_returns_none_when_nothing_configured_and_no_email_on_file(monkeypatch):
    import backend.main as m
    _configure(monkeypatch, whatsapp=False, twilio=False, email=False)

    monkeypatch.setattr(m, "_send_whatsapp_otp", lambda phone, code: True)
    monkeypatch.setattr(m, "_send_sms_otp", lambda phone, code: True)
    monkeypatch.setattr(m, "_send_email_otp", lambda email, code: True)

    channel = _send_otp_code("+263771234567", None, "123456")

    assert channel is None


def test_returns_none_when_email_configured_but_no_email_on_file(monkeypatch):
    """email=None (no address on record for this user) must not crash — just
    skip the last channel, same as if email delivery weren't configured."""
    import backend.main as m
    _configure(monkeypatch, whatsapp=False, twilio=False, email=True)

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
