import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ContentSource, MoralFoundation, Stance


class ContentItemCreate(BaseModel):
    text: str = Field(min_length=1)
    source: ContentSource = ContentSource.OTHER
    author_id: str | None = None
    location: str | None = None
    # Optional raw metrics for Reach (R) / Emotional Intensity (EI) scoring - populated
    # by whatever upstream source feeds this API; default null when absent.
    impressions: int | None = None
    positive_reaction_count: int | None = None
    negative_reaction_count: int | None = None


class ContentItemBatchCreate(BaseModel):
    items: list[ContentItemCreate] = Field(min_length=1)


class ContentItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    text: str
    source: ContentSource
    author_id: str | None
    location: str | None
    outrage_score: float | None
    moral_foundation: MoralFoundation | None
    extracted_claim: str | None
    underlying_grievance: str | None
    stance: Stance | None
    impressions: int | None
    positive_reaction_count: int | None
    negative_reaction_count: int | None
    claim_id: uuid.UUID | None
    created_at: datetime


class ContentItemBatchResult(BaseModel):
    created: list[ContentItemRead]
    failed: list[dict] = Field(default_factory=list)
