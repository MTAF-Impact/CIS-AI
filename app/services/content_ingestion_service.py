"""Shared helpers: build a ContentItem from a raw post, and light grounding context."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.models.content import ContentItem
from app.models.fault_line import FaultLine
from app.models.topic import Topic
from app.schemas.analysis import ContentAnalysisSchema
from app.schemas.content import ContentItemCreate
from app.services.embedding_service import EmbeddingService
from app.services.llm_client import LLMClient

# Caps how much context feeds back into generation prompts.
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


async def analyze_content_item(payload: ContentItemCreate, llm: LLMClient) -> ContentAnalysisSchema:
    """LLM analysis only - call embed separately (single or batched) before
    build_content_item, using analysis.text_en as the embedding input."""
    return await llm.analyze_content(payload.text)


def build_content_item(
    payload: ContentItemCreate, analysis: ContentAnalysisSchema, embedding: list[float]
) -> ContentItem:
    return ContentItem(
        text=payload.text,
        text_en=analysis.text_en,
        source=payload.source,
        author_id=payload.author_id,
        location=payload.location,
        outrage_score=analysis.outrage_score,
        moral_foundation=analysis.moral_foundation,
        extracted_claim=analysis.extracted_claim,
        underlying_grievance=analysis.underlying_grievance,
        sentiment=analysis.sentiment,
        impressions=payload.impressions,
        positive_reaction_count=payload.positive_reaction_count,
        negative_reaction_count=payload.negative_reaction_count,
        external_ref=payload.external_ref,
        embedding=embedding,
    )


async def analyze_and_build_item(
    payload: ContentItemCreate, llm: LLMClient, embedder: EmbeddingService
) -> ContentItem:
    """Single-item convenience: analyze (getting text_en), then embed the translation."""
    analysis = await analyze_content_item(payload, llm)
    embedding = await run_in_threadpool(embedder.embed, analysis.text_en or payload.text)
    return build_content_item(payload, analysis, embedding)
