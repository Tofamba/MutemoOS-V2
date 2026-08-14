"""
Unit tests for scripts/backfill_practice_areas.py's _build_plan() — the
one-time backfill that assigns practice_area to every existing matter that
doesn't have one, via backend.practice_areas.classify_practice_area().

Confirms the core promise: a confident single-category match gets applied,
while anything ambiguous or unrecognized lands in the review list rather
than being force-categorized — same convention as every other backfill
this week.
"""

import asyncio
import uuid
from datetime import datetime, timezone

from scripts.backfill_practice_areas import FIRM_ID, _build_plan

FIRM_UUID = uuid.UUID(FIRM_ID)


class FakeConnection:
    def __init__(self, matters):
        self.matters = matters

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT id, name FROM matters WHERE firm_id=$1 AND practice_area IS NULL"):
            firm_id, = args
            rows = [m for m in self.matters if m["firm_id"] == firm_id and m.get("practice_area") is None]
            rows.sort(key=lambda m: m["created_at"])
            return [{"id": m["id"], "name": m["name"]} for m in rows]
        raise NotImplementedError(f"FakeConnection.fetch: unhandled query: {q}")


def _matter(name, practice_area=None, created_at=None):
    return {
        "id": uuid.uuid4(), "firm_id": FIRM_UUID, "name": name, "practice_area": practice_area,
        "created_at": created_at or datetime.now(timezone.utc),
    }


def test_confident_match_gets_applied():
    matters = [_matter("Family Trust — Trust")]
    conn = FakeConnection(matters)

    plan = asyncio.run(_build_plan(conn))

    assert len(plan["to_apply"]) == 1
    assert plan["to_apply"][0]["practice_area"] == "Trust"
    assert plan["review"] == []


def test_ambiguous_lands_in_review_not_force_categorized():
    matters = [_matter("Mukweva and Paswa Civil — Debt collection/fraud, replicated to special plea")]
    conn = FakeConnection(matters)

    plan = asyncio.run(_build_plan(conn))

    assert plan["to_apply"] == []
    assert len(plan["review"]) == 1
    assert plan["review"][0]["reason"] == "ambiguous"
    assert set(plan["review"][0]["candidates"]) == {"Debt Collection", "Criminal"}


def test_no_keyword_match_lands_in_review_not_force_categorized():
    matters = [_matter("Moyo v Dube — Hearing on Rule Nisi")]
    conn = FakeConnection(matters)

    plan = asyncio.run(_build_plan(conn))

    assert plan["to_apply"] == []
    assert len(plan["review"]) == 1
    assert plan["review"][0]["reason"] == "no_match"
    assert plan["review"][0]["candidates"] == []


def test_matter_with_practice_area_already_set_is_skipped_entirely():
    matters = [_matter("Family Trust — Trust", practice_area="Trust")]
    conn = FakeConnection(matters)

    plan = asyncio.run(_build_plan(conn))

    assert plan["to_apply"] == []
    assert plan["review"] == []


def test_mixed_batch_sorted_correctly_and_order_preserved():
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    m1 = _matter("Atlas — Contract, letter of demand sent", created_at=base)
    m2 = _matter("Mukweva and Paswa Civil — Debt collection/fraud", created_at=base.replace(day=2))
    m3 = _matter("Moyo v Dube — Hearing on Rule Nisi", created_at=base.replace(day=3))
    m4 = _matter("Chamunorwa chivhunga — Eviction", created_at=base.replace(day=4))
    conn = FakeConnection([m2, m4, m1, m3])  # deliberately out of order

    plan = asyncio.run(_build_plan(conn))

    assert [e["name"] for e in plan["to_apply"]] == [
        "Atlas — Contract, letter of demand sent",
        "Chamunorwa chivhunga — Eviction",
    ]
    assert {e["name"] for e in plan["review"]} == {
        "Mukweva and Paswa Civil — Debt collection/fraud",
        "Moyo v Dube — Hearing on Rule Nisi",
    }


def test_empty_database_produces_empty_plan():
    conn = FakeConnection([])
    plan = asyncio.run(_build_plan(conn))
    assert plan == {"to_apply": [], "review": []}
