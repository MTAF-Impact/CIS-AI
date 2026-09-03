"""Coordinated-network detection pipeline: run creation, signal computation, and
persistence for POST /api/v1/detection/runs."""

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

MIN_CANDIDATES_TO_RUN = 2
DEFAULT_RANDOM_SEED = 42
MODEL_VERSIONS = {"leidenalg": "0.12", "igraph": "1.0"}
DEFAULT_RETENTION_MONTHS = 24
COMPARISON_ACCOUNT_CAP_MULTIPLIER = 1
STOP_WORDS = frozenset(
    {"the", "a", "an", "is", "are", "was", "were", "this", "that", "it", "in", "on", "for", "of", "to", "and", "with"}
)


def _library_version_string() -> str:
    return ",".join(f"{name}=={version}" for name, version in MODEL_VERSIONS.items())


def _run_parameters(params: DetectorParameters) -> dict:
    return params.model_dump()


# --- Run creation (synchronous half of POST /api/v1/detection/runs) ----------------


async def create_pending_run(db: AsyncSession, payload: DetectionRunRequest) -> DetectionRun:
    """Writes the detection_run row synchronously so run_id is queryable before the
    202 response returns."""
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
                # Syndicated news content publishes near-identical text simultaneously
                # by design, which would misread as coordination.
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
            source=item.source,
            outbound_urls=tuple(item.outbound_urls or ()),
        )
        for item in rows
    ]


async def _total_post_counts(
    db: AsyncSession, account_ids: list[str], window_start: datetime, window_end: datetime
) -> dict[str, int]:
    """Each account's post volume across all monitored content in the window, not
    just this claim - the denominator the claim-relevance gate needs."""
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
    """Get-or-create account rows keyed by (platform, platform_account_id)."""
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
    """Read-only lookup of w_meta provenance already on an Account row. A missing
    account means the caller falls back to an all-fields-None SignalAccount."""
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
    """Compact content signature for the recurrence fingerprint."""
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
    """Unclustered accounts active on the same claim, for graph contrast. Ranked by
    post volume, capped to roughly the network's own size."""
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
    """A coordinated cluster that isn't about this claim - not surfaced, kept only
    for recalibration review."""
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
    """Deterministic fallback label if the LLM is unavailable or fails."""
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
            is_primary_claim=True,
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
    """Runs the pipeline over every claim in claim_ids against the already-created
    `run_id` row. Loops per claim rather than pooling candidates across claims.

    synthetic_follower_sets/platform_age_baseline_hours: demo-only signal inputs
    (see demo_seed.py); always None on a real run since no follower-graph or
    platform-age-distribution source exists.

    llm: not auto-defaulted - callers must resolve and pass it explicitly via
    Depends(), so a background task never bypasses dependency_overrides in tests.
    Falls back to a deterministic label if None or on failure."""
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
