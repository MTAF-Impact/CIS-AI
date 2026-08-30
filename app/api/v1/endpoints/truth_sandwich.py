import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.enums import ResponseStatus
from app.models.response import InterventionResponse
from app.schemas.response import (
    InterventionResponseRead,
    ResponseReviewRequest,
    TruthSandwichGenerateRequest,
)
from app.services.embedding_service import EmbeddingService, get_embedding_service
from app.services.gemini_client import (
    GeminiClient,
    GeminiNotConfiguredError,
    get_gemini_client,
)
from app.services.truth_sandwich_service import (
    NarrativeNotFoundError,
    generate_truth_sandwich_for_narrative,
)

router = APIRouter(prefix="/response", tags=["truth-sandwich"])


@router.post("/generate", response_model=InterventionResponseRead, status_code=201)
async def generate_response(
    payload: TruthSandwichGenerateRequest,
    db: AsyncSession = Depends(get_db),
    gemini: GeminiClient = Depends(get_gemini_client),
    embedder: EmbeddingService = Depends(get_embedding_service),
) -> InterventionResponse:
    """Draft a structured Truth Sandwich (Core Fact -> Neutral Flag -> Re-stated Fact)
    for a narrative, grounded in matching fault lines. Saved as status=DRAFT for
    human-in-the-loop review."""
    try:
        return await generate_truth_sandwich_for_narrative(
            db, payload.narrative_id, gemini=gemini, embedder=embedder
        )
    except NarrativeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except GeminiNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.patch("/{response_id}/review", response_model=InterventionResponseRead)
async def review_response(
    response_id: uuid.UUID,
    payload: ResponseReviewRequest,
    db: AsyncSession = Depends(get_db),
) -> InterventionResponse:
    """Human-in-the-loop review: approve, edit, or reject a drafted intervention."""
    response = await db.get(InterventionResponse, response_id)
    if response is None:
        raise HTTPException(status_code=404, detail="Response not found")

    response.status = payload.status
    if payload.reviewer_notes is not None:
        response.reviewer_notes = payload.reviewer_notes

    if payload.status == ResponseStatus.EDITED:
        if payload.core_fact is not None:
            response.core_fact = payload.core_fact
        if payload.nuanced_flag is not None:
            response.nuanced_flag = payload.nuanced_flag
        if payload.reiterated_fact is not None:
            response.reiterated_fact = payload.reiterated_fact

    await db.commit()
    await db.refresh(response)
    return response
