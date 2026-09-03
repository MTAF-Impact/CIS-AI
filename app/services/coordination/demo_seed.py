"""Demo/testing tooling for F5. Synthesizes a coordinated-looking burst of content
on one claim, then writes a pending DetectionRun row the same way
POST /api/v1/detection/runs does, so a caller can poll while the real detection
work (returned as run_detection kwargs) runs separately.

Drives all 5 signals as a "perfect condition" simulation - real platforms don't
supply this much signal today (see docs/COORDINATION.md's "Known gaps").

Only `COORD_DEMO_COORDINATED_RATIO` of the account pool is coordinated; the rest
are organic accounts genuinely posting about the same claim at scattered times, so
the demo exercises the detector's discrimination rather than just flagging
everything.

- w_time / w_text: real signal off genuinely close-together, similar posts.
- w_amp: every coordinated post shares one synthetic outbound URL.
- w_meta: coordinated accounts get synthetic-but-structurally-valid provenance
  (clustered creation time, shared bio/location/client) before run_detection
  reads them - the pipeline code itself is unmodified.
- w_struct: coordinated accounts share a synthetic follower set of 3 "hub"
  accounts, via run_detection's synthetic_follower_sets param.

None of this leaks into a real run - every synthetic field is either a column no
real fetcher writes, or an optional pipeline parameter that's None everywhere
except this call site.

The cluster-level SY metric tends to land near 0 here even though the pairwise
w_time edge fires: the null model measures surprise against a baseline, and this
whole candidate pool is generated within seconds, leaving no quiet baseline for
the burst to stand out against. Confidence band can land Low even with
signal_breadth=5 as a result - expected here, not a defect.
"""

import random
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim import Claim
from app.models.content import ContentItem
from app.models.coordination import Account, DetectionRun
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

COORD_DEMO_ACCOUNT_COUNT = 40
COORD_DEMO_COORDINATED_RATIO = 0.6
COORD_DEMO_POSTS_PER_ACCOUNT = 3  # clears relevance_gate.DEFAULT_P_MIN=20 regardless of size
COORD_DEMO_BURST_SECONDS = 90  # each round stays inside one 60s bin
COORD_DEMO_WINDOW_DAYS = 7
COORD_DEMO_HUB_COUNT = 3
# Kept under cluster_metrics.TIGHTEST_WINDOW_HOURS (36h) so _tightest_window_share
# always finds every coordinated account in one window.
COORD_DEMO_ACCOUNT_CREATION_SPREAD_HOURS = 20
COORD_DEMO_ORGANIC_POSTS_PER_ACCOUNT = 3

# SY/AU need a quiet baseline to contrast against - spread wide across the
# window/clock so the burst has something to stand out from.
COORD_DEMO_BASELINE_DAY_OFFSETS = [1, 2, 3, 4, 5]
COORD_DEMO_BASELINE_HOURS = [3, 8, 13, 18, 22]

_TEMPLATES = [
    "{stmt}",
    "{stmt} Everyone needs to see this.",
    "Sharing this because it's true: {stmt}",
    "{stmt} Spread the word.",
    "PSA: {stmt}",
    "Just confirmed - {stmt}",
]

_ORGANIC_TEMPLATES = [
    "Does anyone know more about {stmt}",
    "Not sure what to think about {stmt}",
    "Saw some discussion about {stmt} today.",
    "Following {stmt} - curious how this plays out.",
    "Honestly {stmt} worries me a bit.",
]


