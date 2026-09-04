"""
matter_health.py — a single, computed status per matter (Red/Amber/Blue/
Green, plus Grey), derived entirely from data that already exists on the
matter row. Never manually set by a lawyer.

Foundational piece for the reports roadmap's Firm Pulse and Lawyer Matter
Review (neither built in this pass — this is scoped to computing the
status and surfacing it on the matter panel + the two existing reports'
data only, so those can start consuming it later without a second
implementation).

Deliberately reuses, rather than re-derives, three pieces of logic that
already exist elsewhere:
  - `matters.next_review_date`/`last_activity` and the 30-day review
    cycle (backend/main.py's DEFAULT_REVIEW_INTERVAL_DAYS,
    REVIEW_DIGEST_LOOKAHEAD_DAYS, and add_progress_note()'s
    _resolve_review_dates() call, which is what makes "next_review_date
    has lapsed" already imply "no reviewing action happened" -- any
    note/PATCH rolls it forward automatically, so no separate activity
    check is needed for that condition specifically).
  - `matters.next_deadline`'s existing red/amber/blue day-thresholds,
    already shipped in the frontend's own deadlineChipHtml() (index.html)
    -- reused here as the exact same 7/21-day split, not a second,
    possibly-divergent set of numbers for the same field.
  - `matters.status`'s fixed 5-value set, used as-is for the Grey bucket.

SCOPING NOTE (confirmed with the user, not a permanent architecture
decision): "a court date is imminent" is treated as covered by
next_deadline/next_deadline_note alone, the single existing
"critical date" field on a matter (also what the reminder digest and the
deadline chip both key off) -- NOT a separate join against
calendar_events (event_type='hearing'). compute_matter_health() takes one
matter dict and stays a pure function; if a firm starts tracking hearing
dates that never get mirrored onto next_deadline, this will miss them.
Revisit if that turns out to matter in practice.

TWO DIMENSIONS, ONE COLOR (2026-09-04, confirmed with the user before
hardcoding): operational health (deadlines/reviews/activity, all of the
above) and AML/matter risk (matters.matter_risk/aml_scope, added
2026-09-03) are computed independently by
_compute_operational_health()/_compute_compliance_floor() and then
combined by taking the more severe of the two -- compliance is a FLOOR
on the color, never something operational factors can lower. Severity
order, confirmed with the user: Red > Amber > Blue > Green (Grey stays
a separate short-circuit, unrelated to severity -- an inactive-by-design
matter isn't "at risk", it's just closed). "Blue" is new this pass,
specifically for "AML Scope is In Scope but Matter Risk was never
actually rated" -- the same shape of gap as the PEP-without-risk-rating
bug (backend/main.py's _compute_compliance_status(), 2026-09-02): a real
data gap, but not itself evidence of elevated risk, so it gets its own
tier rather than being lumped into Amber alongside a genuinely
elevated-risk matter. Low/Not Assessed matter_risk (outside that one
gap case) contributes NO floor at all -- absence of a rating is not
itself a risk signal, so it must never invent alarm on a matter nobody
has gotten to yet; color there stays purely operational.

`reasons` always lists every contributing factor from BOTH dimensions,
not just whichever one happened to decide the final color -- a Medium-
risk matter that's also 2 days from an overdue deadline shows Red (the
operational factor is more severe), but its reasons list still names
the Medium risk too, so "why is this Red" never hides half the answer.
"""
from datetime import date, datetime
from typing import Optional

# Matches deadlineChipHtml()'s existing red/amber/blue split for
# next_deadline exactly (frontend/index.html) -- the health badge and the
# deadline chip a lawyer already sees on the same matter must agree.
DEADLINE_RED_DAYS = 7
DEADLINE_AMBER_DAYS = 21

# Matches REVIEW_DIGEST_LOOKAHEAD_DAYS (backend/main.py) -- the same
# "due soon" window the review-nudge email digest already uses.
REVIEW_AMBER_DAYS = 7

