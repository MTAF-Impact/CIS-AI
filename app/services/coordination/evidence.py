"""PRD 10.5.6 - Stage 5: evidence extraction and snapshotting. Every detected network
produces an immutable evidence snapshot at detection time - this is a hard
requirement, because operators frequently delete content once a campaign concludes,
and a report that cannot show its own evidence is worthless."""

import hashlib
import logging
import math
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

import igraph as ig

from app.services.coordination.clustering import DetectedCommunity
from app.services.coordination.fusion import FusedEdge
from app.services.coordination.signals.duplication import find_duplicate_post_pairs
from app.services.coordination.types import SignalAccount, SignalPost
from app.services.multilingual_embedding_service import (
    MultilingualEmbeddingService,
    get_multilingual_embedding_service,
)

logger = logging.getLogger(__name__)

BURST_BIN_WIDTH_SECONDS = 60
BURST_SIGMA_MULTIPLIER = 3


@dataclass(frozen=True)
class BurstBin:
    bin_start: datetime
    post_count: int
    zscore: float
    is_anomalous: bool


@dataclass(frozen=True)
class EvidencePost:
    post_id: str
    account_id: str
    captured_text: str
    posted_at: datetime
    content_sha256: str
    duplicate_group_id: uuid.UUID | None
    is_canonical: bool


@dataclass(frozen=True)
class AccountAnnexEntry:
    account_id: str
    handle: str
    posts_in_cluster: int
    duplication_rate: float
    median_interpost_interval_seconds: float | None
    circadian_coverage: float
    degree_centrality: float
    eigenvector_centrality: float


@dataclass(frozen=True)
class GraphEdgeSnapshot:
    account_a: str
    account_b: str
    w_total: float
    per_signal: dict[str, float]


@dataclass(frozen=True)
class GraphSnapshot:
    edges: list[GraphEdgeSnapshot]
    layout: dict[str, tuple[float, float]]  # account_id -> (x, y)


