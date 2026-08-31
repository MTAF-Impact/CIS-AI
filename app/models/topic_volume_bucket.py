import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TopicVolumeBucket(Base):
    """Hourly rolling history backing Velocity's z-score baseline per topic."""

    __tablename__ = "topic_volume_buckets"
    __table_args__ = (UniqueConstraint("topic_id", "bucket_start", name="uq_topic_bucket"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("topics.id", ondelete="CASCADE"), nullable=False
    )
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    supporting_volume: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
