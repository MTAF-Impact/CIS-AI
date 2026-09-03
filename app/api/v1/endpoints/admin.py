import os
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1.endpoints.claims import _fetch_claim_with_relations, _to_existing_detail
from app.core.database import get_db, get_session_factory
from app.models.enums import DetectionRunStatus
from app.schemas.admin import (
    AdminSettingRead,
    AdminSettingUpdate,
    GenerateCoordinatedNetworkResponse,
    GenerateGenericClaimResponse,
)
from app.services import admin_service
from app.services.coordination import demo_seed, pipeline
from app.services.embedding_service import EmbeddingService, get_embedding_service
from app.services.llm_client import LLMClient, LLMNotConfiguredError, get_llm_client

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/settings", response_model=AdminSettingRead)
async def get_settings(db: AsyncSession = Depends(get_db)) -> AdminSettingRead:
    settings = await admin_service.get_settings(db)
    return AdminSettingRead(over_threshold=settings.over_threshold)


@router.put("/settings", response_model=AdminSettingRead)
async def update_settings(
    payload: AdminSettingUpdate, db: AsyncSession = Depends(get_db)
) -> AdminSettingRead:
    """The single global threshold governing every claim's Over/Under Threshold status."""
    settings = await admin_service.set_threshold(db, payload.over_threshold)
    return AdminSettingRead(over_threshold=settings.over_threshold)


@router.post("/generate-generic-claim", response_model=GenerateGenericClaimResponse, status_code=201)
async def generate_generic_claim(
    topic_hint: str | None = None,
    db: AsyncSession = Depends(get_db),
    llm: LLMClient = Depends(get_llm_client),
    embedder: EmbeddingService = Depends(get_embedding_service),
) -> GenerateGenericClaimResponse:
    """One-click sample Existing/Generic claim for demo/testing, fully scored."""
    try:
        claim = await admin_service.generate_demo_existing_claim(db, llm, embedder, topic_hint)
    except LLMNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    claim = await _fetch_claim_with_relations(db, claim.id)
    return GenerateGenericClaimResponse(claim=await _to_existing_detail(db, claim))


@router.post(
    "/generate-coordinated-network",
    response_model=GenerateCoordinatedNetworkResponse,
    status_code=202,
)
async def generate_coordinated_network(
    background_tasks: BackgroundTasks,
    claim_id: uuid.UUID | None = None,
    topic_hint: str | None = None,
    db: AsyncSession = Depends(get_db),
    llm: LLMClient = Depends(get_llm_client),
    embedder: EmbeddingService = Depends(get_embedding_service),
    session_factory: async_sessionmaker = Depends(get_session_factory),
) -> GenerateCoordinatedNetworkResponse:
    """Demo/testing utility: synthesizes a coordinated-looking content burst (new
    demo claim if claim_id is omitted, else attached to an existing Existing claim)
    and runs it through the real detection pipeline. The detection_run row is
    written synchronously (status=pending) before this returns, so a caller can
    poll it immediately while detection finishes in the background."""
    try:
        claim, run, run_kwargs = await demo_seed.generate_demo_coordinated_network(
            db, llm, embedder, claim_id, topic_hint
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LLMNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    background_tasks.add_task(
        pipeline.run_detection, run_id=run.id, session_factory=session_factory, llm=llm, **run_kwargs
    )
    status = run.status.value if isinstance(run.status, DetectionRunStatus) else run.status
    return GenerateCoordinatedNetworkResponse(run_id=run.id, status=status, claim_id=claim.id)


@router.post("/run-crawler", status_code=202)
async def run_crawler(background_tasks: BackgroundTasks) -> dict:
    """Runs crawler/main.py in-process instead of as a separate Cloud Run Job -
    this service and the crawler ship as one deployment/one container. Pointed at
    this same container's own port via localhost, since Cloud Run's $PORT isn't
    known until runtime.

    Overrides AI_SERVICE_URL unconditionally: self-loopback is correct 100% of the
    time this route runs, so a stale AI_SERVICE_URL from some other config must
    never win here."""
    os.environ["AI_SERVICE_URL"] = f"http://localhost:{os.environ.get('PORT', '8000')}"
    # Local import - crawler/'s deps are only needed by this one route.
    from crawler.config import get_settings as get_crawler_settings
    from crawler.main import run as crawler_run

    if not get_crawler_settings().GOOGLE_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="GOOGLE_API_KEY is required (YouTube Data API v3) - not set.",
        )

    background_tasks.add_task(crawler_run, dry_run=False)
    return {"status": "started", "detail": "Running in the background - check Cloud Logging."}
