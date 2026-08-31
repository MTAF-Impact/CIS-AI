import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ClaimStatus, ClaimType
from app.schemas.content import ContentItemRead


class TopicBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str


class PolicyBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str


class ClaimListItemRead(BaseModel):
    """Card shape for the D1/D2 dashboards - lightweight; full breakdown is detail-only."""

    id: uuid.UUID
    claim_type: ClaimType
    claim_statement: str
    topic: TopicBrief
    status: ClaimStatus
    first_caught_at: datetime
    positive_statement_count: int
    negative_statement_count: int
    final_claim_score: float | None  # D1 only; always None on D2 (never scored)
    is_alerted: bool  # bell icon state (US14) - D1 only; always False on D2 (F3 is EXISTING-only)


class ClaimListEnvelope(BaseModel):
    """List response envelope - `fetched_at` is wall-clock at response time."""

    fetched_at: datetime
    total: int
    items: list[ClaimListItemRead]


class TopAccountEntry(BaseModel):
    """Top 5 accounts by post-volume on the Supporting side - PRD flags this
    interpretation as unconfirmed; revisit if the PM says otherwise."""

    account_handle: str
    contribution_count: int


class ExistingClaimDetailRead(BaseModel):
    """Every score field individually, never just the collapsed FinalClaimScore."""

    id: uuid.UUID
    claim_type: ClaimType
    claim_statement: str
    topic: TopicBrief
    status: ClaimStatus
    first_caught_at: datetime
    created_at: datetime
    updated_at: datetime

    reach_score: float | None
    velocity_score: float | None
    falseness_score: float | None
    harm_score: float | None
    harm_public_safety: float | None
    harm_institutional_trust: float | None
    harm_economic: float | None
    harm_policy_disruption: float | None
    harm_human_confirmed: bool
    emotional_intensity_score: float | None
    emotional_intensity_opposing: float | None
    claim_score: float | None
    npr: float | None
    discount_factor: float | None
    final_claim_score: float | None
    is_dormant: bool
    is_alerted: bool

    activity_content: str | None
    activity_generated_at: datetime | None
    # Truth Sandwich, split for the FE to render as 3 labeled blocks.
    debunk_core_fact: str | None
    debunk_nuanced_flag: str | None
    debunk_reiterated_fact: str | None

    top_accounts: list[TopAccountEntry]
    supporting_statements: list[ContentItemRead]
    opposing_statements: list[ContentItemRead]
    neutral_statements: list[ContentItemRead]
    policies: list[PolicyBrief]


class NonExistingClaimDetailRead(BaseModel):
    id: uuid.UUID
    claim_type: ClaimType
    claim_statement: str
    topic: TopicBrief
    status: ClaimStatus
    first_caught_at: datetime
    created_at: datetime
    updated_at: datetime
    policy: PolicyBrief | None
    activity_content: str | None
    activity_generated_at: datetime | None


class ClaimStatusUpdateRequest(BaseModel):
    status: ClaimStatus


class HarmConfirmRequest(BaseModel):
    """Human confirmation of AI-classified Harm sub-scores; omitted fields keep the AI value."""

    public_safety: float | None = Field(default=None, ge=0.0, le=100.0)
    institutional_trust: float | None = Field(default=None, ge=0.0, le=100.0)
    economic: float | None = Field(default=None, ge=0.0, le=100.0)
    policy_disruption: float | None = Field(default=None, ge=0.0, le=100.0)


class ClusterNowResponse(BaseModel):
    claims_created: int
    claims_updated: int
    content_items_clustered: int


class RescoreResponse(BaseModel):
    claims_rescored: int


class NonExistingClaimPredictRequest(BaseModel):
    """Manual/ad-hoc prediction trigger for an already-registered F2 policy."""

    policy_id: uuid.UUID


class NonExistingClaimPredictResponse(BaseModel):
    claim: NonExistingClaimDetailRead
    predicted_attack_angle: str
    likely_framing: str
