from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.endpoints.claims import _fetch_claim_with_relations, _to_existing_detail
from app.core.database import get_db
from app.schemas.admin import (
    AdminSettingRead,
    AdminSettingUpdate,
    GenerateGenericClaimResponse,
)
from app.services import admin_service
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
    """US32 - a single global threshold governing every claim's Over/Under Threshold
    status on the F3 watchlist (see AlertRow.threshold_status)."""
    settings = await admin_service.set_threshold(db, payload.over_threshold)
    return AdminSettingRead(over_threshold=settings.over_threshold)


@router.post("/generate-generic-claim", response_model=GenerateGenericClaimResponse, status_code=201)
async def generate_generic_claim(
    topic_hint: str | None = None,
    db: AsyncSession = Depends(get_db),
    llm: LLMClient = Depends(get_llm_client),
    embedder: EmbeddingService = Depends(get_embedding_service),
) -> GenerateGenericClaimResponse:
    """US33 - one-click sample Existing/Generic claim for demo/testing, fully scored,
    without waiting on live detection."""
    try:
        claim = await admin_service.generate_demo_existing_claim(db, llm, embedder, topic_hint)
    except LLMNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    claim = await _fetch_claim_with_relations(db, claim.id)
    return GenerateGenericClaimResponse(claim=await _to_existing_detail(db, claim))
