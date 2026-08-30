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
    SyntheticIngestRequest,
    SyntheticIngestResult,
)
from app.services.clustering_service import cluster_unclustered_content
from app.services.content_ingestion_service import (
    analyze_and_build_item,
    build_grounding_context,
)
from app.services.embedding_service import EmbeddingService, get_embedding_service
from app.services.llm_client import (
    LLMClient,
    LLMNotConfiguredError,
    get_llm_client,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingestion"])


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
        item = await analyze_and_build_item(payload, llm, embedding)
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
            return await analyze_and_build_item(entry, llm, vec)
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


@router.post("/generate-synthetic", response_model=SyntheticIngestResult, status_code=201)
async def generate_synthetic_ingest(
    payload: SyntheticIngestRequest,
    db: AsyncSession = Depends(get_db),
    llm: LLMClient = Depends(get_llm_client),
    embedder: EmbeddingService = Depends(get_embedding_service),
) -> SyntheticIngestResult:
    """Prototype stand-in for the live crawler: since a scheduler-driven crawl isn't
    wired up yet, this fabricates realistic Jakarta posts via the LLM and runs them
    through the exact same embed + analyze + persist pipeline real ingested content
    would go through. Meant to be triggered on demand (e.g. a "Generate sample data"
    button in the FE), not a replacement for real ingestion once crawling exists."""
    grounding_context = await build_grounding_context(db)
    try:
        synthetic_posts = await llm.generate_synthetic_posts(
            payload.count, payload.topic_hint, grounding_context
        )
    except LLMNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not synthetic_posts:
        raise HTTPException(status_code=502, detail="LLM returned no synthetic posts")

    embeddings = await run_in_threadpool(
        embedder.embed_batch, [post.text for post in synthetic_posts]
    )

    async def _build(post, vec: list[float]) -> ContentItem | dict:
        entry = ContentItemCreate(
            text=post.text, source=post.source, author_id=post.author_id, location=post.location
        )
        try:
            return await analyze_and_build_item(entry, llm, vec)
        except Exception as exc:
            logger.exception("Failed to analyze synthetic content item")
            return {"text": post.text, "error": str(exc)}

    results = await asyncio.gather(
        *(_build(post, vec) for post, vec in zip(synthetic_posts, embeddings, strict=True))
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

    cluster_result = None
    if payload.auto_cluster and created:
        cluster_result = await cluster_unclustered_content(db, llm=llm, embedder=embedder)

    return SyntheticIngestResult(
        generated=created,
        failed=failed,
        claims_created=cluster_result.claims_created if cluster_result else None,
        claims_updated=cluster_result.claims_updated if cluster_result else None,
        content_items_clustered=cluster_result.content_items_clustered if cluster_result else None,
    )
