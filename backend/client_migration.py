"""
Pure grouping/matching logic for the standalone clients backfill
(scripts/migrate_clients.py) — kept separate from that script so it's
testable without a database connection, and separate from backend/main.py
so importing it doesn't pull in the app's full (heavy) import chain.

Groups existing matters' free-text client_name values into candidate
Client identities: normalizes titles/punctuation, then fuzzy-matches near-
variants ("Huang" / "Mr. Huang" / "H. Huang") using difflib — the same
approach backend/main.py's check_matter_conflict already uses for name
similarity (shared significant word as a strong signal, boosted score),
kept consistent here rather than introducing a new fuzzy-matching
dependency (e.g. python-Levenshtein) for a single script.

A name that occurs only once has nothing to merge with — no ambiguity, so
it's safe to auto-resolve into its own Client record. A name that clusters
with 2+ matters is a *suggestion* only: the caller (scripts/migrate_clients.py)
must never create Client records or set matters.client_id for a review
group without a separate, explicit human-approved step.
"""

import difflib
import re

_TITLE_PREFIXES = (
    "mr", "mrs", "ms", "miss", "dr", "prof", "adv", "advocate", "hon",
    "chief", "rev", "sir", "dame",
)

SIMILARITY_THRESHOLD = 0.8


def normalize_name(name: str) -> str:
    """Lowercase, strip honorific titles and punctuation, collapse whitespace."""
    if not name:
        return ""
    s = re.sub(r"[.,]", " ", name.lower().strip())
    tokens = [t for t in s.split() if t]
    while tokens and tokens[0] in _TITLE_PREFIXES:
        tokens = tokens[1:]
    return " ".join(tokens)


def _name_tokens(s: str) -> set:
    return {t for t in s.split() if len(t) >= 2}


def _similarity(a: str, b: str) -> float:
    score = difflib.SequenceMatcher(None, a, b).ratio()
    # A shared significant token (surname, or a name spelled out where the
    # other side only has an initial) is a stronger signal than raw
    # character similarity — "h huang" vs "huang" share "huang" outright,
    # which should cluster them even though the character-level ratio alone
    # is unremarkable for such short strings.
    if _name_tokens(a) & _name_tokens(b):
        score = max(score, 0.85)
    return score


def group_client_names(matters: list) -> dict:
    """
    matters: list of {"matter_id": ..., "client_name": ...} — every matter
    with a non-null client_name and no client_id yet. Order matters for
    reproducibility; callers should query in a stable order.

    Returns:
        {
          "auto_resolve": [ {matter_id, client_name, normalized_name}, ... ],
          "review_groups": [
              {"group_index": int, "suggested_name": str,
               "members": [ {matter_id, client_name, normalized_name}, ... ]},
              ...
          ],
          "skipped": [ {matter_id, client_name}, ... ],   # blank after normalization
        }

    Single-occurrence normalized names land in auto_resolve — no merge
    ambiguity, safe to create a Client automatically. Anything that
    fuzzy-matches into a cluster of 2+ lands in review_groups and is NEVER
    auto-merged by this function or its caller; a human must approve (or
    split/reject) each group before any Client record is created for it.
    """
    entries = []
    skipped = []
    for m in matters:
        raw = m.get("client_name") or ""
        norm = normalize_name(raw)
        if not norm:
            skipped.append({"matter_id": m["matter_id"], "client_name": raw})
            continue
        entries.append({
            "matter_id": m["matter_id"],
            "client_name": raw,
            "normalized_name": norm,
        })

    # Greedy clustering: walk entries in order, attach each to the first
    # existing cluster whose representative it's similar enough to,
    # otherwise start a new cluster. Deterministic given input order.
    clusters = []  # [{"representative": str, "members": [entry, ...]}, ...]
    for entry in entries:
        norm = entry["normalized_name"]
        placed = False
        for cluster in clusters:
            if _similarity(norm, cluster["representative"]) >= SIMILARITY_THRESHOLD:
                cluster["members"].append(entry)
                placed = True
                break
        if not placed:
            clusters.append({"representative": norm, "members": [entry]})

    auto_resolve = []
    review_groups = []
    for cluster in clusters:
        members = cluster["members"]
        if len(members) == 1:
            auto_resolve.append(members[0])
        else:
            # Longest raw client_name as the suggested display name — usually
            # the least-abbreviated variant ("Mr. Huang" over "H. Huang").
            suggested = max(members, key=lambda e: len(e["client_name"]))["client_name"]
            review_groups.append({
                "group_index": len(review_groups),
                "suggested_name": suggested,
                "members": members,
            })

    return {"auto_resolve": auto_resolve, "review_groups": review_groups, "skipped": skipped}
