"""PRD 10.9 - governance, ethics, and safeguards, scoped to what's still the AI
service's responsibility after the backend ownership split (see docs/COORDINATION.md):
minimum-necessary-retention purging of the evidence-artifact tables it still owns
(10.9.1 point 7).

The standing disclaimer (10.9.2) is no longer rendered by this service - the report/
detail surfaces that showed it moved to the backend, which must reproduce the exact
text (see docs/COORDINATION.md) on its own report and network-detail pages.

The other hard rules in 10.9.1 are enforced by omission rather than by a runtime
check: no model field anywhere stores an attribution/identity guess or a per-account
automation verdict (verified by inspection - grep the coordination package for
"attribut"/"sponsor"/"operated by"/"is_bot" and there is nothing to find), and no
code path calls out to a platform API."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.coordination import (
    CoordinatedNetwork,
    NetworkAccount,
    NetworkBurstBin,
    NetworkEdge,
    NetworkEvidencePost,
)

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_MONTHS = 24


async def purge_expired_evidence(
    db: AsyncSession, retention_months: int = DEFAULT_RETENTION_MONTHS
) -> int:
    """10.9.1 point 7 - evidence snapshots are retained for a configurable period and
    then purged. This deliberately drops the original "except reported" carve-out:
    "reported" status now lives in the backend's own tables, invisible to this
    service under "no shared-table writes" - purging is age-based only. Deletes the
    evidence-artifact rows (posts, burst bins, edges, account membership) for expired
    networks; the CoordinatedNetwork row itself is kept (it's the durable audit
    record - only the raw evidence content decays). Returns the count of networks
    purged. Called automatically at the start of every scheduled sweep
    (pipeline.run_scheduled_sweep) rather than exposed as its own endpoint, so the
    AI service still exposes exactly the one F5 route the backend doc describes."""
    cutoff = datetime.now(UTC) - timedelta(days=retention_months * 30)

    expired_ids = (
        await db.execute(
            select(CoordinatedNetwork.id).where(CoordinatedNetwork.created_at < cutoff)
        )
    ).scalars().all()
    if not expired_ids:
        return 0

    for model in (NetworkEvidencePost, NetworkBurstBin, NetworkEdge, NetworkAccount):
        await db.execute(delete(model).where(model.network_id.in_(expired_ids)))
    await db.commit()
    logger.info("Purged evidence for %d networks older than %d months", len(expired_ids), retention_months)
    return len(expired_ids)
