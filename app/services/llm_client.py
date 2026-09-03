import asyncio
import logging
import re
from functools import lru_cache
from typing import TypeVar

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.core.config import settings
from app.models.enums import Stance
from app.schemas.analysis import (
    ClaimSummarySchema,
    ContentAnalysisSchema,
    DebunkContentSchema,
    DebunkSegmentBatchSchema,
    DebunkSegmentSchema,
    HarmClassificationSchema,
    NetworkLabelSchema,
    NonExistingClaimPredictionSchema,
    PolicyClaimMatchBatchSchema,
    StanceBatchSchema,
    StanceSchema,
    SyntheticPostBatchSchema,
    SyntheticPostSchema,
)

logger = logging.getLogger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

MAX_RATE_LIMIT_RETRIES = 3
DEFAULT_RATE_LIMIT_RETRY_SECONDS = 15.0

# These 429 codes can't succeed no matter how long you wait - fail fast instead.
NON_RETRYABLE_RATE_LIMIT_CODES = frozenset({"insufficient_quota", "credit_balance_exhausted"})
_RETRY_DELAY_PATTERN = re.compile(r"retry in ([\d.]+)s", re.IGNORECASE)


class LLMNotConfiguredError(RuntimeError):
    """Raised when an LLM call is attempted without a configured OPENAI_API_KEY."""


class StanceCountMismatchError(ValueError):
    """Raised when a batch stance classification returns a different number of
    stances than input texts - the caller cannot safely zip a misaligned result."""


def _extract_retry_delay_seconds(exc: openai.RateLimitError) -> float:
    retry_after = exc.response.headers.get("retry-after") if exc.response is not None else None
    if retry_after is not None:
        try:
            return float(retry_after) + 1.0
        except ValueError:
            pass
    match = _RETRY_DELAY_PATTERN.search(str(exc))
    return float(match.group(1)) + 1.0 if match else DEFAULT_RATE_LIMIT_RETRY_SECONDS


CONTENT_ANALYSIS_SYSTEM_PROMPT = """\
You are a climate-misinformation analyst for a civic decision-support system. \
Given a single piece of public content (social post, comment, forum message, or \
transcript excerpt) about local climate/urban policy, analyze it strictly and return \
ONLY the requested JSON fields.

Guidance:
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
- sentiment: the content's own emotional valence toward the climate/city topic it \
  discusses - "positive" (reassured, approving, hopeful), "negative" (alarmed, \
  distrustful, angry, critical), or "neutral" (purely factual/informational, or mixed \
  with no clear lean). Judge this on the content itself, NOT on whether it happens to \
  support or oppose any specific claim - a post calmly correcting a false rumor is \
  informative-toward-positive, not negative, even though it is emotionally charged \
  about the rumor itself; a genuine complaint about a real problem is negative even if \
  no specific claim is involved.
- text_en: an English translation of the content. If it is already in English, echo it \
  back unchanged. Translate meaning faithfully, including tone (keep sarcasm sarcastic, \
  keep outrage outraged) - this text is used for downstream embedding/clustering, not \
  shown to end users, so prioritize semantic fidelity over polish.
"""

CLAIM_SUMMARY_SYSTEM_PROMPT = """\
You are labeling a cluster of related public posts about a local climate/urban policy \
topic for a government decision-support dashboard (a "Claim Repository Bank").

Given a sample of posts that all express the same underlying claim, produce:
- claim_statement: ONE neutral, declarative sentence stating the claim these posts are \
  collectively making. Synthesize this fresh from the pattern across all the sample \
  posts - do not just copy or lightly reword a single post.
- topic_label: a short (2-5 word) label for the broader subject-matter category this \
  claim belongs to (e.g. "Road Pricing & Transit", "Flooding & Waterways", "Waste & \
  Pollution"). Keep it general enough to plausibly cover other related claims, not \
  hyper-specific to this one claim.

  A list of already-existing topic labels may be given below. If this claim clearly \
  belongs under one of them, reuse that EXACT label verbatim (same spelling, wording, \
  and punctuation) rather than inventing a new phrasing for the same subject - e.g. if \
  "Waste & Pollution" already exists, do not output "Waste & Water Pollution" for \
  another waste/pollution claim. Only propose a new label when none of the existing \
  ones genuinely fit.
"""