# No existing precedent for either of these -- confirmed with the user
# before hardcoding (2026-09-01). INACTIVITY_AMBER_DAYS is half of
# DEFAULT_REVIEW_INTERVAL_DAYS (30); INACTIVITY_RED_DAYS reuses
# REVIEW_AMBER_DAYS's own number for the compound "imminent deadline,
# gone quiet" condition below.
INACTIVITY_AMBER_DAYS = 14
INACTIVITY_RED_DAYS = 7

# matters.status values that are genuinely inactive by design, not by
# neglect -- never flagged Amber/Red just for being quiet. 'Awaiting
# Court' is deliberately NOT in this set: it's the status most likely to
# have a real, imminent court date, which is exactly what this feature
# exists to catch.
GREY_STATUSES = {"Awaiting Client", "On Hold", "Closed"}

# Severity order for combining the operational and compliance dimensions
# -- confirmed with the user (2026-09-04): Red > Amber > Blue > Green.
# Grey is deliberately absent here; it's a status-driven short-circuit
# handled before either dimension is even computed, not a severity level
# these two dimensions could ever produce or compete with.
_SEVERITY = {"green": 0, "blue": 1, "amber": 2, "red": 3}


def _parse_date_like(value) -> Optional[date]:
    """Accepts a date, a datetime, an ISO string (either produced by
    _row_to_matter()'s isoformat() calls or a raw asyncpg value), or
    None -- so this function works uniformly regardless of which of the
    three call sites' own query/serialization shape it's fed, without
    each caller needing to normalize first."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _compute_operational_health(matter: dict, today: date) -> dict:
    """
    Deadlines/reviews/activity only -- exactly the logic this module had
    before AML/matter risk existed, unchanged. Returns
    {"status": "red"|"amber"|"green", "reasons": [...]} -- green's
    reasons are deliberately [] here (not a canned message): the
    top-level compute_matter_health() composes the final green message
    itself, since a genuinely green overall result now depends on BOTH
    this function and _compute_compliance_floor() agreeing, and the
    message needs to say so.
    """
    next_deadline = _parse_date_like(matter.get("next_deadline"))
    next_review_date = _parse_date_like(matter.get("next_review_date"))
    last_activity = _parse_date_like(matter.get("last_activity"))
    created_at = _parse_date_like(matter.get("created_at"))
    # The most recent real signal of "someone touched this matter" --
    # last_activity if it's ever been set, falling back to created_at for
    # a matter that's never had a single note/PATCH/document since.
    last_touched = last_activity or created_at

    deadline_days = (next_deadline - today).days if next_deadline else None
    review_days = (next_review_date - today).days if next_review_date else None
    days_since_activity = (today - last_touched).days if last_touched else None

    red_reasons = []
    amber_reasons = []

    if deadline_days is not None and deadline_days < 0:
        red_reasons.append(f"Deadline passed {abs(deadline_days)} day(s) ago with no resolution")

    if review_days is not None and review_days < 0:
        red_reasons.append(f"Review overdue by {abs(review_days)} day(s)")
    elif next_review_date is None:
        # Confirmed with the user (2026-09-01): a matter nobody has ever
        # assessed is more urgent than one that was assessed and later
        # lapsed, not less -- matches _fetch_matter_review_status_rows'
        # own NULLS-FIRST-sorts-most-urgent framing. If this floods
        # existing matters that predate the 30-day review safety net
        # (2026-08-30) with Red, that's a one-time data-quality gap to
        # backfill (set a reasonable default next_review_date on old
        # matters with none) -- not a reason to soften this rule.
        red_reasons.append("Never entered the review cycle — no next_review_date set")

    if (
        deadline_days is not None and 0 <= deadline_days <= DEADLINE_RED_DAYS
        and (days_since_activity is None or days_since_activity > INACTIVITY_RED_DAYS)
    ):
        red_reasons.append(
            f"Deadline in {deadline_days} day(s) with no activity in over {INACTIVITY_RED_DAYS} days"
        )

    if red_reasons:
        return {"status": "red", "reasons": red_reasons}

    # 0-7 days is already covered by the Red escalation above when the
    # matter has also gone quiet; an imminent deadline with recent
    # activity still deserves at least Amber visibility, not silence --
    # so this range overlaps the Red check's window deliberately, it
    # isn't just the 8-21 day remainder.
    if deadline_days is not None and 0 <= deadline_days <= DEADLINE_AMBER_DAYS:
        amber_reasons.append(f"Deadline in {deadline_days} day(s)")

    if review_days is not None and 0 <= review_days <= REVIEW_AMBER_DAYS:
        amber_reasons.append(f"Review due in {review_days} day(s)")

    if days_since_activity is not None and days_since_activity >= INACTIVITY_AMBER_DAYS:
        amber_reasons.append(f"No activity in {days_since_activity} day(s)")

    if amber_reasons:
        return {"status": "amber", "reasons": amber_reasons}

    return {"status": "green", "reasons": []}


def _compute_compliance_floor(matter: dict) -> dict:
    """
    AML scope / matter risk only (matters.aml_scope/matter_risk, added
    2026-09-03) -- a FLOOR on the overall color, never something that
    can lower what operational factors already established. Returns
    {"status": "red"|"amber"|"blue"|"green", "reasons": [...]} -- green
    here means "no compliance floor", not "explicitly assessed and
    clear"; Low/Not Assessed risk (outside the one Blue gap case below)
    contributes nothing, on purpose, so absence of a rating never reads
    as an alarm on a matter nobody has gotten to yet.
    """
    matter_risk = matter.get("matter_risk") or "NotAssessed"
    aml_scope = matter.get("aml_scope") or "NotAssessed"
    reason_suffix = f" — {matter['aml_scope_reason']}" if matter.get("aml_scope_reason") else ""

    if matter_risk == "High":
        return {"status": "red", "reasons": [f"Matter risk: High{reason_suffix}"]}
    if matter_risk == "Medium":
        return {"status": "amber", "reasons": [f"Matter risk: Medium{reason_suffix}"]}
    if aml_scope == "InScope" and matter_risk == "NotAssessed":
        # The PEP-without-risk-rating shape, at matter level: in scope
        # for AML purposes but nobody has actually rated the risk yet --
        # a real gap worth a lawyer's attention, but not itself evidence
        # of elevated risk, so Blue rather than Amber.
        return {"status": "blue", "reasons": ["AML scope: In Scope but Matter Risk not yet assessed"]}
    return {"status": "green", "reasons": []}


def compute_matter_health(matter: dict, today: Optional[date] = None) -> dict:
    """
    Returns {"status": "red"|"amber"|"blue"|"green"|"grey", "reasons": [str, ...]}.

    `reasons` is never empty (a color alone is not a useful signal to a
    lawyer) and always includes every contributing factor from BOTH the
    operational and compliance dimensions, not just whichever one
    decided the final color -- see this module's own docstring for why.
    grey explains which status field made it inactive-by-design.

    `matter` needs: status, next_deadline, next_deadline_note,
    next_review_date, last_activity, created_at, aml_scope, matter_risk,
    aml_scope_reason. Missing/None fields are treated as "no signal from
    that field", not an error -- every caller (the matter panel, My
    Portfolio, Matter Review Status) already has a matter dict shaped
    closely enough to this for the relevant keys to be present when they
    mean something.
    """
    today = today or datetime.utcnow().date()

    status = matter.get("status")
    if status in GREY_STATUSES:
        return {"status": "grey", "reasons": [f"Matter is {status} — not being actively tracked"]}

    operational = _compute_operational_health(matter, today)
    compliance = _compute_compliance_floor(matter)

    overall_status = max(operational["status"], compliance["status"], key=_SEVERITY.__getitem__)
    reasons = operational["reasons"] + compliance["reasons"]
    if not reasons:
        reasons = ["No deadlines, reviews, inactivity, or elevated matter risk flagged"]

    return {"status": overall_status, "reasons": reasons}
