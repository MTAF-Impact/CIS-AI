"""Best-effort dual-writes into the Go backend's own tables (the `cis_` prefix) in this
same shared Supabase Postgres database, so data this AI service creates/changes is
visible to Go without Go having to poll or re-derive it from our tables - and vice versa,
nothing here assumes Go stops writing to its own tables either. Both sides double-write.

Deliberately NOT SQLAlchemy ORM models: the cis_* tables are owned/migrated by the Go
backend, not us - registering them here would make Base.metadata.create_all()/
reset_schema.py try to "own" and recreate them, causing schema drift against Go's own
migrations. Raw parameterized SQL only, and every write is wrapped in its own SAVEPOINT
(db.begin_nested()) so a schema mismatch or transient failure on the Go side can never
abort or corrupt the caller's own primary write - it's logged and skipped instead.

Tables NOT covered here: cis_policies. Its shape (file_path/mime/size, processing_status/
error/attempts, ai_policy_id back-reference) describes a materially different workflow -
Go owns the upload and file storage and expects this service to react and write back
ai_policy_id/processing_status once done, not a simple mirrored insert. That needs an
explicit handoff contract with the Go team before it's safe to implement - see the
README's integration note.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim import Claim
from app.models.enums import ClaimStatus

logger = logging.getLogger(__name__)


async def _best_effort(db: AsyncSession, description: str, stmt, params: dict) -> None:
    try:
        async with db.begin_nested():
            await db.execute(stmt, params)
    except Exception:
        logger.warning(
            "Go backend sync failed (%s) - this service's own write is unaffected",
            description,
            exc_info=True,
        )


async def sync_claim_review_status(db: AsyncSession, claim_id: uuid.UUID, status: ClaimStatus) -> None:
    """Mirrors Claim.status into cis_claim_reviews (upsert - one row per claim, matching
    its unique index on claim_id). Called both at claim-creation time (initial
    'unreviewed') and on every PATCH /claims/{id}/status."""
    now = datetime.now(UTC)
    stmt = text(
        """
        INSERT INTO cis_claim_reviews (id, claim_id, status, reviewed_at, created_at, updated_at)
        VALUES (:id, :claim_id, :status, :now, :now, :now)
        ON CONFLICT (claim_id) DO UPDATE
        SET status = EXCLUDED.status, reviewed_at = EXCLUDED.reviewed_at, updated_at = EXCLUDED.updated_at
        """
    )
    await _best_effort(
        db,
        "cis_claim_reviews upsert",
        stmt,
        {"id": uuid.uuid4(), "claim_id": claim_id, "status": status.value, "now": now},
    )


async def sync_claim_alert_added(db: AsyncSession, claim_id: uuid.UUID) -> None:
    now = datetime.now(UTC)
    stmt = text(
        """
        INSERT INTO cis_claim_alerts (id, claim_id, chart_visible, added_at, created_at, updated_at)
        VALUES (:id, :claim_id, false, :now, :now, :now)
        ON CONFLICT (claim_id) DO NOTHING
        """
    )
    await _best_effort(
        db, "cis_claim_alerts insert", stmt, {"id": uuid.uuid4(), "claim_id": claim_id, "now": now}
    )


async def sync_claim_alert_removed(db: AsyncSession, claim_id: uuid.UUID) -> None:
    stmt = text("DELETE FROM cis_claim_alerts WHERE claim_id = :claim_id")
    await _best_effort(db, "cis_claim_alerts delete", stmt, {"claim_id": claim_id})


async def sync_claim_score_snapshot(db: AsyncSession, claim: Claim) -> None:
    """cis_claim_score_snapshots wants the full breakdown (unlike our own
    ClaimScoreSnapshot, which only tracks final_claim_score) - append-only, matching its
    non-unique (claim_id, captured_at) index."""
    now = datetime.now(UTC)
    stmt = text(
        """
        INSERT INTO cis_claim_score_snapshots (
            id, claim_id, reach_score, velocity_score, falseness_score, harm_score,
            emotional_intensity_score, emotional_intensity_opposing, claim_score, npr,
            discount_factor, final_claim_score, is_dormant, captured_at, created_at
        ) VALUES (
            :id, :claim_id, :reach_score, :velocity_score, :falseness_score, :harm_score,
            :ei, :ei_opposing, :claim_score, :npr, :discount_factor, :final_claim_score,
            :is_dormant, :now, :now
        )
        """
    )
    await _best_effort(
        db,
        "cis_claim_score_snapshots insert",
        stmt,
        {
            "id": uuid.uuid4(),
            "claim_id": claim.id,
            "reach_score": claim.reach_score,
            "velocity_score": claim.velocity_score,
            "falseness_score": claim.falseness_score,
            "harm_score": claim.harm_score,
            "ei": claim.emotional_intensity_score,
            "ei_opposing": claim.emotional_intensity_opposing,
            "claim_score": claim.claim_score,
            "npr": claim.npr,
            "discount_factor": claim.discount_factor,
            "final_claim_score": claim.final_claim_score,
            "is_dormant": claim.is_dormant,
            "now": now,
        },
    )


async def sync_admin_threshold(db: AsyncSession, over_threshold: float) -> None:
    """Upserts cis_settings' 'alert_threshold' key (unique on key)."""
    now = datetime.now(UTC)
    stmt = text(
        """
        INSERT INTO cis_settings (id, key, value, value_type, description, created_at, updated_at)
        VALUES (
            :id, 'alert_threshold', :value, 'number',
            'Global FinalClaimScore threshold (0-100) deciding Over/Under Threshold on the Alert page (PRD US32).',
            :now, :now
        )
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
        """
    )
    await _best_effort(
        db,
        "cis_settings upsert",
        stmt,
        {"id": uuid.uuid4(), "value": str(over_threshold), "now": now},
    )
