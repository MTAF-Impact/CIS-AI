import asyncio
import logging
from functools import lru_cache
from typing import TypeVar

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.core.config import settings
from app.schemas.analysis import (
    ContentAnalysisSchema,
    NarrativeSummarySchema,
    PrebunkPredictionSchema,
    TruthSandwichSchema,
)

logger = logging.getLogger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

MAX_RATE_LIMIT_RETRIES = 3
DEFAULT_RATE_LIMIT_RETRY_SECONDS = 15.0

# These 429 error codes mean "this key cannot succeed no matter how long you wait" -
# retrying wastes ~45s (3 retries) before failing anyway, so fail fast instead.
NON_RETRYABLE_RATE_LIMIT_CODES = frozenset({"insufficient_quota", "credit_balance_exhausted"})


class LLMNotConfiguredError(RuntimeError):
    """Raised when an LLM call is attempted without a configured OPENAI_API_KEY."""


def _extract_retry_delay_seconds(exc: openai.RateLimitError) -> float:
    retry_after = exc.response.headers.get("retry-after") if exc.response is not None else None
    if retry_after is not None:
        try:
            return float(retry_after) + 1.0
        except ValueError:
            pass
    return DEFAULT_RATE_LIMIT_RETRY_SECONDS


CONTENT_ANALYSIS_SYSTEM_PROMPT = """\
You are a climate-misinformation analyst for a civic decision-support system. \
Given a single piece of public content (social post, comment, forum message, or \
transcript excerpt) about local climate/urban policy, analyze it strictly and return \
ONLY the requested JSON fields.

Guidance:
- classification: "misinformation" = false factual claim stated as true without clear \
  intent to deceive; "disinformation" = false claim with signs of deliberate, \
  coordinated intent to mislead; "legitimate_debate" = an opinion, value judgment, or \
  contestable-but-not-factually-false claim; "satire" = clearly exaggerated/comedic; \
  "unknown" = not enough information to classify.
- confidence: your confidence (0.0-1.0) in the classification above.
- outrage_score: emotional intensity / outrage expressed in the text (0.0 = calm/neutral, \
  1.0 = extreme outrage, fear, or anger).
- moral_foundation: the single dominant Moral Foundations Theory dimension driving the \
  emotional reaction (fairness, harm, autonomy, loyalty, authority, purity, or neutral \
  if none dominates).
- extracted_claim: the core factual or quasi-factual claim being made, in one neutral \
  sentence. If no discrete claim exists, summarize the point being made.
- underlying_grievance: the deeper community concern or grievance this content taps into \
  (e.g. distrust of local government, cost-of-living anxiety, historical displacement), \
  in one short phrase.
"""

TRUTH_SANDWICH_SYSTEM_PROMPT = """\
You are drafting a "Truth Sandwich" correction for a civic communications team, using \
neuroscience-backed structure: Core Fact -> Brief Neutral Misinformation Flag -> \
Re-stated Verified Fact.

Strict rules:
1. Ground every factual claim ONLY in the provided policy/reference context. Never \
   invent facts not present in the context.
2. core_fact: state the true, verified fact plainly and first (this is what most \
   readers will remember).
3. nuanced_flag: briefly note that a false or misleading claim is circulating, WITHOUT \
   repeating, quoting, or amplifying the specific viral false claim's wording. Describe \
   it neutrally and briefly (e.g. "A claim suggesting otherwise has been circulating and \
   is not accurate.").
4. reiterated_fact: restate the core verified fact again, in different words, so the \
   correction ends on the truth.
5. Keep tone calm, neutral, non-partisan, and non-condescending.
"""

PREBUNK_SYSTEM_PROMPT = """\
You are a strategic communications analyst helping a city government anticipate \
misinformation before it spreads, ahead of an upcoming climate/urban policy announcement.

Given a description of the policy and relevant grounding context (past grievances, \
fault lines, similar precedents), predict:
- predicted_attack_angle: the single most likely misinformation/disinformation attack \
  angle opponents or bad-faith actors will use against this policy.
- likely_framing: the emotional/rhetorical framing they will likely use (e.g. "government \
  overreach", "hidden tax", "elite ignoring working class").
- inoculation_explainer: a short, plain-language explainer (2-4 sentences) that can be \
  published BEFORE the attack spreads, to pre-bunk it: grounded strictly in the provided \
  policy context, calm in tone, and addressing the likely concern head-on without \
  repeating inflammatory false framing.
"""

