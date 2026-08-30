import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import PolicyStatus
from app.schemas.claim import ClaimListItemRead


class PolicyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str | None
    rolled_out_date: date
    status: PolicyStatus
    file_name: str | None
    processing: bool
    created_at: datetime


class PolicyListResult(BaseModel):
    total: int
    items: list[PolicyRead]


class PolicyDetailRead(PolicyRead):
    existing_claims: list[ClaimListItemRead]
    non_existing_claims: list[ClaimListItemRead]
