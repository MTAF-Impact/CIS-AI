import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.database import get_db
from app.models.content import ContentItem
from app.schemas.content import (
    ContentItemBatchCreate,
    ContentItemBatchResult,
    ContentItemCreate,
    ContentItemRead,
)
from app.services.embedding_service import EmbeddingService, get_embedding_service
from app.services.llm_client import (
    LLMClient,
    LLMNotConfiguredError,
    get_llm_client,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingestion"])


async def _analyze_and_build_item(
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


@router.post("", response_model=ContentItemRead, status_code=201)
async def ingest_content(
    payload: ContentItemCreate,
    db: AsyncSession = Depends(get_db),
    llm: LLMClient = Depends(get_llm_client),
    embedder: EmbeddingService = Depends(get_embedding_service),
) -> ContentItem:
    """Ingest a single piece of content: embed it, classify it via OpenAI, persist it."""
    embedding = await run_in_threadpool(embedder.embed, payload.text)
    try:
        item = await _analyze_and_build_item(payload, llm, embedding)
    except LLMNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


@router.post("/batch", response_model=ContentItemBatchResult, status_code=201)
async def ingest_content_batch(
    payload: ContentItemBatchCreate,
    db: AsyncSession = Depends(get_db),
    llm: LLMClient = Depends(get_llm_client),
    embedder: EmbeddingService = Depends(get_embedding_service),
) -> ContentItemBatchResult:
    """Ingest multiple content items in one call: batch-embed, then classify concurrently."""
    texts = [entry.text for entry in payload.items]
    embeddings = await run_in_threadpool(embedder.embed_batch, texts)

    async def _build(entry: ContentItemCreate, vec: list[float]) -> ContentItem | dict:
        try:
            return await _analyze_and_build_item(entry, llm, vec)
        except Exception as exc:
            logger.exception("Failed to analyze content item during batch ingest")
            return {"text": entry.text, "error": str(exc)}

    results = await asyncio.gather(
        *(_build(entry, vec) for entry, vec in zip(payload.items, embeddings, strict=True))
    )

    created: list[ContentItem] = []
    failed: list[dict] = []
    for result in results:
        if isinstance(result, ContentItem):
            db.add(result)
            created.append(result)
        else:
            failed.append(result)

    if created:
        await db.commit()
        for item in created:
            await db.refresh(item)

    return ContentItemBatchResult(created=created, failed=failed)
