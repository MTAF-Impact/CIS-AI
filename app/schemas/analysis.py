from pydantic import BaseModel, Field

from app.models.enums import MoralFoundation, Stance


class ContentAnalysisSchema(BaseModel):
    """Structured output contract returned by the LLM for a single piece of content at
    ingestion time. Trimmed from the old design - classification/confidence dropped;
    the PRD scores claims via Falseness (F) corpus-matching and Stance, not a per-post
    classification label."""

    outrage_score: float = Field(ge=0.0, le=1.0)
    moral_foundation: MoralFoundation
    extracted_claim: str
    underlying_grievance: str


class ClaimSummarySchema(BaseModel):
    """Structured output contract returned when synthesizing a new claim from a cluster
    of content items: a single representative claim_statement (never copied verbatim
    from one post) plus a candidate topic label used for dynamic topic assignment."""

    claim_statement: str = Field(max_length=500)
    topic_label: str = Field(max_length=255)


class StanceSchema(BaseModel):
    """Structured output contract for a single post's stance relative to a claim."""

    stance: Stance


class StanceBatchSchema(BaseModel):
    """Structured output contract for batch stance classification - stances must be
    returned in the same order as the input texts (enforced by prompt instruction and
    validated by the caller, see LLMClient.classify_stances_batch)."""

    stances: list[Stance]


class HarmClassificationSchema(BaseModel):
    """AI-classified Harm Severity (H) sub-components, each 0-100. Human-confirmed
    separately before being finalized into H (see Claim.harm_human_confirmed)."""

    public_safety: float = Field(ge=0.0, le=100.0)
    institutional_trust: float = Field(ge=0.0, le=100.0)
    economic: float = Field(ge=0.0, le=100.0)
    policy_disruption: float = Field(ge=0.0, le=100.0)


class DebunkContentSchema(BaseModel):
    """Structured Truth Sandwich content for an EXISTING claim's Debunk Activity.
    Kept as a 3-part structure internally for generation quality/auditability, then
    rendered into the single copyable activity_content string the PRD's UI expects
    (see app.services.activity_service)."""

    core_fact: str
    nuanced_flag: str
    reiterated_fact: str


class NonExistingClaimPredictionSchema(BaseModel):
    """Structured output contract for predicting a NON_EXISTING claim ahead of a policy
    announcement: the predicted claim statement itself, a candidate topic label, and the
    Prebunk Activity content. predicted_attack_angle/likely_framing are intermediate
    reasoning aids for the LLM (and useful analyst context in the API response) but are
    NOT persisted as separate columns - only inoculation_explainer becomes the claim's
    cached activity_content."""

    claim_statement: str = Field(max_length=500)
    topic_label: str = Field(max_length=255)
    predicted_attack_angle: str
    likely_framing: str
    inoculation_explainer: str
