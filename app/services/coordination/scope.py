"""Candidate scope and selection. The caller is responsible for scoping posts to a
claim's supporting-side cluster within the window; this module only applies
exclusions and the candidate cap to an already-scoped post list."""

from dataclasses import dataclass

from app.services.coordination.types import SignalPost

DEFAULT_A_MAX = 5000


@dataclass(frozen=True)
class CandidateSelection:
    posts: list[SignalPost]
    account_ids: list[str]
    candidates_count: int
    truncated: bool


def select_candidates(
    posts: list[SignalPost],
    allowlisted_account_ids: set[str] | None = None,
    self_exclusion_account_ids: set[str] | None = None,
    a_max: int = DEFAULT_A_MAX,
) -> CandidateSelection:
    """Excludes allowlisted and self-exclusion accounts before graph construction,
    then caps the remaining candidate set by post volume if it exceeds a_max."""
    excluded = (allowlisted_account_ids or set()) | (self_exclusion_account_ids or set())
    filtered_posts = [p for p in posts if p.account_id not in excluded]

    post_counts: dict[str, int] = {}
    for p in filtered_posts:
        post_counts[p.account_id] = post_counts.get(p.account_id, 0) + 1

    candidates_count = len(post_counts)
    truncated = candidates_count > a_max

    if truncated:
        ranked = sorted(post_counts, key=lambda a: post_counts[a], reverse=True)
        account_ids = ranked[:a_max]
        kept = set(account_ids)
        filtered_posts = [p for p in filtered_posts if p.account_id in kept]
    else:
        account_ids = list(post_counts.keys())

    return CandidateSelection(
        posts=filtered_posts,
        account_ids=account_ids,
        candidates_count=candidates_count,
        truncated=truncated,
    )
