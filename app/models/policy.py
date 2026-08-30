import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.claim import Claim


class Policy(Base):
    """Minimal placeholder entity - F2 (Public Policy Bank) is explicitly out of
    scope for this PRD version. Exists only so Claims have something to correlate to."""

    __tablename__ = "policies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    claim_links: Mapped[list["ClaimPolicy"]] = relationship(
        "ClaimPolicy", back_populates="policy"
    )
    non_existing_claims: Mapped[list["Claim"]] = relationship(
        "Claim", back_populates="policy"
    )


class ClaimPolicy(Base):
    """Many-to-many junction for EXISTING claims <-> Policy. NON_EXISTING claims use
    Claim.policy_id directly instead (one-to-many, exactly one policy each)."""

    __tablename__ = "claim_policies"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), primary_key=True
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policies.id", ondelete="CASCADE"), primary_key=True
    )

    claim: Mapped["Claim"] = relationship("Claim", back_populates="policy_links")
    policy: Mapped["Policy"] = relationship("Policy", back_populates="claim_links")
