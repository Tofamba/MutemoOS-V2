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


# ── notes / attendees (2026-09-18: every Add Event form field should ──────
# appear in the reminder email where it has a value -- title/type/time/
# court/matter context were already rendered (covered above); notes and
# attendees were the real, confirmed gap. Reuses the exact event data
# already stored (_row_to_event() already puts notes/attendees on the
# dict) -- a template fix, no new tracking.

def test_email_shows_notes_when_present():
    text, html = build_reminder_email_body([_event(notes="Bring the signed lease and ID copies")])
    assert "Notes: Bring the signed lease and ID copies" in text
    assert "Bring the signed lease and ID copies" in html


def test_email_omits_notes_field_entirely_when_absent():
    """No value for this event -- no "Notes:" label at all, not an empty one."""
    text, html = build_reminder_email_body([_event()])
    assert "Notes:" not in text
    assert "Notes" not in html


def test_email_truncates_a_long_note_same_as_other_previews():
    long_note = "A" * 250
    text, html = build_reminder_email_body([_event(notes=long_note)])
    # Same _truncate_preview() convention as Matter Review Status/Client
    # Activity -- 160 chars then an ellipsis, not the full 250.
    assert "A" * 250 not in text
    assert "A" * 250 not in html
    assert "…" in text
    assert "…" in html


def test_email_shows_attendees_by_name_when_present():
    text, html = build_reminder_email_body([_event(
        attendees=[{"email": "t.moyo@example.com", "name": "Tendai Moyo"},
                   {"email": "r.rusike@example.com", "name": "Rufaro Rusike"}],
    )])
    assert "Attendees: Tendai Moyo, Rufaro Rusike" in text
    assert "Tendai Moyo, Rufaro Rusike" in html


def test_email_attendee_with_no_name_falls_back_to_email():
    text, html = build_reminder_email_body([_event(
        attendees=[{"email": "t.moyo@example.com", "name": None}],
    )])
    assert "Attendees: t.moyo@example.com" in text
    assert "t.moyo@example.com" in html


def test_email_omits_attendees_field_entirely_when_absent_or_empty():
    text, html = build_reminder_email_body([_event(attendees=[])])
    assert "Attendees:" not in text
    assert "With:" not in html
    text2, html2 = build_reminder_email_body([_event()])  # no attendees key at all (matter-deadline shape)
    assert "Attendees:" not in text2
    assert "With:" not in html2


def test_email_shows_both_notes_and_attendees_together():
    text, html = build_reminder_email_body([_event(
        notes="Discuss settlement offer",
        attendees=[{"email": "client@example.com", "name": "Client Rep"}],
    )])
    assert "Notes: Discuss settlement offer" in text
    assert "Attendees: Client Rep" in text
    assert "Discuss settlement offer" in html
    assert "Client Rep" in html


def test_email_type_matter_context_and_court_already_present_not_a_regression():
    """Confirms, on real code (not the earlier screenshots), that Type,
    Matter context, and Court/Location were already rendered before this
    fix -- only Notes/Attendees were the real gap. A full-field event
    shows everything at once."""
    text, html = build_reminder_email_body([_event(
        event_type="meeting", title="Client Meeting", court="Harare Magistrates Court",
        matter_number="NGM-010-01", case_number="HC 55/26", resolved_client_name="Test Client",
        notes="Discuss next steps", attendees=[{"email": "a@example.com", "name": "Attendee A"}],
    )])
    assert "Client Meeting" in text and "Deadline / Dies" not in text
    assert "Harare Magistrates Court" in text
    assert "NGM-010-01 (HC 55/26) — Test Client" in text
    assert "Discuss next steps" in text
    assert "Attendee A" in text
    assert "Client Meeting" in html
    assert "Harare Magistrates Court" in html
    assert "NGM-010-01 (HC 55/26) — Test Client" in html
    assert "Discuss next steps" in html
    assert "Attendee A" in html
