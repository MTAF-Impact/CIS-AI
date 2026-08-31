"""F5 (PRD Section 10) - Coordinated-Network Detector API surface.

Two endpoints, matching the backend's actual reference contract verbatim
(CIS-Backend internal/aiclient/endpoints.go pathDetectionRun/pathDetectionPurge,
pulled and reviewed this session) - not the earlier guessed single-endpoint design.
Everything else (network list/detail, review, account annex, allowlist, CSV export,
PDF/ZIP reports, F4 config) lives on the backend, which reads the AI's tables
directly. See docs/COORDINATION.md.
"""

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import get_db, get_session_factory
from app.core.security import verify_backend_api_key
from app.models.enums import DetectionRunStatus
from app.schemas.coordination_network import (
    DetectionRunRequest,
    DetectionRunResponse,
    PurgeSnapshotsRequest,
    PurgeSnapshotsResponse,
)
from app.services.coordination import governance, pipeline
from app.services.multilingual_embedding_service import (
    MultilingualEmbeddingService,
    get_multilingual_embedding_service,
)

router = APIRouter(prefix="/detection", tags=["detection"])


@router.post(
    "/runs",
    response_model=DetectionRunResponse,
    status_code=202,
    dependencies=[Depends(verify_backend_api_key)],
)
async def trigger_detection_run(
    payload: DetectionRunRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    embedder: MultilingualEmbeddingService = Depends(get_multilingual_embedding_service),
    session_factory: async_sessionmaker = Depends(get_session_factory),
) -> DetectionRunResponse:
    """PRD 10.5.8 - all three trigger modes (scheduled/velocity/on-demand) are the
    backend's decision now; this just runs the pipeline when asked. The
    detection_run row is created synchronously (status=pending) before the 202
    response, so run_id is real and immediately queryable - the backend never
    polls, it reads the row directly as the pipeline updates it in the
    background."""
    run = await pipeline.create_pending_run(db, payload)
    background_tasks.add_task(
        pipeline.run_detection,
        run_id=run.id,
        claim_ids=payload.claim_ids,
        window_start=payload.window_start,
        window_end=payload.window_end,
        parameters=payload.parameters,
        exclusions=payload.exclusions,
        embedder=embedder,
        session_factory=session_factory,
    )
    # db.refresh() reads status back as a plain str (String column, no native enum
    # type) rather than the DetectionRunStatus instance it was created with.
    status = run.status.value if isinstance(run.status, DetectionRunStatus) else run.status
    return DetectionRunResponse(run_id=run.id, status=status)


@router.post(
    "/snapshots/purge",
    response_model=PurgeSnapshotsResponse,
    dependencies=[Depends(verify_backend_api_key)],
)
async def purge_snapshots(
    payload: PurgeSnapshotsRequest, db: AsyncSession = Depends(get_db)
) -> PurgeSnapshotsResponse:
    """PRD 10.9.1 point 7 - the backend computes which networks are past retention
    (only it can see whether a report was generated from a snapshot) and hands over
    the list; deletion is ours since the rows are AI-owned."""
    count = await governance.purge_expired_evidence(db, payload.network_ids)
    return PurgeSnapshotsResponse(snapshots_purged=count)
