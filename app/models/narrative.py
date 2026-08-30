import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import NarrativeStatus, RiskLevel

if TYPE_CHECKING:
    from app.models.content import ContentItem
    from app.models.response import InterventionResponse


class Narrative(Base):
    """A cluster of related ContentItems representing a distinct narrative/story."""

    __tablename__ = "narratives"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Risk sub-scores (0.0 - 1.0)
    growth_velocity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    emotional_intensity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    geographic_concentration: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    fault_line_relevance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    overall_risk_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    risk_level: Mapped[RiskLevel] = mapped_column(
        String(16), default=RiskLevel.LOW, nullable=False
    )
    status: Mapped[NarrativeStatus] = mapped_column(
        String(32), default=NarrativeStatus.ACTIVE, nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    content_items: Mapped[list["ContentItem"]] = relationship(
        "ContentItem", back_populates="narrative"
    )
    responses: Mapped[list["InterventionResponse"]] = relationship(
        "InterventionResponse", back_populates="narrative"
    )
