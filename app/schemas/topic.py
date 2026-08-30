import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TopicRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime


class TopicCreate(BaseModel):
    """Manual topic creation, for cases outside the dynamic clustering-driven path
    (see app.services.clustering_service.assign_or_create_topic)."""

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
