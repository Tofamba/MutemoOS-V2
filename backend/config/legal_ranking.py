"""
Loads config/legal_ranking.yml — the authority-weight table and tie-break
order for backend/authority_ranker.py — so those values are edited by a
lawyer/policy owner in one YAML file instead of a Python dict literal.

Cached once per process (@lru_cache): a config change requires an app
restart to take effect. No hot-reload — the ranker runs synchronously
inside every /api/search request, and re-reading/re-validating the file on
every call would be pure overhead for a value that changes on the order of
"policy review," not "per request."
"""

import os
from functools import lru_cache

import yaml

from backend.legal_taxonomy import LegalSourceType

_CONFIG_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "config", "legal_ranking.yml")
)


class LegalRankingConfigError(Exception):
    """
    Raised when config/legal_ranking.yml is missing, malformed, or doesn't
    cover every LegalSourceType the classifier (backend/legal_taxonomy.py)
    can actually emit. An unmapped source type must fail loudly at
    startup, not silently default to 0 authority inside the ranker — the
    same failure class as the matter_id=NULL ChromaDB indexing bug: a
    silent default masking a real configuration gap.
    """


def _load_yaml() -> dict:
    if not os.path.exists(_CONFIG_PATH):
        raise LegalRankingConfigError(f"Legal ranking config not found: {_CONFIG_PATH}")
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise LegalRankingConfigError(f"Could not parse {_CONFIG_PATH}: {e}") from e
    if not isinstance(data, dict):
        raise LegalRankingConfigError(f"{_CONFIG_PATH} must contain a YAML mapping at the top level")
    return data


def _validate_and_build_weights(data: dict, source_label: str = None) -> dict:
    """
    Pure validation/construction, factored out of load_authority_weights()
    so tests can exercise it directly against an in-memory dict instead of
    monkeypatching the filesystem or fighting @lru_cache.
    """
    source_label = source_label or str(_CONFIG_PATH)
    weights = data.get("authority_weights")
    if not isinstance(weights, dict) or not weights:
        raise LegalRankingConfigError(
            f"{source_label} is missing a non-empty top-level 'authority_weights' mapping"
        )

    all_source_types = {t.value for t in LegalSourceType}
    configured_types = set(weights.keys())

    missing = all_source_types - configured_types
    if missing:
        raise LegalRankingConfigError(
            f"{source_label}'s authority_weights is missing entries for: {sorted(missing)} — "
            f"every LegalSourceType the classifier can emit (backend/legal_taxonomy.py) must "
            f"have an explicit weight."
        )

    unknown_types = configured_types - all_source_types
    if unknown_types:
        raise LegalRankingConfigError(
            f"{source_label}'s authority_weights defines entries for source types not in "
            f"LegalSourceType: {sorted(unknown_types)}"
        )

    out_of_range = {
        k: v for k, v in weights.items()
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0 <= v <= 100)
    }
    if out_of_range:
        raise LegalRankingConfigError(
            f"{source_label}'s authority_weights has values outside the valid 0-100 range: "
            f"{out_of_range}"
        )

    return {LegalSourceType(k): v for k, v in weights.items()}


def _validate_and_build_tie_break_order(data: dict, source_label: str = None) -> list:
    """Pure validation/construction — see _validate_and_build_weights() above."""
    source_label = source_label or str(_CONFIG_PATH)
    order = data.get("tie_break_order")
    if not isinstance(order, list) or not order:
        raise LegalRankingConfigError(
            f"{source_label} is missing a non-empty top-level 'tie_break_order' list"
        )

    all_source_types = {t.value for t in LegalSourceType}
    unknown_types = set(order) - all_source_types
    if unknown_types:
        raise LegalRankingConfigError(
            f"{source_label}'s tie_break_order references source types not in LegalSourceType: "
            f"{sorted(unknown_types)}"
        )

    return [LegalSourceType(v) for v in order]


@lru_cache(maxsize=1)
def load_authority_weights() -> dict:
    """
    Returns {LegalSourceType: int weight}. Raises LegalRankingConfigError
    if the YAML is missing the 'authority_weights' key, doesn't cover
    every LegalSourceType value, defines a type LegalSourceType doesn't
    recognise, or has any weight outside 0-100.
    """
    return _validate_and_build_weights(_load_yaml())


@lru_cache(maxsize=1)
def load_tie_break_order() -> list:
    """
    Returns an ordered list of LegalSourceType, earliest = wins ties.
    Unlike load_authority_weights(), this does not need to cover every
    LegalSourceType — a type left out simply sorts last among exact-score
    ties (see authority_ranker.rerank), since an incomplete tie-break list
    only affects ordering among identical scores, not the scores
    themselves. It still validates structure and rejects unrecognised
    types, since a typo here should be caught rather than silently ignored.
    """
    return _validate_and_build_tie_break_order(_load_yaml())