def _build_demo_parameters() -> DetectorParameters:
    """Mirrors the pipeline's production-default module constants so a demo run
    behaves identically to a real run with no overrides."""
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
        # Backend-scheduler-only fields; not read anywhere in this pipeline.
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
    execute the background half."""
    if claim_id is not None:
        claim = await db.get(Claim, claim_id)
        if claim is None or claim.claim_type != ClaimType.EXISTING:
            raise ValueError(f"Claim {claim_id} not found or not an Existing claim")
    else:
        claim = await admin_service.generate_demo_existing_claim(db, llm, embedder, topic_hint)

    now = datetime.now(UTC)
    short_id = str(claim.id)[:8]
    rng = random.Random(short_id)

    account_ids = [f"demo_coord_{short_id}_{i:02d}" for i in range(COORD_DEMO_ACCOUNT_COUNT)]
    coordinated_count = round(COORD_DEMO_ACCOUNT_COUNT * COORD_DEMO_COORDINATED_RATIO)
    coordinated_ids = account_ids[:coordinated_count]
    organic_ids = account_ids[coordinated_count:]

    shared_outbound_url = f"https://demo-share.example/{short_id}"

    items: list[ContentItem] = []
    total_burst_posts = coordinated_count * COORD_DEMO_POSTS_PER_ACCOUNT
    # Round-robin across accounts (not grouped by account) so every pair actually
    # co-occurs in the same temporal bins.
    for post_idx in range(COORD_DEMO_POSTS_PER_ACCOUNT):
        for account_idx in range(coordinated_count):
            post_number = post_idx * coordinated_count + account_idx
            author_id = coordinated_ids[account_idx]
            template = _TEMPLATES[post_number % len(_TEMPLATES)]
            text = template.format(stmt=claim.claim_statement)
            offset_seconds = post_number * (COORD_DEMO_BURST_SECONDS / total_burst_posts)
            created_at = now - timedelta(seconds=COORD_DEMO_BURST_SECONDS - offset_seconds)
            item = ContentItem(
                text=text,
                source=ContentSource.SOCIAL,
                author_id=author_id,
                claim_id=claim.id,
                stance=Stance.SUPPORTING,
                embedding=embedder.embed(text),
                created_at=created_at,
                outbound_urls=[shared_outbound_url],
            )
            db.add(item)
            items.append(item)

    # Baseline posts: one per coordinated account per day/hour offset, giving the
    # burst above a quiet baseline to stand out against.
    for account_idx in range(coordinated_count):
        author_id = coordinated_ids[account_idx]
        minute_jitter = (account_idx * 7) % 60
        for day_offset, hour in zip(
            COORD_DEMO_BASELINE_DAY_OFFSETS, COORD_DEMO_BASELINE_HOURS, strict=True
        ):
            text = f"Following the {claim.claim_statement[:80]} situation."
            created_at = (now - timedelta(days=day_offset)).replace(
                hour=hour, minute=minute_jitter, second=0, microsecond=0
            )
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

    # Organic accounts: independent posters on the same claim, scattered timing,
    # no shared signature.
    for account_idx, author_id in enumerate(organic_ids):
        for post_idx in range(COORD_DEMO_ORGANIC_POSTS_PER_ACCOUNT):
            template = _ORGANIC_TEMPLATES[(account_idx + post_idx) % len(_ORGANIC_TEMPLATES)]
            text = template.format(stmt=claim.claim_statement)
            created_at = now - timedelta(
                days=rng.uniform(0.1, COORD_DEMO_WINDOW_DAYS - 0.1),
                hours=rng.uniform(0, 23),
                minutes=rng.uniform(0, 59),
            )
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

    # Get-or-update, not a blind insert: re-running against an existing claim_id
    # reuses the same deterministic account_ids, which would otherwise hit
    # Account's unique constraint on a second call.
    existing_accounts = {
        a.platform_account_id: a
        for a in (
            await db.execute(
                select(Account).where(
                    Account.platform == "unknown", Account.platform_account_id.in_(account_ids)
                )
            )
        ).scalars().all()
    }
    # Must predate the baseline posts (up to 5 days back).
    account_created_base = now - timedelta(days=10)
    shared_bio = "Concerned Jakarta resident. Local news watcher."
    shared_profile_hash = f"demo{short_id}"
    creation_spacing_hours = COORD_DEMO_ACCOUNT_CREATION_SPREAD_HOURS / max(coordinated_count - 1, 1)

    def _upsert_account(account_id: str, **fields) -> None:
        if account_id in existing_accounts:
            existing = existing_accounts[account_id]
            for k, v in fields.items():
                setattr(existing, k, v)
        else:
            db.add(Account(platform="unknown", platform_account_id=account_id, **fields))

    for i, account_id in enumerate(coordinated_ids):
        _upsert_account(
            account_id,
            handle=f"jkt_watcher{i:02d}",
            created_at_platform=account_created_base + timedelta(hours=i * creation_spacing_hours),
            profile_hash=shared_profile_hash,
            bio=shared_bio,
            declared_location="Jakarta, Indonesia",
            client_app="demo-app/1.0",
        )

    for account_id in organic_ids:
        _upsert_account(
            account_id,
            handle=account_id,
            created_at_platform=now - timedelta(days=rng.uniform(30, 700)),
            bio=rng.choice(
                [
                    "Just a Jakarta resident.",
                    "Sharing my thoughts on local issues.",
                    None,
                ]
            ),
        )

    hub_ids = [f"demo_hub_{short_id}_{h}" for h in range(COORD_DEMO_HUB_COUNT)]
    synthetic_follower_sets = {account_id: set(hub_ids) for account_id in coordinated_ids}

    # Synthetic "typical platform account age" baseline, 1 week to 5 years, so this
    # cluster's ~10-day-old accounts rank at the young/anomalous end.
    platform_age_baseline_hours = [
        24 * days for days in (7, 30, 90, 180, 365, 365 * 1.5, 365 * 2, 365 * 3, 365 * 4, 365 * 5)
    ]

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
        "synthetic_follower_sets": synthetic_follower_sets,
        "platform_age_baseline_hours": platform_age_baseline_hours,
    }
    return claim, run, run_kwargs
