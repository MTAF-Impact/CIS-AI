"""Evidence-artifact purge for networks the backend names. The backend computes
which networks are past retention and calls POST /api/v1/detection/snapshots/purge
with the list; this module executes the deletion."""

import logging
import uuid

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.coordination import (
    EvidenceSnapshot,
    NetworkAccount,
    NetworkBurstBin,
    NetworkEdge,
    NetworkEvidencePost,
)

logger = logging.getLogger(__name__)


async def purge_expired_evidence(db: AsyncSession, network_ids: list[uuid.UUID]) -> int:
    """Deletes evidence-artifact rows and the evidence_snapshot row for the named
    networks; the coordinated_network row itself is kept. Returns the purged count."""
    if not network_ids:
        return 0

    for model in (NetworkEvidencePost, NetworkBurstBin, NetworkEdge, NetworkAccount, EvidenceSnapshot):
        await db.execute(delete(model).where(model.network_id.in_(network_ids)))
    await db.commit()
    logger.info("Purged evidence for %d networks", len(network_ids))
    return len(network_ids)
