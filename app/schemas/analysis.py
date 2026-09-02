from pydantic import BaseModel, Field

from app.models.enums import ContentSource, MoralFoundation, Sentiment, Stance


class ContentAnalysisSchema(BaseModel):
    """LLM structured output for a single piece of content at ingestion time."""

    outrage_score: float = Field(ge=0.0, le=1.0)
    moral_foundation: MoralFoundation
    extracted_claim: str
    underlying_grievance: str
    # PRD v1.5 6.6.1 - the content's own emotional valence, assessed independently
    # of any claim. See Sentiment's docstring for why this must never be derived
    # from stance.
    sentiment: Sentiment
    # English translation, used for embedding (English-only model) - echoes the
    # original text when it's already English.
    text_en: str


class ClaimSummarySchema(BaseModel):
    """A fresh claim_statement synthesized from a cluster, plus a candidate topic label."""

    claim_statement: str = Field(max_length=500)
    topic_label: str = Field(max_length=255)


class NetworkLabelSchema(BaseModel):
    """A short, neutral, human-readable label for a detected coordinated network -
    matches coordinated_network.label's String(255) column."""

    label: str = Field(max_length=255)


class StanceSchema(BaseModel):
    """Structured output contract for a single post's stance relative to a claim."""

    stance: Stance


class StanceBatchSchema(BaseModel):
    """Batch stance classification - stances returned in the same order as input texts."""

    stances: list[Stance]


class HarmClassificationSchema(BaseModel):
    """AI-classified Harm Severity (H) sub-components, each 0-100."""

    public_safety: float = Field(ge=0.0, le=100.0)
    institutional_trust: float = Field(ge=0.0, le=100.0)
    economic: float = Field(ge=0.0, le=100.0)
    policy_disruption: float = Field(ge=0.0, le=100.0)


class DebunkContentSchema(BaseModel):
    """Structured Truth Sandwich content for an Existing claim's Debunk Activity."""

    core_fact: str
    nuanced_flag: str
    reiterated_fact: str


class DebunkSegmentSchema(BaseModel):
    """One tailored Debunk Activity draft for a single audience segment (PRD v1.5
    US12) - replaces the single generic DebunkContentSchema draft above."""

    segment_name: str = Field(max_length=255)
    segment_rationale: str
    content: str


class DebunkSegmentBatchSchema(BaseModel):
    """Segments in most-exposed-first order - this order becomes each row's `rank`."""

    segments: list[DebunkSegmentSchema] = Field(min_length=1, max_length=4)


class NonExistingClaimPredictionSchema(BaseModel):
    """Predicted claim statement, topic, and Prebunk content for a policy announcement.
    predicted_attack_angle/likely_framing are analyst context, not persisted."""

    claim_statement: str = Field(max_length=500)
    topic_label: str = Field(max_length=255)
    predicted_attack_angle: str
    likely_framing: str
    inoculation_explainer: str


class SyntheticPostSchema(BaseModel):
    """One LLM-fabricated post standing in for what a live crawler would have ingested."""

    text: str = Field(min_length=1, max_length=2000)
    source: ContentSource
    author_id: str = Field(max_length=255)
    location: str | None = Field(default=None, max_length=255)


class SyntheticPostBatchSchema(BaseModel):
    posts: list[SyntheticPostSchema]


class PolicyClaimMatchBatchSchema(BaseModel):
    """One boolean per candidate claim, same order - true only if genuinely about the policy."""

    matches: list[bool]
