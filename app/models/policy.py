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
    """F2 - Public Policy Bank. File stored inline (bytea) - a deliberate MVP simplification."""

    __tablename__ = "policies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Extracted from the uploaded file - matchmaking's grounding input.
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    file_data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    # Soft reference to the Go backend's cis_policies.id (Flow 1 webhook only) - used to
    # detect a retry and stay idempotent. Null for our own POST /policies upload flow.
    backend_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=True
    )

    rolled_out_date: Mapped[date] = mapped_column(Date, nullable=False)  # drives `status` below

    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.EMBEDDING_DIM), nullable=True
    )

    processing: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)  # matchmaking in progress

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
    """Many-to-many junction, Existing claims only. Non-Existing use Claim.policy_id directly."""

    __tablename__ = "claim_policies"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), primary_key=True
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policies.id", ondelete="CASCADE"), primary_key=True
    )

    claim: Mapped["Claim"] = relationship("Claim", back_populates="policy_links")
    policy: Mapped["Policy"] = relationship("Policy", back_populates="claim_links")
