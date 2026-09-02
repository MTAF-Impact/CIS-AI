"""PRD 10.5.8 - Stage 7: execution model. The backend's actual reference contract
(CIS-Backend internal/aiclient/detection.go, docs/AI-INTEGRATION.md Flow 7/8, pulled
and reviewed this session) drives every shape here, not PRD 10.5.8 read in isolation:
`create_pending_run`/`run_detection` are the two halves of POST /api/v1/detection/runs
(create synchronously so `run_id` is real before the 202 response, then run in the
background); `claim_ids` is always a list - one for a claim-scoped run, many for a
topic-batch run, both in one `detection_run` row. The backend computes window_start/
window_end and sends the full detector parameter set on every call - there is no
DB-backed config or partial-override concept on this side any more.

Known data-availability gaps, all documented rather than faked: ContentItem carries
no reshare/quote/reply/outbound-link fields yet, so w_amp is effectively empty until
ingestion captures them; no ingestion path populates account bio/declared_location/
client_app yet, so w_meta and the PR metric run on a subset of their stated inputs;
no follower-graph source exists, so w_struct is always unavailable; content_items has
no separate posted_at (publish time) field yet, so `network_evidence_post.posted_at`
is backfilled from ContentItem.created_at (ingest time) as an interim stand-in - see
docs/COORDINATION.md. Every signal function already treats "unavailable" as
*unavailable*, not zero - this pipeline inherits that honesty rather than working
around it."""

