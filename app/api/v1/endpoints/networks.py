"""F5 (PRD Section 10) - Coordinated-Network Detector API surface.

Per the backend integration doc's ownership split, the AI service exposes exactly one
F5 endpoint: a run-trigger. Network list/detail, review, account annex, allowlist,
CSV export, PDF/ZIP reports, and F4 config all moved to the backend, which reads the
AI's 9 pipeline-output tables directly (same pattern already used for claims/policies/
etc). See docs/COORDINATION.md.
"""

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.database import get_session_factory
from app.core.security import verify_backend_api_key
from app.schemas.coordination_network import (
    DetectionRunTriggerRequest,
    DetectionRunTriggerResponse,
)
from app.services.coordination.pipeline import trigger_detection_run
from app.services.multilingual_embedding_service import (
    MultilingualEmbeddingService,
    get_multilingual_embedding_service,
)

coordination_router = APIRouter(prefix="/coordination", tags=["coordination"])


@coordination_router.post(
    "/detection-runs",
    response_model=DetectionRunTriggerResponse,
    status_code=202,
    dependencies=[Depends(verify_backend_api_key)],
)
async def trigger_detection(
    payload: DetectionRunTriggerRequest,
    background_tasks: BackgroundTasks,
    embedder: MultilingualEmbeddingService = Depends(get_multilingual_embedding_service),
    session_factory: async_sessionmaker = Depends(get_session_factory),
) -> DetectionRunTriggerResponse:
    """`claim_id` set -> single-claim run (covers what used to be the on-demand and
    velocity-triggered calls). `claim_id` omitted -> full sweep across every Active
    claim (covers the old scheduled trigger), plus a housekeeping evidence-retention
    purge (PRD 10.9.1 point 7) run once at the start of the sweep. All three PRD
    10.5.8 trigger modes are the backend's decision now - it decides *when* to call
    this and with which shape; we just run the pipeline."""
    background_tasks.add_task(
        trigger_detection_run,
        claim_id=payload.claim_id,
        overrides=payload.overrides,
        embedder=embedder,
        session_factory=session_factory,
    )
    return DetectionRunTriggerResponse(claim_id=payload.claim_id, status="scheduled")
