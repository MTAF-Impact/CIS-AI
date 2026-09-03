"""Signal: structural overlap (w_struct), optional. Returns None when no follower
data is available - fusion redistributes the weight."""

import math
from collections import defaultdict

from app.services.coordination.types import pair_key


def compute_structural_overlap(
    follower_sets: dict[str, set[str]] | None,
) -> dict[tuple[str, str], float] | None:
    """Adamic-Adar over follower sets. None means unavailable, not zero similarity."""
    if not follower_sets:
        return None

    connection_owners: dict[str, set[str]] = defaultdict(set)
    for account, followers in follower_sets.items():
        for f in followers:
            connection_owners[f].add(account)

    accounts = list(follower_sets.keys())
    raw_scores: dict[tuple[str, str], float] = {}
    for i in range(len(accounts)):
        for j in range(i + 1, len(accounts)):
            a, b = accounts[i], accounts[j]
            shared = follower_sets[a] & follower_sets[b]
            if not shared:
                continue
            # Adamic-Adar: discount shared connections that are themselves
            # mass-followed (low information value).
            score = sum(
                1.0 / math.log(len(connection_owners[c]) + 1)
                for c in shared
                if len(connection_owners[c]) > 1
            )
            if score > 0:
                raw_scores[pair_key(a, b)] = score

    if not raw_scores:
        return {}

    # Adamic-Adar is unbounded; normalise onto [0,1] within this candidate set so it
    # combines cleanly with the other four signals in fusion.
    max_score = max(raw_scores.values())
    return {k: round(v / max_score, 4) for k, v in raw_scores.items()}
