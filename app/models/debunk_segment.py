import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.claim import Claim


class ClaimDebunkSegment(Base):
    """One tailored Debunk Activity draft per audience segment - replaces the single
    generic draft (Claim.activity_content). Generated once, at claim creation, and
    never regenerated on view. Existing/Generic claims only; the backend falls back
    to activity_content when this table is empty."""

    __tablename__ = "claim_debunk_segments"
    __table_args__ = (
        UniqueConstraint(
            "claim_id", "segment_name", name="claim_debunk_segments_claim_segment_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), nullable=False
    )
    # Card label - an unlabelled variant reads as the generic draft v1.5 removes.
    segment_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Why this segment was identified; rendered as the card's subtitle.
    segment_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Card order, most-exposed segment first.
    rank: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    claim: Mapped["Claim"] = relationship("Claim", back_populates="debunk_segments")
