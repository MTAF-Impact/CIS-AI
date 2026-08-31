"""PRD 10.5.8 - Stage 7: execution model. run_detection_for_claim is the single
pipeline entrypoint threading Stages 0-6 together and persisting the result, following
the same session_factory/BackgroundTasks shape as policy_matchmaking_service.py (F2's
async pipeline). trigger_detection_run is the dispatcher behind the AI service's one
remaining F5 endpoint (POST /coordination/detection-runs) - see networks.py.

Detection parameters (PRD 10.11) are static defaults from app.core.config.settings,
optionally overridden per-run via the request body - CoordinationSettings (the old
DB-backed F4 config) moved to the backend along with the rest of F5 config ownership.

Known data-availability gaps, all documented rather than faked: ContentItem carries
no reshare/quote/reply/outbound-link fields yet, so w_amp is effectively empty until
ingestion captures them; no ingestion path populates account creation
date/profile-hash/bio yet, so w_meta and the PR metric run on handle/timing data only;
no follower-graph source exists, so w_struct is always unavailable. Every signal
function already treats "unavailable" as *unavailable*, not zero - this pipeline
inherits that honesty rather than working around it."""

import logging
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.database import get_session_factory
from app.models.claim import Claim
from app.models.content import ContentItem
from app.models.coordination import (
    Account,
    CoordinatedNetwork,
    DetectionRun,
    NetworkAccount,
    NetworkBurstBin,
    NetworkClaimLink,
    NetworkEdge,
    NetworkEvidencePost,
    OfftopicCluster,
)
from app.models.enums import ClaimStatus, ClaimType, DetectionRunStatus, Stance
from app.schemas.coordination_network import DetectionRunOverrides
from app.services.coordination import governance
from app.services.coordination.cluster_metrics import (
    ClusterMetrics,
    compute_cluster_metrics,
)
from app.services.coordination.clustering import DetectedCommunity, detect_communities
from app.services.coordination.confidence import (
    ALLOWLIST_MAJORITY_THRESHOLD,
    HIGH_BREADTH_MIN,
    HIGH_SCORE_MIN,
    MEDIUM_BREADTH_MIN,
    MEDIUM_SCORE_MIN,
    compute_signal_breadth,
    determine_confidence_band,
    is_allowlist_suppressed,
)
from app.services.coordination.evidence import build_evidence_snapshot
from app.services.coordination.fusion import (
    DEFAULT_WEIGHTS,
    MIN_SIGNAL_FAMILIES_PER_EDGE,
    MULTI_SIGNAL_CONTRIBUTION_THRESHOLD,
    FusedEdge,
    fuse_and_prune,
)
from app.services.coordination.recurrence import (
    DEFAULT_RECURRENCE_THRESHOLD,
    RecurrenceCandidate,
    compute_fingerprint,
    find_recurrence_parent,
)
from app.services.coordination.relevance_gate import evaluate_claim_relevance
from app.services.coordination.scope import select_candidates
from app.services.coordination.signals.coamplification import compute_co_amplification
from app.services.coordination.signals.duplication import compute_content_duplication
from app.services.coordination.signals.provenance import compute_provenance_similarity
from app.services.coordination.signals.structural import compute_structural_overlap
from app.services.coordination.signals.temporal import compute_temporal_synchrony
from app.services.coordination.types import SignalAccount, SignalPost
from app.services.multilingual_embedding_service import (
    MultilingualEmbeddingService,
    get_multilingual_embedding_service,
)

logger = logging.getLogger(__name__)

MIN_CANDIDATES_TO_RUN = 2  # below this, no cluster (N_min=5 by default) could ever form
DEFAULT_RANDOM_SEED = 42
MODEL_VERSIONS = {"leidenalg": "0.12", "igraph": "1.0"}  # recorded for reproducibility, 10.5.6 pt 7
STOP_WORDS = frozenset(
    {"the", "a", "an", "is", "are", "was", "were", "this", "that", "it", "in", "on", "for", "of", "to", "and", "with"}
)


