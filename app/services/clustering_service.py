"""Claim clustering: attach-or-create pipeline, topic assignment, stance, and scoring."""

import logging
import statistics
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import hdbscan
import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import get_session_factory
from app.models.alert import ClaimScoreSnapshot
from app.models.claim import Claim
from app.models.content import ContentItem
from app.models.enums import ClaimStatus, ClaimType, Stance
from app.models.topic import Topic
from app.models.topic_volume_bucket import TopicVolumeBucket
from app.services import (
    config_service,
    falseness_service,
    hazard_context_service,
    scoring_engine,
)
from app.services.activity_service import generate_and_cache_debunk_activity
from app.services.config_service import RuntimeConfig
from app.services.embedding_service import EmbeddingService, get_embedding_service
from app.services.llm_client import LLMClient, get_llm_client

logger = logging.getLogger(__name__)

MIN_CLUSTER_SIZE = 2
MAX_SAMPLE_TEXTS_FOR_SUMMARY = 10


@dataclass
class ClusterResult:
    claims_created: int
    claims_updated: int
    content_items_clustered: int


def _centroid(vectors: list[list[float]]) -> np.ndarray:
    array = np.array(vectors, dtype=float)
    centroid = array.mean(axis=0)
    norm = np.linalg.norm(centroid)
    return centroid / norm if norm > 0 else centroid


