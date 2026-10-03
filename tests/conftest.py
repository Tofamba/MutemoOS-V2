"""
Shared fixtures.

near_midnight_clock: runs a test under four fixed instants around the firm's
local midnight (Harare, UTC+2) -- 21:30 UTC (23:30 local, still "today"),
23:30 UTC (01:30 local, already "tomorrow"), 00:30 UTC (02:30 local) and
midday -- by patching the single clock the app's calendar-date logic reads
(backend.timeutil.utc_now). A test that derives "today" from firm_today()
must hold at every one of these; one that mixes in date.today() or
datetime.utcnow().date() passes at midday and fails inside the two-hour
window, which is the bug this fixture exists to catch.
"""
from datetime import datetime

import pytest

NEAR_MIDNIGHT_INSTANTS = [
    "2026-10-03T21:30:00+00:00",
    "2026-10-03T23:30:00+00:00",
    "2026-10-04T00:30:00+00:00",
    "2026-10-04T12:00:00+00:00",
]


@pytest.fixture(params=NEAR_MIDNIGHT_INSTANTS, ids=["2330local", "0130local", "0230local", "midday"])
def near_midnight_clock(request, monkeypatch):
    import backend.timeutil as tu
    fixed = datetime.fromisoformat(request.param)
    monkeypatch.setattr(tu, "utc_now", lambda: fixed)
    return fixed
