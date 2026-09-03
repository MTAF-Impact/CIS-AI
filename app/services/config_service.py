"""Dynamic scoring/matchmaking parameters, read from the backend-owned `cis_settings`
table. This service is SELECT-only - it must never write that table.

A missing row or table is not an error - every key falls back to the documented
default below."""

import logging
import time
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.falseness_service import (
    DEFAULT_MATCH_THRESHOLD as _DEFAULT_FALSENESS_THRESHOLD,
)
from app.services.falseness_service import (
    LIVE_FACT_CHECK_MATCH_SCORE as _DEFAULT_LIVE_FACT_CHECK_MATCH_SCORE,
)
from app.services.scoring_engine import (
    DEFAULT_HARM_WEIGHTS,
    DEFAULT_REACH_WEIGHTS,
    DEFAULT_SCORE_WEIGHTS,
    DEFAULT_VELOCITY_EPSILON,
    DEFAULT_VELOCITY_ZSCORE_MAX,
    DEFAULT_VELOCITY_ZSCORE_MIN,
    HarmWeights,
    ReachWeights,
    ScoreWeights,
)
from app.services.scoring_engine import GAMMA as _DEFAULT_GAMMA
from app.services.scoring_engine import (
    RELIABILITY_THRESHOLD as _DEFAULT_RELIABILITY_THRESHOLD,
)
from app.services.scoring_engine import (
    TOPIC_ATTACH_THRESHOLD as _DEFAULT_TOPIC_ATTACH_THRESHOLD,
)

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 30.0  # matches the backend's own SETTINGS_CACHE_TTL

# Policy Disruption may never weigh more than a quarter of Harm. The backend
# enforces this on write; clamped here too as defense in depth.
POLICY_DISRUPTION_WEIGHT_CEILING = 0.25

DEFAULTS: dict[str, str] = {
    "scoring.weight_reach": str(DEFAULT_SCORE_WEIGHTS.reach),
    "scoring.weight_velocity": str(DEFAULT_SCORE_WEIGHTS.velocity),
    "scoring.weight_falseness": str(DEFAULT_SCORE_WEIGHTS.falseness),
    "scoring.weight_harm": str(DEFAULT_SCORE_WEIGHTS.harm),
    "scoring.weight_emotional_intensity": str(DEFAULT_SCORE_WEIGHTS.emotional_intensity),
    "scoring.harm_weight_public_safety": str(DEFAULT_HARM_WEIGHTS.public_safety),
    "scoring.harm_weight_institutional_trust": str(DEFAULT_HARM_WEIGHTS.institutional_trust),
    "scoring.harm_weight_economic": str(DEFAULT_HARM_WEIGHTS.economic),
    "scoring.harm_weight_policy_disruption": str(DEFAULT_HARM_WEIGHTS.policy_disruption),
    "scoring.reach_weight_impressions": str(DEFAULT_REACH_WEIGHTS.impressions),
    "scoring.reach_weight_unique_authors": str(DEFAULT_REACH_WEIGHTS.unique_authors),
    "scoring.reach_weight_content_count": str(DEFAULT_REACH_WEIGHTS.content_count),
    "scoring.reach_weight_platform_spread": str(DEFAULT_REACH_WEIGHTS.distinct_platforms),
    "scoring.reach_normalization_window_days": "90",
    "scoring.velocity_interval_hours": "24",
    "scoring.npr_window_hours": "24",
    "scoring.velocity_zscore_min": str(DEFAULT_VELOCITY_ZSCORE_MIN),
    "scoring.velocity_zscore_max": str(DEFAULT_VELOCITY_ZSCORE_MAX),
    "scoring.velocity_epsilon": str(DEFAULT_VELOCITY_EPSILON),
    "scoring.discount_gamma": str(_DEFAULT_GAMMA),
    "scoring.npr_reliability_minimum_posts": str(_DEFAULT_RELIABILITY_THRESHOLD),
    "scoring.falseness_match_threshold": str(_DEFAULT_FALSENESS_THRESHOLD),
    "scoring.falseness_live_match_score": str(_DEFAULT_LIVE_FACT_CHECK_MATCH_SCORE),
    "clustering.claim_attach_threshold": "0.55",
    "clustering.topic_attach_threshold": str(_DEFAULT_TOPIC_ATTACH_THRESHOLD),
    "matchmaking.claim_prefilter_threshold": "0.35",
    "ai.debunk_segment_max_count": "3",
}


