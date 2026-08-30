"""Falseness (F) scoring - PRD v1.1 Section 5.2.3. Kept as a separate module from
rag_service.py on purpose: RAG grounding (retrieve_relevant_fault_lines) is soft/
best-effort context-building for LLM prompts; F-matching here is hard-thresholded and
must NEVER fabricate a value - if there's no confident match, F stays None, and
scoring_engine.claim_score() renormalizes the remaining weights instead of treating
missing F as 0."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.official_source import OfficialSource

DEFAULT_MATCH_THRESHOLD = 0.55


async def compute_falseness_score(
    db: AsyncSession,
    claim_embedding: list[float],
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> float | None:
    """F = SimilarityToKnownDebunk * 100, where SimilarityToKnownDebunk is the top
    cosine-similarity match between the claim's embedding and the OfficialSource
    corpus. Returns None if the corpus is empty (it starts empty - see
    scripts/load_official_sources.py) or no match clears the threshold."""
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

    # pgvector's cosine_distance = 1 - cosine_similarity.
    similarity = 1.0 - row.distance
    if similarity < threshold:
        return None

    return round(min(max(similarity, 0.0), 1.0) * 100, 4)
