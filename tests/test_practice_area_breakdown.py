"""
Unit tests for GET /api/reports/practice-area-breakdown (backend/main.py) —
the partner-facing matter count per practice_area, firm-wide.

Called directly as plain async functions, same convention as this repo's
other backend tests (see tests/test_docx_export.py's docstring for why).
AUTH_ENABLED is False by default, so get_current_user() normally returns a
synthetic partner user — enough for the "partner succeeds" case; the
"associate gets 403" case monkeypatches get_current_user directly, same
pattern as tests/test_rbz_compliance_export.py.
"""

import asyncio
import uuid

import pytest
from fastapi import HTTPException

from backend.main import FIRM_ID, practice_area_breakdown


class FakeConnection:
    def __init__(self, matters):
        self.matters = matters

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT practice_area, COUNT(*) AS matter_count FROM matters"):
            firm_id, = args
            counts = {}
            for m in self.matters:
                if m["firm_id"] != firm_id:
                    continue
                counts[m["practice_area"]] = counts.get(m["practice_area"], 0) + 1
            rows = [{"practice_area": pa, "matter_count": n} for pa, n in counts.items()]
            rows.sort(key=lambda r: -r["matter_count"])
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
    def __init__(self, matters):
        self.conn = FakeConnection(matters)

    def acquire(self):
        return _FakeAcquireCtx(self.conn)


def _matter(practice_area):
    return {"id": uuid.uuid4(), "firm_id": FIRM_ID, "practice_area": practice_area}


def _as_current_user(monkeypatch, m, user_dict):
    async def fake_get_current_user(request):
        return user_dict
    monkeypatch.setattr(m, "get_current_user", fake_get_current_user)


def test_associate_gets_403(monkeypatch):
    import backend.main as m
    associate = {"id": uuid.uuid4(), "firm_id": FIRM_ID, "email": "a@sm.co.zw",
                 "role": "associate", "display_name": "Assoc Person"}
    monkeypatch.setattr(m, "_db_pool", FakePool([]))
    _as_current_user(monkeypatch, m, associate)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(practice_area_breakdown(None))
    assert exc_info.value.status_code == 403


def test_partner_gets_correct_counts_grouped_by_category(monkeypatch):
    import backend.main as m
    matters = [
        _matter("Trust"), _matter("Trust"), _matter("Trust"),
        _matter("Debt Collection"),
        _matter("Family/Matrimonial"), _matter("Family/Matrimonial"),
    ]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters))

    result = asyncio.run(practice_area_breakdown(None))  # synthetic partner user, AUTH_ENABLED=False

    by_area = {r["practice_area"]: r["matter_count"] for r in result}
    assert by_area == {"Trust": 3, "Debt Collection": 1, "Family/Matrimonial": 2}
    assert result[0]["practice_area"] == "Trust"  # sorted by count descending


def test_uncategorized_matters_are_grouped_not_omitted(monkeypatch):
    import backend.main as m
    matters = [_matter("Trust"), _matter(None), _matter(None)]
    monkeypatch.setattr(m, "_db_pool", FakePool(matters))

    result = asyncio.run(practice_area_breakdown(None))

    by_area = {r["practice_area"]: r["matter_count"] for r in result}
    assert by_area["Uncategorized"] == 2
    assert by_area["Trust"] == 1
    assert sum(by_area.values()) == 3  # nothing dropped from the total


def test_no_matters_returns_empty_list(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", FakePool([]))

    result = asyncio.run(practice_area_breakdown(None))

    assert result == []
