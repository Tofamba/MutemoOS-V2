"""
Tests for the contract-review quote-verification pipeline in
backend/main.py — covers two bugs found while diagnosing a real report of
"11047 finding(s) checked and removed" plus "No issues flagged" on a
9-page employment contract:

1. _verify_quote_in_text didn't normalize typographic quote/dash variants
   (curly "smart" quotes vs the plain ASCII an LLM defaults to), and its
   old fixed-step sliding-window fuzzy fallback could miss a near-exact
   match entirely depending on where the difference landed relative to the
   step size — both are exercised directly here since they don't need a
   live Anthropic call.
2. review_contract's Stage 2 loop assumed `findings` was always a list;
   if the model ever returns it as a plain string, `for f in "..."`
   iterates one character at a time, and each "item" fails the
   isinstance(dict) check — silently inflating dropped_unverified_count to
   the string's length while verified_findings stays empty. That's
   exercised through the real endpoint with a mocked Anthropic client.
"""
import asyncio
import json
from types import SimpleNamespace

from backend.main import _normalize_for_match, _verify_quote_in_text, review_contract


# ── _verify_quote_in_text ────────────────────────────────────────────────────

def test_verifies_quote_despite_autocorrected_curly_quotes():
    # Word's autocorrect turns typed straight quotes into curly ones in the
    # .docx XML; an LLM transcribing a quote defaults to plain ASCII —
    # exactly the shape of a Schedule 1 "dictionary" defined-term clause.
    doc = ('In this Agreement, “Confidential Information” means any '
           'non-public information disclosed by the Company to the Employee '
           'during employment.')
    quote = ('"Confidential Information" means any non-public information '
             'disclosed by the Company to the Employee during employment.')
    assert _verify_quote_in_text(quote, doc) is True


def test_verifies_short_defined_term_despite_curly_quotes():
    # Below the 15-char fuzzy floor -- must be rescued by exact-match after
    # typographic normalization, not by the fuzzy fallback.
    doc = 'Schedule 1 – Definitions. “Employee” means the individual named in clause 1.'
    quote = '"Employee"'
    assert _verify_quote_in_text(quote, doc) is True


def test_verifies_exact_clean_quote_baseline():
    doc = ('The Employee shall not, for a period of 24 months following '
           'termination, engage in any competing business within Zimbabwe.')
    assert _verify_quote_in_text(doc, doc) is True


def test_verifies_near_exact_quote_when_corruption_is_at_both_edges():
    # The fuzzy fallback anchors on the *middle* of the quote specifically
    # so a corrected quote mark at either edge can't blind it to an
    # otherwise-real match.
    doc = ('The “Restricted Period” shall be twenty-four (24) months from '
           'the Termination Date, as defined in Schedule 1.”')
    quote = ('"Restricted Period" shall be twenty-four (24) months from the '
              'Termination Date, as defined in Schedule 1."')
    assert _verify_quote_in_text(quote, doc) is True


def test_rejects_genuinely_fabricated_quote():
    # The whole point of verification: a quote describing something the
    # document doesn't say must still be rejected, not waved through by an
    # overly-loose fuzzy match.
    doc = 'The Employee shall not compete for 24 months.'
    quote = 'The Employee agrees to a lifetime worldwide non-compete covering all industries.'
    assert _verify_quote_in_text(quote, doc) is False


def test_rejects_empty_quote():
    assert _verify_quote_in_text('', 'some document text') is False
    assert _verify_quote_in_text(None, 'some document text') is False


def test_normalize_collapses_tabs_and_newlines_like_whitespace():
    assert _normalize_for_match("Clause\t8.3\n\nNon-Compete") == "clause 8.3 non-compete"


# ── review_contract's Stage 2 type guard ─────────────────────────────────────

class FakeUploadFile:
    def __init__(self, filename, content: bytes):
        self.filename = filename
        self._content = content

    async def read(self):
        return self._content


def _fake_tool_use_message(findings, overall_summary="Summary.", stop_reason="tool_use"):
    tool_use_block = SimpleNamespace(type="tool_use", input={"overall_summary": overall_summary, "findings": findings})
    return SimpleNamespace(content=[tool_use_block], stop_reason=stop_reason, usage=SimpleNamespace(output_tokens=123))


def test_findings_as_raw_string_raises_instead_of_inflating_dropped_count(monkeypatch):
    # Reproduces the reported bug directly: `findings` comes back as a
    # string instead of a list. Before the fix, this silently produced
    # dropped_unverified_count == len(that string) and an empty findings
    # list ("No issues flagged"). After the fix it must fail loudly.
    import backend.main as m
    monkeypatch.setattr(
        m.client.messages, "create",
        lambda **kwargs: _fake_tool_use_message("this looks like a findings-shaped string but is not a list")
    )
    upload = FakeUploadFile("contract.txt", b"Some contract body text.")

    from fastapi import HTTPException
    try:
        asyncio.run(review_contract(None, upload, None))
        assert False, "expected review_contract to raise"
    except HTTPException as e:
        assert e.status_code == 502


