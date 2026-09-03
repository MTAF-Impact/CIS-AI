import uuid

from pydantic import BaseModel, Field

from app.schemas.claim import ExistingClaimDetailRead


class AdminSettingRead(BaseModel):
    over_threshold: float


class AdminSettingUpdate(BaseModel):
    over_threshold: float = Field(ge=0.0, le=100.0)


class GenerateGenericClaimResponse(BaseModel):
    claim: ExistingClaimDetailRead


class GenerateCoordinatedNetworkResponse(BaseModel):
    """Acknowledgement only; poll the detection_run table for status."""

    run_id: uuid.UUID
    status: str
    claim_id: uuid.UUID
