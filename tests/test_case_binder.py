"""
Unit tests for backend/case_binder.py's provision_case_binder() — run
directly against config/case_binder_templates.yml, no database needed
(see the module's own docstring for why it's built this way).
"""
from backend.case_binder import known_matter_types, provision_case_binder


def test_known_matter_types_matches_the_seeded_config():
    assert known_matter_types() == ["conveyancing", "debt_collection", "litigation_general"]


def test_conveyancing_returns_its_three_starter_documents():
    docs = provision_case_binder(
        {"matter_number": "TC-001-01"}, "conveyancing", {"full_name": "Chiedza Bvumbe"}
    )
    names = [d["name"] for d in docs]
    assert names == [
        "Client Engagement Letter",
        "Agreement of Sale",
        "Deeds Office Lodgement Checklist",
    ]


def test_debt_collection_returns_its_two_starter_documents():
    docs = provision_case_binder(
        {"matter_number": "BN-001-01"}, "debt_collection", {"full_name": "Blue Ridge Traders"}
    )
    names = [d["name"] for d in docs]
    assert names == ["Client Engagement Letter", "Letter of Demand"]


def test_litigation_general_returns_its_two_starter_documents():
    docs = provision_case_binder(
        {"matter_number": "FG-001-01"}, "litigation_general", {"full_name": "Rutendo Chikwavaire"}
    )
    names = [d["name"] for d in docs]
    assert names == ["Client Engagement Letter", "Case Summary / Instructions Sheet"]


def test_unknown_matter_type_returns_empty_list_not_an_error():
    docs = provision_case_binder(
        {"matter_number": "TC-005-01"}, "some_matter_type_not_in_the_yml", {"full_name": "Someone"}
    )
    assert docs == []


def test_merge_fields_are_substituted_when_needs_merge_fields_is_true():
    docs = provision_case_binder(
        {"matter_number": "RR-002-01"}, "conveyancing",
        {"full_name": "Sunshine Properties (Pvt) Ltd"}, today="2026-08-19",
    )
    engagement_letter = next(d for d in docs if d["name"] == "Client Engagement Letter")
    assert "Sunshine Properties (Pvt) Ltd" in engagement_letter["content"]
    assert "RR-002-01" in engagement_letter["content"]
    assert "2026-08-19" in engagement_letter["content"]
    assert "{{" not in engagement_letter["content"]  # no unsubstituted tokens left


def test_content_left_untouched_when_needs_merge_fields_is_false():
    docs = provision_case_binder(
        {"matter_number": "TC-001-01"}, "conveyancing", {"full_name": "Chiedza Bvumbe"}, today="2026-08-19",
    )
    checklist = next(d for d in docs if d["name"] == "Deeds Office Lodgement Checklist")
    assert "Chiedza Bvumbe" not in checklist["content"]
    assert "TC-001-01" not in checklist["content"]
    assert "2026-08-19" not in checklist["content"]


def test_today_defaults_to_the_real_date_when_not_given():
    from datetime import date
    docs = provision_case_binder(
        {"matter_number": "TC-001-01"}, "conveyancing", {"full_name": "Chiedza Bvumbe"}
    )
    engagement_letter = next(d for d in docs if d["name"] == "Client Engagement Letter")
    assert date.today().isoformat() in engagement_letter["content"]


def test_every_item_carries_name_template_source_and_content():
    docs = provision_case_binder(
        {"matter_number": "TC-001-01"}, "conveyancing", {"full_name": "Chiedza Bvumbe"}
    )
    for d in docs:
        assert set(d.keys()) == {"name", "template_source", "content"}
        assert d["template_source"].startswith("placeholder:")


def test_missing_client_and_matter_fields_do_not_crash():
    # A caller in preview mode may not have a real matter_number yet (e.g.
    # an ambiguous client match with no client_number resolved) -- must
    # degrade gracefully, not KeyError/AttributeError.
    docs = provision_case_binder({}, "conveyancing", {})
    assert len(docs) == 3
    docs2 = provision_case_binder(None, "debt_collection", None)
    assert len(docs2) == 2