import hashlib
import logging
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import get_session_factory
from app.models.claim import Claim
from app.models.content import ContentItem
from app.models.coordination import (
    Account,
    CoordinatedNetwork,
    DetectionRun,
    EvidenceSnapshot,
    NetworkAccount,
    NetworkBurstBin,
    NetworkClaimLink,
    NetworkEdge,
    NetworkEvidencePost,
    OfftopicCluster,
)
from app.models.enums import ClaimType, ContentSource, DetectionRunStatus, Stance
from app.schemas.coordination_network import (
    DetectionRunRequest,
    DetectorParameters,
    Exclusions,
)
from app.services.coordination.cluster_metrics import (
    ClusterMetrics,
    compute_cluster_metrics,
)
from app.services.coordination.clustering import DetectedCommunity, detect_communities
from app.services.coordination.confidence import (
    compute_signal_breadth,
    determine_confidence_band,
    is_allowlist_suppressed,
)
from app.services.coordination.evidence import build_evidence_snapshot
from app.services.coordination.fusion import FusedEdge, fuse_and_prune
from app.services.coordination.recurrence import (
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
from app.services.llm_client import LLMClient
from app.services.multilingual_embedding_service import (
    MultilingualEmbeddingService,
    get_multilingual_embedding_service,
)

logger = logging.getLogger(__name__)

MIN_CANDIDATES_TO_RUN = 2  # below this, no cluster (min_cluster_size=5 by default) could ever form
DEFAULT_RANDOM_SEED = 42
MODEL_VERSIONS = {"leidenalg": "0.12", "igraph": "1.0"}  # recorded for reproducibility, 10.5.6 pt 7
DEFAULT_RETENTION_MONTHS = 24  # initial evidence_snapshot.expires_at; actual purging is backend-driven
COMPARISON_ACCOUNT_CAP_MULTIPLIER = 1  # comparison set capped to ~network size, see _select_comparison_accounts
STOP_WORDS = frozenset(
    {"the", "a", "an", "is", "are", "was", "were", "this", "that", "it", "in", "on", "for", "of", "to", "and", "with"}
)


def _library_version_string() -> str:
    return ",".join(f"{name}=={version}" for name, version in MODEL_VERSIONS.items())


def _run_parameters(params: DetectorParameters) -> dict:
    """Every parameter actually in force for this run, per PRD 10.5.6 point 7 - "a
    report generated months later can state the exact configuration that produced
    it." Persisted verbatim, matching the backend's expectation that
    detection_run.parameters_json holds exactly what it sent."""
    return params.model_dump()


# --- Run creation (synchronous half of POST /api/v1/detection/runs) ----------------


async def create_pending_run(db: AsyncSession, payload: DetectionRunRequest) -> DetectionRun:
    """Writes the detection_run row synchronously, before the 202 response, so
    run_id is real and immediately queryable - the backend never polls, it reads
    this row directly."""
    run = DetectionRun(
        scope_claim_ids=[str(c) for c in payload.claim_ids],
        trigger_source=payload.trigger_source,
        window_start=payload.window_start,
        window_end=payload.window_end,
        parameters=_run_parameters(payload.parameters),
        model_versions=MODEL_VERSIONS,
        library_version=_library_version_string(),
        random_seed=DEFAULT_RANDOM_SEED,
        candidates_count=0,
        status=DetectionRunStatus.PENDING,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


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
                # News sources are excluded from F5 candidate scope entirely (Data
                # Pipeline & Source Spec v1.0, D6): article timestamps are coarse and
                # legitimate syndication means outlets publish near-identical text
                # simultaneously by design - exactly what w_time/w_text would read as
                # coordination. Only RSS is a live news-type source today; RADIO/
                # FORUM/OTHER aren't populated by any real fetcher yet.
                ContentItem.source != ContentSource.RSS,
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
            outbound_urls=tuple(item.outbound_urls or ()),
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


async def _get_or_create_accounts(
    db: AsyncSession, platform_account_ids: set[str], platform: str = "unknown"
) -> dict[str, uuid.UUID]:
    """Get-or-create account rows keyed by (platform, platform_account_id) - the
    table now has a real UNIQUE constraint on that pair (matching the backend's
    reference schema), so this is also where a concurrent-insert race would surface;
    fine for the sequential runs this pipeline supports today."""
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


async def _load_account_provenance(
    db: AsyncSession, platform_account_ids: set[str], platform: str = "unknown"
) -> dict[str, SignalAccount]:
    """Read-only lookup of whatever w_meta provenance already exists on an Account
    row (from a prior detection run, or - today - only the demo generator, which is
    the only writer of these fields; no real fetcher populates them yet). Missing
    account -> not in the returned dict -> caller falls back to an all-fields-None
    SignalAccount, i.e. today's existing "unavailable" behavior for every account no
    prior run or the demo has ever touched. Never creates a row - that's still
    _get_or_create_accounts's job, at persistence time."""
    if not platform_account_ids:
        return {}
    existing = (
        await db.execute(
            select(Account).where(
                Account.platform == platform, Account.platform_account_id.in_(platform_account_ids)
            )
        )
    ).scalars().all()
    return {
        a.platform_account_id: SignalAccount(
            account_id=a.platform_account_id,
            handle=a.handle,
            created_at_platform=a.created_at_platform,
            profile_hash=a.profile_hash,
            bio=a.bio,
            declared_location=a.declared_location,
            client_app=a.client_app,
        )
        for a in existing
    }


async def _load_recurrence_candidates(db: AsyncSession) -> list[RecurrenceCandidate]:
    rows = (
        await db.execute(
            select(NetworkAccount.network_id, Account.platform_account_id)
            .join(Account, Account.id == NetworkAccount.account_id)
            .where(NetworkAccount.membership_role == "member")
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


def _select_comparison_accounts(
    all_candidate_ids: list[str],
    clustered_ids: set[str],
    community_size: int,
    total_posts_by_account: dict[str, int],
) -> list[str]:
    """BEYOND 10.10 (backend gap 7) - US51's "genuine unclustered accounts active on
    the same claim, for contrast". Ranked by post volume (same tie-break convention
    as the A_max scale control) for determinism, capped to roughly the network's own
    size so the comparison set doesn't dwarf the network it's contrasting."""
    unclustered = [a for a in all_candidate_ids if a not in clustered_ids]
    ranked = sorted(unclustered, key=lambda a: total_posts_by_account.get(a, 0), reverse=True)
    cap = max(community_size * COMPARISON_ACCOUNT_CAP_MULTIPLIER, 1)
    return ranked[:cap]


def _build_signals(
    posts: list[SignalPost],
    accounts: list[SignalAccount],
    embedder: MultilingualEmbeddingService,
    params: DetectorParameters,
    common_phrase_allowlist: set[str],
    follower_sets: dict[str, set[str]] | None = None,
) -> dict[str, dict[tuple[str, str], float] | None]:
    return {
        "w_time": compute_temporal_synchrony(
            posts, bin_width_seconds=params.bin_width_seconds, alpha=params.null_model_alpha
        ),
        "w_text": compute_content_duplication(
            posts,
            common_phrase_allowlist=common_phrase_allowlist,
            tau_dup=params.dup_threshold,
            tau_sem=params.sem_threshold,
            l_min=params.min_post_length,
            embedder=embedder,
        ),
        "w_amp": compute_co_amplification(posts),
        "w_meta": compute_provenance_similarity(accounts, half_life_hours=params.provenance_half_life_hours),
        # None for every real (backend-triggered) run - no follower-graph data
        # source exists yet. Only ever non-None when demo_seed.py passes
        # run_detection's synthetic_follower_sets, threaded through from there.
        "w_struct": compute_structural_overlap(follower_sets),
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
    review (the backend's - it reads this table directly)."""
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


async def _generate_network_label(
    llm: LLMClient | None, claim_statement: str, cluster_posts: list[SignalPost]
) -> str:
    """LLMClient.generate_network_label, with a deterministic fallback - never
    blocks persistence on a missing/failed LLM call, matching the graceful-
    degradation posture used everywhere else in this codebase (e.g.
    clustering_service.build_claim_from_content_items's harm-classification try/
    except)."""
    fallback = f"Coordinated activity: {claim_statement}"[:255]
    if llm is None:
        return fallback
    sample_texts = [p.text for p in cluster_posts[:10]]
    try:
        return await llm.generate_network_label(claim_statement, sample_texts)
    except Exception:
        logger.exception("Network label generation failed; using deterministic fallback")
        return fallback


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
    allowlist_suppressed: bool,
    comparison_account_ids: list[str],
    total_posts_by_account: dict[str, int],
    common_phrase_allowlist: set[str],
    claim_statement: str,
    llm: LLMClient | None,
) -> CoordinatedNetwork:
    snapshot = build_evidence_snapshot(
        community, cluster_posts, accounts, edges, embedder=embedder,
        common_phrase_allowlist=common_phrase_allowlist,
    )
    platforms = sorted({p.source for p in cluster_posts if p.source})
    label = await _generate_network_label(llm, claim_statement, cluster_posts)

    network = CoordinatedNetwork(
        run_id=run_id,
        label=label,
        coordination_score=metrics.coordination_score,
        sy=metrics.sy,
        du=metrics.du,
        co=metrics.co,
        pr=metrics.pr,
        au=metrics.au,
        signal_breadth=breadth,
        confidence_band=band,
        raw_counts=metrics.raw_counts,
        account_count=len(community.account_ids),
        post_count=len(cluster_posts),
        platforms=platforms,
        internal_density=community.internal_density,
        conductance=community.conductance,
        comparison_account_count=len(comparison_account_ids),
        fingerprint_hash=fingerprint,
        parent_network_id=parent_network_id,
        allowlist_suppressed=allowlist_suppressed,
    )
    db.add(network)
    await db.flush()

    for entry in snapshot.account_annex:
        xy = snapshot.graph.layout.get(entry.account_id)
        db.add(
            NetworkAccount(
                network_id=network.id,
                account_id=account_id_map[entry.account_id],
                membership_role="member",
                posts_in_cluster=entry.posts_in_cluster,
                duplication_rate=entry.duplication_rate,
                median_interpost_interval_seconds=entry.median_interpost_interval_seconds,
                circadian_coverage=entry.circadian_coverage,
                degree_centrality=entry.degree_centrality,
                eigenvector_centrality=entry.eigenvector_centrality,
                # Per-metric contribution breakdown (not just aggregate stats) is a
                # future refinement - left empty rather than faked.
                score_contribution={},
                layout_x=xy[0] if xy else None,
                layout_y=xy[1] if xy else None,
            )
        )
    comparison_id_map = await _get_or_create_accounts(db, set(comparison_account_ids))
    for account_id in comparison_account_ids:
        db.add(
            NetworkAccount(
                network_id=network.id,
                account_id=comparison_id_map[account_id],
                membership_role="comparison",
                posts_in_cluster=total_posts_by_account.get(account_id, 0),
                duplication_rate=0.0,
                circadian_coverage=0.0,
                degree_centrality=0.0,
                eigenvector_centrality=0.0,
                score_contribution={},
            )
        )
    for edge in snapshot.graph.edges:
        db.add(
            NetworkEdge(
                network_id=network.id,
                account_a=account_id_map[edge.account_a],
                account_b=account_id_map[edge.account_b],
                w_total=edge.w_total,
                w_time=edge.per_signal.get("w_time", 0.0),
                w_text=edge.per_signal.get("w_text", 0.0),
                w_amp=edge.per_signal.get("w_amp", 0.0),
                w_meta=edge.per_signal.get("w_meta", 0.0),
                w_struct=edge.per_signal.get("w_struct", 0.0),
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
                # Interim stand-in for real publish time - see module docstring.
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
            is_primary_claim=True,  # single-claim link per network - see module docstring
            passed_relevance_gate=True,
        )
    )

    snapshot_digest = hashlib.sha256(
        "|".join(sorted(p.content_sha256 for p in snapshot.representative_content)).encode("utf-8")
    ).hexdigest()
    created_at = datetime.now(UTC)
    db.add(
        EvidenceSnapshot(
            network_id=network.id,
            run_id=run_id,
            snapshot_sha256=snapshot_digest,
            evidence_post_count=len(snapshot.representative_content),
            expires_at=created_at + timedelta(days=DEFAULT_RETENTION_MONTHS * 30),
        )
    )
    return network


# --- Top-level entrypoint: the background half of POST /api/v1/detection/runs ------


async def run_detection(
    run_id: uuid.UUID,
    claim_ids: list[uuid.UUID],
    window_start: datetime,
    window_end: datetime,
    parameters: DetectorParameters,
    exclusions: Exclusions,
    embedder: MultilingualEmbeddingService | None = None,
    session_factory: async_sessionmaker | None = None,
    synthetic_follower_sets: dict[str, set[str]] | None = None,
    platform_age_baseline_hours: list[float] | None = None,
    llm: LLMClient | None = None,
) -> None:
    """Runs the full Stage 0-6 pipeline over every claim in claim_ids against the
    already-created `run_id` row (create_pending_run wrote it synchronously before
    the 202 response). One run, one or many claims - PRD 10.5.1 point 6: signals and
    cluster metrics remain computed per claim even in batch mode, so this loops
    internally rather than pooling candidates across claims (pooling is an optional
    efficiency optimisation the PRD explicitly does not require).

    synthetic_follower_sets: w_struct's only wiring point, always None for the real
    backend-triggered contract (POST /api/v1/detection/runs never passes it - no
    follower-graph data source exists for any real platform we've wired up). Exists
    so app/services/coordination/demo_seed.py can simulate a complete-signal
    scenario through this exact function, keyed by account_id -> the set of
    account_ids it "follows". Never persisted anywhere - purely an in-memory signal
    input for the run it's passed to.

    platform_age_baseline_hours: same demo-only story, for PR's age-percentile
    sub-signal (cluster_metrics._age_percentile_inverted) - this service has no live
    platform-wide account-age distribution to compare against, so that sub-signal is
    unavailable (dropped from PR's average, not scored as 0) on every real run
    unless a caller supplies one.

    llm: used only to generate coordinated_network.label (see
    LLMClient.generate_network_label) - falls back to a deterministic label built
    from the claim statement when None or on any failure, never blocks detection.
    Deliberately NOT auto-defaulted to get_llm_client() here the way embedder is
    above - every real caller (trigger_detection_run, generate_coordinated_network,
    scripts/seed_demo_data.py) must resolve and pass it explicitly (Depends() in the
    two endpoints), same as every other background-task-scheduling endpoint in this
    codebase. A bare get_llm_client() call from inside a background task bypasses
    FastAPI's dependency_overrides entirely, which would silently make this the one
    F5 code path hitting a real LLM in tests - see ARCHITECTURE.md's background-task
    section for the historical bug this exact pattern already caused once."""
    embedder = embedder or get_multilingual_embedding_service()
    session_factory = session_factory or get_session_factory()

    async with session_factory() as db:
        run = await db.get(DetectionRun, run_id)
        if run is None:
            logger.error("Detection run %s vanished before it could start", run_id)
            return
        run.status = DetectionRunStatus.RUNNING
        await db.commit()

        allowlisted_handles = {a.handle for a in exclusions.accounts}
        common_phrases = set(exclusions.phrases)

        total_candidates = 0
        any_truncated = False
        unavailable_union: set[str] = set()

        try:
            for claim_id in claim_ids:
                claim = await db.get(Claim, claim_id)
                if claim is None or claim.claim_type != ClaimType.EXISTING:
                    logger.warning(
                        "Detection run %s: claim %s not found or not Existing, skipped", run_id, claim_id
                    )
                    continue

                posts = await _load_candidate_posts(db, claim_id, window_start, window_end)
                selection = select_candidates(
                    posts,
                    allowlisted_account_ids=allowlisted_handles,
                    a_max=parameters.candidate_cap,
                )
                total_candidates += selection.candidates_count
                any_truncated = any_truncated or selection.truncated

                if len(selection.account_ids) < MIN_CANDIDATES_TO_RUN:
                    continue

                account_provenance = await _load_account_provenance(db, set(selection.account_ids))
                signal_accounts = [
                    account_provenance.get(a) or SignalAccount(account_id=a, handle=a)
                    for a in selection.account_ids
                ]
                signals = _build_signals(
                    selection.posts, signal_accounts, embedder, parameters, common_phrases,
                    follower_sets=synthetic_follower_sets,
                )
                edges, unavailable = fuse_and_prune(
                    signals,
                    theta_edge=parameters.edge_threshold,
                    min_signal_families=parameters.min_signal_families,
                )
                unavailable_union.update(unavailable)
                communities = detect_communities(
                    edges, selection.account_ids,
                    k_core=parameters.k_core, resolution=parameters.leiden_resolution,
                    n_min=parameters.min_cluster_size, rho_min=parameters.min_internal_density,
                    random_seed=DEFAULT_RANDOM_SEED,
                )
                if not communities:
                    continue

                total_posts_by_account = await _total_post_counts(
                    db, selection.account_ids, window_start, window_end
                )
                account_id_map = await _get_or_create_accounts(db, set(selection.account_ids))
                recurrence_candidates = await _load_recurrence_candidates(db)
                clustered_ids: set[str] = {a for c in communities for a in c.account_ids}

                for community in communities:
                    member_set = set(community.account_ids)
                    cluster_posts = [p for p in selection.posts if p.account_id in member_set]
                    relevance = evaluate_claim_relevance(
                        community.account_ids, selection.posts, total_posts_by_account,
                        mu_anchor=parameters.anchor_share, p_min=parameters.min_claim_posts,
                        omega_min=parameters.min_link_strength,
                    )
                    fingerprint = compute_fingerprint(community.account_ids, _extract_top_terms(cluster_posts))
                    metrics = compute_cluster_metrics(
                        community, cluster_posts, signal_accounts,
                        signals["w_time"] or {},
                        window_hours=(window_end - window_start).total_seconds() / 3600,
                        now=window_end, embedder=embedder,
                        common_phrase_allowlist=common_phrases,
                        platform_age_baseline_hours=platform_age_baseline_hours,
                    )

                    if not relevance.passed:
                        await _persist_offtopic_cluster(
                            db, run_id, claim_id, community, cluster_posts, metrics,
                            relevance.overlap_ratio, relevance.anchoring_share, fingerprint,
                            relevance.failed_test,
                        )
                        continue

                    allowlist_suppressed = is_allowlist_suppressed(community.account_ids, allowlisted_handles)
                    breadth = compute_signal_breadth(
                        {"sy": metrics.sy, "du": metrics.du, "co": metrics.co, "pr": metrics.pr, "au": metrics.au}
                    )
                    band = determine_confidence_band(
                        metrics.coordination_score, breadth,
                        run_truncated=selection.truncated,
                        unavailable_signal_count=len(unavailable),
                        high_score_min=parameters.high_score_cutoff,
                        high_breadth_min=parameters.high_breadth_cutoff,
                        medium_score_min=parameters.medium_score_cutoff,
                        medium_breadth_min=parameters.medium_breadth_cutoff,
                    )
                    parent_id = find_recurrence_parent(member_set, recurrence_candidates)
                    comparison_ids = _select_comparison_accounts(
                        selection.account_ids, clustered_ids, len(community.account_ids), total_posts_by_account
                    )

                    network = await _persist_network(
                        db, run_id, claim_id, community, cluster_posts, signal_accounts, edges,
                        metrics, breadth, band, fingerprint,
                        uuid.UUID(parent_id) if parent_id else None,
                        relevance.overlap_ratio, relevance.anchoring_share,
                        relevance.claim_cluster_post_count, account_id_map, embedder,
                        allowlist_suppressed, comparison_ids, total_posts_by_account, common_phrases,
                        claim.claim_statement, llm,
                    )
                    recurrence_candidates.append(RecurrenceCandidate(str(network.id), member_set))

            run.status = DetectionRunStatus.COMPLETED
            run.candidates_count = total_candidates
            run.truncated = any_truncated
            run.signals_unavailable = sorted(unavailable_union)
            run.completed_at = datetime.now(UTC)
            await db.commit()
        except Exception as exc:
            logger.exception("Detection run %s failed", run_id)
            run.status = DetectionRunStatus.FAILED
            run.error = str(exc)[:2000]
            run.completed_at = datetime.now(UTC)
            await db.commit()
