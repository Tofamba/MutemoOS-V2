"""
Manual matter-stage sequences per matter_type, backing the Matter
Progress Tracker (visual stepper) — v1, manual advancement only, no
automation, no ZimIECMS integration.

Confirmed before this was written: no "Procedural State Machine" module,
stage taxonomy, or per-stage SLA/duration concept exists anywhere in
this codebase. The only prior art is backend/conveyancing.py's
CONVEYANCING_MILESTONES, already live (a plain <select> in the matter
panel) — reused here as-is, not duplicated. The SLA infrastructure that
does exist (calculate_sla_deadline, sla_deadline, matter_reassignments
in main.py) is a separate, purpose-built system for Legal Corner
panel-lawyer auto-created matters' initial-response turnaround from
matter *creation* — a different trigger event, not reusable for
"time spent in the current stage."

Only the three matter_type values that already have a case-binder
template (config/case_binder_templates.yml) get a defined sequence,
per the explicit scope for this pass. A matter_type with no entry here
gets no stepper -- the matter panel falls back to its existing
plain-text status chip.
"""
from backend.conveyancing import CONVEYANCING_MILESTONES

# Debt collection: the real progression from demand to recovery, not a
# generic "opened/closed" pair -- distinguishing "sent a demand" from
# "issued summons" from "have judgment" matters because the firm's next
# action differs completely at each point.
DEBT_COLLECTION_STAGES = [
    "Letter of Demand Sent",
    "Response / Negotiation",
    "Summons Issued",
    "Judgment Obtained",
    "Warrant of Execution",
    "Collected / Settled",
]

# Litigation: modeled on the ZimIECMS case-stage pattern named in the
# brief -- draft/prepare, file, serve, appear, set down, judgment.
LITIGATION_GENERAL_STAGES = [
    "Draft Created",
    "Documents Prepared",
    "Filed / Case Number Allocated",
    "Pending Service",
    "Pending Appearance / Plea",
    "Set Down",
    "Judgment",
]

MATTER_TYPE_STAGES = {
    "conveyancing": CONVEYANCING_MILESTONES,
    "debt_collection": DEBT_COLLECTION_STAGES,
    "litigation_general": LITIGATION_GENERAL_STAGES,
}


def resolve_stage_sequence(matter_type, practice_area):
    """
    matter_type is the primary lookup key, matching case_binder_templates.yml.
    Conveyancing bridges both taxonomies: a matter with
    practice_area == 'Conveyancing/Property' gets the conveyancing
    sequence even when matter_type isn't exactly 'conveyancing', since
    the existing conveyancing-milestone feature was built keyed on
    practice_area -- this keeps that already-live data reachable without
    requiring every conveyancing matter to also have matter_type set.
    Returns None (not an empty list) when no sequence applies, so callers
    can render the existing plain-text status instead of an empty tracker.
    """
    if matter_type in MATTER_TYPE_STAGES:
        return MATTER_TYPE_STAGES[matter_type]
    if practice_area == "Conveyancing/Property":
        return CONVEYANCING_MILESTONES
    return None


def stage_storage_field(matter_type, practice_area) -> str:
    """
    Which matters column actually holds the current stage value.
    Conveyancing keeps using conveyancing_milestone -- the real, already-
    populated column -- rather than migrating existing data into a new
    unified field. Every other matter_type with a defined sequence uses
    the new generic matters.stage column.
    """
    if resolve_stage_sequence(matter_type, practice_area) is CONVEYANCING_MILESTONES:
        return "conveyancing_milestone"
    return "stage"
