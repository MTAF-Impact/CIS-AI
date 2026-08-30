"""Claim Scoring System - PRD v1.1 Section 5. Replaces the old risk_engine.py's
Narrative-based formula outright (different inputs, different weights, different
philosophy: built for auditability/defensibility, not virality tracking).

All functions are pure (no I/O) and unit-testable without a database, mirroring the
old risk_engine.py's style. Falseness (F) scoring lives in falseness_service.py instead
of here - it needs pgvector similarity search against OfficialSource rows, which is a
different concern (hard-thresholded, never-fabricate) from this module's pure math.
"""

import math
import uuid
from dataclasses import dataclass

TOTAL_MONITORED_PLATFORMS = 5  # len(app.models.enums.ContentSource)
ROLLING_WINDOW_HOURS = 24.0  # reused for both Velocity's delta window and NPR's window
RELIABILITY_THRESHOLD = 25  # posts; midpoint of the PRD's recommended 20-30 range
GAMMA = 0.5  # NPR discount cap - even NPR=1 reduces the score by at most 50%
TOPIC_ATTACH_THRESHOLD = 0.5  # cosine similarity for dynamic topic assignment

# ClaimScore = 0.15*R + 0.15*V + 0.30*F + 0.30*H + 0.10*EI
WEIGHT_R = 0.15
WEIGHT_V = 0.15
WEIGHT_F = 0.30
WEIGHT_H = 0.30
WEIGHT_EI = 0.10

# Harm sub-component weights - PolicyDisruption intentionally weighted lowest: a
# government-run tool scoring "criticism of its own policy" as harm carries bias risk.
HARM_WEIGHT_PUBLIC_SAFETY = 0.35
HARM_WEIGHT_INSTITUTIONAL_TRUST = 0.30
HARM_WEIGHT_ECONOMIC = 0.20
HARM_WEIGHT_POLICY_DISRUPTION = 0.15


@dataclass(frozen=True)
class ReachWeights:
    """R's per-component weights (w1-w4). Placeholder equal-weighting until real
    values are supplied - the PRD leaves these unspecified."""

    impressions: float = 0.25
    unique_authors: float = 0.25
    content_count: float = 0.25
    distinct_platforms: float = 0.25


DEFAULT_REACH_WEIGHTS = ReachWeights()


def raw_reach(
    impressions_sum: int,
    unique_authors: int,
    content_count: int,
    distinct_platforms: int,
    weights: ReachWeights = DEFAULT_REACH_WEIGHTS,
) -> float:
    """R = w1*log(1+Impressions) + w2*log(1+UniqueAuthors) + w3*log(1+ContentCount)
    + w4*(DistinctPlatforms/TotalMonitoredPlatforms). Unbounded raw value - population
    min-max normalize per topic via normalize_minmax_per_topic() before use in ClaimScore."""
    return (
        weights.impressions * math.log1p(max(impressions_sum, 0))
        + weights.unique_authors * math.log1p(max(unique_authors, 0))
        + weights.content_count * math.log1p(max(content_count, 0))
        + weights.distinct_platforms * (distinct_platforms / TOTAL_MONITORED_PLATFORMS)
    )


def normalize_minmax_per_topic(raw_values: dict[uuid.UUID, float]) -> dict[uuid.UUID, float]:
    """Min-max rescale a set of raw values (e.g. raw R for every EXISTING claim within
    one topic) to [0, 100]. Scoped per-topic, not globally - comparing raw reach across
    topics with very different traffic profiles would be apples-to-oranges. A
    single-claim topic (or all-equal values) maps everything to 50 (neutral midpoint)
    rather than dividing by zero."""
    if not raw_values:
        return {}
    values = list(raw_values.values())
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return dict.fromkeys(raw_values, 50.0)
    return {k: round((v - lo) / (hi - lo) * 100, 4) for k, v in raw_values.items()}


def raw_velocity(volume_t: int, volume_t_minus_delta: int, epsilon: float = 1.0) -> float:
    """V_raw = (Volume_t - Volume_t-delta) / (Volume_t-delta + epsilon)."""
    return (volume_t - volume_t_minus_delta) / (volume_t_minus_delta + epsilon)


