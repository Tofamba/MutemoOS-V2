"""
Before/after demonstration of backend/authority_ranker.py against the
exact "Police Amendment Bill H.B.11, 2025" scenario from the bug report.

Uses the same synthetic fixture as tests/test_authority_ranker.py — no
live Chroma/Postgres connection required. Run with:

    python scripts/demo_authority_ranker_police_bill.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.authority_ranker import rerank
from tests.test_authority_ranker import FIXTURE_RESULTS, QUERY

LABELS = {
    "bill": "Bill", "statute": "Act", "constitution": "Constitution",
    "supreme_court_judgment": "Supreme Court Judgment",
    "high_court_judgment": "High Court Judgment",
    "labour_court": "Labour Court Judgment",
    "correspondence": "Correspondence", "firm_precedent": "Firm Precedent",
}


def label(item):
    return LABELS.get(item.get("legal_source_type"), item.get("legal_source_type") or "Unknown")


def title(item):
    return item.get("filename") or item.get("case_name") or "Untitled"


def main():
    print(f'Query: "{QUERY}"\n')

    print("=" * 78)
    print("BEFORE — pure vector similarity (the reported bug)")
    print("=" * 78)
    before = sorted(FIXTURE_RESULTS, key=lambda r: r["similarity"], reverse=True)
    for i, item in enumerate(before, 1):
        print(f"  {i}. {title(item):45s} {label(item):24s} {item['similarity']:.0%} Match")

    print()
    print("=" * 78)
    print("AFTER — authority-first reranking")
    print("=" * 78)
    outcome = rerank(FIXTURE_RESULTS, QUERY)
    print(f"Confidence: {outcome['confidence']}\n")
    for i, item in enumerate(outcome["results"], 1):
        print(f"  {i}. [{item['tier'].upper():10s}] {title(item):45s} {label(item)}")
        print(f"     Score {item['final_score']:.1f}  "
              f"(similarity {item['semantic_similarity']:.2f} + authority {item['authority_score']} "
              f"+ act_match {item['act_name_match']} + title_match {item['query_title_match']} "
              f"+ citation {item['citation_overlap']})")
        print(f"     Why: {' '.join(item['reasons'])}")
    if outcome["excluded_count"]:
        print(f"\n  ({outcome['excluded_count']} item(s) excluded by the hard filter — "
              f"correspondence/narrow-matter firm documents with no reference to the "
              f"query's legal entities)")

    if outcome["cross_references"]:
        print()
        print("=" * 78)
        print("Suggested cross-references (stage 5)")
        print("=" * 78)
        for s in outcome["cross_references"]:
            print(f"  - {s['type']}: {s['search_term']}")


if __name__ == "__main__":
    main()
