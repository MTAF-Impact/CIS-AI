from pydantic import BaseModel, Field

from app.models.enums import ClassificationLabel, MoralFoundation


class ContentAnalysisSchema(BaseModel):
    """Structured output contract returned by Gemini for a single piece of content."""

    classification: ClassificationLabel
    confidence: float = Field(ge=0.0, le=1.0)
    outrage_score: float = Field(ge=0.0, le=1.0)
    moral_foundation: MoralFoundation
    extracted_claim: str
    underlying_grievance: str


class NarrativeSummarySchema(BaseModel):
    """Structured output contract returned by Gemini when labeling a content cluster."""

    title: str = Field(max_length=255)
    summary: str


class PrebunkPredictionSchema(BaseModel):
    """Structured output contract returned by Gemini for a Prebunk attack-angle prediction."""

    predicted_attack_angle: str
    likely_framing: str
    inoculation_explainer: str


class TruthSandwichSchema(BaseModel):
    """Structured output contract returned by Gemini for a Truth Sandwich correction."""

    core_fact: str
    nuanced_flag: str
    reiterated_fact: str