@dataclass(frozen=True)
class DetectionParams:
    """The PRD 10.11 tunables, defaulted from static config and optionally overridden
    per-run (see trigger_detection_run's `overrides` parameter)."""

    window_hours: float = settings.COORDINATION_DEFAULT_WINDOW_HOURS
    a_max: int = settings.COORDINATION_A_MAX
    theta_edge: float = settings.COORDINATION_THETA_EDGE
    k_core: int = settings.COORDINATION_K_CORE
    leiden_resolution: float = settings.COORDINATION_LEIDEN_RESOLUTION
    n_min: int = settings.COORDINATION_N_MIN
    rho_min: float = settings.COORDINATION_RHO_MIN
    mu_anchor: float = settings.COORDINATION_MU_ANCHOR
    p_min: int = settings.COORDINATION_P_MIN
    omega_min: float = settings.COORDINATION_OMEGA_MIN
    bin_width_seconds: int = settings.COORDINATION_BIN_WIDTH_SECONDS
    null_model_alpha: float = settings.COORDINATION_NULL_MODEL_ALPHA
    tau_dup: float = settings.COORDINATION_TAU_DUP
    tau_sem: float = settings.COORDINATION_TAU_SEM
    l_min: int = settings.COORDINATION_L_MIN
    provenance_half_life_hours: float = settings.COORDINATION_PROVENANCE_HALF_LIFE_HOURS
    self_exclusion_handles: list[str] = field(
        default_factory=lambda: list(settings.COORDINATION_SELF_EXCLUSION_HANDLES)
    )


def _effective_params(overrides: DetectionRunOverrides | None) -> DetectionParams:
    if overrides is None:
        return DetectionParams()
    supplied = overrides.model_dump(exclude_none=True)
    valid_fields = {f.name for f in fields(DetectionParams)}
    return DetectionParams(**{k: v for k, v in supplied.items() if k in valid_fields})


def _run_parameters(window_hours: float, params: DetectionParams) -> dict:
    """Every parameter actually in force for this run, per PRD 10.5.6 point 7 - "a
    report generated months later can state the exact configuration that produced
    it." A handful of values without a per-run override yet (confidence-band cutoffs,
    the allowlist-majority threshold, the multi-signal-rule constants) still read
    their compile-time module defaults."""
    return {
        "window_hours": window_hours,
        "a_max": params.a_max,
        "signal_weights": DEFAULT_WEIGHTS,
        "theta_edge": params.theta_edge,
        "min_signal_families_per_edge": MIN_SIGNAL_FAMILIES_PER_EDGE,
        "multi_signal_contribution_threshold": MULTI_SIGNAL_CONTRIBUTION_THRESHOLD,
        "k_core": params.k_core,
        "leiden_resolution": params.leiden_resolution,
        "n_min": params.n_min,
        "rho_min": params.rho_min,
        "mu_anchor": params.mu_anchor,
        "p_min": params.p_min,
        "omega_min": params.omega_min,
        "bin_width_seconds": params.bin_width_seconds,
        "null_model_alpha": params.null_model_alpha,
        "tau_dup": params.tau_dup,
        "tau_sem": params.tau_sem,
        "l_min": params.l_min,
        "provenance_half_life_hours": params.provenance_half_life_hours,
        "self_exclusion_handles": params.self_exclusion_handles,
        "high_confidence_score_min": HIGH_SCORE_MIN,
        "high_confidence_breadth_min": HIGH_BREADTH_MIN,
        "medium_confidence_score_min": MEDIUM_SCORE_MIN,
        "medium_confidence_breadth_min": MEDIUM_BREADTH_MIN,
        "allowlist_majority_threshold": ALLOWLIST_MAJORITY_THRESHOLD,
        "recurrence_match_threshold": DEFAULT_RECURRENCE_THRESHOLD,
    }


# --- Stage 0/1 inputs --------------------------------------------------------------


async def _load_candidate_posts(
    db: AsyncSession, claim_id: uuid.UUID, window_start: datetime, window_end: datetime
) -> list[SignalPost]:
    rows = (
        await db.execute(
            select(ContentItem).where(
                ContentItem.claim_id == claim_id,
                ContentItem.stance == Stance.SUPPORTING,
                ContentItem.created_at >= window_start,
                ContentItem.created_at <= window_end,
                ContentItem.author_id.is_not(None),
            )
        )
    ).scalars().all()
    return [
        SignalPost(
            id=str(item.id),
            account_id=item.author_id,
            text=item.text_en or item.text,
            created_at=item.created_at,
            source=item.source,  # stored as a plain string column, not a native enum type
        )
        for item in rows
    ]


