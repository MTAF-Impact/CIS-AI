"""Generates and caches each claim's Debunk/Prebunk Activity content - once only."""

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
    """Debunk Activity for an Existing claim - the Truth Sandwich, once only."""
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
    """Prebunk Activity for a Non-Existing claim - just the inoculation explainer."""
    return inoculation_explainer
