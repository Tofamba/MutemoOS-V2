"""
Unit tests for backend/matter_health.py's compute_matter_health() -- a
pure function over a matter dict, no DB needed. `today` is always passed
explicitly so every test is deterministic regardless of when it runs.
"""
from datetime import date, datetime, timedelta

from backend.matter_health import compute_matter_health

TODAY = date(2026, 9, 1)


def _matter(**overrides):
    m = {
        "status": "Active",
        "next_deadline": None,
        "next_deadline_note": None,
        "next_review_date": (TODAY + timedelta(days=15)).isoformat(),
        "last_activity": TODAY.isoformat(),
        "created_at": (TODAY - timedelta(days=60)).isoformat(),
    }
    m.update(overrides)
    return m


# ── Grey: inactive by design, never flagged for being quiet ─────────────────

def test_awaiting_client_is_grey_even_with_a_lapsed_deadline_and_review():
    m = _matter(
        status="Awaiting Client",
        next_deadline=(TODAY - timedelta(days=30)).isoformat(),
        next_review_date=(TODAY - timedelta(days=30)).isoformat(),
        last_activity=(TODAY - timedelta(days=90)).isoformat(),
    )
    result = compute_matter_health(m, today=TODAY)
    assert result["status"] == "grey"
    assert "Awaiting Client" in result["reasons"][0]


def test_on_hold_is_grey():
    result = compute_matter_health(_matter(status="On Hold"), today=TODAY)
    assert result["status"] == "grey"


def test_closed_is_grey():
    result = compute_matter_health(_matter(status="Closed"), today=TODAY)
    assert result["status"] == "grey"


def test_awaiting_court_is_not_grey_and_still_flags_a_real_deadline():
    """Awaiting Court is deliberately NOT grey -- it's the status most
    likely to have a real, imminent court date."""
    m = _matter(status="Awaiting Court", next_deadline=(TODAY + timedelta(days=3)).isoformat())
    result = compute_matter_health(m, today=TODAY)
    assert result["status"] in ("amber", "red")


# ── Red ───────────────────────────────────────────────────────────────────

def test_passed_deadline_is_red():
    m = _matter(next_deadline=(TODAY - timedelta(days=5)).isoformat())
    result = compute_matter_health(m, today=TODAY)
    assert result["status"] == "red"
    assert any("passed 5 day" in r for r in result["reasons"])


def test_lapsed_review_is_red():
    m = _matter(next_review_date=(TODAY - timedelta(days=10)).isoformat())
    result = compute_matter_health(m, today=TODAY)
    assert result["status"] == "red"
    assert any("overdue by 10 day" in r for r in result["reasons"])


def test_never_set_review_date_is_red_not_amber():
    """Confirmed with the user (2026-09-01): a matter nobody has ever
    assessed is more urgent than one assessed and later lapsed, not
    less -- this is Red, deliberately not softened to Amber."""
    m = _matter(next_review_date=None)
    result = compute_matter_health(m, today=TODAY)
    assert result["status"] == "red"
    assert any("Never entered the review cycle" in r for r in result["reasons"])


def test_imminent_deadline_with_no_recent_activity_is_red():
    m = _matter(
        next_deadline=(TODAY + timedelta(days=3)).isoformat(),
        last_activity=(TODAY - timedelta(days=10)).isoformat(),
    )
    result = compute_matter_health(m, today=TODAY)
    assert result["status"] == "red"
    assert any("Deadline in 3 day(s) with no activity" in r for r in result["reasons"])


def test_imminent_deadline_with_recent_activity_is_amber_not_red():
    """The escalation to Red requires BOTH an imminent deadline AND
    quiet -- recent activity on an imminent deadline still deserves
    visibility (Amber), not silence, but isn't escalated to Red."""
    m = _matter(
        next_deadline=(TODAY + timedelta(days=3)).isoformat(),
        last_activity=(TODAY - timedelta(days=1)).isoformat(),
    )
    result = compute_matter_health(m, today=TODAY)
    assert result["status"] == "amber"
    assert any("Deadline in 3 day(s)" in r for r in result["reasons"])