async def _total_post_counts(
    db: AsyncSession, account_ids: list[str], window_start: datetime, window_end: datetime
) -> dict[str, int]:
    """Each account's post volume across ALL monitored content in W - not just this
    claim - the denominator the claim-relevance gate needs (10.5.1a)."""
    if not account_ids:
        return {}
    rows = (
        await db.execute(
            select(ContentItem.author_id, func.count())
            .where(
                ContentItem.author_id.in_(account_ids),
                ContentItem.created_at >= window_start,
                ContentItem.created_at <= window_end,
            )
            .group_by(ContentItem.author_id)
        )
    ).all()
    return dict(rows)


async def _load_allowlisted_handles(db: AsyncSession) -> set[str]:
    """Reads the backend-owned `cis_coordination_allowlist` table - the integration
    doc's one explicit exception to "no shared-table access" (read-only, only this
    table). ASSUMPTION pending backend confirmation: the doc names the table but not
    its schema, so the column names below (handle, removed_at) are carried over from
    this service's own now-removed CoordinationAllowlist model as the closest known
    shape - reconcile against the backend's actual DDL before this reads real data."""
    rows = (
        await db.execute(
            text("SELECT handle FROM cis_coordination_allowlist WHERE removed_at IS NULL")
        )
    ).scalars().all()
    return set(rows)


async def _get_or_create_accounts(
    db: AsyncSession, platform_account_ids: set[str], platform: str = "unknown"
) -> dict[str, uuid.UUID]:
    """Get-or-create coordination_accounts rows keyed by (platform,
    platform_account_id). No unique DB constraint on that pair yet (Phase 0 didn't
    add one) - fine for the sequential/scheduled runs this pipeline supports today; a
    future migration should add one before concurrent runs become a real concern."""
    if not platform_account_ids:
        return {}
    existing = (
        await db.execute(
            select(Account).where(
                Account.platform == platform, Account.platform_account_id.in_(platform_account_ids)
            )
        )
    ).scalars().all()
    mapping = {a.platform_account_id: a.id for a in existing}
    for platform_account_id in platform_account_ids - mapping.keys():
        account = Account(platform=platform, platform_account_id=platform_account_id, handle=platform_account_id)
        db.add(account)
        await db.flush()
        mapping[platform_account_id] = account.id
    return mapping


async def _load_recurrence_candidates(db: AsyncSession) -> list[RecurrenceCandidate]:
    rows = (
        await db.execute(
            select(NetworkAccount.network_id, Account.platform_account_id).join(
                Account, Account.id == NetworkAccount.account_id
            )
        )
    ).all()
    by_network: dict[uuid.UUID, set[str]] = defaultdict(set)
    for network_id, platform_account_id in rows:
        by_network[network_id].add(platform_account_id)
    return [RecurrenceCandidate(str(nid), members) for nid, members in by_network.items()]


def _extract_top_terms(posts: list[SignalPost], limit: int = 5) -> list[str]:
    """Lightweight recurrence-fingerprint input (10.5.7) - not a PRD-defined signal,
    just a compact content signature alongside the member-ID set."""
    counts: Counter[str] = Counter()
    for p in posts:
        for word in p.text.lower().split():
            cleaned = "".join(c for c in word if c.isalnum())
            if len(cleaned) > 3 and cleaned not in STOP_WORDS:
                counts[cleaned] += 1
    return [w for w, _ in counts.most_common(limit)]


def _build_signals(
    posts: list[SignalPost],
    accounts: list[SignalAccount],
    embedder: MultilingualEmbeddingService,
    params: DetectionParams,
) -> dict[str, dict[tuple[str, str], float] | None]:
    return {
        "w_time": compute_temporal_synchrony(
            posts, bin_width_seconds=params.bin_width_seconds, alpha=params.null_model_alpha
        ),
        "w_text": compute_content_duplication(
            posts, tau_dup=params.tau_dup, tau_sem=params.tau_sem, l_min=params.l_min, embedder=embedder
        ),
        "w_amp": compute_co_amplification(posts),
        "w_meta": compute_provenance_similarity(accounts, half_life_hours=params.provenance_half_life_hours),
        "w_struct": compute_structural_overlap(None),  # no follower-graph data source yet
    }


# --- Stage 4-6: per-community processing --------------------------------------------


