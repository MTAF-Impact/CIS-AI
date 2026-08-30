import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import ClassificationLabel, ResponseStatus, ResponseType
from app.models.narrative import Narrative
from app.models.response import InterventionResponse
from app.services import rag_service
from app.services.embedding_service import EmbeddingService, get_embedding_service
from app.services.llm_client import LLMClient, get_llm_client


class NarrativeNotFoundError(Exception):
    pass


def _summarize_viral_claim(narrative: Narrative) -> str:
    """Build a neutral description of what's circulating, from the narrative and its
    flagged content items, WITHOUT quoting the false claims verbatim (OpenAI is
    separately instructed not to repeat/amplify them either - this is defense in depth)."""
    flagged_claims = [
        item.extracted_claim
        for item in narrative.content_items
        if item.classification
        in {ClassificationLabel.MISINFORMATION, ClassificationLabel.DISINFORMATION}
        and item.extracted_claim
    ]
    parts = [f"Narrative: {narrative.title}"]
    if narrative.summary:
        parts.append(f"Summary: {narrative.summary}")
    if flagged_claims:
        parts.append("Claims circulating in this narrative: " + "; ".join(flagged_claims[:5]))
    return "\n".join(parts)


async def generate_truth_sandwich_for_narrative(
    db: AsyncSession,
    narrative_id: uuid.UUID,
    llm: LLMClient | None = None,
    embedder: EmbeddingService | None = None,
) -> InterventionResponse:
    llm = llm or get_llm_client()
    embedder = embedder or get_embedding_service()

    stmt = (
        select(Narrative)
        .where(Narrative.id == narrative_id)
        .options(selectinload(Narrative.content_items))
    )
    narrative = (await db.execute(stmt)).scalar_one_or_none()
    if narrative is None:
        raise NarrativeNotFoundError(f"Narrative {narrative_id} not found")

    viral_claim_summary = _summarize_viral_claim(narrative)

    query_text = narrative.summary or narrative.title
    fault_lines = await rag_service.retrieve_relevant_fault_lines(db, query_text, embedder)
    grounding_context = rag_service.build_grounding_context(fault_lines)

    sandwich = await llm.generate_truth_sandwich(viral_claim_summary, grounding_context)

    response = InterventionResponse(
        narrative_id=narrative.id,
        response_type=ResponseType.TRUTH_SANDWICH,
        core_fact=sandwich.core_fact,
        nuanced_flag=sandwich.nuanced_flag,
        reiterated_fact=sandwich.reiterated_fact,
        status=ResponseStatus.DRAFT,
    )
    db.add(response)
    await db.commit()
    await db.refresh(response)
    return response