def _stable_sigmoid(z: float) -> float:
    """Numerically stable logistic sigmoid - math.exp(-z) / math.exp(z) overflows for
    large |z| in the naive form (a claim's velocity can legitimately spike orders of
    magnitude past a quiet topic's baseline), so branch on the sign of z instead."""
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    exp_z = math.exp(z)
    return exp_z / (1.0 + exp_z)


def velocity_zscore(
    raw_v: float, topic_baseline_mean: float, topic_baseline_std: float
) -> float:
    """Normalizes raw growth rate against the topic's historical baseline as a z-score,
    then squashes it into [0, 100] via a standard logistic curve (z=0 -> 50, unbounded
    z -> approaches 0 or 100) so it's comparable to the other 0-100 parameters."""
    if topic_baseline_std < 1e-9:
        z = 0.0  # no variance in baseline - can't compute a meaningful z-score
    else:
        z = (raw_v - topic_baseline_mean) / topic_baseline_std
    return round(100.0 * _stable_sigmoid(z), 4)


def harm_score(
    public_safety: float, institutional_trust: float, economic: float, policy_disruption: float
) -> float:
    """H = 0.35*PublicSafety + 0.30*InstitutionalTrust + 0.20*Economic + 0.15*PolicyDisruption.
    Each sub-component is assumed already on a 0-100 scale (AI-classified, human-confirmed)."""
    score = (
        HARM_WEIGHT_PUBLIC_SAFETY * public_safety
        + HARM_WEIGHT_INSTITUTIONAL_TRUST * institutional_trust
        + HARM_WEIGHT_ECONOMIC * economic
        + HARM_WEIGHT_POLICY_DISRUPTION * policy_disruption
    )
    return round(min(max(score, 0.0), 100.0), 4)


def emotional_intensity(outrage_word_density_avg: float, negative_reaction_ratio_avg: float) -> float:
    """EI = 0.5*OutrageWordDensity + 0.5*NegativeReactionRatio, both 0-1 ratios,
    scaled to the shared 0-100 parameter range."""
    score = 50.0 * outrage_word_density_avg + 50.0 * negative_reaction_ratio_avg
    return round(min(max(score, 0.0), 100.0), 4)


def claim_score(r: float, v: float, f: float | None, h: float, ei: float) -> float:
    """ClaimScore = 0.15*R + 0.15*V + 0.30*F + 0.30*H + 0.10*EI, each already 0-100.

    If F is None (no confident match against the OfficialSource corpus - never
    fabricated), F's 0.30 weight is dropped and the remaining weights are renormalized
    to sum to 1.0, rather than treating missing F as 0 (which would wrongly assert
    "confirmed true" and systematically depress every score while the corpus is empty).
    """
    if f is None:
        remaining_weight = WEIGHT_R + WEIGHT_V + WEIGHT_H + WEIGHT_EI
        score = (WEIGHT_R * r + WEIGHT_V * v + WEIGHT_H * h + WEIGHT_EI * ei) / remaining_weight
    else:
        score = WEIGHT_R * r + WEIGHT_V * v + WEIGHT_F * f + WEIGHT_H * h + WEIGHT_EI * ei
    return round(min(max(score, 0.0), 100.0), 4)


def compute_npr(supporting_volume: int, opposing_volume: int) -> tuple[float | None, bool]:
    """NPR = OpposingVolume / (SupportingVolume + OpposingVolume) within the rolling
    window. Returns (npr, is_dormant) - if both volumes are 0 in the window, NPR is not
    calculated and the claim is flagged dormant instead of discounted."""
    total = supporting_volume + opposing_volume
    if total == 0:
        return None, True
    return round(opposing_volume / total, 4), False


def discount_factor(npr: float | None, total_volume: int) -> float:
    """DiscountFactor = 1 - (gamma * NPR). Defaults to 1 (no discount) when NPR is None
    (dormant) or total volume is below the reliability threshold (too little data to
    trust the pushback signal)."""
    if npr is None or total_volume < RELIABILITY_THRESHOLD:
        return 1.0
    return round(1.0 - GAMMA * npr, 4)


def final_claim_score(claim_score_value: float, discount: float) -> float:
    """FinalClaimScore = ClaimScore * DiscountFactor - the number actually used for
    D1 dashboard ranking."""
    return round(claim_score_value * discount, 4)