async def _persist_offtopic_cluster(
    db: AsyncSession,
    run_id: uuid.UUID,
    claim_id: uuid.UUID,
    community: DetectedCommunity,
    cluster_posts: list[SignalPost],
    metrics: ClusterMetrics,
    relevance_overlap_ratio: float,
    relevance_anchoring_share: float,
    fingerprint: str,
    failed_test: str,
) -> None:
    """PRD 10.5.1a point 7 - a real coordinated cluster that isn't about this claim.
    Suppressed from the network list, retained only for aggregate recalibration
    review (now the backend's - it reads this table directly)."""
    db.add(
        OfftopicCluster(
            run_id=run_id,
            claim_id=claim_id,
            coordination_signals={
                "sy": metrics.sy,
                "du": metrics.du,
                "co": metrics.co,
                "pr": metrics.pr,
                "au": metrics.au,
                "coordination_score": metrics.coordination_score,
            },
            overlap_ratio=relevance_overlap_ratio,
            anchoring_share=relevance_anchoring_share,
            account_count=len(community.account_ids),
            post_count=len(cluster_posts),
            fingerprint_hash=fingerprint,
            failed_test=failed_test,
        )
    )


async def _persist_network(
    db: AsyncSession,
    run_id: uuid.UUID,
    claim_id: uuid.UUID,
    community: DetectedCommunity,
    cluster_posts: list[SignalPost],
    accounts: list[SignalAccount],
    edges: list[FusedEdge],
    metrics: ClusterMetrics,
    breadth: int,
    band,
    fingerprint: str,
    parent_network_id: uuid.UUID | None,
    relevance_overlap_ratio: float,
    relevance_anchoring_share: float,
    relevance_post_count: int,
    account_id_map: dict[str, uuid.UUID],
    embedder: MultilingualEmbeddingService,
) -> CoordinatedNetwork:
    snapshot = build_evidence_snapshot(community, cluster_posts, accounts, edges, embedder=embedder)
    platforms = sorted({p.source for p in cluster_posts if p.source})

    network = CoordinatedNetwork(
        run_id=run_id,
        coordination_score=metrics.coordination_score,
        sy=metrics.sy,
        du=metrics.du,
        co=metrics.co,
        pr=metrics.pr,
        au=metrics.au,
        signal_breadth=breadth,
        confidence_band=band,
        account_count=len(community.account_ids),
        post_count=len(cluster_posts),
        platforms=platforms,
        internal_density=community.internal_density,
        conductance=community.conductance,
        graph_layout={account_id: list(xy) for account_id, xy in snapshot.graph.layout.items()},
        fingerprint_hash=fingerprint,
        parent_network_id=parent_network_id,
    )
    db.add(network)
    await db.flush()

    for entry in snapshot.account_annex:
        db.add(
            NetworkAccount(
                network_id=network.id,
                account_id=account_id_map[entry.account_id],
                posts_in_cluster=entry.posts_in_cluster,
                duplication_rate=entry.duplication_rate,
                median_interpost_interval_seconds=entry.median_interpost_interval_seconds,
                circadian_coverage=entry.circadian_coverage,
                degree_centrality=entry.degree_centrality,
                eigenvector_centrality=entry.eigenvector_centrality,
                # Per-metric contribution breakdown (not just aggregate stats) is a
                # future refinement - left empty rather than faked.
                score_contribution={},
            )
        )
    for edge in snapshot.graph.edges:
        db.add(
            NetworkEdge(
                network_id=network.id,
                account_a_id=account_id_map[edge.account_a],
                account_b_id=account_id_map[edge.account_b],
                w_total=edge.w_total,
                w_time=edge.per_signal.get("w_time"),
                w_text=edge.per_signal.get("w_text"),
                w_amp=edge.per_signal.get("w_amp"),
                w_meta=edge.per_signal.get("w_meta"),
                w_struct=edge.per_signal.get("w_struct"),
                signal_count=len(edge.per_signal),
            )
        )
    for post in snapshot.representative_content:
        db.add(
            NetworkEvidencePost(
                network_id=network.id,
                account_id=account_id_map[post.account_id],
                post_platform_id=post.post_id,
                captured_text=post.captured_text,
                posted_at=post.posted_at,
                content_sha256=post.content_sha256,
                duplicate_group_id=post.duplicate_group_id,
                is_canonical=post.is_canonical,
            )
        )
    for burst_bin in snapshot.burst_timeline:
        db.add(
            NetworkBurstBin(
                network_id=network.id,
                bin_start=burst_bin.bin_start,
                bin_width_seconds=60,
                post_count=burst_bin.post_count,
                zscore=burst_bin.zscore,
                is_anomalous=burst_bin.is_anomalous,
            )
        )
    db.add(
        NetworkClaimLink(
            network_id=network.id,
            claim_id=claim_id,
            overlap_ratio=relevance_overlap_ratio,
            anchoring_share=relevance_anchoring_share,
            claim_cluster_post_count=relevance_post_count,
            is_primary_claim=True,  # single-claim run - see module docstring
            passed_relevance_gate=True,
        )
    )
    return network


