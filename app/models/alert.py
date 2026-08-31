import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ClaimAlert(Base):
    """F3 watchlist (C3) - a claim only appears here once a user explicitly adds it via
    the F1 bell icon (US14). EXISTING claims only, per US26 - enforced at the service
    layer, not here, since the DB has no clean way to express "claim_type = existing"
    as a FK-level constraint."""

    __tablename__ = "claim_alerts"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), primary_key=True
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ClaimScoreSnapshot(Base):
    """A point-in-time FinalClaimScore recording, appended every time a claim is
    (re)scored - powers the F3 trend chart [C1], which needs FinalClaimScore over time.
    Claim itself only ever holds the current score, so this is the only history."""

    __tablename__ = "claim_score_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), nullable=False
    )
    final_claim_score: Mapped[float] = mapped_column(Float, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
