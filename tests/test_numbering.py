"""
Unit tests for backend/numbering.py — the pure logic behind automatic
client_number/matter_number assignment: deriving a lawyer's initials from
their name, disambiguating a collision, and computing the next sequence
number for a given prefix.
"""

from backend.numbering import (
    disambiguate_initials,
    format_client_number,
    format_matter_number,
    generate_initials,
    next_sequence,
)


# ── generate_initials ────────────────────────────────────────────────────

def test_generate_initials_three_word_name():
    assert generate_initials("Nyaradzo Gilbertina Maphosa") == "NGM"


def test_generate_initials_two_word_name():
    assert generate_initials("Ostern Mutero") == "OM"


def test_generate_initials_handles_middle_initial_with_period():
    assert generate_initials("Jingini R. Tsivama") == "JRT"


def test_generate_initials_handles_middle_initial_without_period():
    assert generate_initials("Honour P Mkushi") == "HPM"


def test_generate_initials_farai_siyakurima():
    assert generate_initials("Farai Siyakurima") == "FS"


def test_generate_initials_single_word_already_short_and_uppercase_passes_through():
    """The synthetic AUTH_ENABLED=False dev user has display_name == 'NGM'
    literally — must not get mangled into first-two-letters."""
    assert generate_initials("NGM") == "NGM"


def test_generate_initials_single_real_word_name_uses_first_two_letters():
    assert generate_initials("Prince") == "PR"


def test_generate_initials_empty_or_blank_falls_back():
    assert generate_initials("") == "X"
    assert generate_initials("   ") == "X"


# ── disambiguate_initials ────────────────────────────────────────────────

def test_disambiguate_initials_no_collision_returns_base():
    assert disambiguate_initials("NGM", set()) == "NGM"
    assert disambiguate_initials("NGM", {"OM", "FS"}) == "NGM"


def test_disambiguate_initials_single_collision_appends_2():
    assert disambiguate_initials("NGM", {"NGM"}) == "NGM2"


def test_disambiguate_initials_multiple_collisions_finds_lowest_free_suffix():
    assert disambiguate_initials("NGM", {"NGM", "NGM2"}) == "NGM3"
    assert disambiguate_initials("NGM", {"NGM", "NGM2", "NGM3"}) == "NGM4"


def test_disambiguate_initials_gap_still_takes_lowest_free_suffix():
    """NGM2 was freed up somehow (e.g. a since-deleted seed) — NGM3 taken,
    NGM2 still open, so it's used rather than skipping to NGM4."""
    assert disambiguate_initials("NGM", {"NGM", "NGM3"}) == "NGM2"


# ── next_sequence ─────────────────────────────────────────────────────────

def test_next_sequence_no_existing_numbers_starts_at_one():
    assert next_sequence([], "NGM") == 1


def test_next_sequence_finds_max_and_increments():
    assert next_sequence(["NGM-001", "NGM-002", "NGM-003"], "NGM") == 4


def test_next_sequence_ignores_unrelated_prefixes():
    assert next_sequence(["OM-001", "OM-002"], "NGM") == 1


def test_next_sequence_ignores_none_and_blank_values():
    assert next_sequence([None, "", "NGM-001"], "NGM") == 2


def test_next_sequence_handles_out_of_order_input():
    assert next_sequence(["NGM-003", "NGM-001", "NGM-002"], "NGM") == 4


def test_next_sequence_for_matter_numbers_uses_client_number_as_prefix():
    """Matter numbering nests one level deeper: prefix is the full
    client_number (e.g. "NGM-007"), not just the initials."""
    assert next_sequence(["NGM-007-01", "NGM-007-02"], "NGM-007") == 3


def test_next_sequence_matter_numbers_do_not_leak_across_clients():
    assert next_sequence(["NGM-007-01", "NGM-008-01", "NGM-008-02"], "NGM-007") == 2


# ── format_client_number / format_matter_number ──────────────────────────

def test_format_client_number_zero_pads_to_three_digits():
    assert format_client_number("NGM", 7) == "NGM-007"
    assert format_client_number("NGM", 123) == "NGM-123"


def test_format_matter_number_zero_pads_to_two_digits():
    assert format_matter_number("NGM-007", 2) == "NGM-007-02"
    assert format_matter_number("NGM-007", 11) == "NGM-007-11"
