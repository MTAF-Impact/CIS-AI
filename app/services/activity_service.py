"""Generates and caches each claim's Debunk/Prebunk Activity content - once only."""

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim import Claim
from app.models.debunk_segment import ClaimDebunkSegment
from app.services import config_service, rag_service
from app.services.config_service import RuntimeConfig
from app.services.embedding_service import EmbeddingService
from app.services.llm_client import LLMClient

logger = logging.getLogger(__name__)


async def generate_and_cache_debunk_activity(
    db: AsyncSession,
    claim: Claim,
    llm: LLMClient,
    embedder: EmbeddingService,
    supporting_texts: list[str] | None = None,
    config: RuntimeConfig | None = None,
) -> None:
    """Debunk Activity for an Existing claim - the Truth Sandwich, once only. Also
    generates the per-audience-segment drafts from the same Supporting-side sample,
    cached the same way and guarded by the same early return."""
    if claim.activity_content is not None:
        return

    config = config or await config_service.get_config(db)

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

    await _generate_debunk_segments(
        db, claim, llm, grounding_context, supporting_texts, config.debunk_segment_max_count
    )


async def _generate_debunk_segments(
    db: AsyncSession,
    claim: Claim,
    llm: LLMClient,
    grounding_context: str,
    supporting_texts: list[str] | None,
    max_count: int,
) -> None:
    """Failure here never blocks the generic draft above, which the frontend
    renders as the fallback when this table is empty."""
    sample = supporting_texts or [claim.claim_statement]
    try:
        segments = await llm.generate_debunk_segments(
            claim.claim_statement, grounding_context, sample[:10]
        )
    except Exception:
        logger.exception("Debunk segmentation failed; falling back to the single generic draft")
        return

    # Dedupe on segment_name before insert: claim_debunk_segments has a
    # UNIQUE(claim_id, segment_name) constraint and the LLM doesn't always return
    # distinct names. Dedupe before capping to max_count so a dropped duplicate
    # doesn't itself eat into the cap.
    seen_names: set[str] = set()
    deduped = []
    for segment in segments:
        if segment.segment_name in seen_names:
            logger.warning(
                "Dropping duplicate debunk segment name %r for claim %s",
                segment.segment_name,
                claim.id,
            )
            continue
        seen_names.add(segment.segment_name)
        deduped.append(segment)

    for rank, segment in enumerate(deduped[:max_count]):
        db.add(
            ClaimDebunkSegment(
                claim_id=claim.id,
                segment_name=segment.segment_name,
                segment_rationale=segment.segment_rationale,
                content=segment.content,
                rank=rank,
            )
        )


def render_prebunk_activity(inoculation_explainer: str) -> str:
    """Prebunk Activity for a Non-Existing claim - just the inoculation explainer."""
    return inoculation_explainer
