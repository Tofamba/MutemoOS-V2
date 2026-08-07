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


def split_review_group_by_exact_name(members: list) -> list:
    """
    For a review_group a human has REJECTED as a single merge (its members
    are not all the same person/entity — e.g. "Kudzai Madzingira" and
    "Kudzai Ndanga" only clustered because they share the token "kudzai"),
    groups its members by EXACT normalized_name instead of fuzzy matching.
    No further fuzzy clustering is applied — a human has already decided
    this group is not one identity, so the only thing left to detect is
    members that are literally the same name appearing on multiple matters
    (e.g. "Vongai Murigo" named on two separate matters), which should
    still collapse into one client rather than becoming duplicates.

    members: a review_group's "members" list, each
        {"matter_id": ..., "client_name": ..., "normalized_name": ...}.

    Returns a list of {"full_name": str, "members": [...]}, one entry per
    distinct normalized_name, in first-appearance order. "full_name" is
    the longest raw client_name among that name's members, matching
    group_client_names()'s own display-name convention.
    """
    order = []
    by_name = {}
    for m in members:
        key = m["normalized_name"]
        if key not in by_name:
            by_name[key] = []
            order.append(key)
        by_name[key].append(m)

    return [
        {
            "full_name": max(by_name[key], key=lambda e: len(e["client_name"]))["client_name"],
            "members": by_name[key],
        }
        for key in order
    ]


def match_client_name(name: str, candidates: list) -> dict:
    """
    Matches a single new client name against a pool of candidate existing
    clients — e.g. clients already in the DB, plus (for a caller processing
    several new names in one batch, such as a bulk-onboarding upload)
    clients already created/matched earlier in that same batch, so a
    repeated near-identical name within one upload reuses the same client
    rather than creating a near-duplicate.

    Reuses normalize_name/_similarity — the same fuzzy-matching primitives
    group_client_names() uses for batch clustering — but for a different
    shape: one name against a known candidate pool, not clustering an
    unordered batch of matter client_names against each other.

    candidates: list of {"id": ..., "full_name": ...}

    Returns one of:
        {"status": "no_match"}
        {"status": "matched", "candidate": {"id", "full_name"}}
        {"status": "ambiguous", "candidates": [{"id", "full_name"}, ...]}

    "ambiguous" means 2+ candidates score at or above SIMILARITY_THRESHOLD
    — genuinely unclear which (if any) is the same person/entity. Callers
    must not guess; surface it for human review, same as review_groups
    from group_client_names().
    """
    norm = normalize_name(name)
    scored = []
    for c in candidates:
        score = _similarity(norm, normalize_name(c["full_name"]))
        if score >= SIMILARITY_THRESHOLD:
            scored.append((score, c))
    scored.sort(key=lambda x: -x[0])

    if not scored:
        return {"status": "no_match"}
    if len(scored) == 1:
        return {"status": "matched", "candidate": scored[0][1]}
    return {"status": "ambiguous", "candidates": [c for _, c in scored]}
