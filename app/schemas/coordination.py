from datetime import datetime

from pydantic import BaseModel, Field


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
