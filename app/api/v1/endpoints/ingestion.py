import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from app.core.database import get_db, get_session_factory
from app.core.security import verify_backend_api_key
from app.models.content import ContentItem
from app.schemas.content import (
    ContentItemBatchCreate,
    ContentItemBatchResult,
    ContentItemCreate,
    ContentItemRead,
    SyntheticIngestRequest,
    SyntheticIngestResult,
)
from app.services.clustering_service import (
    cluster_unclustered_content,
    cluster_unclustered_content_task,
)
from app.services.content_ingestion_service import (
    analyze_and_build_item,
    analyze_content_item,
    build_content_item,
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


async def _analyze_and_build_batch(
    entries: list[ContentItemCreate], llm: LLMClient, embedder: EmbeddingService
) -> tuple[list[ContentItem], list[dict]]:
    """Analyze each entry concurrently, then embed all translations in one batched call."""

    async def _analyze(entry: ContentItemCreate):
        try:
            return await analyze_content_item(entry, llm)
        except Exception as exc:
            logger.exception("Failed to analyze content item")
            return {"text": entry.text, "error": str(exc)}

    analyses = await asyncio.gather(*(_analyze(entry) for entry in entries))

    ok_pairs = [(e, a) for e, a in zip(entries, analyses, strict=True) if not isinstance(a, dict)]
    failed = [a for a in analyses if isinstance(a, dict)]
    if not ok_pairs:
        return [], failed

    texts_en = [analysis.text_en or entry.text for entry, analysis in ok_pairs]
    embeddings = await run_in_threadpool(embedder.embed_batch, texts_en)

    created = [
        build_content_item(entry, analysis, embedding)
        for (entry, analysis), embedding in zip(ok_pairs, embeddings, strict=True)
    ]
    return created, failed


@router.post(
    "",
    response_model=ContentItemRead,
    status_code=201,
    dependencies=[Depends(verify_backend_api_key)],
)
async def ingest_content(
    payload: ContentItemCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    llm: LLMClient = Depends(get_llm_client),
    embedder: EmbeddingService = Depends(get_embedding_service),
    session_factory: async_sessionmaker = Depends(get_session_factory),
) -> ContentItem:
    """Analyzes, translates, embeds, and persists a content item; idempotent on external_ref."""
    if payload.external_ref:
        existing = (
            await db.execute(
                select(ContentItem).where(ContentItem.external_ref == payload.external_ref)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    try:
        item = await analyze_and_build_item(payload, llm, embedder)
    except LLMNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    db.add(item)
    await db.commit()
    await db.refresh(item)
    background_tasks.add_task(
        cluster_unclustered_content_task, llm=llm, embedder=embedder, session_factory=session_factory
    )
    return item


@router.post(
    "/batch",
    response_model=ContentItemBatchResult,
    status_code=201,
    dependencies=[Depends(verify_backend_api_key)],
)
async def ingest_content_batch(
    payload: ContentItemBatchCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    llm: LLMClient = Depends(get_llm_client),
    embedder: EmbeddingService = Depends(get_embedding_service),
    session_factory: async_sessionmaker = Depends(get_session_factory),
) -> ContentItemBatchResult:
    """Skips already-seen external_refs before spending any LLM call, then
    batch-analyzes/embeds the rest. Auto-triggers clustering afterward."""
    refs = [entry.external_ref for entry in payload.items if entry.external_ref]
    existing_refs: set[str] = set()
    if refs:
        existing_refs = set(
            (
                await db.execute(
                    select(ContentItem.external_ref).where(ContentItem.external_ref.in_(refs))
                )
            )
            .scalars()
            .all()
        )

    skipped = [e.external_ref for e in payload.items if e.external_ref in existing_refs]
    to_process = [e for e in payload.items if e.external_ref not in existing_refs]

    created, failed = await _analyze_and_build_batch(to_process, llm, embedder)

    if created:
        db.add_all(created)
        await db.commit()
        for item in created:
            await db.refresh(item)
        background_tasks.add_task(
            cluster_unclustered_content_task, llm=llm, embedder=embedder, session_factory=session_factory
        )

    return ContentItemBatchResult(created=created, failed=failed, skipped=skipped)


@router.post("/generate-synthetic", response_model=SyntheticIngestResult, status_code=201)
async def generate_synthetic_ingest(
    payload: SyntheticIngestRequest,
    db: AsyncSession = Depends(get_db),
    llm: LLMClient = Depends(get_llm_client),
    embedder: EmbeddingService = Depends(get_embedding_service),
) -> SyntheticIngestResult:
    """Prototype stand-in for the live crawler - fabricates posts via the LLM and runs
    them through the normal ingest pipeline."""
    grounding_context = await build_grounding_context(db)
    try:
        synthetic_posts = await llm.generate_synthetic_posts(
            payload.count, payload.topic_hint, grounding_context
        )
    except LLMNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not synthetic_posts:
        raise HTTPException(status_code=502, detail="LLM returned no synthetic posts")

    entries = [
        ContentItemCreate(
            text=post.text, source=post.source, author_id=post.author_id, location=post.location
        )
        for post in synthetic_posts
    ]
    created, failed = await _analyze_and_build_batch(entries, llm, embedder)

    if created:
        db.add_all(created)
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
