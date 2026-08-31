"""Request/response shapes for the Go backend's 3 documented HTTP touchpoints (see
docs/AI-INTEGRATION.md in the CIS-Backend repo) - Flow 1 (inbound policy webhook) and
Flow 3 (inbound generate-generic-claim) here; Flow 2's outbound callback body is built
directly in app.services.backend_callback_service, since nothing here consumes it."""

import uuid
from datetime import date

from pydantic import BaseModel


class PolicyMatchmakingWebhookRequest(BaseModel):
    policy_id: uuid.UUID  # cis_policies.id - echoed back in the Flow 2 callback path
    name: str
    description: str | None = None
    rolled_out_date: date
    status: str | None = None  # informational only; we derive our own from rolled_out_date
    file_name: str | None = None
    file_mime_type: str | None = None
    document_url: str | None = None


class PolicyMatchmakingAckResponse(BaseModel):
    status: str = "processing"


class GenerateGenericClaimWebhookRequest(BaseModel):
    claim_type: str | None = None  # only "existing"/"generic" aliases are supported
    topic_id: uuid.UUID | None = None


class GenerateGenericClaimWebhookResponse(BaseModel):
    claim_id: uuid.UUID
    claim_statement: str
    topic_id: uuid.UUID
    message: str = "generated"
