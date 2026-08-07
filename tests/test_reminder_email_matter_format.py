"""
Unit tests for the reminder email's matter-identity line format:
"{matter_number} ({case_number}) — {client_name}: {existing text}" (e.g.
"NGM-007-02 (HC 300/26) — Huang Li Qiang: Filing deadline tomorrow").

Covers the three pure pieces in backend/main.py:
  - extract_case_reference(): pulls a case number out of matter free text
    following the onboarding template's "Reference/Case No. — description"
    convention — never invents one out of a plain descriptive label.
  - _matter_identity_prefix(): assembles the lead-in from whatever pieces
    are actually available, omitting the rest gracefully.
  - build_reminder_email_body(): the actual email text/HTML output, so a
    regression here (e.g. a bare "None" leaking into the email) is caught
    directly rather than only at the level of the two helpers above.
"""

from backend.main import (
    build_reminder_email_body,
    extract_case_reference,
    _matter_identity_prefix,
)


# ── extract_case_reference ───────────────────────────────────────────────

def test_extract_case_reference_finds_reference_before_dash():
    assert extract_case_reference("HC 300/26 — Commercial contract dispute") == "HC 300/26"


def test_extract_case_reference_none_when_no_dash_separator():
    assert extract_case_reference("Just a plain matter name") is None


def test_extract_case_reference_none_when_leading_segment_has_no_digit():
    """Not every matter's text actually follows the "Reference — description"
    convention — a plain descriptive label before the dash (e.g. a party
    name) must not be mistaken for a case number."""
    assert extract_case_reference("Mukweva Criminal — Criminal fraud, following up") is None


def test_extract_case_reference_none_for_blank_or_none():
    assert extract_case_reference("") is None
    assert extract_case_reference(None) is None


# ── _matter_identity_prefix ──────────────────────────────────────────────

def test_matter_identity_prefix_full_format():
    e = {"matter_number": "NGM-007-02", "case_number": "HC 300/26", "resolved_client_name": "Huang Li Qiang"}
    assert _matter_identity_prefix(e) == "NGM-007-02 (HC 300/26) — Huang Li Qiang"


def test_matter_identity_prefix_matter_number_only_no_case_number():
    e = {"matter_number": "NGM-007-02", "case_number": None, "resolved_client_name": "Huang Li Qiang"}
    assert _matter_identity_prefix(e) == "NGM-007-02 — Huang Li Qiang"


def test_matter_identity_prefix_no_matter_number_falls_back_to_client_only():
    """Legacy matter, no matter_number — still renders sensibly (client
    name alone), not a dangling separator or "None"."""
    e = {"matter_number": None, "case_number": None, "resolved_client_name": "Huang Li Qiang"}
    assert _matter_identity_prefix(e) == "Huang Li Qiang"


def test_matter_identity_prefix_nothing_available_returns_empty_string():
    assert _matter_identity_prefix({}) == ""
    assert _matter_identity_prefix({"matter_number": None, "case_number": None, "resolved_client_name": None}) == ""


# ── build_reminder_email_body ────────────────────────────────────────────

def _event(**overrides):
    e = {
        "event_type": "deadline", "title": "Filing deadline tomorrow", "date": "2026-08-08",
        "days_until": 1, "time": None, "court": None,
        "matter_name": None, "matter_number": None, "case_number": None, "resolved_client_name": None,
    }
    e.update(overrides)
    return e


def test_email_full_format_matter_number_case_number_and_client():
    text, html = build_reminder_email_body([_event(
        matter_number="NGM-007-02", case_number="HC 300/26", resolved_client_name="Huang Li Qiang",
    )])
    assert "NGM-007-02 (HC 300/26) — Huang Li Qiang: Deadline / Dies: Filing deadline tomorrow" in text
    assert "NGM-007-02 (HC 300/26) — Huang Li Qiang" in html
    assert "None" not in text and "None" not in html


def test_email_matter_number_only_no_case_number_found():
    text, html = build_reminder_email_body([_event(
        matter_number="NGM-007-02", resolved_client_name="Huang Li Qiang",
    )])
    assert "NGM-007-02 — Huang Li Qiang: " in text
    assert "()" not in text  # no empty parens when no case number was found
    assert "None" not in text and "None" not in html


def test_email_legacy_matter_no_matter_number_renders_sensibly():
    """Legacy matter with no matter_number at all — must not crash or show
    a literal "None"; falls back to client name alone."""
    text, html = build_reminder_email_body([_event(resolved_client_name="Huang Li Qiang")])
    assert "Huang Li Qiang: Deadline / Dies: Filing deadline tomorrow" in text
    assert "None" not in text and "None" not in html


def test_email_non_matter_event_unchanged_old_trailing_display():
    """A plain calendar event with no linked matter (e.g. a staff meeting)
    keeps the old trailing "(matter_name)" display — regression check that
    the new format doesn't touch events with nothing to resolve."""
    text, html = build_reminder_email_body([_event(
        title="Staff Meeting", matter_name="Weekly Sync",
    )])
    assert "(Weekly Sync)" in text
    assert "Weekly Sync" in html
    # No new-format leading identity block was fabricated for this event.
    assert " — Weekly Sync: " not in text


def test_email_event_with_nothing_at_all_still_renders():
    text, html = build_reminder_email_body([_event()])
    assert "Filing deadline tomorrow" in text
    assert "None" not in text and "None" not in html