@dataclass(frozen=True)
class RuntimeConfig:
    """One immutable snapshot of every AI-owned cis_settings value, already cast to
    its real type. Read once per pass and threaded down explicitly - never re-read
    mid-pass, so a settings edit can't split one claim's scoring across two configs."""

    score_weights: ScoreWeights
    harm_weights: HarmWeights
    reach_weights: ReachWeights
    reach_normalization_window_days: int
    velocity_interval_hours: float
    npr_window_hours: float
    velocity_zscore_min: float
    velocity_zscore_max: float
    velocity_epsilon: float
    discount_gamma: float
    npr_reliability_minimum_posts: int
    falseness_match_threshold: float
    falseness_live_match_score: float
    claim_attach_threshold: float
    topic_attach_threshold: float
    claim_prefilter_threshold: float
    debunk_segment_max_count: int


def _build(raw: dict[str, str]) -> RuntimeConfig:
    def get(key: str) -> str:
        return raw.get(key) or DEFAULTS[key]

    def as_float(key: str) -> float:
        return float(get(key))

    def as_int(key: str) -> int:
        return int(float(get(key)))

    score_weights = ScoreWeights(
        reach=as_float("scoring.weight_reach"),
        velocity=as_float("scoring.weight_velocity"),
        falseness=as_float("scoring.weight_falseness"),
        harm=as_float("scoring.weight_harm"),
        emotional_intensity=as_float("scoring.weight_emotional_intensity"),
    )
    weights_total = (
        score_weights.reach
        + score_weights.velocity
        + score_weights.falseness
        + score_weights.harm
        + score_weights.emotional_intensity
    )
    if abs(weights_total - 1.0) > 1e-6:
        # Logged rather than raised - a bad row here shouldn't take scoring down.
        logger.warning("cis_settings score weights sum to %.4f, not 1.00 - using them anyway", weights_total)

    harm_weights = HarmWeights(
        public_safety=as_float("scoring.harm_weight_public_safety"),
        institutional_trust=as_float("scoring.harm_weight_institutional_trust"),
        economic=as_float("scoring.harm_weight_economic"),
        policy_disruption=min(
            as_float("scoring.harm_weight_policy_disruption"), POLICY_DISRUPTION_WEIGHT_CEILING
        ),
    )

    return RuntimeConfig(
        score_weights=score_weights,
        harm_weights=harm_weights,
        reach_weights=ReachWeights(
            impressions=as_float("scoring.reach_weight_impressions"),
            unique_authors=as_float("scoring.reach_weight_unique_authors"),
            content_count=as_float("scoring.reach_weight_content_count"),
            distinct_platforms=as_float("scoring.reach_weight_platform_spread"),
        ),
        reach_normalization_window_days=as_int("scoring.reach_normalization_window_days"),
        velocity_interval_hours=as_float("scoring.velocity_interval_hours"),
        npr_window_hours=as_float("scoring.npr_window_hours"),
        velocity_zscore_min=as_float("scoring.velocity_zscore_min"),
        velocity_zscore_max=as_float("scoring.velocity_zscore_max"),
        velocity_epsilon=as_float("scoring.velocity_epsilon"),
        discount_gamma=as_float("scoring.discount_gamma"),
        npr_reliability_minimum_posts=as_int("scoring.npr_reliability_minimum_posts"),
        falseness_match_threshold=as_float("scoring.falseness_match_threshold"),
        falseness_live_match_score=as_float("scoring.falseness_live_match_score"),
        claim_attach_threshold=as_float("clustering.claim_attach_threshold"),
        topic_attach_threshold=as_float("clustering.topic_attach_threshold"),
        claim_prefilter_threshold=as_float("matchmaking.claim_prefilter_threshold"),
        debunk_segment_max_count=as_int("ai.debunk_segment_max_count"),
    )


DEFAULT_CONFIG = _build({})

_cache: RuntimeConfig | None = None
_cache_loaded_at: float = 0.0


async def load_config(db: AsyncSession) -> RuntimeConfig:
    """Always hits the DB - bypasses the cache. Most callers want get_config()
    instead; this is the cache's own refill path."""
    try:
        # SAVEPOINT, not a plain db.rollback(): the caller's session often has real
        # uncommitted work already flushed (e.g. a just-created Claim), and a bare
        # rollback() on failure would discard that too, not just this SELECT.
        async with db.begin_nested():
            rows = (await db.execute(text("SELECT key, value FROM cis_settings"))).all()
    except DBAPIError:
        logger.warning(
            "cis_settings not readable (table may not exist yet) - using built-in defaults",
            exc_info=True,
        )
        return DEFAULT_CONFIG
    return _build({key: value for key, value in rows})


async def get_config(db: AsyncSession) -> RuntimeConfig:
    """Cached with a 30s TTL. Call once per request/background-task entry point and
    thread the result down explicitly."""
    global _cache, _cache_loaded_at
    now = time.monotonic()
    if _cache is None or (now - _cache_loaded_at) >= CACHE_TTL_SECONDS:
        _cache = await load_config(db)
        _cache_loaded_at = now
    return _cache