# --- Top-level entrypoint -----------------------------------------------------------


async def run_detection_for_claim(
    claim_id: uuid.UUID,
    window_hours: float | None = None,
    embedder: MultilingualEmbeddingService | None = None,
    session_factory: async_sessionmaker | None = None,
    params: DetectionParams | None = None,
) -> uuid.UUID | None:
    """Runs the full Stage 0-6 pipeline for one claim and persists the result.
    Returns the created detection_run id, or None if the claim doesn't exist or isn't
    an Existing claim. Safe to call repeatedly - each call is one fresh run, not
    idempotent by design (recurrence tracking is how repeat detections get linked,
    not deduplication)."""
    embedder = embedder or get_multilingual_embedding_service()
    session_factory = session_factory or get_session_factory()
    params = params or DetectionParams()

    async with session_factory() as db:
        claim = await db.get(Claim, claim_id)
        if claim is None or claim.claim_type != ClaimType.EXISTING:
            logger.warning("Detection run skipped - claim %s not found or not Existing", claim_id)
            return None

        effective_window_hours = window_hours if window_hours is not None else params.window_hours

        window_end = datetime.now(UTC)
        window_start = window_end - timedelta(hours=effective_window_hours)

        run = DetectionRun(
            scope_claim_ids=[str(claim_id)],
            window_start=window_start,
            window_end=window_end,
            parameters=_run_parameters(effective_window_hours, params),
            model_versions=MODEL_VERSIONS,
            random_seed=DEFAULT_RANDOM_SEED,
            candidates_count=0,
            status=DetectionRunStatus.RUNNING,
        )
        db.add(run)
        await db.flush()

        try:
            posts = await _load_candidate_posts(db, claim_id, window_start, window_end)
            allowlisted_handles = await _load_allowlisted_handles(db)
            selection = select_candidates(
                posts,
                allowlisted_account_ids=allowlisted_handles,
                self_exclusion_account_ids=set(params.self_exclusion_handles),
                a_max=params.a_max,
            )

            if len(selection.account_ids) < MIN_CANDIDATES_TO_RUN:
                run.status = DetectionRunStatus.COMPLETED
                run.candidates_count = selection.candidates_count
                run.truncated = selection.truncated
                run.completed_at = datetime.now(UTC)
                await db.commit()
                return run.id

            signal_accounts = [SignalAccount(account_id=a, handle=a) for a in selection.account_ids]
            signals = _build_signals(selection.posts, signal_accounts, embedder, params)
            edges, unavailable = fuse_and_prune(signals, theta_edge=params.theta_edge)
            communities = detect_communities(
                edges, selection.account_ids,
                k_core=params.k_core, resolution=params.leiden_resolution,
                n_min=params.n_min, rho_min=params.rho_min,
                random_seed=DEFAULT_RANDOM_SEED,
            )

            total_posts_by_account = await _total_post_counts(
                db, selection.account_ids, window_start, window_end
            )
            account_id_map = await _get_or_create_accounts(db, set(selection.account_ids))
            recurrence_candidates = await _load_recurrence_candidates(db)

            for community in communities:
                member_set = set(community.account_ids)
                cluster_posts = [p for p in selection.posts if p.account_id in member_set]
                relevance = evaluate_claim_relevance(
                    community.account_ids, selection.posts, total_posts_by_account,
                    mu_anchor=params.mu_anchor, p_min=params.p_min, omega_min=params.omega_min,
                )
                fingerprint = compute_fingerprint(community.account_ids, _extract_top_terms(cluster_posts))
                metrics = compute_cluster_metrics(
                    community,
                    cluster_posts,
                    signal_accounts,
                    signals["w_time"] or {},
                    window_hours=effective_window_hours,
                    now=window_end,
                    embedder=embedder,
                )

                if not relevance.passed:
                    await _persist_offtopic_cluster(
                        db, run.id, claim_id, community, cluster_posts, metrics,
                        relevance.overlap_ratio, relevance.anchoring_share, fingerprint,
                        relevance.failed_test,
                    )
                    continue

                if is_allowlist_suppressed(community.account_ids, allowlisted_handles):
                    logger.info(
                        "Network suppressed as allowlist hit (run %s, %d members)",
                        run.id, len(community.account_ids),
                    )
                    continue

                breadth = compute_signal_breadth(
                    {"sy": metrics.sy, "du": metrics.du, "co": metrics.co, "pr": metrics.pr, "au": metrics.au}
                )
                band = determine_confidence_band(
                    metrics.coordination_score, breadth,
                    run_truncated=selection.truncated,
                    unavailable_signal_count=len(unavailable),
                )
                parent_id = find_recurrence_parent(member_set, recurrence_candidates)

                network = await _persist_network(
                    db, run.id, claim_id, community, cluster_posts, signal_accounts, edges,
                    metrics, breadth, band, fingerprint,
                    uuid.UUID(parent_id) if parent_id else None,
                    relevance.overlap_ratio, relevance.anchoring_share,
                    relevance.claim_cluster_post_count, account_id_map, embedder,
                )
                recurrence_candidates.append(RecurrenceCandidate(str(network.id), member_set))

            run.status = DetectionRunStatus.COMPLETED
            run.candidates_count = selection.candidates_count
            run.truncated = selection.truncated
            run.signals_unavailable = unavailable
            run.completed_at = datetime.now(UTC)
            await db.commit()
        except Exception:
            logger.exception("Detection run %s failed for claim %s", run.id, claim_id)
            run.status = DetectionRunStatus.FAILED
            run.completed_at = datetime.now(UTC)
            await db.commit()

        return run.id


