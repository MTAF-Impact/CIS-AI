"""Renders and caches the single AI-generated Activity content block per claim - folds in
the old InterventionResponse entirely (Claim.status itself is the review-state lifecycle
the PRD wants, so there's no separate approve/reject workflow to maintain here).

Generation is eager and one-time: every function here is idempotent and never re-calls
the LLM once activity_content is already set, per the PRD's "generated ONCE at claim
creation" requirement.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim import Claim
from app.services import rag_service
from app.services.embedding_service import EmbeddingService
from app.services.llm_client import LLMClient

logger = logging.getLogger(__name__)


async def generate_and_cache_debunk_activity(
    db: AsyncSession, claim: Claim, llm: LLMClient, embedder: EmbeddingService
) -> None:
    """Debunk Activity for an EXISTING claim: the LLM still returns a structured
    Truth Sandwich (core_fact/nuanced_flag/reiterated_fact) for generation quality, but
    only the rendered concatenation is persisted - the PRD wants one copyable block,
    not separate sub-fields."""
    if claim.activity_content is not None:
        return

    fault_lines = await rag_service.retrieve_relevant_fault_lines(
        db, claim.claim_statement, embedder
    )
    grounding_context = rag_service.build_grounding_context(fault_lines)

    try:
        content = await llm.generate_debunk(claim.claim_statement, grounding_context)
    except Exception:
        logger.exception("Debunk activity generation failed; leaving activity_content unset")
        return

    claim.activity_content = (
        f"{content.core_fact} {content.nuanced_flag} {content.reiterated_fact}"
    )
    claim.debunk_core_fact = content.core_fact
    claim.debunk_nuanced_flag = content.nuanced_flag
    claim.debunk_reiterated_fact = content.reiterated_fact
    claim.activity_generated_at = datetime.now(UTC)


def render_prebunk_activity(inoculation_explainer: str) -> str:
    """Prebunk Activity for a NON_EXISTING claim is just the inoculation explainer
    itself - predicted_attack_angle/likely_framing are useful LLM reasoning context
    (returned to the caller separately) but are not part of the publishable content."""
    return inoculation_explainer
