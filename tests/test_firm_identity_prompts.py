"""
Unit tests for Part 1 of the multi-tenancy hardening pass: AI system
prompts (contract review, legal research, affidavit, document drafting)
now resolve the firm's name/city live from the `firms` table via
get_firm_identity(), instead of the frozen FIRM_NAME/FIRM_CITY constants
(set once at process start from env vars, and stale the moment
PATCH /api/settings renames the firm without a restart).

Two things every prompt site is checked for:
1. The value genuinely traces to the DB, not the constant -- proven by
   using a firms row whose name/city differs from FIRM_NAME/FIRM_CITY and
   confirming the *different* value is what reaches the model.
2. Zero regression for the real current firm -- proven by confirming the
   prompt text is byte-identical to the old hardcoded-constant template
   when get_firm_identity() returns exactly what's actually seeded in
   production today (name=FIRM_NAME, city="Harare" -- see
   run_migrations()'s INSERT INTO firms seed, which hardcodes "Harare"
   directly rather than deriving it from FIRM_CITY).

Called directly as plain async functions / sync calls, same convention
as tests/test_contract_review_verification.py and
tests/test_synthesis_token_budget.py (which this mirrors for capturing
the exact `system=` kwarg sent to client.messages.create).
"""
import asyncio
from types import SimpleNamespace

import pytest

import backend.main as m
from backend.main import (
    AffidavitRequest,
    CONTRACT_REVIEW_SYSTEM,
    DOCUMENT_SYSTEM_BASE,
    FIRM_NAME,
    generate_affidavit,
    get_firm_identity,
    review_contract,
    synthesise_answer_sync,
)


class FakeUploadFile:
    def __init__(self, filename, content: bytes):
        self.filename = filename
        self._content = content

    async def read(self):
        return self._content


class _FakeAcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _FirmsFakeConnection:
    def __init__(self, name, city):
        self.name = name
        self.city = city

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT name, city FROM firms WHERE id=$1"):
            return {"name": self.name, "city": self.city}
        raise NotImplementedError(f"unhandled query: {q}")


class _FirmsFakePool:
    def __init__(self, name, city):
        self.conn = _FirmsFakeConnection(name, city)

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


DIFFERENT_FIRM_NAME = "Zvirevo & Partners"
DIFFERENT_FIRM_CITY = "Bulawayo"


# ── get_firm_identity(): traces to the DB, not the frozen constants ─────

def test_get_firm_identity_reads_from_the_firms_table(monkeypatch):
    monkeypatch.setattr(m, "_db_pool", _FirmsFakePool(DIFFERENT_FIRM_NAME, DIFFERENT_FIRM_CITY))

    result = asyncio.run(get_firm_identity())

    assert result == {"name": DIFFERENT_FIRM_NAME, "city": DIFFERENT_FIRM_CITY}
    assert result["name"] != FIRM_NAME  # proves it isn't just echoing the constant


def test_get_firm_identity_falls_back_to_constants_when_firm_row_has_no_name(monkeypatch):
    monkeypatch.setattr(m, "_db_pool", _FirmsFakePool(None, None))

    result = asyncio.run(get_firm_identity())

    assert result == {"name": m.FIRM_NAME, "city": m.FIRM_CITY}


# ── Contract review: system prompt traces to the DB ──────────────────────

def test_contract_review_prompt_uses_the_live_firm_name_not_the_constant(monkeypatch):
    monkeypatch.setattr(m, "get_firm_identity", lambda: _async_return({"name": DIFFERENT_FIRM_NAME, "city": DIFFERENT_FIRM_CITY}))
    captured = {}

    def fake_create(**kwargs):
        captured["system"] = kwargs.get("system")
        tool_use_block = SimpleNamespace(type="tool_use", input={"overall_summary": "s", "findings": []})
        return SimpleNamespace(content=[tool_use_block], stop_reason="tool_use", usage=SimpleNamespace(output_tokens=1))

    monkeypatch.setattr(m.client.messages, "create", fake_create)
    upload = FakeUploadFile("contract.txt", b"Some contract body text.")

    asyncio.run(review_contract(None, upload, None))

    assert DIFFERENT_FIRM_NAME in captured["system"]
    assert DIFFERENT_FIRM_CITY in captured["system"]
    assert FIRM_NAME not in captured["system"] or FIRM_NAME == ""


def test_contract_review_prompt_is_byte_identical_for_the_real_current_firm(monkeypatch):
    """Zero-regression check: get_firm_identity() returning exactly what's
    actually seeded for Sawyer & Mkushi today (name=FIRM_NAME, city=
    "Harare" -- the literal run_migrations() seeds, not derived from
    FIRM_CITY) must produce the exact same system prompt the old
    hardcoded-constant template did."""
    monkeypatch.setattr(m, "get_firm_identity", lambda: _async_return({"name": FIRM_NAME, "city": "Harare"}))
    captured = {}

    def fake_create(**kwargs):
        captured["system"] = kwargs.get("system")
        tool_use_block = SimpleNamespace(type="tool_use", input={"overall_summary": "s", "findings": []})
        return SimpleNamespace(content=[tool_use_block], stop_reason="tool_use", usage=SimpleNamespace(output_tokens=1))

    monkeypatch.setattr(m.client.messages, "create", fake_create)
    upload = FakeUploadFile("contract.txt", b"Some contract body text.")

    asyncio.run(review_contract(None, upload, None))

    old_template_output = CONTRACT_REVIEW_SYSTEM.format(FIRM_NAME=FIRM_NAME, FIRM_CITY="Harare")
    assert captured["system"] == old_template_output


