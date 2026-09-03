"""Falseness (F) scoring - hard-thresholded pgvector match, never fabricates a value.

Two independent paths, tried in order:
1. Cosine-similarity match against OfficialSource (seeded from TurnBackHoax.id via
   scripts/seed_debunk_corpus.py - a known-hoax corpus).
2. If that doesn't clear threshold, a live Google Fact Check Tools API query on the
   claim's own text - skipped silently whenever settings.GOOGLE_API_KEY is unset
   or the API returns nothing usable."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.official_source import OfficialSource
from app.services import fact_check_client

DEFAULT_MATCH_THRESHOLD = 0.55
LIVE_FACT_CHECK_MATCH_SCORE = 75.0  # no continuous similarity score from this path - a
# matched ClaimReview with a false-reading textualRating is a real verified verdict,
# not a modelled guess, so it's scored fixed-high rather than left at a fake precision.


async def compute_falseness_score(
    db: AsyncSession,
    claim_embedding: list[float],
    claim_statement: str | None = None,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    live_match_score: float = LIVE_FACT_CHECK_MATCH_SCORE,
) -> float | None:
    """F = top cosine-similarity match against OfficialSource * 100, or (when that
    misses and claim_statement is given) a live Google Fact Check API match. None if
    neither path finds anything - never fabricated."""
    stmt = (
        select(
            OfficialSource.id,
            OfficialSource.embedding.cosine_distance(claim_embedding).label("distance"),
        )
        .where(OfficialSource.embedding.is_not(None))
        .order_by("distance")
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    if row is not None:
        similarity = 1.0 - row.distance  # pgvector distance = 1 - similarity
        if similarity >= threshold:
            return round(min(max(similarity, 0.0), 1.0) * 100, 4)

    if claim_statement:
        claims = await fact_check_client.search_fact_checks(claim_statement)
        if fact_check_client.has_false_rating(claims):
            return live_match_score

    return None
