"""Predicts NON_EXISTING claims ahead of a policy announcement (PRD Section 3.3 / D2).
NON_EXISTING claims have no content by construction and are never scored - see
app.models.claim.Claim's claim_type docstring for the fixed-by-pipeline-of-origin
reasoning."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
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


async def _find_or_create_policy(db: AsyncSession, title: str, description: str) -> Policy:
    """F2 (Public Policy Bank) is out of scope - this is the minimal bridge until it
    exists: find an existing Policy by exact title, or create one inline."""
    policy = (
        await db.execute(select(Policy).where(Policy.title == title))
    ).scalar_one_or_none()
    if policy is not None:
        return policy

    policy = Policy(title=title, description=description)
    db.add(policy)
    await db.flush()
    return policy


async def predict_non_existing_claim(
    db: AsyncSession,
    policy_title: str,
    policy_description: str,
    llm: LLMClient,
    embedder: EmbeddingService,
) -> NonExistingClaimPrediction:
    policy = await _find_or_create_policy(db, policy_title, policy_description)

    fault_lines = await rag_service.retrieve_relevant_fault_lines(
        db, f"{policy_title} {policy_description}", embedder
    )
    grounding_context = rag_service.build_grounding_context(fault_lines)

    prediction = await llm.predict_non_existing_claim(
        policy_title, policy_description, grounding_context
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
    await db.commit()
    await db.refresh(claim)

    return NonExistingClaimPrediction(
        claim=claim,
        predicted_attack_angle=prediction.predicted_attack_angle,
        likely_framing=prediction.likely_framing,
    )