# ── Affidavit: system prompt traces to the DB ────────────────────────────

def test_affidavit_prompt_uses_the_live_firm_name_not_the_constant(monkeypatch):
    monkeypatch.setattr(m, "get_firm_identity", lambda: _async_return({"name": DIFFERENT_FIRM_NAME, "city": DIFFERENT_FIRM_CITY}))
    captured = {}

    def fake_create(**kwargs):
        captured["system"] = kwargs.get("system")
        return SimpleNamespace(content=[SimpleNamespace(text="AFFIDAVIT TEXT")])

    monkeypatch.setattr(m.client.messages, "create", fake_create)
    req = AffidavitRequest(matter_summary="A dispute over unpaid rent.")

    asyncio.run(generate_affidavit(req, None))

    assert DIFFERENT_FIRM_NAME in captured["system"]
    assert DIFFERENT_FIRM_CITY in captured["system"]


def test_affidavit_prompt_is_byte_identical_for_the_real_current_firm(monkeypatch):
    monkeypatch.setattr(m, "get_firm_identity", lambda: _async_return({"name": FIRM_NAME, "city": "Harare"}))
    captured = {}

    def fake_create(**kwargs):
        captured["system"] = kwargs.get("system")
        return SimpleNamespace(content=[SimpleNamespace(text="AFFIDAVIT TEXT")])

    monkeypatch.setattr(m.client.messages, "create", fake_create)
    req = AffidavitRequest(matter_summary="A dispute over unpaid rent.")

    asyncio.run(generate_affidavit(req, None))

    assert captured["system"] == m.AFFIDAVIT_SYSTEM.format(FIRM_NAME=FIRM_NAME, FIRM_CITY="Harare")


# ── synthesise_answer_sync: firm_name/firm_city parameters ───────────────

def test_synthesise_answer_sync_uses_passed_firm_identity_not_the_constant():
    captured = {}

    class _FakeMsg:
        content = [SimpleNamespace(text="ANSWER")]

    def fake_create(**kwargs):
        captured["content"] = kwargs["messages"][0]["content"]
        return _FakeMsg()

    orig_create = m.client.messages.create
    m.client.messages.create = fake_create
    try:
        synthesise_answer_sync(
            "a query", [{"text": "x", "similarity": 0.9}], [], [],
            firm_name=DIFFERENT_FIRM_NAME, firm_city=DIFFERENT_FIRM_CITY,
        )
    finally:
        m.client.messages.create = orig_create

    assert f"legal research assistant for {DIFFERENT_FIRM_NAME}, {DIFFERENT_FIRM_CITY}" in captured["content"]


def test_synthesise_answer_sync_falls_back_to_constants_when_not_passed():
    """Backward-compat: a caller that doesn't pass firm_name/firm_city
    (there shouldn't be any left, but the default exists defensively)
    still gets the frozen constants rather than crashing."""
    captured = {}

    class _FakeMsg:
        content = [SimpleNamespace(text="ANSWER")]

    def fake_create(**kwargs):
        captured["content"] = kwargs["messages"][0]["content"]
        return _FakeMsg()

    orig_create = m.client.messages.create
    m.client.messages.create = fake_create
    try:
        synthesise_answer_sync("a query", [{"text": "x", "similarity": 0.9}], [], [])
    finally:
        m.client.messages.create = orig_create

    assert f"legal research assistant for {m.FIRM_NAME}, {m.FIRM_CITY}" in captured["content"]


# ── Document drafting: system prompt traces to the DB ────────────────────

def test_call_document_generation_model_uses_the_passed_firm_identity():
    captured = {}

    def fake_create(**kwargs):
        captured["system"] = kwargs.get("system")
        return SimpleNamespace(content=[SimpleNamespace(text="DOC")])

    orig_create = m.client.messages.create
    m.client.messages.create = fake_create
    try:
        m._call_document_generation_model("Draft this.", 4096, DIFFERENT_FIRM_NAME, DIFFERENT_FIRM_CITY)
    finally:
        m.client.messages.create = orig_create

    assert captured["system"] == DOCUMENT_SYSTEM_BASE.format(FIRM_NAME=DIFFERENT_FIRM_NAME, FIRM_CITY=DIFFERENT_FIRM_CITY)
    assert DIFFERENT_FIRM_NAME in captured["system"]


def test_call_document_generation_model_is_byte_identical_for_the_real_current_firm():
    captured = {}

    def fake_create(**kwargs):
        captured["system"] = kwargs.get("system")
        return SimpleNamespace(content=[SimpleNamespace(text="DOC")])

    orig_create = m.client.messages.create
    m.client.messages.create = fake_create
    try:
        m._call_document_generation_model("Draft this.", 4096, FIRM_NAME, "Harare")
    finally:
        m.client.messages.create = orig_create

    assert captured["system"] == DOCUMENT_SYSTEM_BASE.format(FIRM_NAME=FIRM_NAME, FIRM_CITY="Harare")


async def _async_return(value):
    return value