def test_findings_as_json_encoded_string_recovers_instead_of_502ing(monkeypatch):
    # Reproduces the exact production failure seen after the type-guard
    # fix shipped: the model emitted a complete, well-formed, correctly
    # shaped JSON array for `findings` -- but as a *string* value rather
    # than a native array in the tool-use input. Both real occurrences
    # involved a finding that quoted contract clause text containing
    # literal embedded double quotes (a salary clause phrased `... "NET of
    # US$2000.00 per month excluding all relevant taxes..." `verbatim in
    # the source document). Since the content itself is genuinely
    # recoverable JSON (not truncated garbage), this should succeed with
    # real findings rather than 502.
    import backend.main as m
    doc_text = (
        b'The Employee shall be paid "NET of US$2000.00 per month excluding all '
        b'relevant taxes required by the Government of Zimbabwe."'
    )
    findings_json_string = json.dumps([
        {
            "category": "compliance",
            "severity": "high",
            "title": "Net salary arrangement may violate PAYE requirements",
            "description": 'The contract specifies payment of "NET of US$2000.00 per month excluding '
                            'all relevant taxes required by the Government of Zimbabwe," which is '
                            "contradictory and potentially non-compliant.",
            "quote": 'The Employee shall be paid "NET of US$2000.00 per month excluding all '
                     'relevant taxes required by the Government of Zimbabwe."',
        },
    ])
    monkeypatch.setattr(m.client.messages, "create", lambda **kwargs: _fake_tool_use_message(findings_json_string))
    upload = FakeUploadFile("contract.txt", doc_text)

    result = asyncio.run(review_contract(None, upload, None))

    assert result["dropped_unverified_count"] == 0
    assert len(result["findings"]) == 1
    assert result["findings"][0]["title"] == "Net salary arrangement may violate PAYE requirements"
    assert result["findings"][0]["verification"] == "verified"


def test_findings_as_non_json_string_still_502s(monkeypatch):
    # The recovery attempt must not weaken the guard -- a string that
    # isn't valid JSON (or doesn't decode to a list) is a genuine
    # generation failure and must still fail loudly, not silently.
    import backend.main as m
    monkeypatch.setattr(
        m.client.messages, "create",
        lambda **kwargs: _fake_tool_use_message('{"not": "a list, a dict"}')
    )
    upload = FakeUploadFile("contract.txt", b"Some contract body text.")

    from fastapi import HTTPException
    try:
        asyncio.run(review_contract(None, upload, None))
        assert False, "expected review_contract to raise"
    except HTTPException as e:
        assert e.status_code == 502


def test_single_malformed_entry_in_otherwise_valid_list_is_dropped_not_fatal(monkeypatch):
    # The narrower, already-existing guard: one bad entry inside a real
    # list should be skipped (dropped_count == 1), not treated as a
    # generation failure -- confirms the new top-level guard didn't change
    # this existing, correct behaviour.
    import backend.main as m
    doc_text = b"The Employee shall not compete for 24 months following termination."
    findings = [
        "a stray string entry, not an object",
        {"category": "risky_term", "severity": "medium", "title": "Non-compete",
         "description": "desc", "quote": "The Employee shall not compete for 24 months following termination."},
    ]
    monkeypatch.setattr(m.client.messages, "create", lambda **kwargs: _fake_tool_use_message(findings))
    upload = FakeUploadFile("contract.txt", doc_text)

    result = asyncio.run(review_contract(None, upload, None))

    assert result["dropped_unverified_count"] == 1
    assert len(result["findings"]) == 1
    assert result["findings"][0]["verification"] == "verified"


def test_missing_findings_key_defaults_to_empty_not_an_error(monkeypatch):
    import backend.main as m
    fake_message = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", input={"overall_summary": "ok"})],
        stop_reason="tool_use", usage=SimpleNamespace(output_tokens=50),
    )
    monkeypatch.setattr(m.client.messages, "create", lambda **kwargs: fake_message)
    upload = FakeUploadFile("contract.txt", b"Some contract body text.")

    result = asyncio.run(review_contract(None, upload, None))

    assert result["findings"] == []
    assert result["dropped_unverified_count"] == 0


def test_findings_truncated_mid_string_by_max_tokens_still_502s_with_a_useful_hint(monkeypatch):
    # The genuinely-different failure mode found on a later production
    # retry: `findings` comes back as a string again, but this time it's
    # incomplete (cut off mid-object, no closing bracket) because the
    # generation hit max_tokens -- json.loads() must legitimately fail to
    # parse it (never fabricate a partial list from truncated JSON), and
    # the resulting error should call out that the response was cut off,
    # not just report a generic format mismatch.
    import backend.main as m
    truncated_json_string = (
        '[\n  {\n    "category": "compliance",\n    "severity": "high",\n    '
        '"title": "Net salary payment structure potentially violates statutory tax obligations",\n    '
        '"description": "The contract specifies a \'NET of US$2000.00 per month excluding all relevant '
        'taxes required by the Government of Zimbabwe.\' This structure appears to shift the employer'
    )
    monkeypatch.setattr(
        m.client.messages, "create",
        lambda **kwargs: _fake_tool_use_message(truncated_json_string, stop_reason="max_tokens")
    )
    upload = FakeUploadFile("contract.txt", b"Some contract body text.")

    from fastapi import HTTPException
    try:
        asyncio.run(review_contract(None, upload, None))
        assert False, "expected review_contract to raise"
    except HTTPException as e:
        assert e.status_code == 502
        assert "cut off" in e.detail.lower()
