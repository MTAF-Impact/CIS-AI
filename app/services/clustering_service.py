import logging
from dataclasses import dataclass

import hdbscan
import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content import ContentItem
from app.models.fault_line import FaultLine
from app.models.narrative import Narrative
from app.services import risk_engine
from app.services.gemini_client import GeminiClient, get_gemini_client

logger = logging.getLogger(__name__)

MIN_CLUSTER_SIZE = 2
MAX_SAMPLE_TEXTS_FOR_SUMMARY = 10


@dataclass
class ClusterResult:
    narratives_created: int
    narratives_updated: int
    content_items_clustered: int


async def _fetch_fault_line_embeddings(
    db: AsyncSession,
) -> list[tuple[str, list[float]]]:
    stmt = select(FaultLine).where(FaultLine.embedding.is_not(None))
    result = await db.execute(stmt)
    return [(str(fl.id), fl.embedding) for fl in result.scalars().all()]


def _centroid(items: list[ContentItem]) -> np.ndarray:
    vectors = np.array([item.embedding for item in items], dtype=float)
    centroid = vectors.mean(axis=0)
    norm = np.linalg.norm(centroid)
    return centroid / norm if norm > 0 else centroid


async def _score_and_persist_narrative(
    db: AsyncSession,
    narrative: Narrative,
    cluster_items: list[ContentItem],
    fault_line_embeddings: list[tuple[str, list[float]]],
) -> None:
    centroid = _centroid(cluster_items)
    growth_velocity = risk_engine.compute_growth_velocity(
        [item.created_at for item in cluster_items]
    )
    emotional_intensity = risk_engine.compute_emotional_intensity(
        [item.outrage_score for item in cluster_items]
    )
    geographic_concentration = risk_engine.compute_geographic_concentration(
        [item.location for item in cluster_items]
    )
    fault_line_relevance, _matched_ids = risk_engine.compute_fault_line_relevance(
        centroid, fault_line_embeddings
    )
    risk_score = risk_engine.calculate_risk_score(
        growth_velocity, emotional_intensity, geographic_concentration, fault_line_relevance
    )

    narrative.growth_velocity = growth_velocity
    narrative.emotional_intensity = emotional_intensity
    narrative.geographic_concentration = geographic_concentration
    narrative.fault_line_relevance = fault_line_relevance
    narrative.overall_risk_score = risk_score
    narrative.risk_level = risk_engine.determine_risk_level(risk_score)


async def cluster_unclustered_content(
    db: AsyncSession, gemini: GeminiClient | None = None
) -> ClusterResult:
    """Cluster all not-yet-clustered ContentItems into Narratives.

    1. Attach unclustered items to an existing narrative if they are close enough to its
       current centroid (keeps narratives dynamic/growing rather than fragmenting).
    2. Cluster whatever remains with HDBSCAN and create new narratives for each cluster
       (label -1 = noise is left unclustered).
    """
    gemini = gemini or get_gemini_client()

    unclustered_stmt = select(ContentItem).where(
        ContentItem.narrative_id.is_(None), ContentItem.embedding.is_not(None)
    )
    unclustered_items = list((await db.execute(unclustered_stmt)).scalars().all())

    if not unclustered_items:
        return ClusterResult(0, 0, 0)

    fault_line_embeddings = await _fetch_fault_line_embeddings(db)

    # --- Pass 1: try to attach to existing narratives ---
    existing_narratives = list((await db.execute(select(Narrative))).scalars().all())
    narratives_updated_ids: set = set()
    still_unclustered: list[ContentItem] = []

    if existing_narratives:
        narrative_items_stmt = select(ContentItem).where(
            ContentItem.narrative_id.is_not(None), ContentItem.embedding.is_not(None)
        )
        all_clustered_items = list((await db.execute(narrative_items_stmt)).scalars().all())
        items_by_narrative: dict = {}
        for item in all_clustered_items:
            items_by_narrative.setdefault(item.narrative_id, []).append(item)

        centroids = {
            narrative.id: _centroid(items)
            for narrative, items in (
                (n, items_by_narrative.get(n.id, [])) for n in existing_narratives
            )
            if items
        }

        for item in unclustered_items:
            item_vec = np.asarray(item.embedding)
            best_narrative_id, best_score = None, -1.0
            for narrative_id, centroid in centroids.items():
                score = float(np.dot(item_vec, centroid))
                if score > best_score:
                    best_narrative_id, best_score = narrative_id, score

            if best_narrative_id is not None and best_score >= 0.55:
                item.narrative_id = best_narrative_id
                items_by_narrative.setdefault(best_narrative_id, []).append(item)
                narratives_updated_ids.add(best_narrative_id)
            else:
                still_unclustered.append(item)

        for narrative in existing_narratives:
            if narrative.id in narratives_updated_ids:
                await _score_and_persist_narrative(
                    db, narrative, items_by_narrative[narrative.id], fault_line_embeddings
                )
    else:
        still_unclustered = unclustered_items

    # --- Pass 2: cluster the remainder into brand-new narratives ---
    narratives_created = 0
    newly_clustered_count = 0

    if len(still_unclustered) >= MIN_CLUSTER_SIZE:
        embeddings = np.array([item.embedding for item in still_unclustered], dtype=float)
        clusterer = hdbscan.HDBSCAN(min_cluster_size=MIN_CLUSTER_SIZE, min_samples=1)
        labels = clusterer.fit_predict(embeddings)

        for label in sorted(set(labels)):
            if label == -1:
                continue
            cluster_items = [
                item for item, item_label in zip(still_unclustered, labels) if item_label == label
            ]
            if len(cluster_items) < MIN_CLUSTER_SIZE:
                continue

            sample_texts = [item.text for item in cluster_items[:MAX_SAMPLE_TEXTS_FOR_SUMMARY]]
            try:
                summary = await gemini.summarize_narrative(sample_texts)
                title, summary_text = summary.title, summary.summary
            except Exception:  # noqa: BLE001 - never let labeling failures block clustering
                logger.exception("Gemini narrative summarization failed; using fallback title")
                title = sample_texts[0][:80]
                summary_text = None

            narrative = Narrative(title=title, summary=summary_text)
            db.add(narrative)
            await db.flush()  # assign narrative.id

            for item in cluster_items:
                item.narrative_id = narrative.id

            await _score_and_persist_narrative(db, narrative, cluster_items, fault_line_embeddings)

            narratives_created += 1
            newly_clustered_count += len(cluster_items)

    await db.commit()

    total_clustered = sum(1 for item in unclustered_items if item.narrative_id is not None)
    return ClusterResult(
        narratives_created=narratives_created,
        narratives_updated=len(narratives_updated_ids),
        content_items_clustered=total_clustered,
    )