NETWORK_LABEL_SYSTEM_PROMPT = """\
You are labeling a detected coordinated-posting-behavior network for a civic \
decision-support system's internal reporting (PRD Section 10, F5). This is a \
pattern description, not an accusation - coordinated posting behaviour has \
legitimate explanations (organised civic campaigns, newsroom syndication, community \
mobilisation), and this label must NEVER assert automation, inauthenticity, bad \
faith, or the identity/affiliation of any account - that determination is a human \
reviewer's job, not this system's.

Given the claim these accounts are all posting in support of, and a sample of their \
posts, produce a short (3-8 word) neutral, descriptive label for this network - \
naming WHAT topic/claim the coordinated activity centers on, never WHO is behind it \
or WHY. Good: "ERP Road Pricing Amplification Cluster", "Flood Warning Claim Push". \
Bad: "Bot Network Spreading Misinformation", "Fake Accounts Attacking Policy" \
(asserts identity/intent this system cannot determine).
"""

STANCE_SYSTEM_PROMPT = """\
You are classifying a single post's stance toward a specific claim, for a civic \
decision-support system tracking misinformation spread and organic public pushback.

Given the claim statement and one post, classify the post's stance as exactly one of:
- "supporting": the post agrees with, spreads, or repeats the claim.
- "opposing": the post disputes, corrects, mocks, or pushes back against the claim.
- "neutral": the post mentions the claim's topic without clearly taking a position \
  either way.
Base this ONLY on the post's relationship to the given claim, not on the post's general \
tone.
"""

STANCE_BATCH_SYSTEM_PROMPT = """\
You are classifying multiple posts' stances toward a single specific claim, for a civic \
decision-support system tracking misinformation spread and organic public pushback.

Given the claim statement and a numbered list of posts, classify EACH post's stance as \
exactly one of "supporting", "opposing", or "neutral" (same definitions as single-post \
classification). Return the stances list in EXACTLY the same order as the numbered \
posts - one stance per post, same count as posts given.
"""

