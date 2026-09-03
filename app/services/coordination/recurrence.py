"""Recurrence tracking. A network that reappears across several separate claims
over weeks is harder to explain as coincidence than one burst."""

import hashlib
from dataclasses import dataclass

DEFAULT_RECURRENCE_THRESHOLD = 0.50


def compute_fingerprint(member_account_ids: list[str], top_terms: list[str]) -> str:
    """Hashed sorted set of member platform IDs plus a top-term signature - a
    compact, stable identity key for one detected network. Recurrence matching
    (below) compares real member-ID sets, not this hash directly, to also catch
    partial-overlap cases."""
    canonical = "|".join(sorted(set(member_account_ids))) + "::" + "|".join(sorted(set(top_terms)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def member_jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


@dataclass(frozen=True)
class RecurrenceCandidate:
    network_id: str
    member_account_ids: set[str]


def find_recurrence_parent(
    new_member_ids: set[str],
    candidates: list[RecurrenceCandidate],
    threshold: float = DEFAULT_RECURRENCE_THRESHOLD,
) -> str | None:
    """Returns the network_id of the best-matching prior network (highest Jaccard
    among candidates clearing the threshold), or None. A recurring network still
    must pass the relevance gate against the new claim on its own merits."""
    best_id: str | None = None
    best_score = 0.0
    for candidate in candidates:
        score = member_jaccard(new_member_ids, candidate.member_account_ids)
        if score >= threshold and score > best_score:
            best_id, best_score = candidate.network_id, score
    return best_id
