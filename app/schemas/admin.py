from pydantic import BaseModel, Field

from app.schemas.claim import ExistingClaimDetailRead


class AdminSettingRead(BaseModel):
    over_threshold: float


class AdminSettingUpdate(BaseModel):
    over_threshold: float = Field(ge=0.0, le=100.0)


class GenerateGenericClaimResponse(BaseModel):
    claim: ExistingClaimDetailRead
