import uuid
from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.alert import ClaimAlert, ClaimScoreSnapshot
from app.models.claim import Claim
from app.schemas.alert import (
    AlertListResult,
    AlertRow,
    ChartPoint,
    ChartSeries,
    ThresholdStatus,
)
from app.services import admin_service

router = APIRouter(prefix="/alerts", tags=["alerts"])

_BUCKET_FORMAT = {
    "day": "%Y-%m-%d",
    "week": "%G-W%V",
    "month": "%Y-%m",
    "year": "%Y",
}


@router.get("", response_model=AlertListResult)
async def list_alerts(
    db: AsyncSession = Depends(get_db),
    q: str | None = None,
    limit: int = Query(default=50, le=1000),
    offset: int = Query(default=0, ge=0),
) -> AlertListResult:
    """F3 watchlist table [C3] - sorted most-recently-added first (US30)."""
    settings = await admin_service.get_settings(db)

    stmt = select(ClaimAlert, Claim).join(Claim, Claim.id == ClaimAlert.claim_id)
    if q:
        stmt = stmt.where(Claim.claim_statement.ilike(f"%{q}%"))

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()

    stmt = stmt.order_by(ClaimAlert.added_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).all()

    items = [
        AlertRow(
            claim_id=claim.id,
            claim_statement=claim.claim_statement,
            claim_created_date=claim.created_at,
            final_claim_score=claim.final_claim_score or 0.0,
            threshold_status=(
                ThresholdStatus.OVER_THRESHOLD
                if (claim.final_claim_score or 0.0) >= settings.over_threshold
                else ThresholdStatus.UNDER_THRESHOLD
            ),
            added_at=alert.added_at,
        )
        for alert, claim in rows
    ]
    return AlertListResult(total=total, items=items)


@router.get("/chart", response_model=list[ChartSeries])
async def alert_chart(
    db: AsyncSession = Depends(get_db),
    claim_ids: list[uuid.UUID] = Query(default_factory=list),
    granularity: str = Query(default="week", pattern="^(day|week|month|year)$"),
) -> list[ChartSeries]:
    """[C1]/[C2] - FinalClaimScore over time for whichever watchlist claims the FE
    currently has checked (US28). Only claim_ids that are actually on the F3 watchlist
    are honored - the chart-visibility selection itself is FE-local state (default
    empty on page load, per US28), not persisted server-side."""
    if not claim_ids:
        return []

    alerted_ids = set(
        (
            await db.execute(select(ClaimAlert.claim_id).where(ClaimAlert.claim_id.in_(claim_ids)))
        )
        .scalars()
        .all()
    )
    if not alerted_ids:
        return []

    claims = {
        claim.id: claim
        for claim in (
            await db.execute(select(Claim).where(Claim.id.in_(alerted_ids)))
        )
        .scalars()
        .all()
    }

    snapshots = (
        await db.execute(
            select(ClaimScoreSnapshot)
            .where(ClaimScoreSnapshot.claim_id.in_(alerted_ids))
            .order_by(ClaimScoreSnapshot.recorded_at)
        )
    ).scalars().all()

    fmt = _BUCKET_FORMAT[granularity]
    buckets: dict[uuid.UUID, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    bucket_timestamps: dict[str, datetime] = {}
    for snap in snapshots:
        key = snap.recorded_at.strftime(fmt)
        bucket_timestamps.setdefault(key, snap.recorded_at)
        buckets[snap.claim_id][key].append(snap.final_claim_score)

    series = []
    for claim_id in alerted_ids:
        claim = claims.get(claim_id)
        if claim is None:
            continue
        points = [
            ChartPoint(recorded_at=bucket_timestamps[key], final_claim_score=sum(scores) / len(scores))
            for key, scores in sorted(buckets[claim_id].items(), key=lambda kv: bucket_timestamps[kv[0]])
        ]
        series.append(
            ChartSeries(claim_id=claim_id, claim_statement=claim.claim_statement, points=points)
        )
    return series
