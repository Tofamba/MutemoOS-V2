"""
Unit tests for the LEGAL_FEED_SERVICE_TOKEN separation in backend/main.py's
upload_legal_update() and upload_zlr_document() — mutemo-legal-feed's own
machine-to-machine credential, deliberately distinct from MUTEMO_ADMIN_TOKEN
(which also unlocks /api/admin/*) so a leaked feed credential can't reach
anything beyond these two upload endpoints.

Rather than depending on AUTH_ENABLED (which is False in this test process,
making the get_current_user() fallback always succeed via the synthetic
dev user regardless of what's wrong with the token — not a useful signal),
these tests monkeypatch _check_permission() as a spy/raiser to observe
directly whether the token short-circuits the permission-check fallback or
not. That's the actual behavior worth locking down.

Called directly as plain async functions, same convention as
tests/test_clients_api.py.
"""
import asyncio
import uuid
from datetime import datetime, timezone

import pytest

from backend.main import FIRM_ID, upload_legal_update, upload_zlr_document


class FakeConnection:
    def __init__(self):
        self.inserted = []

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO legal_updates"):
            row = {"id": args[0], "firm_id": args[1], "filename": args[2], "source_url": args[6]}
            self.inserted.append(row)
            return row
        if q.startswith("INSERT INTO zlr_entries"):
            row = {"id": args[0], "firm_id": args[1], "filename": args[2], "zimlii_url": args[5]}
            self.inserted.append(row)
            return row
        raise NotImplementedError(f"FakeConnection.fetchrow: unhandled query: {q}")

    async def fetch(self, query, *args):
        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {query}")

    async def execute(self, query, *args):
        return "OK"


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self):
        self.conn = FakeConnection()

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


class FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.cookies = {}


class FakeBackgroundTasks:
    def add_task(self, *args, **kwargs):
        pass


def _permission_denying_check(monkeypatch, module):
    """Replace _check_permission with one that always raises — if a test
    still succeeds, the token path must have bypassed it entirely; if a
    test fails with this exact error, the fallback permission-check path
    was reached instead."""
    def _deny(user, permission):
        raise AssertionError("PERMISSION_CHECK_REACHED")
    monkeypatch.setattr(module, "_check_permission", _deny)


def test_correct_feed_token_bypasses_permission_check_entirely(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    monkeypatch.setattr(m, "LEGAL_FEED_SERVICE_TOKEN", "test-feed-token")
    _permission_denying_check(monkeypatch, m)

    result = asyncio.run(upload_legal_update(
        background_tasks=FakeBackgroundTasks(),
        file=None, source_type="legislation", source_name="Veritas", reference="SI 123/2026",
        source_url="https://veritaszim.net/x", scraped_at="", summary="", title="Test Act",
        request=FakeRequest(headers={"X-Feed-Service-Token": "test-feed-token"}),
    ))
    assert result["processing"] is True


def test_missing_feed_token_falls_back_to_permission_check(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    monkeypatch.setattr(m, "LEGAL_FEED_SERVICE_TOKEN", "test-feed-token")
    _permission_denying_check(monkeypatch, m)

    with pytest.raises(AssertionError, match="PERMISSION_CHECK_REACHED"):
        asyncio.run(upload_legal_update(
            background_tasks=FakeBackgroundTasks(),
            file=None, source_type="legislation", source_name="Veritas", reference="",
            source_url="https://veritaszim.net/y", scraped_at="", summary="", title="",
            request=FakeRequest(headers={}),
        ))


def test_old_admin_token_header_no_longer_authenticates_the_feed(monkeypatch):
    """The whole point of this change: MUTEMO_ADMIN_TOKEN (sent as
    X-Admin-Token, the feed's old header) must NOT satisfy the new check —
    only LEGAL_FEED_SERVICE_TOKEN via X-Feed-Service-Token does."""
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    monkeypatch.setattr(m, "LEGAL_FEED_SERVICE_TOKEN", "test-feed-token")
    monkeypatch.setattr(m, "ADMIN_TOKEN", "old-admin-token")
    _permission_denying_check(monkeypatch, m)

    with pytest.raises(AssertionError, match="PERMISSION_CHECK_REACHED"):
        asyncio.run(upload_legal_update(
            background_tasks=FakeBackgroundTasks(),
            file=None, source_type="legislation", source_name="Veritas", reference="",
            source_url="https://veritaszim.net/z", scraped_at="", summary="", title="",
            request=FakeRequest(headers={"X-Admin-Token": "old-admin-token"}),
        ))


def test_zlr_upload_correct_feed_token_bypasses_permission_check(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    monkeypatch.setattr(m, "LEGAL_FEED_SERVICE_TOKEN", "test-feed-token")
    _permission_denying_check(monkeypatch, m)

    result = asyncio.run(upload_zlr_document(
        background_tasks=FakeBackgroundTasks(),
        file=None, source="ZLR", volume_year=None, zimlii_url=None,
        source_url="https://zimlii.org/case/1", case_name="S v Test", citation="",
        court="", judge="", judgment_date="", summary="", scraped_at="",
        request=FakeRequest(headers={"X-Feed-Service-Token": "test-feed-token"}),
    ))
    assert result is not None


def test_zlr_upload_old_admin_token_no_longer_authenticates(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool())
    monkeypatch.setattr(m, "LEGAL_FEED_SERVICE_TOKEN", "test-feed-token")
    monkeypatch.setattr(m, "ADMIN_TOKEN", "old-admin-token")
    _permission_denying_check(monkeypatch, m)

    with pytest.raises(AssertionError, match="PERMISSION_CHECK_REACHED"):
        asyncio.run(upload_zlr_document(
            background_tasks=FakeBackgroundTasks(),
            file=None, source="ZLR", volume_year=None, zimlii_url=None,
            source_url="https://zimlii.org/case/2", case_name="S v Test2", citation="",
            court="", judge="", judgment_date="", summary="", scraped_at="",
            request=FakeRequest(headers={"X-Admin-Token": "old-admin-token"}),
        ))
