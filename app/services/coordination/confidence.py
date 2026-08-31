"""PRD 10.6.2/10.6.3 - confidence banding and suppression rules. A high composite
score with SignalBreadth = 1 cannot reach High confidence, by design - that
configuration is the characteristic shape of a false positive, not of a campaign."""

from app.models.enums import ConfidenceBand

SIGNAL_FAMILY_SCORE_THRESHOLD = 50.0
HIGH_SCORE_MIN = 70.0
HIGH_BREADTH_MIN = 3
MEDIUM_SCORE_MIN = 55.0
MEDIUM_BREADTH_MIN = 2
ALLOWLIST_MAJORITY_THRESHOLD = 0.60


def compute_signal_breadth(component_scores: dict[str, float]) -> int:
    """Count of distinct signal families (SY/DU/CO/PR/AU) independently scoring >= 50
    for a cluster - the primary confidence-banding input alongside CoordinationScore."""
    return sum(1 for score in component_scores.values() if score >= SIGNAL_FAMILY_SCORE_THRESHOLD)


def determine_confidence_band(
    coordination_score: float,
    breadth: int,
    run_truncated: bool = False,
    unavailable_signal_count: int = 0,
    high_score_min: float = HIGH_SCORE_MIN,
    high_breadth_min: int = HIGH_BREADTH_MIN,
    medium_score_min: float = MEDIUM_SCORE_MIN,
    medium_breadth_min: int = MEDIUM_BREADTH_MIN,
) -> ConfidenceBand:
    """The four cutoffs are backend-configurable now (CISDetectorSettings'
    high/medium score+breadth cutoffs) - module constants remain the defaults."""
    if coordination_score >= high_score_min and breadth >= high_breadth_min:
        band = ConfidenceBand.HIGH
    elif coordination_score >= medium_score_min and breadth >= medium_breadth_min:
        band = ConfidenceBand.MEDIUM
    else:
        band = ConfidenceBand.LOW

    # 10.6.3 point 4: a degraded run (candidate truncation, or >=2 signal families
    # unavailable) caps confidence at Medium regardless of score - it never raises a
    # Low band, only ever pulls a High one down.
    if (run_truncated or unavailable_signal_count >= 2) and band == ConfidenceBand.HIGH:
        return ConfidenceBand.MEDIUM
    return band


def is_allowlist_suppressed(
    member_account_ids: list[str], allowlisted_account_ids: set[str]
) -> bool:
    """10.6.3 point 3: a network whose membership is >= 60% allowlisted accounts is
    suppressed entirely and logged as an allowlist hit, not merely down-ranked."""
    if not member_account_ids:
        return False
    allowlisted_count = sum(1 for a in member_account_ids if a in allowlisted_account_ids)
    return (allowlisted_count / len(member_account_ids)) >= ALLOWLIST_MAJORITY_THRESHOLD
