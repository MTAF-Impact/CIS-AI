import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import NarrativeStatus, RiskLevel
from app.schemas.content import ContentItemRead


class NarrativeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    summary: str | None
    growth_velocity: float
    emotional_intensity: float
    geographic_concentration: float
    fault_line_relevance: float
    overall_risk_score: float
    risk_level: RiskLevel
    status: NarrativeStatus
    created_at: datetime
    updated_at: datetime


class FaultLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    community_name: str
    grievance_theme: str
    description: str | None


class NarrativeDetailRead(NarrativeRead):
    content_items: list[ContentItemRead] = []
    matched_fault_lines: list[FaultLineRead] = []


class ClusterNowResponse(BaseModel):
    narratives_created: int
    narratives_updated: int
    content_items_clustered: int
