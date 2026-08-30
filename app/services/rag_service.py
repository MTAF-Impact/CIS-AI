from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fault_line import FaultLine
from app.services.embedding_service import EmbeddingService, get_embedding_service

DEFAULT_TOP_K = 3


async def retrieve_relevant_fault_lines(
    db: AsyncSession,
    query_text: str,
    embedder: EmbeddingService | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> list[FaultLine]:
    """Vector similarity search over known community fault lines, used to ground
    Prebunk predictions and Truth Sandwich corrections in real local context."""
    embedder = embedder or get_embedding_service()
    query_embedding = embedder.embed(query_text)

    stmt = (
        select(FaultLine)
        .where(FaultLine.embedding.is_not(None))
        .order_by(FaultLine.embedding.cosine_distance(query_embedding))
        .limit(top_k)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


def build_grounding_context(fault_lines: list[FaultLine], extra_notes: str | None = None) -> str:
    """Render retrieved fault lines (and any extra grounding text) into a plain-text
    context block suitable for a OpenAI prompt."""
    lines: list[str] = []
    for fl in fault_lines:
        entry = f"- [{fl.community_name}] {fl.grievance_theme}"
        if fl.description:
            entry += f": {fl.description}"
        lines.append(entry)

    if extra_notes:
        lines.append(f"- [Additional context] {extra_notes}")

    return "\n".join(lines) if lines else ""
