"""Test doubles for external services (OpenAI, sentence-transformers) so the test suite
never needs a real API key or network access to run.

FakeLLMClient implements the exact same public interface as app.services.llm_client.LLMClient
so it can be swapped in via FastAPI's dependency_overrides or passed directly to service
functions that accept an `llm` parameter.
"""

from app.models.enums import MoralFoundation, Stance
from app.schemas.analysis import (
    ClaimSummarySchema,
    ContentAnalysisSchema,
    DebunkContentSchema,
    HarmClassificationSchema,
    NonExistingClaimPredictionSchema,
)


def _fake_topic_label(text: str) -> str:
    lowered = text.lower()
    if "erp" in lowered or "congestion" in lowered or "toll" in lowered:
        return "Road Pricing & Transit"
    if "tree" in lowered or "mrt" in lowered or "monas" in lowered:
        return "Transit Construction"
    if "flood" in lowered or "banjir" in lowered or "ciliwung" in lowered:
        return "Flooding & Waterways"
    if "waste" in lowered or "sunter" in lowered or "smoke" in lowered:
        return "Waste & Pollution"
    return "General"


class FakeLLMClient:
    """Deterministic stand-in for LLMClient. Classification/stance are keyword-driven
    so tests can assert on realistic-looking downstream behavior without a live model call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def analyze_content(self, text: str) -> ContentAnalysisSchema:
        self.calls.append(("analyze_content", (text,), {}))
        lowered = text.lower()
        outrage = 0.8 if ("hidden tax" in lowered or "secretly" in lowered) else 0.4
        return ContentAnalysisSchema(
            outrage_score=outrage,
            moral_foundation=MoralFoundation.FAIRNESS,
            extracted_claim=text[:200],
            underlying_grievance="fake grievance for testing",
        )

    async def summarize_claim(self, sample_texts: list[str]) -> ClaimSummarySchema:
        self.calls.append(("summarize_claim", (sample_texts,), {}))
        return ClaimSummarySchema(
            claim_statement="Fake claim statement synthesized for testing.",
            topic_label=_fake_topic_label(sample_texts[0]) if sample_texts else "General",
        )

    async def classify_stance(self, claim_statement: str, post_text: str) -> Stance:
        self.calls.append(("classify_stance", (claim_statement, post_text), {}))
        lowered = post_text.lower()
        if any(kw in lowered for kw in ("not true", "debunk", "false", "incorrect", "myth")):
            return Stance.OPPOSING
        if "unrelated" in lowered:
            return Stance.NEUTRAL
        return Stance.SUPPORTING

    async def classify_stances_batch(
        self, claim_statement: str, texts: list[str]
    ) -> list[Stance]:
        self.calls.append(("classify_stances_batch", (claim_statement, texts), {}))
        return [await self.classify_stance(claim_statement, text) for text in texts]

    async def classify_harm(
        self, claim_statement: str, sample_supporting_texts: list[str]
    ) -> HarmClassificationSchema:
        self.calls.append(("classify_harm", (claim_statement, sample_supporting_texts), {}))
        return HarmClassificationSchema(
            public_safety=40.0,
            institutional_trust=50.0,
            economic=30.0,
            policy_disruption=20.0,
        )

    async def generate_debunk(
        self, claim_statement: str, grounding_context: str
    ) -> DebunkContentSchema:
        self.calls.append(("generate_debunk", (claim_statement, grounding_context), {}))
        return DebunkContentSchema(
            core_fact="Fake core fact for testing.",
            nuanced_flag="A claim suggesting otherwise has circulated and is not accurate.",
            reiterated_fact="Fake reiterated fact for testing.",
        )

    async def predict_non_existing_claim(
        self, policy_title: str, policy_description: str, grounding_context: str
    ) -> NonExistingClaimPredictionSchema:
        self.calls.append(
            ("predict_non_existing_claim", (policy_title, policy_description, grounding_context), {})
        )
        return NonExistingClaimPredictionSchema(
            claim_statement=f"Fake predicted claim about {policy_title} for testing.",
            topic_label=_fake_topic_label(policy_description),
            predicted_attack_angle="Fake attack angle: hidden costs",
            likely_framing="Fake framing: government overreach",
            inoculation_explainer="Fake inoculation explainer grounded in test context.",
        )


class AlwaysFailingLLMClient:
    """Simulates LLMNotConfiguredError, for testing the 503 error-translation path."""

    async def analyze_content(self, text: str) -> ContentAnalysisSchema:
        raise self._error()

    async def summarize_claim(self, sample_texts: list[str]) -> ClaimSummarySchema:
        raise self._error()

    async def classify_stance(self, claim_statement: str, post_text: str) -> Stance:
        raise self._error()

    async def classify_stances_batch(
        self, claim_statement: str, texts: list[str]
    ) -> list[Stance]:
        raise self._error()

    async def classify_harm(
        self, claim_statement: str, sample_supporting_texts: list[str]
    ) -> HarmClassificationSchema:
        raise self._error()

    async def generate_debunk(
        self, claim_statement: str, grounding_context: str
    ) -> DebunkContentSchema:
        raise self._error()

    async def predict_non_existing_claim(
        self, policy_title: str, policy_description: str, grounding_context: str
    ) -> NonExistingClaimPredictionSchema:
        raise self._error()

    @staticmethod
    def _error() -> Exception:
        from app.services.llm_client import LLMNotConfiguredError

        return LLMNotConfiguredError("fake: no key configured")