@dataclass(frozen=True)
class EvidenceSnapshot:
    burst_timeline: list[BurstBin]
    representative_content: list[EvidencePost]
    account_annex: list[AccountAnnexEntry]
    graph: GraphSnapshot


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_burst_timeline(
    cluster_posts: list[SignalPost], bin_width_seconds: int = BURST_BIN_WIDTH_SECONDS
) -> list[BurstBin]:
    """Full per-bin volume series across the cluster's own posting window - every
    bin, not just anomalous ones, so the detail page (US53) can render a real chart."""
    if not cluster_posts:
        return []
    bin_counts: dict[int, int] = defaultdict(int)
    for p in cluster_posts:
        bin_counts[int(p.created_at.timestamp() // bin_width_seconds)] += 1

    counts = list(bin_counts.values())
    mean = sum(counts) / len(counts)
    variance = sum((c - mean) ** 2 for c in counts) / len(counts)
    std = math.sqrt(variance)
    threshold = mean + BURST_SIGMA_MULTIPLIER * std

    bins: list[BurstBin] = []
    for bin_idx in sorted(bin_counts):
        count = bin_counts[bin_idx]
        zscore = (count - mean) / std if std > 0 else 0.0
        bins.append(
            BurstBin(
                bin_start=datetime.fromtimestamp(bin_idx * bin_width_seconds, tz=UTC),
                post_count=count,
                zscore=round(zscore, 4),
                is_anomalous=count > threshold,
            )
        )
    return bins


def build_representative_content(
    cluster_posts: list[SignalPost],
    embedder: MultilingualEmbeddingService | None = None,
    common_phrase_allowlist: set[str] | None = None,
) -> list[EvidencePost]:
    """Groups duplicate posts into connected components via union-find over the
    pairwise duplicate flags (2a/2b); the earliest post in each group is canonical.
    Every post is captured with its SHA-256 regardless of group membership, so a
    later-deleted post still has durable evidence (US54)."""
    embedder = embedder or get_multilingual_embedding_service()
    eligible, duplicate_pairs = find_duplicate_post_pairs(
        cluster_posts, common_phrase_allowlist=common_phrase_allowlist, embedder=embedder
    )

    parent = list(range(len(eligible)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, j in duplicate_pairs:
        union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for idx in range(len(eligible)):
        groups[find(idx)].append(idx)

    duplicate_group_by_post_id: dict[str, uuid.UUID] = {}
    canonical_post_ids: set[str] = set()
    for members in groups.values():
        if len(members) < 2:
            continue
        member_posts = [eligible[m] for m in members]
        # Deterministic UUID (uuid5, not uuid4) so the same duplicate set produces
        # the same group id across regenerations - the backend's schema requires a
        # real uuid column here, not the truncated sha256 string this used to be.
        group_id = uuid.uuid5(uuid.NAMESPACE_OID, "|".join(sorted(p.id for p in member_posts)))
        canonical = min(member_posts, key=lambda p: p.created_at)
        canonical_post_ids.add(canonical.id)
        for p in member_posts:
            duplicate_group_by_post_id[p.id] = group_id

    return [
        EvidencePost(
            post_id=p.id,
            account_id=p.account_id,
            captured_text=p.text,
            posted_at=p.created_at,
            content_sha256=_sha256(p.text),
            duplicate_group_id=duplicate_group_by_post_id.get(p.id),
            is_canonical=p.id in canonical_post_ids,
        )
        for p in cluster_posts
    ]


def _duplication_rate_by_account(
    cluster_posts: list[SignalPost], representative_content: list[EvidencePost]
) -> dict[str, float]:
    duplicated_ids = {e.post_id for e in representative_content if e.duplicate_group_id is not None}
    counts: dict[str, int] = defaultdict(int)
    dup_counts: dict[str, int] = defaultdict(int)
    for p in cluster_posts:
        counts[p.account_id] += 1
        if p.id in duplicated_ids:
            dup_counts[p.account_id] += 1
    return {a: dup_counts.get(a, 0) / c for a, c in counts.items() if c > 0}


def _member_subgraph(community: DetectedCommunity, edges: list[FusedEdge]) -> tuple[ig.Graph, list[FusedEdge]]:
    member_set = set(community.account_ids)
    member_edges = [e for e in edges if e.account_a in member_set and e.account_b in member_set]
    graph = ig.Graph()
    graph.add_vertices(community.account_ids)
    if member_edges:
        graph.add_edges(
            [(e.account_a, e.account_b) for e in member_edges],
            attributes={"weight": [e.w_total for e in member_edges]},
        )
    return graph, member_edges


def build_account_annex(
    community: DetectedCommunity,
    cluster_posts: list[SignalPost],
    accounts: list[SignalAccount],
    edges: list[FusedEdge],
    duplication_rates: dict[str, float] | None = None,
) -> list[AccountAnnexEntry]:
    posts_by_account: dict[str, list[SignalPost]] = defaultdict(list)
    for p in cluster_posts:
        posts_by_account[p.account_id].append(p)
    accounts_by_id = {a.account_id: a for a in accounts}

    graph, member_edges = _member_subgraph(community, edges)
    degree = dict(zip(community.account_ids, graph.degree(), strict=True))
    try:
        eigenvector = dict(
            zip(
                community.account_ids,
                graph.eigenvector_centrality(weights="weight" if member_edges else None),
                strict=True,
            )
        )
    except Exception:
        logger.exception(
            "Eigenvector centrality failed for network with %d members; falling back to 0.0",
            len(community.account_ids),
        )
        eigenvector = dict.fromkeys(community.account_ids, 0.0)

    entries: list[AccountAnnexEntry] = []
    for account_id in community.account_ids:
        account_posts = posts_by_account.get(account_id, [])
        times = sorted(p.created_at for p in account_posts)
        deltas = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
        median_interval = deltas[len(deltas) // 2] if deltas else None
        circadian = len({t.hour for t in times}) / 24 if times else 0.0
        account = accounts_by_id.get(account_id)

        entries.append(
            AccountAnnexEntry(
                account_id=account_id,
                handle=account.handle if account else "",
                posts_in_cluster=len(account_posts),
                duplication_rate=round((duplication_rates or {}).get(account_id, 0.0), 4),
                median_interpost_interval_seconds=median_interval,
                circadian_coverage=round(circadian, 4),
                degree_centrality=round(
                    degree.get(account_id, 0) / max(len(community.account_ids) - 1, 1), 4
                ),
                eigenvector_centrality=round(eigenvector.get(account_id, 0.0), 4),
            )
        )
    return entries


def build_graph_snapshot(community: DetectedCommunity, edges: list[FusedEdge]) -> GraphSnapshot:
    """Layout uses Fruchterman-Reingold (igraph's built-in layout('fr')) as a
    force-directed substitute for ForceAtlas2 (10.5.6 point 5) - both serve the same
    rendering purpose, and this avoids pulling in a second, less-maintained
    graph-layout dependency just for cosmetic parity with the spec's named algorithm."""
    graph, member_edges = _member_subgraph(community, edges)
    layout = graph.layout("fr") if graph.vcount() else []
    coordinates = {
        account_id: (round(pos[0], 4), round(pos[1], 4))
        for account_id, pos in zip(community.account_ids, layout, strict=True)
    }

    return GraphSnapshot(
        edges=[
            GraphEdgeSnapshot(
                account_a=e.account_a,
                account_b=e.account_b,
                w_total=e.w_total,
                per_signal=dict(e.per_signal),
            )
            for e in member_edges
        ],
        layout=coordinates,
    )


def build_evidence_snapshot(
    community: DetectedCommunity,
    cluster_posts: list[SignalPost],
    accounts: list[SignalAccount],
    edges: list[FusedEdge],
    embedder: MultilingualEmbeddingService | None = None,
    common_phrase_allowlist: set[str] | None = None,
) -> EvidenceSnapshot:
    embedder = embedder or get_multilingual_embedding_service()
    representative_content = build_representative_content(
        cluster_posts, embedder=embedder, common_phrase_allowlist=common_phrase_allowlist
    )
    duplication_rates = _duplication_rate_by_account(cluster_posts, representative_content)

    return EvidenceSnapshot(
        burst_timeline=build_burst_timeline(cluster_posts),
        representative_content=representative_content,
        account_annex=build_account_annex(community, cluster_posts, accounts, edges, duplication_rates),
        graph=build_graph_snapshot(community, edges),
    )
