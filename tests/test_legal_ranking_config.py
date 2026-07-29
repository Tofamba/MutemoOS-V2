"""
Unit tests for backend/config/legal_ranking.py.

Exercises the pure validation functions directly against in-memory dicts
rather than the real config/legal_ranking.yml — this avoids monkeypatching
the filesystem and sidesteps load_authority_weights()/load_tie_break_order()
being @lru_cache'd (which would otherwise make "does it raise on bad input"
untestable after the first successful call in a process).
"""

import pytest

from backend.config.legal_ranking import (
    LegalRankingConfigError,
    _validate_and_build_tie_break_order,
    _validate_and_build_weights,
    load_authority_weights,
    load_tie_break_order,
)
from backend.legal_taxonomy import LegalSourceType

ALL_TYPES = [t.value for t in LegalSourceType]


def _complete_weights(overrides: dict = None) -> dict:
    """A valid authority_weights mapping covering every LegalSourceType."""
    weights = {t: 50 for t in ALL_TYPES}
    if overrides:
        weights.update(overrides)
    return weights


# ── The real config file, loaded once per process ────────────────────────────

def test_real_config_loads_without_error():
    """config/legal_ranking.yml itself must be valid — this is the actual
    startup path the app exercises when backend.authority_ranker is imported."""
    weights = load_authority_weights()
    assert isinstance(weights, dict)
    assert all(isinstance(k, LegalSourceType) for k in weights)
    assert all(0 <= v <= 100 for v in weights.values())
    assert set(weights.keys()) == set(LegalSourceType)

    order = load_tie_break_order()
    assert isinstance(order, list) and order
    assert all(isinstance(t, LegalSourceType) for t in order)


# ── Missing source type ──────────────────────────────────────────────────────

def test_raises_on_missing_source_type():
    weights = _complete_weights()
    del weights["correspondence"]  # simulate a type the YAML forgot to cover
    with pytest.raises(LegalRankingConfigError, match="correspondence"):
        _validate_and_build_weights({"authority_weights": weights}, source_label="test.yml")


def test_raises_on_multiple_missing_source_types():
    weights = _complete_weights()
    del weights["bill"]
    del weights["opinion"]
    with pytest.raises(LegalRankingConfigError) as exc_info:
        _validate_and_build_weights({"authority_weights": weights}, source_label="test.yml")
    assert "bill" in str(exc_info.value)
    assert "opinion" in str(exc_info.value)


# ── Out-of-range weight ───────────────────────────────────────────────────────

@pytest.mark.parametrize("bad_value", [-1, 101, 150, -50])
def test_raises_on_out_of_range_weight(bad_value):
    weights = _complete_weights({"constitution": bad_value})
    with pytest.raises(LegalRankingConfigError, match="0-100"):
        _validate_and_build_weights({"authority_weights": weights}, source_label="test.yml")


def test_boolean_weight_is_rejected():
    """bool is a subclass of int in Python — must not silently pass as a valid weight."""
    weights = _complete_weights({"constitution": True})
    with pytest.raises(LegalRankingConfigError, match="0-100"):
        _validate_and_build_weights({"authority_weights": weights}, source_label="test.yml")


def test_non_numeric_weight_is_rejected():
    weights = _complete_weights({"constitution": "high"})
    with pytest.raises(LegalRankingConfigError, match="0-100"):
        _validate_and_build_weights({"authority_weights": weights}, source_label="test.yml")


# ── Structural failures ──────────────────────────────────────────────────────

def test_raises_when_authority_weights_key_absent():
    with pytest.raises(LegalRankingConfigError, match="authority_weights"):
        _validate_and_build_weights({}, source_label="test.yml")


def test_raises_when_authority_weights_is_empty():
    with pytest.raises(LegalRankingConfigError, match="authority_weights"):
        _validate_and_build_weights({"authority_weights": {}}, source_label="test.yml")


def test_raises_on_unrecognised_source_type_in_weights():
    weights = _complete_weights({"some_made_up_type": 50})
    with pytest.raises(LegalRankingConfigError, match="some_made_up_type"):
        _validate_and_build_weights({"authority_weights": weights}, source_label="test.yml")


def test_valid_weights_build_correctly():
    weights = _complete_weights({"constitution": 100, "correspondence": 0})
    result = _validate_and_build_weights({"authority_weights": weights}, source_label="test.yml")
    assert result[LegalSourceType.CONSTITUTION] == 100
    assert result[LegalSourceType.CORRESPONDENCE] == 0
    assert len(result) == len(ALL_TYPES)


# ── tie_break_order ───────────────────────────────────────────────────────────

def test_tie_break_order_does_not_require_full_coverage():
    """Unlike weights, a partial tie_break_order is valid — it just means
    unlisted types sort last among exact-score ties."""
    order = ["constitution", "statute"]
    result = _validate_and_build_tie_break_order({"tie_break_order": order}, source_label="test.yml")
    assert result == [LegalSourceType.CONSTITUTION, LegalSourceType.STATUTE]


def test_tie_break_order_raises_on_unrecognised_type():
    with pytest.raises(LegalRankingConfigError, match="not_a_real_type"):
        _validate_and_build_tie_break_order(
            {"tie_break_order": ["constitution", "not_a_real_type"]}, source_label="test.yml"
        )


def test_tie_break_order_raises_when_empty():
    with pytest.raises(LegalRankingConfigError, match="tie_break_order"):
        _validate_and_build_tie_break_order({"tie_break_order": []}, source_label="test.yml")


def test_tie_break_order_raises_when_key_absent():
    with pytest.raises(LegalRankingConfigError, match="tie_break_order"):
        _validate_and_build_tie_break_order({}, source_label="test.yml")
