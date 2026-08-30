import uuid
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.core.database import Base
from app.models.enums import PolicyStatus

if TYPE_CHECKING:
    from app.models.claim import Claim


class Policy(Base):
    """F2 - Public Policy Bank (PRD v1.3). A policy is authored by uploading a source
    document (PDF/Word); the AI matchmaking pipeline then links it to matching Existing
    claims and predicts new Non-Existing claims for it - see
    app.services.policy_matchmaking_service.

    The uploaded file is stored inline (bytea) rather than in a separate object-storage
    bucket - a deliberate MVP simplification: this project already runs entirely against
    a single Supabase Postgres instance with no other storage credentials configured, and
    policy documents are small/occasional uploads, not high-volume media."""

    __tablename__ = "policies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Full text extracted from the uploaded PDF/Word file at upload time - used as the
    # matchmaking pipeline's grounding input (see policy_matchmaking_service.py).
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    file_data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    # Rolled Out / Not Rolled Out (US41) is intentionally NOT a stored column - it's
    # derived from rolled_out_date vs. wall-clock time (see the `status` property below),
    # so it's always correct without needing a scheduled re-evaluation job.
    rolled_out_date: Mapped[date] = mapped_column(Date, nullable=False)

    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.EMBEDDING_DIM), nullable=True
    )

    # True while the async AI matchmaking job (US42) is running after creation - the F2
    # UI shows a "Processing" badge and must not let the user act on this policy's
    # claims until it flips to False.
    processing: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    claim_links: Mapped[list["ClaimPolicy"]] = relationship(
        "ClaimPolicy", back_populates="policy"
    )
    non_existing_claims: Mapped[list["Claim"]] = relationship(
        "Claim", back_populates="policy"
    )

    @property
    def status(self) -> PolicyStatus:
        today = datetime.now(UTC).date()
        return PolicyStatus.ROLLED_OUT if self.rolled_out_date <= today else PolicyStatus.NOT_ROLLED_OUT


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