HARM_CLASSIFICATION_SYSTEM_PROMPT = """\
You are assessing the potential real-world harm of a false or misleading claim for a \
city government risk-triage system, BEFORE a human reviewer confirms your assessment. \
Every score must be defensible to a non-technical policy reviewer, so anchor your score \
to the band descriptions below rather than an impressionistic 0-100 guess.

If live hazard-context grounding is given below the claim, use it: an alleged hazard \
that contradicts an active BMKG forecast/warning (e.g. a false flood/storm claim \
when none is forecast) supports a HIGHER public_safety score than the same claim \
with no grounding available; a claim that merely restates a genuinely active \
warning is not itself false and should not be scored as if it were. Absence of \
grounding context is not evidence either way - score from the claim text alone.

Score each dimension 0-100, using this rubric (PRD v1.3.1 Section 6.2.4):

public_safety - risk of physical harm, panic, or dangerous behavior:
  0-20    No physical safety implication. Purely informational/opinion-based, no
          actionable instruction, warning, or safety-related assertion.
  21-40   Minimal. References a safety-adjacent topic (infrastructure, health,
          environment) but suggests no specific danger, action, or behavior change.
  41-60   Moderate. Could plausibly influence individual behavior into minor risk
          (e.g. discourages use of a safe facility) but no imminent/specific hazard.
  61-80   High. Contains a specific false warning, alleged hazard, or instruction that
          could lead people to take a risky action (false contamination warning, fake
          evacuation advisory, incorrect emergency guidance) without immediate
          life-threatening urgency.
  81-100  Severe. Directly incites panic, dangerous mass behavior, or physical
          confrontation, or spreads false emergency/life-safety information (fake
          disaster alert, incitement to violence, dangerous health misinformation
          with immediate risk of harm).

institutional_trust - erosion of public trust in government competence/intent:
  0-20    No implication. Does not reference government competence, integrity, intent.
  21-40   Minimal. Mentions a government body/official neutrally, no alleged
          wrongdoing, incompetence, or bad faith.
  41-60   Moderate. Alleges a specific instance of error, inefficiency, or poor
          judgment, but does not generalize to intent, corruption, or systemic failure.
  61-80   High. Alleges deliberate deception, hidden agenda, or corruption by a
          specific body/official, likely to reduce confidence in that institution.
  81-100  Severe. Asserts broad, systemic conspiracy, malicious intent, or fundamental
          illegitimacy (e.g. "the government is lying to control/harm citizens"),
          able to erode trust across multiple institutions or government as a whole.

economic - potential financial harm:
  0-20    No implication. No plausible connection to financial behavior, markets,
          property, or commerce.
  21-40   Minimal. References an economic topic (prices, jobs, business) without
          asserting a specific negative financial outcome or urging economic action.
  41-60   Moderate. Could influence localized/small-scale financial behavior (e.g.
          discourage patronage of one business) without broad market effect.
  61-80   High. Could plausibly trigger a meaningful financial reaction (panic
          selling, sector boycott, localized property-value drop) for a community or
          industry segment.
  81-100  Severe. Could plausibly trigger large-scale economic harm (market-wide
          panic, city-wide devaluation, mass business closures, capital flight).

policy_disruption - how much the claim undermines a specific active policy rollout. \
Score this conservatively and narrowly - only the claim's effect on policy execution, \
NOT general criticism or disagreement with the policy itself. Ordinary criticism is not \
automatically high on this dimension; a government tool must not treat disagreement \
with its own policy as "harm":
  0-20    No policy implication. Unrelated to any active/upcoming government policy.
  21-40   Minimal. References a policy topic in passing, doesn't question or oppose
          a specific active policy.
  41-60   Moderate. Expresses disagreement/skepticism toward a specific active
          policy, but framed as opinion/debate rather than a false factual assertion.
  61-80   High. Makes a specific false factual assertion about an active policy's
          mechanics, timeline, or effects, likely to create confusion that could
          measurably slow or complicate implementation.
  81-100  Severe. Makes a specific false assertion likely to cause direct,
          large-scale non-compliance, organized resistance, or abandonment of a
          policy before it can be evaluated on its merits.
"""

DEBUNK_CONTENT_SYSTEM_PROMPT = """\
You are drafting the Debunk Activity for a claim in a civic communications team's Claim \
Repository Bank, using the neuroscience-backed "Truth Sandwich" structure: Core Fact -> \
Brief Neutral Misinformation Flag -> Re-stated Verified Fact.

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

DEBUNK_SEGMENTS_SYSTEM_PROMPT = """\
You are identifying the audience segments most exposed to a false or misleading claim, \
and drafting a tailored corrective message for each one, for a city government's Claim \
Repository Bank (PRD v1.5 US12) - this REPLACES a single generic Debunk draft with one \
draft per segment.

Given the claim being debunked, grounded reference/policy context, and a sample of the \
actual posts spreading it (used only to infer who is engaging - never quote them back \
verbatim), do the following:

1. Identify 1-5 distinct audience segments most exposed to or affected by this claim, \
   inferred from the demographic, interest, or community pattern visible across the \
   sample posts (e.g. "Commuters", "Small business owners near the site", "Parents of \
   school-age children"). Do not invent a segment the sample gives no basis for - if the \
   posts show no real variation in who is engaging, one broad segment is the correct \
   answer, not several contrived ones.
2. Order segments most-exposed-first (roughly, by how much of the sample they represent).
3. For each segment, write:
   - segment_name: a short (2-5 word) label for the card (e.g. "Commuters").
   - segment_rationale: one short sentence on why this segment is exposed and what \
     specific framing or concern makes the claim land differently for them.
   - content: a tailored corrective message addressing THIS segment's specific framing/ \
     concern, following the same Truth Sandwich discipline as a single debunk - lead with \
     the true, verified fact; briefly and neutrally note that a false claim is circulating \
     WITHOUT repeating its specific wording; end by restating the true fact. Ground every \
     factual statement ONLY in the provided context. Keep tone calm, neutral, and \
     non-condescending, and make each draft genuinely speak to that segment's concern - \
     if the segments are real, the drafts should read differently from each other, not \
     restate the same paragraph with the segment name swapped in.
