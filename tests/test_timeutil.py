"""
Tests for backend/timeutil.py and the one-time-reference rule (2026-10-04).

Real issue: calendar-date logic mixed date.today() (the machine's local date),
datetime.utcnow().date() (UTC) and, in the frontend, the browser's local date.
For two hours around Harare midnight they disagreed -- a note added at 00:30
local got yesterday's last-reviewed date, health reasons could disagree by a
day with the deadline chip beside them, and two tests failed on any Harare
machine at that hour. Everything now reads firm_today() (UTC+2, one clock).
"""
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import backend.timeutil as tu
from backend.matter_health import compute_matter_health
from backend.timeutil import firm_today, to_firm_date


def _clock(monkeypatch, iso):
    fixed = datetime.fromisoformat(iso)
    monkeypatch.setattr(tu, "utc_now", lambda: fixed)


# ── firm_today / to_firm_date around local midnight, both directions ─────────

@pytest.mark.parametrize("iso,expected", [
    ("2026-10-03T21:59:59+00:00", date(2026, 10, 3)),   # 23:59:59 Harare -> still the 3rd
    ("2026-10-03T22:00:00+00:00", date(2026, 10, 4)),   # 00:00:00 Harare -> the 4th begins
    ("2026-10-03T23:30:00+00:00", date(2026, 10, 4)),   # the window the old code got wrong
    ("2026-10-04T21:59:59+00:00", date(2026, 10, 4)),
    ("2026-10-04T22:00:00+00:00", date(2026, 10, 5)),
])
def test_firm_today_follows_harare_midnight_not_utc_midnight(monkeypatch, iso, expected):
    _clock(monkeypatch, iso)
    assert firm_today() == expected


def test_firm_today_respects_a_deployment_offset_override(monkeypatch):
    _clock(monkeypatch, "2026-10-03T23:30:00+00:00")
    monkeypatch.setattr(tu, "FIRM_TZ", timezone(timedelta(hours=-5)))
    assert firm_today() == date(2026, 10, 3)  # 18:30 local


def test_to_firm_date_converts_aware_timestamps_and_leaves_the_rest():
    aware = datetime(2026, 10, 3, 23, 30, tzinfo=timezone.utc)
    assert to_firm_date(aware) == date(2026, 10, 4)
    assert to_firm_date(datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)) == date(2026, 10, 3)
    assert to_firm_date(datetime(2026, 10, 3, 23, 30)) == date(2026, 10, 3)  # naive: already a calendar value
    assert to_firm_date(date(2026, 10, 3)) == date(2026, 10, 3)


# ── real features read the same clock ────────────────────────────────────────

def _deadline_reason(matter):
    reasons = compute_matter_health(matter)["reasons"]
    return next(r for r in reasons if "Deadline in" in r or "Deadline" in r)


def test_matter_health_deadline_countdown_flips_at_harare_midnight_not_utc_midnight(monkeypatch):
    matter = {"status": "Active", "next_deadline": "2026-10-14", "next_review_date": "2026-11-30",
              "last_activity": datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)}
    _clock(monkeypatch, "2026-10-03T21:30:00+00:00")   # 23:30 Harare on the 3rd
    assert "11 day" in _deadline_reason(matter)
    _clock(monkeypatch, "2026-10-03T23:30:00+00:00")   # 01:30 Harare on the 4th (UTC still the 3rd)
    assert "10 day" in _deadline_reason(matter)


def test_review_dates_are_stamped_with_the_firm_local_date_after_midnight(monkeypatch):
    from backend.main import _resolve_review_dates
    _clock(monkeypatch, "2026-10-03T23:30:00+00:00")   # 01:30 Harare on the 4th
    next_review, last_reviewed = _resolve_review_dates(None)
    assert last_reviewed == date(2026, 10, 4)           # was the 3rd under the old UTC clock
    assert next_review == date(2026, 10, 4) + timedelta(days=30)


def test_compliance_anchor_uses_the_firm_local_date_of_created_at():
    from backend.main import _ai_action_anchor_date
    opened = datetime(2026, 10, 3, 23, 30, tzinfo=timezone.utc)   # 01:30 Harare on the 4th
    assert _ai_action_anchor_date({"due_date": None, "created_at": opened}) == date(2026, 10, 4)


def test_days_overdue_is_one_consistent_calendar_count_near_midnight(monkeypatch):
    """Opened 01:30 local on the 4th, checked 02:30 local the same day: that is
    0 days, not -1 (UTC anchor on the 3rd vs firm 'today' on the 4th) or +1."""
    from backend.main import _ai_action_anchor_date
    opened = datetime(2026, 10, 3, 23, 30, tzinfo=timezone.utc)
    _clock(monkeypatch, "2026-10-04T00:30:00+00:00")
    anchor = _ai_action_anchor_date({"due_date": None, "created_at": opened})
    assert (firm_today() - anchor).days == 0


# ── regression guard: nobody reintroduces a second calendar clock ────────────

_FORBIDDEN = re.compile(r"\bdate\.today\(\)|datetime\.utcnow\(\)\.date\(\)|datetime\.now\(\)\.date\(\)")


def test_backend_calendar_dates_only_come_from_firm_today():
    """date.today() is the machine's local date and utcnow().date() is UTC; both
    drift from firm time for two hours a day. A deliberate exception must carry a
    `utc-ok:` comment (with the reason) on the line itself or just above it."""
    offenders = []
    for path in sorted(Path(__file__).resolve().parent.parent.joinpath("backend").glob("*.py")):
        if path.name == "timeutil.py":
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if line.lstrip().startswith("#") or not _FORBIDDEN.search(line):
                continue
            context = "\n".join(lines[max(0, i - 4):i + 1])
            if "utc-ok" not in context:
                offenders.append(f"{path.name}:{i + 1}: {line.strip()}")
    assert not offenders, "use backend.timeutil.firm_today() (or mark `utc-ok:`):\n" + "\n".join(offenders)
