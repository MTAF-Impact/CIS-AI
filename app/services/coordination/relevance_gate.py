"""Claim-relevance gate. A detected cluster must pass three tests against the claim
it's attributed to, or it's suppressed as an off-topic coordinated cluster."""

from dataclasses import dataclass

from app.services.coordination.types import SignalPost

DEFAULT_MU_ANCHOR = 0.60
DEFAULT_P_MIN = 20
DEFAULT_OMEGA_MIN = 0.15

FailedTest = str  # "anchoring" | "evidence_volume" | "link_strength"


@dataclass(frozen=True)
class RelevanceResult:
    passed: bool
    overlap_ratio: float
    anchoring_share: float
    claim_cluster_post_count: int
    failed_test: FailedTest | None


def evaluate_claim_relevance(
    member_account_ids: list[str],
    claim_scoped_posts: list[SignalPost],
    total_posts_by_account: dict[str, int],
    mu_anchor: float = DEFAULT_MU_ANCHOR,
    p_min: int = DEFAULT_P_MIN,
    omega_min: float = DEFAULT_OMEGA_MIN,
) -> RelevanceResult:
    """claim_scoped_posts: this claim's supporting-cluster posts (any member, not
    just this cluster's). total_posts_by_account: each member's post count across
    all monitored content in the window. A member missing from this dict falls back
    to its claim-scoped count, assuming 100% of its activity is on this claim."""
    member_set = set(member_account_ids)
    posts_per_account: dict[str, int] = dict.fromkeys(member_set, 0)
    for p in claim_scoped_posts:
        if p.account_id in member_set:
            posts_per_account[p.account_id] += 1

    claim_cluster_post_count = sum(posts_per_account.values())
    anchored_members = sum(1 for count in posts_per_account.values() if count >= 2)
    anchoring_share = anchored_members / len(member_set) if member_set else 0.0

    total_posts = sum(
        total_posts_by_account.get(a, count) for a, count in posts_per_account.items()
    )
    overlap_ratio = claim_cluster_post_count / total_posts if total_posts > 0 else 0.0

    if anchoring_share < mu_anchor:
        failed: FailedTest | None = "anchoring"
    elif claim_cluster_post_count < p_min:
        failed = "evidence_volume"
    elif overlap_ratio < omega_min:
        failed = "link_strength"
    else:
        failed = None

    return RelevanceResult(
        passed=failed is None,
        overlap_ratio=round(overlap_ratio, 4),
        anchoring_share=round(anchoring_share, 4),
        claim_cluster_post_count=claim_cluster_post_count,
        failed_test=failed,
    )