NARRATIVE_SUMMARY_SYSTEM_PROMPT = """\
You are labeling a cluster of related public posts about a local climate/urban policy \
topic for a decision-support dashboard. Given a sample of posts from the same cluster, \
produce a short, neutral, descriptive title (under 10 words) and a 1-3 sentence summary \
of what this narrative is about and why people are engaging with it.
"""


class LLMClient:
    """Thin async wrapper around the OpenAI SDK (Responses API) with strict structured
    JSON output via Pydantic `text_format` schemas.

    Client construction is lazy and never raises: a missing/invalid OPENAI_API_KEY only
    surfaces when a generation call is actually made, so the rest of the app (and demo
    seeding) can still start up and exercise non-LLM code paths without a key configured.
    """

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._model = model or settings.OPENAI_MODEL
        self._api_key = api_key or settings.OPENAI_API_KEY
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            if not self._api_key:
                raise LLMNotConfiguredError(
                    "OPENAI_API_KEY is not configured - set it in your .env to use LLM "
                    "analysis features."
                )
            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def _generate_structured(
        self,
        *,
        prompt: str,
        system_instruction: str,
        schema: type[SchemaT],
    ) -> SchemaT:
        client = self._get_client()

        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                # Note: temperature is intentionally omitted - reasoning-tier models like
                # gpt-5.6-luna reject it (400 Unsupported parameter).
                response = await client.responses.parse(
                    model=self._model,
                    instructions=system_instruction,
                    input=prompt,
                    text_format=schema,
                )
                break
            except openai.RateLimitError as exc:
                if exc.code in NON_RETRYABLE_RATE_LIMIT_CODES or attempt >= MAX_RATE_LIMIT_RETRIES:
                    raise
                delay = _extract_retry_delay_seconds(exc)
                logger.warning(
                    "OpenAI rate-limited (attempt %d/%d), retrying in %.1fs",
                    attempt + 1,
                    MAX_RATE_LIMIT_RETRIES,
                    delay,
                )
                await asyncio.sleep(delay)

        if response.output_parsed is None:
            raise ValueError("OpenAI returned no parsed output for structured generation")
        return response.output_parsed

    async def analyze_content(self, text: str) -> ContentAnalysisSchema:
        """Classify a piece of content, score outrage, and extract claim/grievance."""
        prompt = f"Content to analyze:\n\n{text}"
        return await self._generate_structured(
            prompt=prompt,
            system_instruction=CONTENT_ANALYSIS_SYSTEM_PROMPT,
            schema=ContentAnalysisSchema,
        )

    async def summarize_narrative(self, sample_texts: list[str]) -> NarrativeSummarySchema:
        """Generate a concise title + summary for a cluster of related content items."""
        joined = "\n---\n".join(sample_texts[:20])
        prompt = f"Sample posts from this cluster:\n\n{joined}"
        return await self._generate_structured(
            prompt=prompt,
            system_instruction=NARRATIVE_SUMMARY_SYSTEM_PROMPT,
            schema=NarrativeSummarySchema,
        )

    async def predict_prebunk(
        self, policy_description: str, grounding_context: str
    ) -> PrebunkPredictionSchema:
        """Predict the likely misinformation attack angle against a policy and draft a prebunk."""
        prompt = (
            f"Policy description:\n{policy_description}\n\n"
            f"Grounding context (fault lines / precedents):\n{grounding_context or 'None provided.'}"
        )
        return await self._generate_structured(
            prompt=prompt,
            system_instruction=PREBUNK_SYSTEM_PROMPT,
            schema=PrebunkPredictionSchema,
        )

    async def generate_truth_sandwich(
        self, viral_claim_summary: str, grounding_context: str
    ) -> TruthSandwichSchema:
        """Draft a structured Truth Sandwich correction grounded in provided policy text."""
        prompt = (
            f"Circulating claim (for context only - do not quote verbatim in the flag):\n"
            f"{viral_claim_summary}\n\n"
            f"Grounded reference/policy context:\n{grounding_context or 'None provided.'}"
        )
        return await self._generate_structured(
            prompt=prompt,
            system_instruction=TRUTH_SANDWICH_SYSTEM_PROMPT,
            schema=TruthSandwichSchema,
        )


@lru_cache
def get_llm_client() -> LLMClient:
    return LLMClient()
