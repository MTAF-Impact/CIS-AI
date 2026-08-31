"""Claim Scoring System (PRD Section 6) - pure math, no I/O. Falseness (F) lives in
falseness_service.py instead, since it needs a DB round-trip."""

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

# PolicyDisruption weighted lowest - avoid scoring policy criticism itself as harm.
HARM_WEIGHT_PUBLIC_SAFETY = 0.35
HARM_WEIGHT_INSTITUTIONAL_TRUST = 0.30
HARM_WEIGHT_ECONOMIC = 0.20
HARM_WEIGHT_POLICY_DISRUPTION = 0.15


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


def raw_velocity(volume_t: int, volume_t_minus_delta: int, epsilon: float = 1.0) -> float:
    """V_raw = (Volume_t - Volume_t-delta) / (Volume_t-delta + epsilon)."""
    return (volume_t - volume_t_minus_delta) / (volume_t_minus_delta + epsilon)


def _stable_sigmoid(z: float) -> float:
    """Numerically stable logistic sigmoid - avoids overflow for large |z|."""
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    exp_z = math.exp(z)
    return exp_z / (1.0 + exp_z)


def velocity_zscore(
    raw_v: float, topic_baseline_mean: float, topic_baseline_std: float
) -> float:
    """Z-score against the topic's baseline, squashed to [0, 100] (z=0 -> 50)."""
    if topic_baseline_std < 1e-9:
        z = 0.0  # no variance in baseline - can't compute a meaningful z-score
    else:
        z = (raw_v - topic_baseline_mean) / topic_baseline_std
    return round(100.0 * _stable_sigmoid(z), 4)


def harm_score(
    public_safety: float, institutional_trust: float, economic: float, policy_disruption: float
) -> float:
    """H = 0.35*PublicSafety + 0.30*InstitutionalTrust + 0.20*Economic + 0.15*PolicyDisruption."""
    score = (
        HARM_WEIGHT_PUBLIC_SAFETY * public_safety
        + HARM_WEIGHT_INSTITUTIONAL_TRUST * institutional_trust
        + HARM_WEIGHT_ECONOMIC * economic
        + HARM_WEIGHT_POLICY_DISRUPTION * policy_disruption
    )
    return round(min(max(score, 0.0), 100.0), 4)


def emotional_intensity(outrage_word_density_avg: float, negative_reaction_ratio_avg: float) -> float:
    """EI = (0.5*OutrageWordDensity + 0.5*NegativeReactionRatio) * 100."""
    score = 50.0 * outrage_word_density_avg + 50.0 * negative_reaction_ratio_avg
    return round(min(max(score, 0.0), 100.0), 4)


def claim_score(r: float, v: float, f: float | None, h: float, ei: float) -> float:
    """ClaimScore = 0.15R + 0.15V + 0.30F + 0.30H + 0.10EI. Missing F renormalizes the
    rest instead of scoring as 0."""
    if f is None:
        remaining_weight = WEIGHT_R + WEIGHT_V + WEIGHT_H + WEIGHT_EI
        score = (WEIGHT_R * r + WEIGHT_V * v + WEIGHT_H * h + WEIGHT_EI * ei) / remaining_weight
    else:
        score = WEIGHT_R * r + WEIGHT_V * v + WEIGHT_F * f + WEIGHT_H * h + WEIGHT_EI * ei
    return round(min(max(score, 0.0), 100.0), 4)


def compute_npr(supporting_volume: int, opposing_volume: int) -> tuple[float | None, bool]:
    """NPR = OpposingVolume / (SupportingVolume + OpposingVolume). Returns (npr, is_dormant)."""
    total = supporting_volume + opposing_volume
    if total == 0:
        return None, True
    return round(opposing_volume / total, 4), False


def discount_factor(npr: float | None, total_volume: int) -> float:
    """DiscountFactor = 1 - (gamma * NPR); defaults to 1 when dormant or below threshold."""
    if npr is None or total_volume < RELIABILITY_THRESHOLD:
        return 1.0
    return round(1.0 - GAMMA * npr, 4)


def final_claim_score(claim_score_value: float, discount: float) -> float:
    """FinalClaimScore = ClaimScore * DiscountFactor."""
    return round(claim_score_value * discount, 4)
