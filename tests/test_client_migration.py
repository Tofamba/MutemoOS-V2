"""
Unit tests for backend/client_migration.py — the pure grouping/fuzzy-
matching logic behind scripts/migrate_clients.py.

Covers the two cases the migration script's design hinges on:
  - a client name that appears on exactly one matter auto-resolves (no
    merge ambiguity, safe to create a Client without review)
  - names that fuzzy-match into a group of 2+ matters are surfaced as a
    review group and are NEVER auto-merged by this logic
"""

from backend.client_migration import (
    group_client_names,
    match_client_name,
    normalize_name,
    split_review_group_by_exact_name,
)


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


# ── split_review_group_by_exact_name ─────────────────────────────────────────
# For a review group a human has REJECTED as a false-positive merge (real
# example from production: "Kudzai Madzingira" and "Kudzai Ndanga" only
# clustered because they share the token "kudzai") — splits it into one
# client per distinct exact name, without re-running fuzzy matching.

def test_split_creates_one_client_per_distinct_name():
    """Two different people incorrectly clustered on a shared token."""
    members = [
        {"matter_id": "m1", "client_name": "Kudzai Madzingira", "normalized_name": "kudzai madzingira"},
        {"matter_id": "m2", "client_name": "Kudzai Ndanga", "normalized_name": "kudzai ndanga"},
    ]
    result = split_review_group_by_exact_name(members)
    assert len(result) == 2
    names = {r["full_name"] for r in result}
    assert names == {"Kudzai Madzingira", "Kudzai Ndanga"}
    for r in result:
        assert len(r["members"]) == 1


def test_split_collapses_exact_duplicate_names_within_the_group():
    """Real production case: a rejected group of 3 members where 2 are
    literally the same person on two different matters ("Vongai Murigo"
    twice) and 1 is a genuinely different person ("Vongai Maroyi") who only
    got clustered in on shared-token similarity. Must produce exactly 2
    clients, not 3 — the same-name pair should NOT become duplicates."""
    members = [
        {"matter_id": "m1", "client_name": "Vongai Murigo", "normalized_name": "vongai murigo"},
        {"matter_id": "m2", "client_name": "Vongai Murigo", "normalized_name": "vongai murigo"},
        {"matter_id": "m3", "client_name": "Vongai Maroyi", "normalized_name": "vongai maroyi"},
    ]
    result = split_review_group_by_exact_name(members)
    assert len(result) == 2

    by_name = {r["full_name"]: r for r in result}
    assert set(by_name) == {"Vongai Murigo", "Vongai Maroyi"}
    assert len(by_name["Vongai Murigo"]["members"]) == 2
    assert {m["matter_id"] for m in by_name["Vongai Murigo"]["members"]} == {"m1", "m2"}
    assert len(by_name["Vongai Maroyi"]["members"]) == 1
    assert by_name["Vongai Maroyi"]["members"][0]["matter_id"] == "m3"


def test_split_picks_longest_raw_name_as_display_name():
    members = [
        {"matter_id": "m1", "client_name": "Muza Trust", "normalized_name": "muza trust"},
        {"matter_id": "m2", "client_name": "Mutungwe Trust", "normalized_name": "mutungwe trust"},
    ]
    result = split_review_group_by_exact_name(members)
    full_names = {r["full_name"] for r in result}
    assert full_names == {"Muza Trust", "Mutungwe Trust"}


def test_split_preserves_first_appearance_order():
    members = [
        {"matter_id": "m1", "client_name": "Pastor Linda", "normalized_name": "pastor linda"},
        {"matter_id": "m2", "client_name": "Pastor Charlotte", "normalized_name": "pastor charlotte"},
    ]
    result = split_review_group_by_exact_name(members)
    assert [r["full_name"] for r in result] == ["Pastor Linda", "Pastor Charlotte"]


# ── match_client_name ────────────────────────────────────────────────────
# Used by the bulk-onboarding endpoint to check a new client name from an
# uploaded form against existing clients (and clients already resolved
# earlier in the same upload) before creating a duplicate.

def test_match_client_name_no_candidates_is_no_match():
    assert match_client_name("Huang Li Qiang", []) == {"status": "no_match"}


def test_match_client_name_no_similar_candidate_is_no_match():
    candidates = [{"id": "c1", "full_name": "Peter Ndlovu"}]
    assert match_client_name("Vengesai Enterprises", candidates) == {"status": "no_match"}


def test_match_client_name_exact_existing_name_matches():
    candidates = [{"id": "c1", "full_name": "Huang Li Qiang"}]
    result = match_client_name("Huang Li Qiang", candidates)
    assert result == {"status": "matched", "candidate": {"id": "c1", "full_name": "Huang Li Qiang"}}


def test_match_client_name_near_variant_matches_single_candidate():
    """Same fuzzy tolerance as the migration script's own grouping."""
    candidates = [{"id": "c1", "full_name": "Huang Li Qiang"}]
    result = match_client_name("Mr. Huang Li Qiang", candidates)
    assert result["status"] == "matched"
    assert result["candidate"]["id"] == "c1"


def test_match_client_name_two_similar_candidates_is_ambiguous_not_guessed():
    candidates = [
        {"id": "c1", "full_name": "John Moyo"},
        {"id": "c2", "full_name": "Jon Moyo"},
    ]
    result = match_client_name("J. Moyo", candidates)
    assert result["status"] == "ambiguous"
    ids = {c["id"] for c in result["candidates"]}
    assert ids == {"c1", "c2"}


def test_match_client_name_unrelated_candidates_do_not_cause_false_ambiguity():
    candidates = [
        {"id": "c1", "full_name": "Peter Ndlovu"},
        {"id": "c2", "full_name": "Vengesai Enterprises"},
    ]
    result = match_client_name("Huang Li Qiang", candidates)
    assert result == {"status": "no_match"}
