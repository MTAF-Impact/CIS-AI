import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.core.database import Base
from app.models.enums import ClaimStatus, ClaimType

if TYPE_CHECKING:
    from app.models.content import ContentItem
    from app.models.policy import ClaimPolicy, Policy
    from app.models.topic import Topic


class Claim(Base):
    """Replaces Narrative. claim_type is fixed by pipeline of origin (EXISTING claims
    come from clustering real content; NON_EXISTING claims come from the prediction
    flow and are never scored) - see app.services.clustering_service and
    app.services.claim_prediction_service.

    PRD v1.3 simplified the status model to one shared ClaimStatus set for both types
    (no more type-specific PREBUNK/DEBUNK), so there is no longer a status/type CHECK
    constraint here."""

    __tablename__ = "claims"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    claim_type: Mapped[ClaimType] = mapped_column(String(16), nullable=False)
    claim_statement: Mapped[str] = mapped_column(Text, nullable=False)
    topic_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("topics.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[ClaimStatus] = mapped_column(
        String(16), default=ClaimStatus.UNREVIEWED, nullable=False
    )
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policies.id", ondelete="SET NULL"), nullable=True
    )
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.EMBEDDING_DIM), nullable=True
    )
    first_caught_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # --- Scoring (EXISTING only; stay NULL for NON_EXISTING, which is never scored) ---
    reach_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    velocity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    falseness_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    harm_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    harm_public_safety: Mapped[float | None] = mapped_column(Float, nullable=True)
    harm_institutional_trust: Mapped[float | None] = mapped_column(Float, nullable=True)
    harm_economic: Mapped[float | None] = mapped_column(Float, nullable=True)
    harm_policy_disruption: Mapped[float | None] = mapped_column(Float, nullable=True)
    harm_human_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    emotional_intensity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    emotional_intensity_opposing: Mapped[float | None] = mapped_column(Float, nullable=True)
    claim_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    npr: Mapped[float | None] = mapped_column(Float, nullable=True)
    discount_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_claim_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_dormant: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- Cached activity block (folds in the old InterventionResponse entirely) ---
    # activity_content is the single copyable block the PRD requires (US12/US20 - "one
    # AI-generated, copyable content block"). For EXISTING claims it's the concatenation
    # of the 3 debunk_* fields below; those are stored separately, additionally, so the
    # FE can render the Truth Sandwich as 3 distinct labeled blocks (Fact / Flag / Fact
    # Restated) instead of one run-on paragraph. NON_EXISTING claims (Prebunk) only ever
    # populate activity_content - there's no equivalent structured breakdown for them.
    activity_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    activity_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    debunk_core_fact: Mapped[str | None] = mapped_column(Text, nullable=True)
    debunk_nuanced_flag: Mapped[str | None] = mapped_column(Text, nullable=True)
    debunk_reiterated_fact: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    topic: Mapped["Topic"] = relationship("Topic", back_populates="claims")
    policy: Mapped["Policy | None"] = relationship(
        "Policy", back_populates="non_existing_claims"
    )
    policy_links: Mapped[list["ClaimPolicy"]] = relationship(
        "ClaimPolicy", back_populates="claim"
    )
    content_items: Mapped[list["ContentItem"]] = relationship(
        "ContentItem", back_populates="claim"
    )
