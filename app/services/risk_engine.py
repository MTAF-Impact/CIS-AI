from collections import Counter
from datetime import UTC, datetime, timedelta

import numpy as np

from app.models.enums import RiskLevel

# Risk = (0.35 * Growth) + (0.25 * Outrage) + (0.15 * Geo) + (0.25 * FaultLineRelevance)
GROWTH_WEIGHT = 0.35
EMOTIONAL_WEIGHT = 0.25
GEO_WEIGHT = 0.15
FAULT_LINE_WEIGHT = 0.25

RISK_LOW_MAX = 0.4
RISK_MEDIUM_MAX = 0.7

GROWTH_WINDOW_HOURS = 6.0
FAULT_LINE_MATCH_THRESHOLD = 0.45


def calculate_risk_score(
    growth_velocity: float,
    emotional_intensity: float,
    geographic_concentration: float,
    fault_line_relevance: float,
) -> float:
    """Deterministic weighted risk score in [0.0, 1.0]."""
    score = (
        GROWTH_WEIGHT * growth_velocity
        + EMOTIONAL_WEIGHT * emotional_intensity
        + GEO_WEIGHT * geographic_concentration
        + FAULT_LINE_WEIGHT * fault_line_relevance
    )
    return round(min(max(score, 0.0), 1.0), 4)


def determine_risk_level(risk_score: float) -> RiskLevel:
    if risk_score > RISK_MEDIUM_MAX:
        return RiskLevel.HIGH
    if risk_score >= RISK_LOW_MAX:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def compute_growth_velocity(
    timestamps: list[datetime],
    window_hours: float = GROWTH_WINDOW_HOURS,
    now: datetime | None = None,
) -> float:
    """Fraction of a narrative's volume that arrived within the recent window - a bounded,
    explainable proxy for how fast the narrative is currently growing."""
    if not timestamps:
        return 0.0
    reference = now or max(timestamps)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    cutoff = reference - timedelta(hours=window_hours)
    total = len(timestamps)
    recent = sum(
        1
        for t in timestamps
        if (t if t.tzinfo else t.replace(tzinfo=UTC)) >= cutoff
    )
    return round(min(max(recent / total, 0.0), 1.0), 4)


def compute_emotional_intensity(outrage_scores: list[float | None]) -> float:
    """Average outrage score across a narrative's content items."""
    valid = [s for s in outrage_scores if s is not None]
    if not valid:
        return 0.0
    return round(min(max(sum(valid) / len(valid), 0.0), 1.0), 4)


def compute_geographic_concentration(locations: list[str | None]) -> float:
    """Fraction of geo-tagged posts concentrated in the single most common location."""
    valid = [loc for loc in locations if loc]
    if not valid:
        return 0.0
    counts = Counter(valid)
    top_count = counts.most_common(1)[0][1]
    return round(min(max(top_count / len(valid), 0.0), 1.0), 4)


def compute_fault_line_relevance(
    centroid: np.ndarray, fault_line_embeddings: list[tuple[str, list[float]]]
) -> tuple[float, list[str]]:
    """Cosine similarity (embeddings are unit-normalized, so dot product suffices) between a
    narrative's centroid and each known fault line. Returns (best_score, matched_fault_line_ids)
    for every fault line above the match threshold."""
    if not fault_line_embeddings:
        return 0.0, []

    scored: list[tuple[float, str]] = []
    for fault_line_id, embedding in fault_line_embeddings:
        if embedding is None:
            continue
        similarity = float(np.dot(centroid, np.asarray(embedding)))
        scored.append((similarity, fault_line_id))

    if not scored:
        return 0.0, []

    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score = round(min(max(scored[0][0], 0.0), 1.0), 4)
    matched_ids = [fid for score, fid in scored if score >= FAULT_LINE_MATCH_THRESHOLD]
    return best_score, matched_ids