async def assign_or_create_topic(
    db: AsyncSession,
    claim_embedding: list[float],
    candidate_label: str,
    topic_attach_threshold: float = scoring_engine.TOPIC_ATTACH_THRESHOLD,
) -> Topic:
    """Attach to the nearest topic by centroid similarity, or create a new one.

    An exact (case-insensitive) name match always wins over the embedding threshold:
    the LLM is now shown existing topic names and asked to reuse one verbatim when it
    fits, so if it did reuse a name, that is a stronger signal than a centroid score
    that happened to fall just under topic_attach_threshold - without this, two claims
    the LLM itself labeled identically could still end up as two different topic rows
    with the same name."""
    existing_topics = list(
        (await db.execute(select(Topic).where(Topic.embedding.is_not(None)))).scalars().all()
    )

    normalized_label = candidate_label.strip().lower()
    name_match = next(
        (t for t in existing_topics if t.name.strip().lower() == normalized_label), None
    )
    if name_match is not None:
        sibling_embeddings = list(
            (
                await db.execute(
                    select(Claim.embedding).where(
                        Claim.topic_id == name_match.id, Claim.embedding.is_not(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        name_match.embedding = _centroid([*sibling_embeddings, claim_embedding]).tolist()
        return name_match

    claim_vec = np.asarray(claim_embedding)

    best_topic, best_score = None, -1.0
    for topic in existing_topics:
        score = float(np.dot(claim_vec, np.asarray(topic.embedding)))
        if score > best_score:
            best_topic, best_score = topic, score

    if best_topic is not None and best_score >= topic_attach_threshold:
        sibling_embeddings = list(
            (
                await db.execute(
                    select(Claim.embedding).where(
                        Claim.topic_id == best_topic.id, Claim.embedding.is_not(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        best_topic.embedding = _centroid([*sibling_embeddings, claim_embedding]).tolist()
        return best_topic

    new_topic = Topic(name=candidate_label, embedding=claim_embedding)
    db.add(new_topic)
    await db.flush()
    return new_topic


async def _increment_topic_volume_bucket(
    db: AsyncSession, topic_id: uuid.UUID, when: datetime
) -> None:
    bucket_start = when.replace(minute=0, second=0, microsecond=0)
    stmt = select(TopicVolumeBucket).where(
        TopicVolumeBucket.topic_id == topic_id, TopicVolumeBucket.bucket_start == bucket_start
    )
    bucket = (await db.execute(stmt)).scalar_one_or_none()
    if bucket is None:
        bucket = TopicVolumeBucket(topic_id=topic_id, bucket_start=bucket_start, supporting_volume=0)
        db.add(bucket)
        await db.flush()
    bucket.supporting_volume += 1


async def _reach_inputs(
    db: AsyncSession, claim_id: uuid.UUID, window_days: int
) -> tuple[int, int, int, int]:
    """Reach's inputs, scoped to a trailing window (AP-10) so the baseline follows the
    recent past rather than the whole history of the deployment - a claim's Reach score
    is about its current standing, not a lifetime-cumulative count."""
    window_start = datetime.now(UTC) - timedelta(days=window_days)
    stmt = select(
        func.coalesce(func.sum(ContentItem.impressions), 0),
        func.count(func.distinct(ContentItem.author_id)),
        func.count(ContentItem.id),
        func.count(func.distinct(ContentItem.source)),
    ).where(
        ContentItem.claim_id == claim_id,
        ContentItem.stance == Stance.SUPPORTING,
        ContentItem.created_at >= window_start,
    )
    impressions, unique_authors, content_count, distinct_platforms = (await db.execute(stmt)).one()
    return impressions, unique_authors, content_count, distinct_platforms


async def renormalize_topic_reach(
    db: AsyncSession, topic_id: uuid.UUID, config: RuntimeConfig | None = None
) -> list[Claim]:
    """Min-max normalize Reach across every claim in a topic; returns the touched claims."""
    claims = list(
        (
            await db.execute(
                select(Claim).where(Claim.topic_id == topic_id, Claim.claim_type == ClaimType.EXISTING)
            )
        )
        .scalars()
        .all()
    )
    if not claims:
        return []

    config = config or await config_service.get_config(db)
    raw_values = {}
    for claim in claims:
        impressions, unique_authors, content_count, distinct_platforms = await _reach_inputs(
            db, claim.id, config.reach_normalization_window_days
        )
        raw_values[claim.id] = scoring_engine.raw_reach(
            impressions, unique_authors, content_count, distinct_platforms, weights=config.reach_weights
        )

    normalized = scoring_engine.normalize_minmax_per_topic(raw_values)
    for claim in claims:
        claim.reach_score = normalized[claim.id]
    return claims


async def _claim_volume_windows(
    db: AsyncSession, claim_id: uuid.UUID, window_hours: float
) -> tuple[int, int]:
    now = datetime.now(UTC)
    window_start = now - timedelta(hours=window_hours)
    prev_window_start = now - timedelta(hours=2 * window_hours)

    volume_t = (
        await db.execute(
            select(func.count())
            .select_from(ContentItem)
            .where(
                ContentItem.claim_id == claim_id,
                ContentItem.stance == Stance.SUPPORTING,
                ContentItem.created_at >= window_start,
            )
        )
    ).scalar_one()
    volume_prev = (
        await db.execute(
            select(func.count())
            .select_from(ContentItem)
            .where(
                ContentItem.claim_id == claim_id,
                ContentItem.stance == Stance.SUPPORTING,
                ContentItem.created_at >= prev_window_start,
                ContentItem.created_at < window_start,
            )
        )
    ).scalar_one()
    return volume_t, volume_prev


async def _topic_velocity_baseline(db: AsyncSession, topic_id: uuid.UUID) -> tuple[float, float]:
    """Baseline growth-rate mean/std for Velocity's z-score; (0, 0) if fewer than 3 buckets."""
    rows = (
        await db.execute(
            select(TopicVolumeBucket.supporting_volume)
            .where(TopicVolumeBucket.topic_id == topic_id)
            .order_by(TopicVolumeBucket.bucket_start)
        )
    ).scalars().all()
    if len(rows) < 3:
        return 0.0, 0.0

    deltas = [scoring_engine.raw_velocity(rows[i], rows[i - 1]) for i in range(1, len(rows))]
    mean = statistics.mean(deltas)
    std = statistics.pstdev(deltas) if len(deltas) > 1 else 0.0
    return mean, std


async def _npr_volumes(
    db: AsyncSession, claim_id: uuid.UUID, window_hours: float
) -> tuple[int, int]:
    window_start = datetime.now(UTC) - timedelta(hours=window_hours)
    rows = (
        await db.execute(
            select(ContentItem.stance, func.count())
            .where(
                ContentItem.claim_id == claim_id,
                ContentItem.created_at >= window_start,
                ContentItem.stance.in_([Stance.SUPPORTING, Stance.OPPOSING]),
            )
            .group_by(ContentItem.stance)
        )
    ).all()
    counts = dict(rows)
    return counts.get(Stance.SUPPORTING, 0), counts.get(Stance.OPPOSING, 0)


async def _emotional_intensity_inputs(
    db: AsyncSession, claim_id: uuid.UUID, stance: Stance
) -> tuple[float, float] | None:
    """(avg_outrage, negative_reaction_ratio) for a stance, or None if no such content yet."""
    row = (
        await db.execute(
            select(
                func.avg(ContentItem.outrage_score),
                func.coalesce(func.sum(ContentItem.negative_reaction_count), 0),
                func.coalesce(func.sum(ContentItem.positive_reaction_count), 0),
                func.count(ContentItem.id),
            ).where(ContentItem.claim_id == claim_id, ContentItem.stance == stance)
        )
    ).one()
    if row[3] == 0:
        return None

    avg_outrage = float(row[0]) if row[0] is not None else 0.0
    negative_sum, positive_sum = row[1], row[2]
    total_reactions = negative_sum + positive_sum
    negative_ratio = (negative_sum / total_reactions) if total_reactions > 0 else 0.0
    return avg_outrage, negative_ratio


async def rescore_claim(db: AsyncSession, claim: Claim, config: RuntimeConfig | None = None) -> None:
    """Recomputes V/F/EI/NPR/discount/final. Reach and Harm are handled elsewhere."""
    config = config or await config_service.get_config(db)

    supporting_vol, opposing_vol = await _npr_volumes(db, claim.id, config.npr_window_hours)
    npr, is_dormant = scoring_engine.compute_npr(supporting_vol, opposing_vol)
    discount = scoring_engine.discount_factor(
        npr,
        supporting_vol + opposing_vol,
        gamma=config.discount_gamma,
        reliability_threshold=config.npr_reliability_minimum_posts,
    )

    volume_t, volume_prev = await _claim_volume_windows(db, claim.id, config.velocity_interval_hours)
    raw_v = scoring_engine.raw_velocity(volume_t, volume_prev, epsilon=config.velocity_epsilon)
    baseline_mean, baseline_std = await _topic_velocity_baseline(db, claim.topic_id)
    velocity = scoring_engine.velocity_zscore(
        raw_v,
        baseline_mean,
        baseline_std,
        z_min=config.velocity_zscore_min,
        z_max=config.velocity_zscore_max,
    )

    falseness = (
        await falseness_service.compute_falseness_score(
            db,
            claim.embedding,
            claim.claim_statement,
            threshold=config.falseness_match_threshold,
            live_match_score=config.falseness_live_match_score,
        )
        if claim.embedding is not None
        else None
    )

    supporting_ei_inputs = await _emotional_intensity_inputs(db, claim.id, Stance.SUPPORTING)
    ei = scoring_engine.emotional_intensity(*supporting_ei_inputs) if supporting_ei_inputs else 0.0

    # Display-only - never fed into scoring.
    opposing_ei_inputs = await _emotional_intensity_inputs(db, claim.id, Stance.OPPOSING)
    ei_opposing = (
        scoring_engine.emotional_intensity(*opposing_ei_inputs) if opposing_ei_inputs else None
    )

    harm = claim.harm_score if claim.harm_score is not None else 0.0
    score = scoring_engine.claim_score(
        claim.reach_score or 0.0, velocity, falseness, harm, ei, weights=config.score_weights
    )
    final = scoring_engine.final_claim_score(score, discount)

    claim.velocity_score = velocity
    claim.falseness_score = falseness
    claim.emotional_intensity_score = ei
    claim.emotional_intensity_opposing = ei_opposing
    claim.npr = npr
    claim.discount_factor = discount
    claim.is_dormant = is_dormant
    claim.claim_score = score
    claim.final_claim_score = final

    db.add(ClaimScoreSnapshot(claim_id=claim.id, final_claim_score=final))


async def rescore_all_existing_claims(db: AsyncSession, config: RuntimeConfig | None = None) -> int:
    """Standalone rescore of every existing claim, independent of clustering."""
    config = config or await config_service.get_config(db)
    topics = list((await db.execute(select(Topic.id))).scalars().all())
    rescored = 0
    for topic_id in topics:
        claims = await renormalize_topic_reach(db, topic_id, config)
        for claim in claims:
            await rescore_claim(db, claim, config)
            rescored += 1
    await db.commit()
    return rescored


async def build_claim_from_content_items(
    db: AsyncSession,
    cluster_items: list[ContentItem],
    llm: LLMClient,
    embedder: EmbeddingService,
    config: RuntimeConfig | None = None,
) -> Claim:
    """Build one new existing claim from a cluster of content items. Shared by Pass 2
    below and admin_service's demo-claim generator."""
    config = config or await config_service.get_config(db)
    sample_texts = [item.text for item in cluster_items[:MAX_SAMPLE_TEXTS_FOR_SUMMARY]]
    existing_topic_names = list(
        (await db.execute(select(Topic.name))).scalars().all()
    )
    try:
        summary = await llm.summarize_claim(sample_texts, existing_topic_names)
        claim_statement, topic_label = summary.claim_statement, summary.topic_label
    except Exception:
        logger.exception("LLM claim summarization failed; using fallback statement")
        claim_statement = sample_texts[0][:500]
        topic_label = "General"

    claim_embedding = embedder.embed(claim_statement)
    topic = await assign_or_create_topic(
        db, claim_embedding, topic_label, topic_attach_threshold=config.topic_attach_threshold
    )

    claim = Claim(
        claim_type=ClaimType.EXISTING,
        claim_statement=claim_statement,
        topic_id=topic.id,
        status=ClaimStatus.UNREVIEWED,
        embedding=claim_embedding,
        first_caught_at=min(item.created_at for item in cluster_items),
    )
    db.add(claim)
    await db.flush()

    try:
        stances = await llm.classify_stances_batch(
            claim_statement, [item.text for item in cluster_items]
        )
    except Exception:
        logger.exception("Batch stance classification failed; falling back per-item")
        stances = [
            await llm.classify_stance(claim_statement, item.text) for item in cluster_items
        ]

    for item, stance in zip(cluster_items, stances, strict=True):
        item.claim_id = claim.id
        item.stance = stance
        if stance == Stance.SUPPORTING:
            await _increment_topic_volume_bucket(db, topic.id, item.created_at)

    supporting_texts = [
        item.text
        for item, stance in zip(cluster_items, stances, strict=True)
        if stance == Stance.SUPPORTING
    ] or sample_texts
    hazard_context = await hazard_context_service.fetch_bmkg_context()
    try:
        harm = await llm.classify_harm(claim_statement, supporting_texts[:10], hazard_context)
    except Exception:
        logger.exception("Harm classification failed; leaving harm fields unset")
    else:
        claim.harm_public_safety = harm.public_safety
        claim.harm_institutional_trust = harm.institutional_trust
        claim.harm_economic = harm.economic
        claim.harm_policy_disruption = harm.policy_disruption
        claim.harm_score = scoring_engine.harm_score(
            harm.public_safety,
            harm.institutional_trust,
            harm.economic,
            harm.policy_disruption,
            weights=config.harm_weights,
        )

    await generate_and_cache_debunk_activity(db, claim, llm, embedder, supporting_texts, config)
    return claim


async def cluster_unclustered_content(
    db: AsyncSession,
    llm: LLMClient | None = None,
    embedder: EmbeddingService | None = None,
    config: RuntimeConfig | None = None,
) -> ClusterResult:
    """Cluster all not-yet-clustered ContentItems into Claims: attach to an existing
    claim where close enough, HDBSCAN the rest into new claims, then rescore every
    touched topic."""
    llm = llm or get_llm_client()
    embedder = embedder or get_embedding_service()
    config = config or await config_service.get_config(db)

    unclustered_items = list(
        (
            await db.execute(
                select(ContentItem).where(
                    ContentItem.claim_id.is_(None), ContentItem.embedding.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    if not unclustered_items:
        return ClusterResult(0, 0, 0)

    touched_topic_ids: set[uuid.UUID] = set()

    # --- Pass 1: attach to existing claims ---
    existing_claims = list(
        (
            await db.execute(
                select(Claim).where(
                    Claim.claim_type == ClaimType.EXISTING, Claim.embedding.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    claim_vecs = {c.id: np.asarray(c.embedding) for c in existing_claims}
    claims_by_id = {c.id: c for c in existing_claims}

    still_unclustered: list[ContentItem] = []
    for item in unclustered_items:
        item_vec = np.asarray(item.embedding)
        best_id, best_score = None, -1.0
        for claim_id, vec in claim_vecs.items():
            score = float(np.dot(item_vec, vec))
            if score > best_score:
                best_id, best_score = claim_id, score

        attached = False
        if best_id is not None and best_score >= config.claim_attach_threshold:
            claim = claims_by_id[best_id]
            try:
                stance = await llm.classify_stance(claim.claim_statement, item.text)
            except Exception:
                logger.exception("Stance classification failed; leaving item unclustered")
            else:
                item.claim_id = claim.id
                item.stance = stance
                touched_topic_ids.add(claim.topic_id)
                if stance == Stance.SUPPORTING:
                    await _increment_topic_volume_bucket(db, claim.topic_id, item.created_at)
                attached = True

        if not attached:
            still_unclustered.append(item)

    # --- Pass 2: cluster the remainder into brand-new claims ---
    claims_created = 0

    if len(still_unclustered) >= MIN_CLUSTER_SIZE:
        embeddings = np.array([item.embedding for item in still_unclustered], dtype=float)
        labels = hdbscan.HDBSCAN(min_cluster_size=MIN_CLUSTER_SIZE, min_samples=1).fit_predict(
            embeddings
        )

        for label in sorted(set(labels)):
            if label == -1:
                continue
            cluster_items = [
                item for item, item_label in zip(still_unclustered, labels) if item_label == label
            ]
            if len(cluster_items) < MIN_CLUSTER_SIZE:
                continue

            claim = await build_claim_from_content_items(db, cluster_items, llm, embedder, config)
            touched_topic_ids.add(claim.topic_id)
            claims_created += 1

    if not touched_topic_ids:
        await db.commit()
        return ClusterResult(0, 0, 0)

    # --- Renormalize Reach per touched topic, then rescore every claim in it ---
    claims_to_rescore: dict[uuid.UUID, Claim] = {}
    for topic_id in touched_topic_ids:
        for claim in await renormalize_topic_reach(db, topic_id, config):
            claims_to_rescore[claim.id] = claim

    for claim in claims_to_rescore.values():
        await rescore_claim(db, claim, config)

    await db.commit()

    total_clustered = sum(1 for item in unclustered_items if item.claim_id is not None)
    return ClusterResult(
        claims_created=claims_created,
        claims_updated=len(claims_to_rescore) - claims_created,
        content_items_clustered=total_clustered,
    )


async def cluster_unclustered_content_task(
    llm: LLMClient | None = None,
    embedder: EmbeddingService | None = None,
    session_factory: async_sessionmaker | None = None,
    config: RuntimeConfig | None = None,
) -> None:
    """BackgroundTasks wrapper - opens its own session since the request is already done."""
    session_factory = session_factory or get_session_factory()
    async with session_factory() as db:
        try:
            result = await cluster_unclustered_content(db, llm=llm, embedder=embedder, config=config)
            logger.info(
                "Background clustering complete: %d claims created, %d updated, "
                "%d items clustered",
                result.claims_created,
                result.claims_updated,
                result.content_items_clustered,
            )
        except Exception:
            logger.exception("Background clustering pass failed")
