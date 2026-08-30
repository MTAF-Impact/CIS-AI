import uuid

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.models.enums import NarrativeStatus, RiskLevel
from app.models.fault_line import FaultLine
from app.models.narrative import Narrative
from app.schemas.narrative import ClusterNowResponse, FaultLineRead, NarrativeDetailRead, NarrativeRead
from app.services import risk_engine
from app.services.clustering_service import cluster_unclustered_content
from app.services.gemini_client import GeminiClient, get_gemini_client

router = APIRouter(prefix="/narratives", tags=["narratives"])


@router.get("", response_model=list[NarrativeRead])
async def list_narratives(
    db: AsyncSession = Depends(get_db),
    risk_level: RiskLevel | None = None,
    status: NarrativeStatus | None = None,
    limit: int = Query(default=50, le=200),
) -> list[Narrative]:
    """List tracked narratives sorted by overall risk score and growth velocity, descending."""
    stmt = select(Narrative)
    if risk_level is not None:
        stmt = stmt.where(Narrative.risk_level == risk_level)
    if status is not None:
        stmt = stmt.where(Narrative.status == status)
    stmt = stmt.order_by(
        Narrative.overall_risk_score.desc(), Narrative.growth_velocity.desc()
    ).limit(limit)

    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/cluster-now", response_model=ClusterNowResponse)
async def cluster_now(
    db: AsyncSession = Depends(get_db),
    gemini: GeminiClient = Depends(get_gemini_client),
) -> ClusterNowResponse:
    """Trigger immediate re-clustering of any not-yet-clustered content items."""
    result = await cluster_unclustered_content(db, gemini=gemini)
    return ClusterNowResponse(
        narratives_created=result.narratives_created,
        narratives_updated=result.narratives_updated,
        content_items_clustered=result.content_items_clustered,
    )


@router.get("/{narrative_id}", response_model=NarrativeDetailRead)
async def get_narrative(
    narrative_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> NarrativeDetailRead:
    """Detailed narrative view: linked posts (timeline via created_at) and matched fault lines."""
    stmt = (
        select(Narrative)
        .where(Narrative.id == narrative_id)
        .options(selectinload(Narrative.content_items))
    )
    narrative = (await db.execute(stmt)).scalar_one_or_none()
    if narrative is None:
        raise HTTPException(status_code=404, detail="Narrative not found")

    timeline = sorted(narrative.content_items, key=lambda item: item.created_at)

    matched_fault_lines: list[FaultLine] = []
    embedded_items = [item for item in timeline if item.embedding is not None]
    if embedded_items:
        centroid = np.array([item.embedding for item in embedded_items], dtype=float).mean(axis=0)
        norm = np.linalg.norm(centroid)
        if norm > 0:
            centroid = centroid / norm

        fault_lines = list(
            (await db.execute(select(FaultLine).where(FaultLine.embedding.is_not(None))))
            .scalars()
            .all()
        )
        fault_line_embeddings = [(str(fl.id), fl.embedding) for fl in fault_lines]
        _score, matched_ids = risk_engine.compute_fault_line_relevance(
            centroid, fault_line_embeddings
        )
        matched_ids_set = set(matched_ids)
        matched_fault_lines = [fl for fl in fault_lines if str(fl.id) in matched_ids_set]

    return NarrativeDetailRead(
        id=narrative.id,
        title=narrative.title,
        summary=narrative.summary,
        growth_velocity=narrative.growth_velocity,
        emotional_intensity=narrative.emotional_intensity,
        geographic_concentration=narrative.geographic_concentration,
        fault_line_relevance=narrative.fault_line_relevance,
        overall_risk_score=narrative.overall_risk_score,
        risk_level=narrative.risk_level,
        status=narrative.status,
        created_at=narrative.created_at,
        updated_at=narrative.updated_at,
        content_items=timeline,
        matched_fault_lines=[FaultLineRead.model_validate(fl) for fl in matched_fault_lines],
    )
