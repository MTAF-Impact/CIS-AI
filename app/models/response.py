import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import ResponseStatus, ResponseType

if TYPE_CHECKING:
    from app.models.narrative import Narrative


class InterventionResponse(Base):
    """A drafted intervention (Prebunk explainer or Truth Sandwich correction) for a narrative."""

    __tablename__ = "intervention_responses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    narrative_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("narratives.id", ondelete="CASCADE"), nullable=True
    )
    response_type: Mapped[ResponseType] = mapped_column(String(32), nullable=False)

    # Truth Sandwich structure: Core Fact -> Neutral Misinformation Flag -> Re-stated Fact
    core_fact: Mapped[str | None] = mapped_column(Text, nullable=True)
    nuanced_flag: Mapped[str | None] = mapped_column(Text, nullable=True)
    reiterated_fact: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[ResponseStatus] = mapped_column(
        String(16), default=ResponseStatus.DRAFT, nullable=False
    )
    reviewer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    narrative: Mapped["Narrative | None"] = relationship(
        "Narrative", back_populates="responses"
    )
