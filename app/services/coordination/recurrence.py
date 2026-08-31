"""PRD 10.5.7 - Stage 6: recurrence tracking. A network that reappears across
several separate claims and unrelated policies over weeks is substantially harder to
explain as coincidence than one burst - this is the F5 counterpart of the fault-line
map's immune-memory concept, and platform referrals citing recurrence are more
actionable."""

import hashlib
from dataclasses import dataclass

DEFAULT_RECURRENCE_THRESHOLD = 0.50


def compute_fingerprint(member_account_ids: list[str], top_terms: list[str]) -> str:
    """Hashed sorted set of member platform IDs plus a top-term signature - a
    compact, stable identity key for one detected network, stored on
    coordinated_network.fingerprint_hash. Actual recurrence *matching* (below)
    compares real member-ID sets, not this hash directly - a hash match alone would
    only catch identical membership, missing the partial-overlap case the spec
    describes."""
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
    among candidates clearing the threshold), or None. The new detection then
    inherits that parent's history (coordinated_network.parent_network_id) but
    *never* its claim-relevance verdict - 10.5.1a point 8 is explicit that a
    recurring network must pass the relevance gate against the new claim on its own
    merits."""
    best_id: str | None = None
    best_score = 0.0
    for candidate in candidates:
        score = member_jaccard(new_member_ids, candidate.member_account_ids)
        if score >= threshold and score > best_score:
            best_id, best_score = candidate.network_id, score
    return best_id
