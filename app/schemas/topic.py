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
    """Manual topic creation, outside the dynamic clustering-driven path."""

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