"""

NON_EXISTING_CLAIM_PREDICTION_SYSTEM_PROMPT = """\
You are a strategic communications analyst helping a city government anticipate \
misinformation before it spreads, ahead of an upcoming climate/urban policy announcement. \
This claim has NOT appeared in public discourse yet - you are predicting what is likely \
to emerge, for the Claim Repository Bank's Non-Existing (predicted) claims queue.

Given a description of the policy and relevant grounding context (past grievances, \
fault lines, similar precedents), produce:
- claim_statement: ONE neutral, declarative sentence stating the specific false or \
  misleading claim you predict is most likely to emerge and circulate about this policy.
- topic_label: a short (2-5 word) label for the broader subject-matter category this \
  predicted claim belongs to, matching the style used for existing claims' topics.
- predicted_attack_angle: the single most likely misinformation/disinformation attack \
  angle opponents or bad-faith actors will use against this policy.
- likely_framing: the emotional/rhetorical framing they will likely use (e.g. "government \
  overreach", "hidden tax", "elite ignoring working class").
- inoculation_explainer: a short, plain-language explainer (2-4 sentences) that can be \
  published BEFORE the attack spreads, to pre-bunk it: grounded strictly in the provided \
  policy context, calm in tone, and addressing the likely concern head-on without \
  repeating inflammatory false framing. This becomes the claim's Prebunk Activity - the \
  actual publishable content.
"""

SYNTHETIC_POSTS_SYSTEM_PROMPT = """\
You are simulating realistic public social/forum posts for a Jakarta civic \
misinformation-monitoring prototype. Live crawling is not wired up yet for this demo - \
you are generating plausible synthetic posts that stand in for what a real crawler would \
have ingested, so the pipeline has realistic data to work with end-to-end.

Generate a diverse batch of short posts (1-3 sentences each) about Jakarta urban/climate \
policy topics (e.g. ERP road pricing, MRT construction, flood control / Ciliwung \
normalization, waste management, tree removal, land subsidence, coastal reclamation), \
written the way real Jakarta residents would actually post.

Write every post in English, even though real Jakarta residents would often write in \
Bahasa Indonesia - the embedding model powering this system is English-only, so non-English \
text degrades clustering/matching quality. Keep real Jakarta place/policy names as-is.

Mix the posts realistically across the batch:
- Some posts spread an unverified or exaggerated claim as fact.
- Some posts express a genuine, calm concern or question.
- Some posts push back on, correct, or mock another claim or rumor.
- Some posts are neutral commentary or observations.

For each post, invent:
- text: the post content itself (casual tone, may include minor typos/slang, no hashtag \
  spam, no markdown).
- source: which platform it plausibly came from ("social", "forum", "rss", or "radio" \
  for a transcript excerpt - use "other" only if nothing else fits).
