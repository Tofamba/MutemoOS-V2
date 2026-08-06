"""
Unit tests for _best_excerpt_window in backend/main.py — the query-aware
excerpt selector that replaced a flat chunk["text"][:400] slice in
_zlr_semantic_search.

The bug this fixes: a ZLR chunk (~2500 chars from chunk_text()'s 500-word
chunking) almost always opens with case-caption/coram boilerplate (case
name, court, judge, hearing dates) before reaching the facts or holding.
A short excerpt taken from chunk start showed only that boilerplate —
enough for the case NAME to drive a good similarity match, but not enough
substance for a drafting model to recognize the case as genuinely on
point and cite it. See the Kombayi reproduction below, which mirrors the
actual case from the bug report (a query about a matter v. Ministry of
Local Government correctly matching "KOMBAYI & ORS v MINISTER OF LOCAL
GOVERNMENT & ORS" on name, but the old excerpt never got past "the first
respondent is the Minister...").
"""

from backend.main import ZLR_EXCERPT_CHARS, _best_excerpt_window, chunk_text

# Realistic Zimbabwean judgment shape: caption/coram block with no
# sentence-ending punctuation, then facts, then the actual legal reasoning
# and holding, then the order. This structure — not just length — is what
# broke the old flat slice.
KOMBAYI_JUDGMENT = """KOMBAYI & ORS v MINISTER OF LOCAL GOVERNMENT & ORS
HC 4521/19
HIGH COURT OF ZIMBABWE
MUREMBA J
HARARE, 14 & 22 May 2020

This is an application for a declaratur brought in terms of section 14 of the High Court Act [Chapter 7:06]. The applicants are residents of Ward 12 within the jurisdiction of the second respondent, a local authority established under the Urban Councils Act [Chapter 29:15]. The first respondent is the Minister of Local Government, Public Works and National Housing, cited in his official capacity as the Minister responsible for administering the Act.

The background facts are largely common cause. The second respondent resolved, at a full council meeting held on 3 March 2019, to increase supplementary rates payable by residents of Ward 12 by 340 percent, purportedly pursuant to section 219 of the Urban Councils Act. No public consultation process preceded the resolution, and no notice of the proposed increase was published in a newspaper of general circulation as required by section 219(3) of the Act. The applicants aver that this omission renders the resolution procedurally unlawful and therefore invalid.

It is trite that a local authority exercising a statutory power to levy rates must comply strictly with the procedural preconditions attached to that power by the enabling statute. Failure to do so does not merely render the exercise of the power irregular it renders it void ab initio, since the power itself was never validly invoked. This principle finds clear support in the reasoning of this Court in a long line of authority dealing with ultra vires administrative action by local authorities, all to the same effect: a local authority is a creature of statute, and its powers do not exist independently of the statute which creates them, but are strictly bounded by the conditions the legislature has attached to their exercise. Where those conditions include public notice and consultation, as section 219(3) plainly requires, that requirement is not a mere formality that can be dispensed with when administratively inconvenient. It exists precisely to give affected ratepayers an opportunity to make representations before their financial burden is materially increased, and a local authority that bypasses it has not validly exercised its rate-making power at all.

In the result, the application succeeds. It is declared that the resolution of the second respondent dated 3 March 2019 purporting to increase supplementary rates for Ward 12 is unlawful and of no force or effect. The second respondent shall pay the applicants costs of suit.
"""

QUERY = "application against Ministry of Local Government for unlawful rate increase without consultation"


def _kombayi_chunk_text():
    chunks = chunk_text(KOMBAYI_JUDGMENT, page_count=2, doc_id="test-doc", matter_id="zlr")
    assert len(chunks) == 1, "test fixture expected to fit in a single chunk"
    return chunks[0]["text"]


def test_short_text_returned_unchanged():
    text = "Short chunk, nowhere near the window size."
    assert _best_excerpt_window(text, "any query") == text


def test_kombayi_excerpt_reaches_past_caption_into_holding_and_outcome():
    text = _kombayi_chunk_text()
    excerpt = _best_excerpt_window(text, QUERY)

    assert len(excerpt) <= ZLR_EXCERPT_CHARS
    # The old chunk[:400] excerpt never got past the caption/first
    # sentence — none of this would have been present.
    assert "void ab initio" in excerpt
    assert "application succeeds" in excerpt
    assert "unlawful and of no force" in excerpt


def test_old_flat_slice_would_have_missed_the_holding():
    """Confirms the fixture actually reproduces the bug being fixed —
    the old chunk[:400] slice for this same chunk stops mid-sentence in
    the caption/first-sentence boilerplate, nowhere near the holding."""
    text = _kombayi_chunk_text()
    old_excerpt = text[:400]
    assert "void ab initio" not in old_excerpt
    assert "application succeeds" not in old_excerpt


def test_widened_length_is_1200_to_1500_chars():
    assert 1200 <= ZLR_EXCERPT_CHARS <= 1500


def test_excerpt_starts_on_a_word_boundary_not_mid_word():
    text = _kombayi_chunk_text()
    excerpt = _best_excerpt_window(text, QUERY)
    first_word = excerpt.split(" ", 1)[0]
    # A real word from the source text, not a fragment like "tutory".
    assert text.count(f" {first_word} ") >= 1 or text.startswith(first_word)


def test_falls_back_to_chunk_start_when_query_has_no_overlap():
    text = _kombayi_chunk_text()
    excerpt = _best_excerpt_window(text, "zzz nonexistent query terms qqq")
    assert excerpt == text[:ZLR_EXCERPT_CHARS]


def test_falls_back_to_chunk_start_when_query_is_empty():
    text = _kombayi_chunk_text()
    excerpt = _best_excerpt_window(text, "")
    assert excerpt == text[:ZLR_EXCERPT_CHARS]


def test_custom_window_size_respected():
    text = _kombayi_chunk_text()
    excerpt = _best_excerpt_window(text, QUERY, window_chars=600)
    assert len(excerpt) <= 600
