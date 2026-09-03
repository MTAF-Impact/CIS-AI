"""Admin Setting Page: the global Over/Under Threshold config and the "Generate
Generic Claim" demo utility."""

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_setting import SINGLETON_ID, AdminSetting
from app.models.claim import Claim
from app.models.policy import ClaimPolicy, Policy
from app.schemas.content import ContentItemCreate
from app.services import clustering_service
from app.services.content_ingestion_service import (
    analyze_and_build_item,
    build_grounding_context,
)
from app.services.embedding_service import EmbeddingService
from app.services.llm_client import LLMClient

# Enough for a realistic mixed-stance demo claim without being slow to generate.
DEMO_CLAIM_POST_COUNT = 6


async def get_settings(db: AsyncSession) -> AdminSetting:
    settings = await db.get(AdminSetting, SINGLETON_ID)
    if settings is None:
        settings = AdminSetting(id=SINGLETON_ID)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


async def set_threshold(db: AsyncSession, over_threshold: float) -> AdminSetting:
    settings = await get_settings(db)
    settings.over_threshold = over_threshold
    await db.commit()
    await db.refresh(settings)
    return settings


async def _link_to_related_policy(db: AsyncSession, claim: Claim) -> None:
    """C1 (backend integration ask) - the F1 detail page's Related Policies panel was
    empty for every demo claim because nothing ever created a claim_policies row.
    Links to the nearest-by-embedding Policy when any Policy has one; else the most
    recently created Policy; no-ops if no Policy exists at all - nothing to link to."""
    if claim.embedding is not None:
        candidates = (
            await db.execute(select(Policy).where(Policy.embedding.is_not(None)))
        ).scalars().all()
        if candidates:
            claim_vec = np.asarray(claim.embedding)
            nearest = max(
                candidates, key=lambda p: float(np.dot(claim_vec, np.asarray(p.embedding)))
            )
            db.add(ClaimPolicy(claim_id=claim.id, policy_id=nearest.id))
            return

    most_recent = (
        await db.execute(select(Policy).order_by(Policy.created_at.desc()).limit(1))
    ).scalar_one_or_none()
    if most_recent is not None:
        db.add(ClaimPolicy(claim_id=claim.id, policy_id=most_recent.id))


async def generate_demo_existing_claim(
    db: AsyncSession,
    llm: LLMClient,
    embedder: EmbeddingService,
    topic_hint: str | None = None,
) -> Claim:
    """One-click sample Existing claim via the same construction pipeline HDBSCAN uses."""
    grounding_context = await build_grounding_context(db)
    posts = await llm.generate_synthetic_posts(DEMO_CLAIM_POST_COUNT, topic_hint, grounding_context)

    items = []
    for post in posts:
        entry = ContentItemCreate(
            text=post.text, source=post.source, author_id=post.author_id, location=post.location
        )
        items.append(await analyze_and_build_item(entry, llm, embedder))
    db.add_all(items)
    await db.flush()

    claim = await clustering_service.build_claim_from_content_items(db, items, llm, embedder)

    for touched_claim in await clustering_service.renormalize_topic_reach(db, claim.topic_id):
        await clustering_service.rescore_claim(db, touched_claim)

    await _link_to_related_policy(db, claim)

    await db.commit()
    await db.refresh(claim)
    return claim