- author_id: a plausible fake handle/username (never a real person's name).
- location: a real Jakarta neighborhood or landmark relevant to the post's topic, or null \
  if not applicable.

If grounding context (existing community fault lines and/or active topics) is provided, \
lean some - not all - of the posts toward continuing those specific threads for realistic \
continuity; the rest should range freely across other plausible Jakarta topics.

Return exactly the requested number of posts.
"""

POLICY_CLAIM_MATCH_SYSTEM_PROMPT = """\
You are deciding, for a city government's Claim Repository Bank, which already-tracked
misinformation claims are genuinely ABOUT a specific public policy - not merely adjacent
in topic.

Given a policy's title/description and a numbered list of candidate claim statements,
return one boolean per claim, in the exact same order: true only if the claim is
specifically about this policy (would a reasonable policy owner say "yes, this claim is
about my policy"?), false if it is about a different, merely topically-related policy or
subject. Err toward false when genuinely unsure - a missed link is far less costly than
a wrong one polluting the policy's claim list.
"""


class LLMClient:
    """Async OpenAI Responses API wrapper with strict structured JSON output. Client
    construction is lazy - a missing key only raises on the first real call."""

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
                # temperature omitted - reasoning-tier models reject it.
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
        """Ingestion-time analysis: outrage, moral foundation, extracted claim/grievance."""
        prompt = f"Content to analyze:\n\n{text}"
        return await self._generate_structured(
            prompt=prompt,
            system_instruction=CONTENT_ANALYSIS_SYSTEM_PROMPT,
            schema=ContentAnalysisSchema,
        )

    async def summarize_claim(
        self, sample_texts: list[str], existing_topic_names: list[str] | None = None
    ) -> ClaimSummarySchema:
        """Synthesize a claim_statement + candidate topic_label from a new cluster.
        existing_topic_names lets the LLM reuse an exact existing label instead of
        inventing a differently-worded near-duplicate for the same subject."""
        joined = "\n---\n".join(sample_texts[:20])
        prompt = f"Sample posts from this cluster:\n\n{joined}"
        if existing_topic_names:
            topics_list = "\n".join(f"- {name}" for name in existing_topic_names)
            prompt += f"\n\nAlready-existing topic labels (reuse one verbatim if it fits):\n{topics_list}"
        return await self._generate_structured(
            prompt=prompt,
            system_instruction=CLAIM_SUMMARY_SYSTEM_PROMPT,
            schema=ClaimSummarySchema,
        )

    async def generate_network_label(
        self, claim_statement: str, sample_texts: list[str]
    ) -> str:
        """Short neutral label for a detected coordinated network (PRD 10.5.5/10.6 -
        coordinated_network.label). See NETWORK_LABEL_SYSTEM_PROMPT for the
        governance constraint this must respect (never assert automation/bad-faith/
        identity, PRD 10.9.1)."""
        joined = "\n---\n".join(sample_texts[:10])
        prompt = f"Claim being supported:\n{claim_statement}\n\nSample posts:\n{joined}"
        result = await self._generate_structured(
            prompt=prompt,
            system_instruction=NETWORK_LABEL_SYSTEM_PROMPT,
            schema=NetworkLabelSchema,
        )
        return result.label

    async def classify_stance(self, claim_statement: str, post_text: str) -> Stance:
        """Classify a single post's stance toward an already-known claim."""
        prompt = f"Claim statement:\n{claim_statement}\n\nPost:\n{post_text}"
        result = await self._generate_structured(
            prompt=prompt,
            system_instruction=STANCE_SYSTEM_PROMPT,
            schema=StanceSchema,
        )
        return result.stance

    async def classify_stances_batch(
        self, claim_statement: str, texts: list[str]
    ) -> list[Stance]:
        """Batch stance classification for a new cluster. Raises StanceCountMismatchError
        on a count mismatch."""
        numbered = "\n".join(f"{i + 1}. {text}" for i, text in enumerate(texts))
        prompt = f"Claim statement:\n{claim_statement}\n\nPosts:\n{numbered}"
        result = await self._generate_structured(
            prompt=prompt,
            system_instruction=STANCE_BATCH_SYSTEM_PROMPT,
            schema=StanceBatchSchema,
        )
        if len(result.stances) != len(texts):
            raise StanceCountMismatchError(
                f"Expected {len(texts)} stances, got {len(result.stances)}"
            )
        return result.stances

    async def classify_harm(
        self,
        claim_statement: str,
        sample_supporting_texts: list[str],
        hazard_context: str = "",
    ) -> HarmClassificationSchema:
        """AI-classify the 4 Harm Severity sub-components. hazard_context is an
        optional live-grounding block (e.g. active BMKG forecasts - see
        app/services/hazard_context_service.py) - omitted entirely from the prompt
        when empty, never padded with a placeholder."""
        joined = "\n---\n".join(sample_supporting_texts[:10])
        prompt = f"Claim statement:\n{claim_statement}\n\nSample supporting posts:\n{joined}"
        if hazard_context:
            prompt += f"\n\nLive hazard-context grounding:\n{hazard_context}"
        return await self._generate_structured(
            prompt=prompt,
            system_instruction=HARM_CLASSIFICATION_SYSTEM_PROMPT,
            schema=HarmClassificationSchema,
        )

    async def generate_debunk(
        self, claim_statement: str, grounding_context: str
    ) -> DebunkContentSchema:
        """Draft the structured Truth Sandwich for an Existing claim's Debunk Activity."""
        prompt = (
            f"Claim being debunked (for context only - do not quote verbatim in the flag):\n"
            f"{claim_statement}\n\n"
            f"Grounded reference/policy context:\n{grounding_context or 'None provided.'}"
        )
        return await self._generate_structured(
            prompt=prompt,
            system_instruction=DEBUNK_CONTENT_SYSTEM_PROMPT,
            schema=DebunkContentSchema,
        )

    async def generate_debunk_segments(
        self, claim_statement: str, grounding_context: str, sample_texts: list[str]
    ) -> list[DebunkSegmentSchema]:
        """PRD v1.5 US12 - one tailored debunk draft per audience segment, inferred from
        a sample of the claim's Supporting-side posts."""
        joined = "\n---\n".join(sample_texts[:10])
        prompt = (
            f"Claim being debunked (for context only - do not quote verbatim in any "
            f"segment's flag):\n{claim_statement}\n\n"
            f"Grounded reference/policy context:\n{grounding_context or 'None provided.'}\n\n"
            f"Sample posts spreading the claim (for inferring audience segments only):\n"
            f"{joined or 'None provided.'}"
        )
        result = await self._generate_structured(
            prompt=prompt,
            system_instruction=DEBUNK_SEGMENTS_SYSTEM_PROMPT,
            schema=DebunkSegmentBatchSchema,
        )
        return result.segments

    async def predict_non_existing_claim(
        self, policy_title: str, policy_description: str, grounding_context: str
    ) -> NonExistingClaimPredictionSchema:
        """Predict a Non-Existing claim ahead of a policy announcement."""
        prompt = (
            f"Policy title: {policy_title}\n"
            f"Policy description:\n{policy_description}\n\n"
            f"Grounding context (fault lines / precedents):\n{grounding_context or 'None provided.'}"
        )
        return await self._generate_structured(
            prompt=prompt,
            system_instruction=NON_EXISTING_CLAIM_PREDICTION_SYSTEM_PROMPT,
            schema=NonExistingClaimPredictionSchema,
        )

    async def generate_synthetic_posts(
        self, count: int, topic_hint: str | None, grounding_context: str
    ) -> list[SyntheticPostSchema]:
        """Fabricate `count` realistic posts, standing in for a not-yet-wired-up live crawler."""
        prompt = (
            f"Number of posts to generate: {count}\n"
            f"Topic focus (optional steer, blank = your judgement across realistic "
            f"Jakarta urban/climate topics): {topic_hint or 'None - use your judgement'}\n\n"
            f"Grounding context (existing community fault lines / active topics, for "
            f"continuity - optional):\n{grounding_context or 'None provided.'}"
        )
        result = await self._generate_structured(
            prompt=prompt,
            system_instruction=SYNTHETIC_POSTS_SYSTEM_PROMPT,
            schema=SyntheticPostBatchSchema,
        )
        return result.posts

    async def confirm_policy_claim_matches(
        self, policy_title: str, policy_description: str, candidate_claim_statements: list[str]
    ) -> list[bool]:
        """Confirms which candidate claims are genuinely about `policy_title`. Raises
        ValueError on a count mismatch."""
        numbered = "\n".join(
            f"{i + 1}. {statement}" for i, statement in enumerate(candidate_claim_statements)
        )
        prompt = (
            f"Policy title: {policy_title}\n"
            f"Policy description:\n{policy_description or 'None provided.'}\n\n"
            f"Candidate claims:\n{numbered}"
        )
        result = await self._generate_structured(
            prompt=prompt,
            system_instruction=POLICY_CLAIM_MATCH_SYSTEM_PROMPT,
            schema=PolicyClaimMatchBatchSchema,
        )
        if len(result.matches) != len(candidate_claim_statements):
            raise ValueError(
                f"Expected {len(candidate_claim_statements)} match booleans, "
                f"got {len(result.matches)}"
            )
        return result.matches


@lru_cache
def get_llm_client() -> LLMClient:
    return LLMClient()
