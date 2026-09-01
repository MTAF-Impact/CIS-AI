"""Generates and caches each claim's Debunk/Prebunk Activity content - once only."""

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim import Claim
from app.models.debunk_segment import ClaimDebunkSegment
from app.services import rag_service
from app.services.embedding_service import EmbeddingService
from app.services.llm_client import LLMClient

logger = logging.getLogger(__name__)


async def generate_and_cache_debunk_activity(
    db: AsyncSession,
    claim: Claim,
    llm: LLMClient,
    embedder: EmbeddingService,
    supporting_texts: list[str] | None = None,
) -> None:
    """Debunk Activity for an Existing claim - the Truth Sandwich, once only. Also
    generates the per-audience-segment drafts (PRD v1.5 US12) from the same
    Supporting-side sample, cached the same way and guarded by the same early return."""
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

    await _generate_debunk_segments(db, claim, llm, grounding_context, supporting_texts)


async def _generate_debunk_segments(
    db: AsyncSession,
    claim: Claim,
    llm: LLMClient,
    grounding_context: str,
    supporting_texts: list[str] | None,
) -> None:
    """PRD v1.5 US12. Failure here never blocks the generic draft above, which the
    backend documents as the fallback the FE renders when this table is empty."""
    sample = supporting_texts or [claim.claim_statement]
    try:
        segments = await llm.generate_debunk_segments(
            claim.claim_statement, grounding_context, sample[:10]
        )
    except Exception:
        logger.exception("Debunk segmentation failed; falling back to the single generic draft")
        return

    # Dedupe on segment_name before insert: `claim_debunk_segments` has a
    # UNIQUE(claim_id, segment_name) constraint, and the prompt's "distinct
    # segments" instruction is not a hard guarantee. Without this, a repeated
    # name from a single LLM response would only surface as an IntegrityError at
    # the caller's eventual commit - which, from cluster_unclustered_content's
    # Pass 2, is one transaction shared by every claim created in that run. A
    # naming collision on one claim's segments would then roll back all of them.
    seen_names: set[str] = set()
    rank = 0
    for segment in segments:
        if segment.segment_name in seen_names:
            logger.warning(
                "Dropping duplicate debunk segment name %r for claim %s",
                segment.segment_name,
                claim.id,
            )
            continue
        seen_names.add(segment.segment_name)
        db.add(
            ClaimDebunkSegment(
                claim_id=claim.id,
                segment_name=segment.segment_name,
                segment_rationale=segment.segment_rationale,
                content=segment.content,
                rank=rank,
            )
        )
        rank += 1


def render_prebunk_activity(inoculation_explainer: str) -> str:
    """Prebunk Activity for a Non-Existing claim - just the inoculation explainer."""
    return inoculation_explainer
