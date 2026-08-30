import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ResponseStatus, ResponseType


class InterventionResponseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    narrative_id: uuid.UUID | None
    response_type: ResponseType
    core_fact: str | None
    nuanced_flag: str | None
    reiterated_fact: str | None
    status: ResponseStatus
    reviewer_notes: str | None
    created_at: datetime
    updated_at: datetime


class TruthSandwichGenerateRequest(BaseModel):
    narrative_id: uuid.UUID


class ResponseReviewRequest(BaseModel):
    status: ResponseStatus
    reviewer_notes: str | None = None
    # Allows a human reviewer to submit edited text alongside status=EDITED
    core_fact: str | None = None
    nuanced_flag: str | None = None
    reiterated_fact: str | None = None


class PrebunkPredictRequest(BaseModel):
    policy_description: str = Field(min_length=1)
    policy_title: str | None = None


class PrebunkPredictResponse(BaseModel):
    predicted_attack_angle: str
    likely_framing: str
    inoculation_explainer: str
    grounding_sources: list[str] = Field(default_factory=list)


class CIBCheckPost(BaseModel):
    id: str
    text: str
    author_id: str
    created_at: datetime
    account_created_at: datetime | None = None


class CIBCheckRequest(BaseModel):
    posts: list[CIBCheckPost] = Field(min_length=2)


class CIBCluster(BaseModel):
    post_ids: list[str]
    author_ids: list[str]
    reason: list[str]
    coordination_score: float


class CIBCheckResponse(BaseModel):
    coordination_risk_score: float
    is_likely_coordinated: bool
    clusters: list[CIBCluster]
