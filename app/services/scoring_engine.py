"""Claim Scoring System (PRD Section 6) - pure math, no I/O. Falseness (F) lives in
falseness_service.py instead, since it needs a DB round-trip."""

import math
import uuid
from dataclasses import dataclass

TOTAL_MONITORED_PLATFORMS = 5  # len(app.models.enums.ContentSource)
RELIABILITY_THRESHOLD = 25  # posts; midpoint of the PRD's recommended 20-30 range
GAMMA = 0.5  # NPR discount cap - even NPR=1 reduces the score by at most 50%
TOPIC_ATTACH_THRESHOLD = 0.5  # cosine similarity for dynamic topic assignment
DEFAULT_VELOCITY_EPSILON = 1.0  # deliberate low-volume damping, not a bare div-by-zero guard
DEFAULT_VELOCITY_ZSCORE_MIN = -3.0
DEFAULT_VELOCITY_ZSCORE_MAX = 3.0

# Every weight/threshold constant below is a fallback default now - the real values
# are read from the backend-owned `cis_settings` table at runtime (see
# app/services/config_service.py, documentation/CIS/AI_DYNAMIC_PARAMETER.md). Kept
# here, not just in config_service, so every function in this file stays a pure,
# independently-testable unit with a sane default when called directly (as the unit
# tests and demo/admin call sites do).


@dataclass(frozen=True)
class ScoreWeights:
    """ClaimScore = reach*R + velocity*V + falseness*F + harm*H + emotional_intensity*EI."""

    reach: float = 0.15
    velocity: float = 0.15
    falseness: float = 0.30
    harm: float = 0.30
    emotional_intensity: float = 0.10


DEFAULT_SCORE_WEIGHTS = ScoreWeights()


@dataclass(frozen=True)
class HarmWeights:
    """PolicyDisruption weighted lowest - avoid scoring policy criticism itself as harm."""

    public_safety: float = 0.35
    institutional_trust: float = 0.30
    economic: float = 0.20
    policy_disruption: float = 0.15


DEFAULT_HARM_WEIGHTS = HarmWeights()


@dataclass(frozen=True)
class ReachWeights:
    """R's per-component weights (w1-w4) - placeholder equal-weighting."""

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
    """Raw Reach - unbounded; normalize via normalize_minmax_per_topic() before use."""
    return (
        weights.impressions * math.log1p(max(impressions_sum, 0))
        + weights.unique_authors * math.log1p(max(unique_authors, 0))
        + weights.content_count * math.log1p(max(content_count, 0))
        + weights.distinct_platforms * (distinct_platforms / TOTAL_MONITORED_PLATFORMS)
    )


def normalize_minmax_per_topic(raw_values: dict[uuid.UUID, float]) -> dict[uuid.UUID, float]:
    """Min-max rescale a set of raw values to [0, 100]; all-equal values map to 50."""
    if not raw_values:
        return {}
    values = list(raw_values.values())
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return dict.fromkeys(raw_values, 50.0)
    return {k: round((v - lo) / (hi - lo) * 100, 4) for k, v in raw_values.items()}


def raw_velocity(
    volume_t: int, volume_t_minus_delta: int, epsilon: float = DEFAULT_VELOCITY_EPSILON
) -> float:
    """V_raw = (Volume_t - Volume_t-delta) / (Volume_t-delta + epsilon)."""
    return (volume_t - volume_t_minus_delta) / (volume_t_minus_delta + epsilon)


def velocity_zscore(
    raw_v: float,
    topic_baseline_mean: float,
    topic_baseline_std: float,
    z_min: float = DEFAULT_VELOCITY_ZSCORE_MIN,
    z_max: float = DEFAULT_VELOCITY_ZSCORE_MAX,
) -> float:
    """Z-score against the topic's baseline, min-max mapped onto [0, 100] against
    [z_min, z_max] and clamped at the extremes (PRD Sec 6.2.2) - z=0 -> 50 only when
    the configured range is symmetric around zero, which is the documented default."""
    if topic_baseline_std < 1e-9:
        z = 0.0  # no variance in baseline - can't compute a meaningful z-score
    else:
        z = (raw_v - topic_baseline_mean) / topic_baseline_std
    z = min(max(z, z_min), z_max)
    return round((z - z_min) / (z_max - z_min) * 100, 4)


def harm_score(
    public_safety: float,
    institutional_trust: float,
    economic: float,
    policy_disruption: float,
    weights: HarmWeights = DEFAULT_HARM_WEIGHTS,
) -> float:
    """H = weights.public_safety*PublicSafety + weights.institutional_trust*InstitutionalTrust
    + weights.economic*Economic + weights.policy_disruption*PolicyDisruption."""
    score = (
        weights.public_safety * public_safety
        + weights.institutional_trust * institutional_trust
        + weights.economic * economic
        + weights.policy_disruption * policy_disruption
    )
    return round(min(max(score, 0.0), 100.0), 4)


def emotional_intensity(outrage_word_density_avg: float, negative_reaction_ratio_avg: float) -> float:
    """EI = (0.5*OutrageWordDensity + 0.5*NegativeReactionRatio) * 100."""
    score = 50.0 * outrage_word_density_avg + 50.0 * negative_reaction_ratio_avg
    return round(min(max(score, 0.0), 100.0), 4)


def claim_score(
    r: float, v: float, f: float | None, h: float, ei: float, weights: ScoreWeights = DEFAULT_SCORE_WEIGHTS
) -> float:
    """ClaimScore = weights.reach*R + weights.velocity*V + weights.falseness*F +
    weights.harm*H + weights.emotional_intensity*EI. Missing F renormalizes the rest
    over (1 - weights.falseness) instead of scoring as 0."""
    if f is None:
        remaining_weight = weights.reach + weights.velocity + weights.harm + weights.emotional_intensity
        score = (
            weights.reach * r + weights.velocity * v + weights.harm * h + weights.emotional_intensity * ei
        ) / remaining_weight
    else:
        score = (
            weights.reach * r
            + weights.velocity * v
            + weights.falseness * f
            + weights.harm * h
            + weights.emotional_intensity * ei
        )
    return round(min(max(score, 0.0), 100.0), 4)


def compute_npr(supporting_volume: int, opposing_volume: int) -> tuple[float | None, bool]:
    """NPR = OpposingVolume / (SupportingVolume + OpposingVolume). Returns (npr, is_dormant)."""
    total = supporting_volume + opposing_volume
    if total == 0:
        return None, True
    return round(opposing_volume / total, 4), False


def discount_factor(
    npr: float | None,
    total_volume: int,
    gamma: float = GAMMA,
    reliability_threshold: int = RELIABILITY_THRESHOLD,
) -> float:
    """DiscountFactor = 1 - (gamma * NPR); defaults to 1 when dormant or below threshold."""
    if npr is None or total_volume < reliability_threshold:
        return 1.0
    return round(1.0 - gamma * npr, 4)


def final_claim_score(claim_score_value: float, discount: float) -> float:
    """FinalClaimScore = ClaimScore * DiscountFactor."""
    return round(claim_score_value * discount, 4)
