import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.core.database import Base
from app.models.enums import ClassificationLabel, ContentSource, MoralFoundation

if TYPE_CHECKING:
    from app.models.narrative import Narrative


class ContentItem(Base):
    """A single raw piece of ingested content (social post, RSS item, radio transcript, ...)."""

    __tablename__ = "content_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[ContentSource] = mapped_column(
        String(32), default=ContentSource.OTHER, nullable=False
    )
    author_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Gemini structured analysis output
    classification: Mapped[ClassificationLabel | None] = mapped_column(
        String(32), nullable=True
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    outrage_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    moral_foundation: Mapped[MoralFoundation | None] = mapped_column(
        String(32), nullable=True
    )
    extracted_claim: Mapped[str | None] = mapped_column(Text, nullable=True)
    underlying_grievance: Mapped[str | None] = mapped_column(Text, nullable=True)

    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.EMBEDDING_DIM), nullable=True
    )

    narrative_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("narratives.id", ondelete="SET NULL"), nullable=True
    )
    narrative: Mapped["Narrative | None"] = relationship(
        "Narrative", back_populates="content_items"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
