"""PRD 10.9 - governance, ethics, and safeguards, scoped to what's the AI service's
responsibility after the backend ownership split (see docs/COORDINATION.md):
executing the evidence-artifact purge for networks the backend names (10.9.1 point 7).

The backend computes *which* networks are past retention - it alone can see whether
a report was generated from a snapshot (cis_network_reports is backend-owned), so it
alone can honour PRD 10.9.1 point 7's "except where a report was generated" exception.
It calls POST /api/v1/detection/snapshots/purge with the list; this module just
executes the deletion, since the rows are AI-owned.

The standing disclaimer (10.9.2) is no longer rendered by this service - the report/
detail surfaces that showed it moved to the backend, which must reproduce the exact
text (see docs/COORDINATION.md) on its own report and network-detail pages.

The other hard rules in 10.9.1 are enforced by omission rather than by a runtime
check: no model field anywhere stores an attribution/identity guess or a per-account
automation verdict (verified by inspection - grep the coordination package for
"attribut"/"sponsor"/"operated by"/"is_bot" and there is nothing to find), and no
code path calls out to a platform API."""

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
    """Deletes the evidence-artifact rows (posts, burst bins, edges, account
    membership) and the evidence_snapshot row for exactly the named networks; the
    coordinated_network row itself is kept (it's the durable audit record - only the
    raw evidence content decays). Returns the count of networks purged (== number of
    ids that actually had something to delete)."""
    if not network_ids:
        return 0

    for model in (NetworkEvidencePost, NetworkBurstBin, NetworkEdge, NetworkAccount, EvidenceSnapshot):
        await db.execute(delete(model).where(model.network_id.in_(network_ids)))
    await db.commit()
    logger.info("Purged evidence for %d networks (backend-supplied list)", len(network_ids))
    return len(network_ids)
