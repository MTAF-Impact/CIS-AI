"""Falseness (F) scoring - hard-thresholded pgvector match, never fabricates a value."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.official_source import OfficialSource

DEFAULT_MATCH_THRESHOLD = 0.55


async def compute_falseness_score(
    db: AsyncSession,
    claim_embedding: list[float],
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> float | None:
    """F = top cosine-similarity match against OfficialSource * 100, or None if no match."""
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
    if row is None:
        return None

    similarity = 1.0 - row.distance  # pgvector distance = 1 - similarity
    if similarity < threshold:
        return None

    return round(min(max(similarity, 0.0), 1.0) * 100, 4)
