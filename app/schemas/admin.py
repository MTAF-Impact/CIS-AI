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
    """Acknowledgement only, mirroring DetectionRunResponse - the detection_run row
    is already written (status=pending) by the time this returns, so callers poll
    that table directly rather than this endpoint."""

    run_id: uuid.UUID
    status: str
    claim_id: uuid.UUID
