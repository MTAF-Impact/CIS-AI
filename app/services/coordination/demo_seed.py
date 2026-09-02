"""Demo/testing tooling for F5 - not part of the real backend contract
(app/api/v1/endpoints/networks.py). Synthesizes a coordinated-looking burst of
content on one claim, then writes a pending DetectionRun row using the exact same
synchronous-placeholder pattern POST /api/v1/detection/runs already uses, so a
caller can poll immediately while the real detection work (returned as
run_detection kwargs) runs separately.

Deliberately drives only w_time + w_text: w_amp and w_meta are currently dead in the
real pipeline - ContentItem/SignalPost carry no reshare/quote/reply/outbound-link
fields (w_amp), and run_detection builds SignalAccount(account_id, handle) with
every enrichment field left at its default None regardless of what's on the Account
row (w_meta) - so a synthetic account profile wouldn't move either signal. w_time +
w_text alone already clears MIN_SIGNAL_FAMILIES_PER_EDGE = 2. See docs/COORDINATION.md.

Known characteristic, not a bug: the cluster-level SY (synchrony) metric tends to
land near 0 for this generator even though the pairwise w_time edge signal fires -
compute_temporal_synchrony's null model measures whether a pair's co-occurrence is
*surprising* relative to the candidate pool's overall bin activity, and here the
entire candidate pool (burst + the claim's own seed posts) is generated within the
same few seconds of wall-clock time, so there's no quiet baseline for a burst to
stand out against. A real detection run has weeks of organic activity providing that
contrast; a script that runs in seconds does not. Confidence band typically comes
back Low as a result - expected, not a defect (the existing pipeline e2e test fixture
has the same property: it asserts the score is in range and signal_breadth >= 2,
never a specific confidence band, for the same reason).
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim import Claim
from app.models.content import ContentItem
from app.models.coordination import DetectionRun
from app.models.enums import ClaimType, ContentSource, Stance
from app.schemas.coordination_network import (
    DetectionRunRequest,
    DetectorParameters,
    Exclusions,
)
from app.services import admin_service
from app.services.clustering_service import (
    _increment_topic_volume_bucket,
    renormalize_topic_reach,
    rescore_claim,
)
from app.services.coordination import (
    clustering,
    confidence,
    fusion,
    pipeline,
    recurrence,
    relevance_gate,
    scope,
)
from app.services.coordination.signals import duplication, provenance, temporal
from app.services.embedding_service import EmbeddingService
from app.services.llm_client import LLMClient

COORD_DEMO_ACCOUNT_COUNT = 8
COORD_DEMO_POSTS_PER_ACCOUNT = 3  # 24 total posts, clears relevance_gate.DEFAULT_P_MIN=20
COORD_DEMO_BURST_SECONDS = 90  # each of the 3 rounds (30s wide) stays inside one 60s bin
COORD_DEMO_WINDOW_DAYS = 7

_TEMPLATES = [
    "{stmt}",
    "{stmt} Everyone needs to see this.",
    "Sharing this because it's true: {stmt}",
    "{stmt} Spread the word.",
    "PSA: {stmt}",
    "Just confirmed - {stmt}",
]


def _build_demo_parameters() -> DetectorParameters:
    """Every field mirrors the pipeline's own production-default module constants
    (imported, not restated) so a demo run behaves identically to a real run with no
    overrides - the backend would send exactly these values absent any F4 tuning."""
    return DetectorParameters(
        window_days=COORD_DEMO_WINDOW_DAYS,
        bin_width_seconds=temporal.DEFAULT_BIN_WIDTH_SECONDS,
        null_model_alpha=temporal.DEFAULT_NULL_MODEL_ALPHA,
        dup_threshold=duplication.DEFAULT_TAU_DUP,
        sem_threshold=duplication.DEFAULT_TAU_SEM,
        min_post_length=duplication.DEFAULT_L_MIN,
        edge_threshold=fusion.DEFAULT_THETA_EDGE,
        min_signal_families=fusion.MIN_SIGNAL_FAMILIES_PER_EDGE,
        k_core=clustering.DEFAULT_K_CORE,
        leiden_resolution=clustering.DEFAULT_RESOLUTION,
        min_cluster_size=clustering.DEFAULT_N_MIN,
        min_internal_density=clustering.DEFAULT_RHO_MIN,
        beta_time=fusion.DEFAULT_WEIGHTS["w_time"],
        beta_text=fusion.DEFAULT_WEIGHTS["w_text"],
        beta_amp=fusion.DEFAULT_WEIGHTS["w_amp"],
        beta_meta=fusion.DEFAULT_WEIGHTS["w_meta"],
        beta_struct=fusion.DEFAULT_WEIGHTS["w_struct"],
        provenance_half_life_hours=provenance.DEFAULT_CREATION_HALF_LIFE_HOURS,
        anchor_share=relevance_gate.DEFAULT_MU_ANCHOR,
        min_claim_posts=relevance_gate.DEFAULT_P_MIN,
        min_link_strength=relevance_gate.DEFAULT_OMEGA_MIN,
        high_score_cutoff=confidence.HIGH_SCORE_MIN,
        high_breadth_cutoff=confidence.HIGH_BREADTH_MIN,
        medium_score_cutoff=confidence.MEDIUM_SCORE_MIN,
        medium_breadth_cutoff=confidence.MEDIUM_BREADTH_MIN,
        # Not read anywhere in this pipeline - backend-scheduler-only fields per the
        # real contract (CISDetectorSettings). Fixed placeholders.
        cadence_hours=6,
        candidate_cap=scope.DEFAULT_A_MAX,
        recurrence_threshold=recurrence.DEFAULT_RECURRENCE_THRESHOLD,
        velocity_trigger_threshold=2.0,
    )


async def generate_demo_coordinated_network(
    db: AsyncSession,
    llm: LLMClient,
    embedder: EmbeddingService,
    claim_id: uuid.UUID | None = None,
    topic_hint: str | None = None,
) -> tuple[Claim, DetectionRun, dict]:
    """Returns (claim, pending_run, run_detection_kwargs) - the caller decides how to
    execute the background half (BackgroundTasks for the endpoint, a direct await for
    the seed script)."""
    if claim_id is not None:
        claim = await db.get(Claim, claim_id)
        if claim is None or claim.claim_type != ClaimType.EXISTING:
            raise ValueError(f"Claim {claim_id} not found or not an Existing claim")
    else:
        claim = await admin_service.generate_demo_existing_claim(db, llm, embedder, topic_hint)

    now = datetime.now(UTC)
    short_id = str(claim.id)[:8]
    total_posts = COORD_DEMO_ACCOUNT_COUNT * COORD_DEMO_POSTS_PER_ACCOUNT
    items: list[ContentItem] = []
    # Interleaved (round-robin across accounts, not grouped by account): every
    # account posts once per "round", all rounds packed into the same tight window,
    # so every pair of accounts actually co-occurs in the same temporal bins -
    # grouping each account's posts together instead would stagger accounts into a
    # sequential "relay" with near-zero cross-account synchrony, defeating the whole
    # point of a coordinated burst.
    for post_idx in range(COORD_DEMO_POSTS_PER_ACCOUNT):
        for account_idx in range(COORD_DEMO_ACCOUNT_COUNT):
            post_number = post_idx * COORD_DEMO_ACCOUNT_COUNT + account_idx
            author_id = f"demo_coord_{short_id}_{account_idx:02d}"
            template = _TEMPLATES[post_number % len(_TEMPLATES)]
            text = template.format(stmt=claim.claim_statement)
            offset_seconds = post_number * (COORD_DEMO_BURST_SECONDS / total_posts)
            created_at = now - timedelta(seconds=COORD_DEMO_BURST_SECONDS - offset_seconds)
            item = ContentItem(
                text=text,
                source=ContentSource.SOCIAL,
                author_id=author_id,
                claim_id=claim.id,
                stance=Stance.SUPPORTING,
                embedding=embedder.embed(text),
                created_at=created_at,
            )
            db.add(item)
            items.append(item)
    await db.flush()

    for item in items:
        await _increment_topic_volume_bucket(db, claim.topic_id, item.created_at)

    for touched_claim in await renormalize_topic_reach(db, claim.topic_id):
        await rescore_claim(db, touched_claim)
    await db.commit()
    await db.refresh(claim)

    window_end = now
    window_start = now - timedelta(days=COORD_DEMO_WINDOW_DAYS)
    parameters = _build_demo_parameters()
    exclusions = Exclusions()

    request = DetectionRunRequest(
        claim_ids=[claim.id],
        trigger_source="on_demand",
        window_start=window_start,
        window_end=window_end,
        parameters=parameters,
        exclusions=exclusions,
    )
    run = await pipeline.create_pending_run(db, request)

    run_kwargs = {
        "claim_ids": [claim.id],
        "window_start": window_start,
        "window_end": window_end,
        "parameters": parameters,
        "exclusions": exclusions,
    }
    return claim, run, run_kwargs