def test_multiple_red_conditions_all_appear_in_reasons():
    m = _matter(
        next_deadline=(TODAY - timedelta(days=2)).isoformat(),
        next_review_date=(TODAY - timedelta(days=40)).isoformat(),
    )
    result = compute_matter_health(m, today=TODAY)
    assert result["status"] == "red"
    assert len(result["reasons"]) == 2
    assert any("Deadline passed" in r for r in result["reasons"])
    assert any("Review overdue" in r for r in result["reasons"])


# ── Amber ─────────────────────────────────────────────────────────────────

def test_deadline_well_out_but_within_amber_window():
    m = _matter(next_deadline=(TODAY + timedelta(days=15)).isoformat())
    result = compute_matter_health(m, today=TODAY)
    assert result["status"] == "amber"
    assert any("Deadline in 15 day(s)" in r for r in result["reasons"])


def test_deadline_beyond_amber_window_is_not_flagged():
    m = _matter(next_deadline=(TODAY + timedelta(days=25)).isoformat())
    result = compute_matter_health(m, today=TODAY)
    assert result["status"] == "green"


def test_review_due_soon_is_amber():
    m = _matter(next_review_date=(TODAY + timedelta(days=5)).isoformat())
    result = compute_matter_health(m, today=TODAY)
    assert result["status"] == "amber"
    assert any("Review due in 5 day(s)" in r for r in result["reasons"])


def test_review_due_beyond_lookahead_window_is_not_flagged():
    m = _matter(next_review_date=(TODAY + timedelta(days=10)).isoformat())
    result = compute_matter_health(m, today=TODAY)
    assert result["status"] == "green"


def test_gone_quiet_for_14_days_is_amber():
    m = _matter(last_activity=(TODAY - timedelta(days=14)).isoformat())
    result = compute_matter_health(m, today=TODAY)
    assert result["status"] == "amber"
    assert any("No activity in 14 day(s)" in r for r in result["reasons"])


def test_quiet_for_13_days_is_not_yet_flagged():
    m = _matter(last_activity=(TODAY - timedelta(days=13)).isoformat())
    result = compute_matter_health(m, today=TODAY)
    assert result["status"] == "green"


def test_no_last_activity_falls_back_to_created_at():
    m = _matter(last_activity=None, created_at=(TODAY - timedelta(days=20)).isoformat())
    result = compute_matter_health(m, today=TODAY)
    assert result["status"] == "amber"
    assert any("No activity in 20 day(s)" in r for r in result["reasons"])


# ── Green ─────────────────────────────────────────────────────────────────

def test_clean_matter_is_green_with_a_real_reason_not_an_empty_list():
    m = _matter(next_deadline=None, next_review_date=(TODAY + timedelta(days=20)).isoformat())
    result = compute_matter_health(m, today=TODAY)
    assert result["status"] == "green"
    assert result["reasons"]  # never an empty list, even for green


# ── Input robustness -- real date/datetime objects, not just ISO strings ────

def test_accepts_real_date_and_datetime_objects_not_just_iso_strings():
    """The three real call sites (matter panel via _row_to_matter's
    isoformat() strings, and any future caller passing raw asyncpg
    values) must both work without the caller normalizing first."""
    m = {
        "status": "Active",
        "next_deadline": TODAY - timedelta(days=1),  # real date object
        "next_deadline_note": None,
        "next_review_date": datetime(2026, 9, 20, 14, 30),  # real datetime object
        "last_activity": datetime(2026, 8, 25, 9, 0),
        "created_at": datetime(2026, 6, 1, 0, 0),
    }
    result = compute_matter_health(m, today=TODAY)
    assert result["status"] == "red"
    assert any("Deadline passed 1 day" in r for r in result["reasons"])


def test_missing_keys_treated_as_no_signal_not_an_error():
    """A near-empty dict doesn't crash -- missing next_review_date
    correctly still triggers the "never entered the review cycle" Red
    rule (no key at all is indistinguishable from an explicit None)."""
    result = compute_matter_health({"status": "Active"}, today=TODAY)
    assert result["status"] == "red"
    assert any("Never entered the review cycle" in r for r in result["reasons"])
