"""Predicts NON_EXISTING claims for a policy (PRD Section 3.3 / D2, and the F2 AI
matchmaking pipeline's US42(b)). NON_EXISTING claims have no content by construction and
are never scored - see app.models.claim.Claim's claim_type docstring for the
fixed-by-pipeline-of-origin reasoning."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim import Claim
from app.models.enums import ClaimStatus, ClaimType
from app.models.policy import Policy
from app.services import rag_service
from app.services.activity_service import render_prebunk_activity
from app.services.clustering_service import assign_or_create_topic
from app.services.embedding_service import EmbeddingService
from app.services.llm_client import LLMClient


@dataclass
class NonExistingClaimPrediction:
    claim: Claim
    predicted_attack_angle: str
    likely_framing: str


async def predict_non_existing_claim(
    db: AsyncSession,
    policy: Policy,
    llm: LLMClient,
    embedder: EmbeddingService,
    already_covered_claim_statements: list[str] | None = None,
) -> NonExistingClaimPrediction:
    """Predicts one new NON_EXISTING claim for `policy`. If
    `already_covered_claim_statements` is given (the Existing claims the matchmaking
    pipeline already confirmed-matched to this policy), the LLM is asked to predict a
    claim distinct from those, rather than duplicating an aspect already covered."""
    fault_lines = await rag_service.retrieve_relevant_fault_lines(
        db, f"{policy.title} {policy.description or ''}", embedder
    )
    grounding_context = rag_service.build_grounding_context(fault_lines)
    if already_covered_claim_statements:
        covered = "\n".join(f"- {s}" for s in already_covered_claim_statements)
        grounding_context = (
            f"{grounding_context}\n\nClaims already matched to this policy (predict "
            f"something NOT already covered by these):\n{covered}"
        )

    prediction = await llm.predict_non_existing_claim(
        policy.title, policy.description or "", grounding_context
    )

    claim_embedding = embedder.embed(prediction.claim_statement)
    topic = await assign_or_create_topic(db, claim_embedding, prediction.topic_label)

    now = datetime.now(UTC)
    claim = Claim(
        claim_type=ClaimType.NON_EXISTING,
        claim_statement=prediction.claim_statement,
        topic_id=topic.id,
        status=ClaimStatus.UNREVIEWED,
        policy_id=policy.id,
        embedding=claim_embedding,
        first_caught_at=now,  # no content to derive this from - use prediction time
        activity_content=render_prebunk_activity(prediction.inoculation_explainer),
        activity_generated_at=now,
    )
    db.add(claim)
    await db.flush()

    return NonExistingClaimPrediction(
        claim=claim,
        predicted_attack_angle=prediction.predicted_attack_angle,
        likely_framing=prediction.likely_framing,
    )