# --- Sweep + the single external trigger dispatcher --------------------------------


async def run_scheduled_sweep(
    session_factory: async_sessionmaker | None = None,
    embedder: MultilingualEmbeddingService | None = None,
    params: DetectionParams | None = None,
) -> list[uuid.UUID]:
    """Runs detection across every claim with status Active, plus a housekeeping
    evidence-retention purge (PRD 10.9.1 point 7) beforehand. Stateless and doesn't
    track when it was last run; cadence is entirely the backend's decision now (it
    calls POST /coordination/detection-runs with no claim_id on whatever schedule it
    chooses - PRD 10.5.8 point 1)."""
    session_factory = session_factory or get_session_factory()
    embedder = embedder or get_multilingual_embedding_service()
    params = params or DetectionParams()

    async with session_factory() as db:
        await governance.purge_expired_evidence(db)
        claim_ids = (
            await db.execute(
                select(Claim.id).where(
                    Claim.claim_type == ClaimType.EXISTING, Claim.status == ClaimStatus.ACTIVE
                )
            )
        ).scalars().all()

    run_ids: list[uuid.UUID] = []
    for claim_id in claim_ids:
        run_id = await run_detection_for_claim(
            claim_id, embedder=embedder, session_factory=session_factory, params=params
        )
        if run_id is not None:
            run_ids.append(run_id)
    return run_ids


async def trigger_detection_run(
    claim_id: uuid.UUID | None,
    overrides: DetectionRunOverrides | None,
    embedder: MultilingualEmbeddingService | None = None,
    session_factory: async_sessionmaker | None = None,
) -> None:
    """The dispatcher behind POST /coordination/detection-runs (networks.py) - the
    AI service's one remaining F5 endpoint. claim_id set -> single-claim run;
    claim_id None -> full active-claims sweep. Runs as a background task, so nothing
    is returned to the caller beyond the 202 already sent."""
    params = _effective_params(overrides)
    if claim_id is not None:
        await run_detection_for_claim(
            claim_id, embedder=embedder, session_factory=session_factory, params=params
        )
    else:
        await run_scheduled_sweep(session_factory=session_factory, embedder=embedder, params=params)
