"""
Title/Deeds Digitization Validation — document-collection checklist.

A real, active government programme: registered conveyancers validate a
client's property title ownership on the client's behalf (a separate
government portal handles the actual submission -- this module and
everything built on it tracks only the firm's own document-collection
and client-readiness side, never touches that portal).

Deliberately modeled on two existing, proven patterns rather than a new
one (confirmed with the user before building):
  - The catalog of items per category is config-driven YAML
    (config/title_deeds_checklist.yml), read once and cached -- same
    convention as backend/case_binder.py's config/case_binder_templates.yml,
    so the actual document lists can be edited without touching Python.
  - The persisted per-item shape it feeds (matter_checklist_items in
    backend/main.py) mirrors beneficial_owners/authorized_representatives:
    a status enum, a responsible person, and a document evidence link
    (FK straight into `documents`) -- not a new tracking mechanism.

This module itself is pure: no DB, no network I/O beyond reading the
YAML file once (cached). It returns category item lists and computes
completion counts; backend/main.py owns all persistence.
"""
import os
from functools import lru_cache
from typing import Optional

import yaml

_CONFIG_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "config", "title_deeds_checklist.yml")
)

# The three main checklists, chosen by the client's own client_type.
# Deliberately NOT covering every CLIENT_TYPES value (Partnership, Estate,
# NonProfit, Government, Other): the user's request only defined document
# lists for Individual/Company/Trust, and inventing a mapping for the
# others would be a guess, not a documented fact. A matter whose client
# doesn't map here simply isn't auto-seeded -- see create_matter()'s own
# comment in main.py -- a lawyer can still seed any category manually via
# POST .../checklist/seed.
CLIENT_TYPE_TO_CHECKLIST_CATEGORY = {
    "Individual": "individual",
    "Company": "company",
    "Trust": "trust",
}

# The two SPA/representative sub-checklist variants -- additive to
# whichever main checklist already applies, seeded only when the
# registered owner isn't validating in person (2. in the request).
SPA_CATEGORIES = ("spa_principal", "spa_representative")

CATEGORY_LABELS = {
    "individual": "Individual",
    "company": "Company",
    "trust": "Trust",
    "spa_principal": "SPA / Representative — Principal-Led (notarized SPA)",
    "spa_representative": "SPA / Representative — Representative-Led (sworn affidavit)",
}


class TitleDeedsChecklistConfigError(Exception):
    """Raised when config/title_deeds_checklist.yml is missing or malformed."""


@lru_cache
def _load_categories() -> dict:
    if not os.path.exists(_CONFIG_PATH):
        raise TitleDeedsChecklistConfigError(f"Title deeds checklist config not found: {_CONFIG_PATH}")
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise TitleDeedsChecklistConfigError(f"Could not parse {_CONFIG_PATH}: {e}") from e
    if not isinstance(data, dict):
        raise TitleDeedsChecklistConfigError(f"{_CONFIG_PATH} must contain a YAML mapping at the top level")
    return data


def known_categories() -> list:
    """The category keys this config actually defines a checklist for --
    request-level validation (POST .../checklist/seed) checks against
    this real config rather than a second, separately maintained list."""
    return sorted(_load_categories().keys())


def checklist_items_for_category(category: str) -> list:
    """
    Returns [{"item_key": str, "item_label": str}, ...] for this category,
    in the config's own order (display order, not alphabetical). An
    unrecognised category returns an empty list rather than raising --
    same "not itself an error at this layer" stance as
    case_binder.provision_case_binder(); request-level validation of
    `category` against known_categories() happens in the caller.
    """
    categories = _load_categories()
    items = categories.get(category) or []
    return [{"item_key": i["item_key"], "item_label": i["item_label"]} for i in items]


def compute_checklist_completion(items: list) -> dict:
    """
    Pure aggregation over already-fetched checklist item rows/dicts (each
    needing only a "status" key) -- same "small pure function over a list
    of dicts" shape as compute_matter_health()/_compute_compliance_status(),
    not a new pattern. Returns:
        {"collected": int, "total": int, "status": "not_started"|"in_progress"|"complete"}
    total=0 (nothing seeded yet) is "not_started", not a divide-by-zero
    special case masquerading as "complete" -- an empty checklist has
    collected nothing.
    """
    total = len(items)
    collected = sum(1 for i in items if i.get("status") == "Collected")
    if total == 0:
        status = "not_started"
    elif collected == total:
        status = "complete"
    elif collected > 0:
        status = "in_progress"
    else:
        status = "not_started"
    return {"collected": collected, "total": total, "status": status}


def category_display_label(category: Optional[str]) -> str:
    """Human-readable label for a category key, falling back to the raw
    key itself for one that's in the config but not (yet) in
    CATEGORY_LABELS -- never raises on an unrecognised value."""
    if not category:
        return ""
    return CATEGORY_LABELS.get(category, category)
