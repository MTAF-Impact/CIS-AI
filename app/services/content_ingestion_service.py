"""Shared building blocks for turning a raw post (real or LLM-fabricated) into a
persisted ContentItem, and for building light thematic grounding context for LLM
generation prompts. Used by the real ingestion endpoints, the synthetic-data generator,
and the F4 "Generate Generic Claim" demo utility."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content import ContentItem
from app.models.fault_line import FaultLine
from app.models.topic import Topic
from app.schemas.content import ContentItemCreate
from app.services.llm_client import LLMClient

# Caps how much existing fault-line/topic context is fed back into generation prompts -
# just enough for thematic continuity, not a full corpus dump.
GROUNDING_FAULT_LINE_LIMIT = 5
GROUNDING_TOPIC_LIMIT = 10


async def build_grounding_context(db: AsyncSession) -> str:
    fault_lines = (
        await db.execute(
            select(FaultLine.community_name, FaultLine.grievance_theme).limit(
                GROUNDING_FAULT_LINE_LIMIT
            )
        )
    ).all()
    topics = (
        await db.execute(select(Topic.name).limit(GROUNDING_TOPIC_LIMIT))
    ).scalars().all()

    parts = []
    if fault_lines:
        lines = "\n".join(f"- {name}: {theme}" for name, theme in fault_lines)
        parts.append(f"Known community fault lines:\n{lines}")
    if topics:
        parts.append(f"Active topics: {', '.join(topics)}")
    return "\n\n".join(parts)


async def analyze_and_build_item(
    payload: ContentItemCreate,
    llm: LLMClient,
    embedding: list[float],
) -> ContentItem:
    analysis = await llm.analyze_content(payload.text)
    return ContentItem(
        text=payload.text,
        source=payload.source,
        author_id=payload.author_id,
        location=payload.location,
        outrage_score=analysis.outrage_score,
        moral_foundation=analysis.moral_foundation,
        extracted_claim=analysis.extracted_claim,
        underlying_grievance=analysis.underlying_grievance,
        impressions=payload.impressions,
        positive_reaction_count=payload.positive_reaction_count,
        negative_reaction_count=payload.negative_reaction_count,
        embedding=embedding,
    )
