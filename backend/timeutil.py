"""
The one time reference for calendar-date logic (2026-10-04).

Anything the app treats as a CALENDAR DATE -- "days until a deadline", "days
overdue", "reviewed today", the default next-review date, a verification or
collected date stamped "today" -- must use firm_today(), never date.today() or
datetime.utcnow().date().

Why: those two disagree. date.today() is the machine's local date (UTC on the
Railway container, Harare on a dev machine), and datetime.utcnow().date() is
UTC. A Zimbabwean firm lives on Harare time (UTC+2, no daylight saving), and
the frontend already computes deadline chips in the browser's local date. So
between 00:00 and 02:00 Harare time a UTC-based backend was still on
"yesterday": a note added at 00:30 recorded yesterday's last-reviewed date, and
server-written health reasons could disagree by a day with the chip beside
them. The same split made two tests fail for a couple of hours around local
midnight on a Harare machine.

Instants (created_at/updated_at timestamps, session expiry) stay UTC, and the
reminder/digest scheduler stays keyed to send_hour_utc -- those are points in
time or UTC hours, not calendar dates.

The offset is a fixed number of hours (default +2, Zimbabwe's Central Africa
Time) rather than a tz database lookup: no DST to model, and no dependency on
tzdata being installed. Override per deployment with MUTEMO_UTC_OFFSET_HOURS.
"""
import os
from datetime import date, datetime, timedelta, timezone

FIRM_UTC_OFFSET_HOURS = float(os.environ.get("MUTEMO_UTC_OFFSET_HOURS", "2"))
FIRM_TZ = timezone(timedelta(hours=FIRM_UTC_OFFSET_HOURS))


def utc_now() -> datetime:
    """The current instant, timezone-aware UTC. The single clock tests patch."""
    return datetime.now(timezone.utc)


def firm_now() -> datetime:
    return utc_now().astimezone(FIRM_TZ)


def firm_today() -> date:
    return firm_now().date()


def to_firm_date(value):
    """The firm-local calendar date of a stored timestamp.

    A timezone-aware datetime (what Postgres timestamptz columns return, in
    UTC) is converted to firm time first -- a record created at 23:30 UTC is
    already tomorrow in Harare. A naive datetime is taken to already be a
    local calendar value and is just truncated; a plain date is returned as is.
    """
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(FIRM_TZ).date()
        return value.date()
    return value
