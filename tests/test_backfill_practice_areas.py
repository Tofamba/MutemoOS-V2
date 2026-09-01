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


# ── cmd_apply() end-to-end idempotency ──────────────────────────────────────
# _build_plan() already recomputes fresh from live DB state every call (not
# from a saved report) and only ever selects practice_area IS NULL rows --
# confirming that translates into a genuinely safe re-run of the real apply
# command, not just the read-only plan, per instruction.

from types import SimpleNamespace

from scripts.backfill_practice_areas import cmd_apply


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ApplyFakeConnection(FakeConnection):
    """Extends the report-only FakeConnection above with the execute()/
    transaction()/close() machinery cmd_apply() actually needs."""

    def __init__(self, matters):
        super().__init__(matters)
        self.executed = []

    def transaction(self):
        return _FakeTransaction()

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("UPDATE matters SET practice_area=$1 WHERE id=$2"):
            practice_area, matter_id = args
            for m in self.matters:
                if m["id"] == matter_id:
                    m["practice_area"] = practice_area
            self.executed.append((practice_area, matter_id))
            return "UPDATE 1"
        raise NotImplementedError(f"FakeConnection.execute: unhandled query: {q}")

    async def close(self):
        pass


def _apply_args(yes=True):
    return SimpleNamespace(database_url="postgres://fake", yes=yes)


def _run_apply_against(monkeypatch, conn, yes=True):
    import scripts.backfill_practice_areas as m

    class _FakeAsyncpg:
        @staticmethod
        async def connect(database_url):
            return conn
    monkeypatch.setattr(m, "asyncpg", _FakeAsyncpg())
    asyncio.run(cmd_apply(_apply_args(yes=yes)))


def test_apply_writes_confident_matches_and_leaves_review_matters_untouched(monkeypatch):
    confident = _matter("Family Trust — Trust")
    ambiguous = _matter("Mukweva and Paswa Civil — Debt collection/fraud")
    conn = _ApplyFakeConnection([confident, ambiguous])

    _run_apply_against(monkeypatch, conn)

    assert confident["practice_area"] == "Trust"
    assert ambiguous["practice_area"] is None
    assert len(conn.executed) == 1


def test_apply_is_idempotent_second_run_touches_nothing(monkeypatch):
    """The real safety-net property: re-running `apply --yes` against a
    database that already has this backfill's own results applied must
    not re-write, double-apply, or error on anything -- _build_plan()'s
    own WHERE practice_area IS NULL means an already-set matter is simply
    invisible to the second run's plan."""
    confident = _matter("Family Trust — Trust")
    ambiguous = _matter("Mukweva and Paswa Civil — Debt collection/fraud")
    conn = _ApplyFakeConnection([confident, ambiguous])

    _run_apply_against(monkeypatch, conn)
    first_run_writes = list(conn.executed)
    assert len(first_run_writes) == 1

    _run_apply_against(monkeypatch, conn)

    # Zero additional writes on the second run -- the confident matter no
    # longer matches practice_area IS NULL, and the ambiguous one was
    # never written in the first place.
    assert conn.executed == first_run_writes
    assert confident["practice_area"] == "Trust"  # unchanged, not re-applied
    assert ambiguous["practice_area"] is None  # still never guessed


def test_apply_dry_run_without_yes_writes_nothing(monkeypatch):
    confident = _matter("Family Trust — Trust")
    conn = _ApplyFakeConnection([confident])

    _run_apply_against(monkeypatch, conn, yes=False)

    assert conn.executed == []
    assert confident["practice_area"] is None
