"""
Case Binder provisioning — the starter document checklist a new matter
gets automatically, based on its matter_type. Config-driven
(config/case_binder_templates.yml) rather than hard-coded, so the starter
checklist per matter type can be edited without touching Python — same
convention as backend/config/legal_ranking.py's YAML-backed config.

provision_case_binder() is pure: no DB, no network I/O beyond reading the
YAML file once (cached). It returns document records to create — it does
not persist them anywhere. The caller (POST /api/onboarding/intake in
backend/main.py) owns persistence: writing the documents row, uploading
to R2, tagging matter_id/firm_id — none of which this module knows about,
by design, so it stays unit-testable against the YAML directly with no
database.
"""
import os
from datetime import date
from functools import lru_cache

import yaml

_CONFIG_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "config", "case_binder_templates.yml")
)


class CaseBinderConfigError(Exception):
    """Raised when config/case_binder_templates.yml is missing or malformed."""


@lru_cache
def _load_templates() -> dict:
    if not os.path.exists(_CONFIG_PATH):
        raise CaseBinderConfigError(f"Case binder template config not found: {_CONFIG_PATH}")
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise CaseBinderConfigError(f"Could not parse {_CONFIG_PATH}: {e}") from e
    if not isinstance(data, dict):
        raise CaseBinderConfigError(f"{_CONFIG_PATH} must contain a YAML mapping at the top level")
    return data


def known_matter_types() -> list:
    """
    The matter_type keys this config actually has a starter checklist
    for — POST /api/onboarding/intake validates its request's matter_type
    against this (real config), rather than a second, separately
    maintained list of valid values that could drift out of sync with it.
    """
    return sorted(_load_templates().keys())


def _apply_merge_fields(text: str, client_full_name: str, matter_number: str, today_str: str) -> str:
    return (
        (text or "")
        .replace("{{client_name}}", client_full_name or "")
        .replace("{{matter_number}}", matter_number or "")
        .replace("{{today}}", today_str)
    )


def provision_case_binder(matter: dict, matter_type: str, client: dict, today: str = None) -> list:
    """
    Returns the starter document records for a new matter of this type —
    not yet persisted anywhere. Each item:
        {"name": str, "template_source": str, "content": str,
         "provenance_document_type": str}

    matter: {"matter_number": str}
    client: {"full_name": str}
    today: ISO date string to merge in; defaults to date.today() if not
        given — accepted as a parameter (rather than always calling
        date.today() internally) so callers/tests can pin a specific date
        deterministically without monkeypatching the datetime module.

    provenance_document_type comes straight from the YAML (one of
    backend/main.py's PROVENANCE_DOCUMENT_TYPES) and falls back to
    "General" if an item doesn't specify one. document_status is
    deliberately NOT included here — every auto-provisioned document gets
    document_status='Draft', the same fixed value regardless of item or
    matter_type, so the caller (POST /api/onboarding/intake) sets it
    directly rather than this function threading a constant through.

    An unrecognised matter_type (not a key in the YAML) returns an empty
    list rather than raising — a matter_type with no defined starter
    checklist is a legitimate, expected case (e.g. one not yet added to
    the config), not an error at this layer. Request-level validation of
    matter_type against known_matter_types() happens in the caller.
    """
    templates = _load_templates()
    items = templates.get(matter_type) or []

    today_str = today or date.today().isoformat()
    client_full_name = (client or {}).get("full_name") or ""
    matter_number = (matter or {}).get("matter_number") or ""

    result = []
    for item in items:
        content_template = item.get("content_template", "")
        if item.get("needs_merge_fields"):
            content = _apply_merge_fields(content_template, client_full_name, matter_number, today_str)
        else:
            content = content_template
        result.append({
            "name": item["name"],
            "template_source": item.get("template_source"),
            "content": content,
            "provenance_document_type": item.get("provenance_document_type") or "General",
        })
    return result
