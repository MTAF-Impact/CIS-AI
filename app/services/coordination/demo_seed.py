"""Demo/testing tooling for F5 - not part of the real backend contract
(app/api/v1/endpoints/networks.py). Synthesizes a coordinated-looking burst of
content on one claim, then writes a pending DetectionRun row using the exact same
synchronous-placeholder pattern POST /api/v1/detection/runs already uses, so a
caller can poll immediately while the real detection work (returned as
run_detection kwargs) runs separately.

Drives all 5 signals - a deliberate "perfect condition" simulation, not a
reflection of what any real platform we've wired up can actually supply today
(see docs/SOURCES.md / docs/COORDINATION.md's "Known gaps" for the honest real-data
picture, which this does NOT change one bit).

Only `COORD_DEMO_COORDINATED_RATIO` of the account pool is actually coordinated -
the rest are "organic" accounts genuinely posting about the same claim, at their own
scattered times, with none of the shared signature below. 100% of the candidate pool
being coordinated would be an unrealistic demo (and a bad one - it wouldn't show the
detector actually *discriminating* real coordination from incidental noise in the
same claim's supporting cluster, which is the whole point of the multi-signal rule
and the relevance gate). The organic accounts are expected to fail to cluster with
the coordinated group, or fall out during k-core reduction - that's the detector
doing its job, not this generator failing to do its.

- w_time / w_text: real signal, computed off genuinely close-together, genuinely
  similar `ContentItem` rows - same as before, nothing simulated about these two.
- w_amp: every post carries the same single synthetic `outbound_urls` entry
  (`ContentItem.outbound_urls`, a column that exists **only** for this generator -
  no real fetcher populates it, see the model's own docstring). Identical target
  set for every account -> cosine similarity 1.0 for every pair, a clean stand-in
  for "this group is mass-sharing the same external link."
- w_meta: real `Account` rows are pre-created with synthetic-but-structurally-valid
  provenance (`created_at_platform` clustered within hours of each other, a shared
  `bio`/`declared_location`/`client_app`) *before* run_detection executes, so
  `pipeline._load_account_provenance` finds real data to read - the pipeline code
  itself is completely unmodified, it's just being fed a complete-signal scenario.
- w_struct: `run_detection`'s `synthetic_follower_sets` param (see its docstring -
  always None for every real, backend-triggered run) is given a shared set of 3
  synthetic "hub" accounts every demo account follows.

None of this leaks into a real detection run - every synthetic field is either a
column no real fetcher writes to, or an explicit optional pipeline parameter that
defaults to None/absent everywhere except this one call site.

Known characteristic, not a bug: the cluster-level SY (synchrony) metric tends to
land near 0 for this generator even though the pairwise w_time edge signal fires -
compute_temporal_synchrony's null model measures whether a pair's co-occurrence is
*surprising* relative to the candidate pool's overall bin activity, and here the
entire candidate pool (burst + the claim's own seed posts) is generated within the
same few seconds of wall-clock time, so there's no quiet baseline for a burst to
stand out against. A real detection run has weeks of organic activity providing that
contrast; a script that runs in seconds does not. Confidence band can still land Low
as a result even with signal_breadth=5 - expected, not a defect (the pipeline e2e
test fixture has the same property: it asserts score-in-range and
signal_breadth >= 2, never a specific confidence band, for the same reason).
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

COORD_DEMO_ACCOUNT_COUNT = 40  # many low-activity accounts acting in synchrony, not
# a handful posting a lot - more realistic than 8 for a coordinated-network demo.
COORD_DEMO_COORDINATED_RATIO = 0.6  # the rest are organic noise in the same
# candidate pool - see module docstring for why 100% coordinated would be unrealistic.
COORD_DEMO_POSTS_PER_ACCOUNT = 3  # per coordinated account - total burst volume is
# this * coordinated_count, clears relevance_gate.DEFAULT_P_MIN=20 regardless of size.
COORD_DEMO_BURST_SECONDS = 90  # each of the 3 rounds stays inside one 60s bin -
# round width is BURST_SECONDS/POSTS_PER_ACCOUNT, independent of account count.
COORD_DEMO_WINDOW_DAYS = 7
COORD_DEMO_HUB_COUNT = 3  # synthetic "hub" accounts every *coordinated* account
# follows (w_struct) - organic accounts follow nothing here, see _build_accounts.
# Total coordinated-account-creation spread, kept well under
# cluster_metrics.TIGHTEST_WINDOW_HOURS (36h) regardless of how many are coordinated,
# so _tightest_window_share always finds every one of them in the one window.
COORD_DEMO_ACCOUNT_CREATION_SPREAD_HOURS = 20
COORD_DEMO_ORGANIC_POSTS_PER_ACCOUNT = 3

# SY (synchrony) and AU (automation/circadian) both need a quiet, spread-out
# per-account posting *baseline* to contrast against - without one, the whole
# candidate pool (burst-only) is uniformly "busy" everywhere, so nothing reads as
# anomalous (documented since this generator's first version) and every account's
# own circadian coverage is a single hour (all its posts are in the 90s burst).
# These days/hours are deliberately spread wide across the window/clock, not
# clustered, to be a legitimate "quiet baseline" for the burst to stand out against.
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

# Organic accounts' own phrasing - deliberately NOT the coordinated templates above,
# and never combined with the shared outbound_urls/bio/profile_hash/hub-following
# the coordinated group gets (see _build_accounts / the organic posts loop).
_ORGANIC_TEMPLATES = [
    "Does anyone know more about {stmt}",
    "Not sure what to think about {stmt}",
    "Saw some discussion about {stmt} today.",
    "Following {stmt} - curious how this plays out.",
    "Honestly {stmt} worries me a bit.",
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
    rng = random.Random(short_id)  # deterministic per-claim, not true randomness

    account_ids = [f"demo_coord_{short_id}_{i:02d}" for i in range(COORD_DEMO_ACCOUNT_COUNT)]
    coordinated_count = round(COORD_DEMO_ACCOUNT_COUNT * COORD_DEMO_COORDINATED_RATIO)
    coordinated_ids = account_ids[:coordinated_count]
    organic_ids = account_ids[coordinated_count:]

    # w_amp: identical target set for every *coordinated* account (see module
    # docstring) -> cosine similarity 1.0 for every pair among them, regardless of
    # TF-IDF weighting (it scales both vectors in a pair identically when they're
    # the same single-target one-hot vector). Organic accounts get none at all.
    shared_outbound_url = f"https://demo-share.example/{short_id}"

    items: list[ContentItem] = []
    total_burst_posts = coordinated_count * COORD_DEMO_POSTS_PER_ACCOUNT
    # Interleaved (round-robin across accounts, not grouped by account): every
    # account posts once per "round", all rounds packed into the same tight window,
    # so every pair of accounts actually co-occurs in the same temporal bins -
    # grouping each account's posts together instead would stagger accounts into a
    # sequential "relay" with near-zero cross-account synchrony, defeating the whole
    # point of a coordinated burst. Organic accounts never enter this loop.
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

    # Baseline posts (see COORD_DEMO_BASELINE_* above) - organic-looking single
    # posts per coordinated account, spread across earlier days/hours in the
    # window, giving the tight burst above something quiet to stand out against.
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

    # Organic accounts: genuinely independent posters on the same claim. Scattered
    # timing across the *entire* window (own random day/hour/minute per post, no
    # synchrony with each other or the burst), varied non-templated phrasing, and
    # no outbound_urls - _targets() returns an empty set for these posts, so
    # compute_co_amplification correctly drops these accounts from w_amp entirely
    # (see its own "accounts = [a for a, targets in ... if targets]" filter).
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

    # w_meta: pre-create real Account rows (platform="unknown", matching
    # pipeline.py's own get-or-create default) with synthetic-but-structurally-
    # valid provenance, so pipeline._load_account_provenance finds real data when
    # run_detection executes later - the pipeline itself reads this unmodified.
    # Get-or-update, not a blind insert: re-running against an existing claim_id
    # reuses the same deterministic account_ids (derived from claim.id), which
    # would otherwise hit Account's (platform, platform_account_id) unique
    # constraint on a second call.
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
    # 10 days back, not 2 - must predate the baseline posts above (up to 5 days
    # back), or the simulation would show posts older than the account itself.
    account_created_base = now - timedelta(days=10)
    shared_bio = "Concerned Jakarta resident. Local news watcher."
    # PR's _handle_template_share needs HANDLE_TEMPLATE_RE
    # (^[a-z]+_[a-z]+\d{2,4}$) to match and share the same (prefix, digit-length)
    # across accounts - author_id/platform_account_id stay the unique dedup key,
    # only the separate `handle` field needs to fit the template.
    # _duplicate_profile_image_share just needs a shared, non-null profile_hash;
    # this is a synthetic placeholder, not a real perceptual hash of any image.
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

    # Organic accounts: no shared handle template, no shared profile_hash/bio/
    # location/client_app, and creation dates spread widely and independently
    # (30 days to ~2 years old) rather than clustered - none of PR's four
    # sub-signals should read these as anomalous the way the coordinated group is.
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

    # w_struct: every *coordinated* demo account "follows" the same 3 synthetic hub
    # accounts - threaded into run_detection's synthetic_follower_sets, see its
    # docstring for why this never affects a real, backend-triggered run. Organic
    # accounts have no entry at all (not an empty set - genuinely absent), so
    # compute_structural_overlap never compares them to anyone.
    hub_ids = [f"demo_hub_{short_id}_{h}" for h in range(COORD_DEMO_HUB_COUNT)]
    synthetic_follower_sets = {account_id: set(hub_ids) for account_id in coordinated_ids}

    # PR's age-percentile sub-signal: a synthetic "typical platform account age"
    # baseline spanning 1 week to 5 years old, so this cluster's ~10-day-old
    # accounts rank at the young/anomalous end - see run_detection's docstring for
    # why this never affects a real run (no live baseline exists to supply here).
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
