import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PolicyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str | None
    created_at: datetime


class PolicyCreate(BaseModel):
    """Minimal manual creation - F2 (Public Policy Bank) is out of scope; this exists
    only so claims have something to correlate to (see app.models.policy.Policy)."""

    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
