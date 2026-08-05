"""
Unit tests for backend/client_migration.py — the pure grouping/fuzzy-
matching logic behind scripts/migrate_clients.py.

Covers the two cases the migration script's design hinges on:
  - a client name that appears on exactly one matter auto-resolves (no
    merge ambiguity, safe to create a Client without review)
  - names that fuzzy-match into a group of 2+ matters are surfaced as a
    review group and are NEVER auto-merged by this logic
"""

from backend.client_migration import group_client_names, normalize_name


# ── normalize_name ───────────────────────────────────────────────────────────

def test_normalize_name_lowercases_and_strips_titles():
    assert normalize_name("Mr. Huang") == "huang"
    assert normalize_name("MRS. Jane Moyo") == "jane moyo"
    assert normalize_name("Dr Tendai Chikwanha") == "tendai chikwanha"


def test_normalize_name_strips_punctuation_and_collapses_whitespace():
    assert normalize_name("H.  Huang") == "h huang"
    assert normalize_name("Moyo,  John") == "moyo john"


def test_normalize_name_empty_input():
    assert normalize_name("") == ""
    assert normalize_name(None) == ""


# ── group_client_names: single-occurrence auto-resolves ─────────────────────

def test_single_occurrence_name_auto_resolves():
    matters = [{"matter_id": "m1", "client_name": "John Moyo"}]
    result = group_client_names(matters)
    assert len(result["auto_resolve"]) == 1
    assert result["auto_resolve"][0]["matter_id"] == "m1"
    assert result["review_groups"] == []


def test_multiple_unrelated_single_occurrence_names_all_auto_resolve():
    matters = [
        {"matter_id": "m1", "client_name": "John Moyo"},
        {"matter_id": "m2", "client_name": "Peter Ndlovu"},
        {"matter_id": "m3", "client_name": "Sithole Transport (Pvt) Ltd"},
    ]
    result = group_client_names(matters)
    assert len(result["auto_resolve"]) == 3
    assert result["review_groups"] == []
    resolved_ids = {e["matter_id"] for e in result["auto_resolve"]}
    assert resolved_ids == {"m1", "m2", "m3"}


# ── group_client_names: multi-occurrence produces a review group, never auto-merges ──

def test_fuzzy_variants_of_same_name_form_one_review_group():
    """The exact scenario from the task: 'Huang' / 'Mr. Huang' / 'H. Huang'
    across three different matters must cluster into ONE review group, not
    three separate auto-resolved clients and not silently merged either."""
    matters = [
        {"matter_id": "m1", "client_name": "Huang"},
        {"matter_id": "m2", "client_name": "Mr. Huang"},
        {"matter_id": "m3", "client_name": "H. Huang"},
    ]
    result = group_client_names(matters)

    assert result["auto_resolve"] == []
    assert len(result["review_groups"]) == 1
    group = result["review_groups"][0]
    assert {m["matter_id"] for m in group["members"]} == {"m1", "m2", "m3"}
    # A suggestion is offered, but nothing has been merged/applied by this
    # function — that's the caller's job, gated on human approval.
    assert group["suggested_name"] in ("Huang", "Mr. Huang", "H. Huang")


def test_review_group_matters_are_excluded_from_auto_resolve():
    matters = [
        {"matter_id": "m1", "client_name": "Huang"},
        {"matter_id": "m2", "client_name": "Mr. Huang"},
        {"matter_id": "m3", "client_name": "Unrelated Client"},
    ]
    result = group_client_names(matters)
    auto_ids = {e["matter_id"] for e in result["auto_resolve"]}
    review_ids = {m["matter_id"] for g in result["review_groups"] for m in g["members"]}
    assert auto_ids == {"m3"}
    assert review_ids == {"m1", "m2"}
    assert auto_ids.isdisjoint(review_ids)


def test_dissimilar_names_do_not_get_merged_into_the_same_group():
    matters = [
        {"matter_id": "m1", "client_name": "John Moyo"},
        {"matter_id": "m2", "client_name": "Peter Ndlovu"},
    ]
    result = group_client_names(matters)
    assert len(result["auto_resolve"]) == 2
    assert result["review_groups"] == []


def test_three_or_more_occurrences_still_a_single_group_not_auto_merged():
    matters = [
        {"matter_id": f"m{i}", "client_name": name}
        for i, name in enumerate(["Chikwanha", "Mr. Chikwanha", "T. Chikwanha", "Chikwanha, Tendai"])
    ]
    result = group_client_names(matters)
    assert result["auto_resolve"] == []
    assert len(result["review_groups"]) == 1
    assert len(result["review_groups"][0]["members"]) == 4


def test_blank_client_name_is_skipped_not_auto_resolved():
    matters = [
        {"matter_id": "m1", "client_name": ""},
        {"matter_id": "m2", "client_name": "   "},
        {"matter_id": "m3", "client_name": "Real Client"},
    ]
    result = group_client_names(matters)
    skipped_ids = {e["matter_id"] for e in result["skipped"]}
    assert skipped_ids == {"m1", "m2"}
    assert len(result["auto_resolve"]) == 1
    assert result["auto_resolve"][0]["matter_id"] == "m3"


def test_group_client_names_never_mutates_input():
    matters = [
        {"matter_id": "m1", "client_name": "Huang"},
        {"matter_id": "m2", "client_name": "Mr. Huang"},
    ]
    original = [dict(m) for m in matters]
    group_client_names(matters)
    assert matters == original
