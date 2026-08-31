from datetime import UTC, datetime

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

from app.schemas.coordination import CIBCheckPost, CIBCheckResponse, CIBCluster
from app.services.embedding_service import EmbeddingService, get_embedding_service

# Deterministic heuristic weights - each in [0, 1], summing to 1.0 when all three trigger.
BURST_WINDOW_SECONDS = 10 * 60
TEXT_SIMILARITY_THRESHOLD = 0.80
ACCOUNT_CREATION_CLUSTER_SECONDS = 24 * 60 * 60

WEIGHT_BURST_TIMING = 0.35
WEIGHT_TEXT_SIMILARITY = 0.40
WEIGHT_ACCOUNT_CLUSTERING = 0.25

PAIR_FLAG_THRESHOLD = 0.60


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _pair_reasons_and_score(
    post_a: CIBCheckPost, post_b: CIBCheckPost, similarity: float
) -> tuple[list[str], float]:
    reasons: list[str] = []
    score = 0.0

    time_delta = abs((_aware(post_a.created_at) - _aware(post_b.created_at)).total_seconds())
    if time_delta <= BURST_WINDOW_SECONDS:
        reasons.append("burst_timing")
        score += WEIGHT_BURST_TIMING

    if similarity >= TEXT_SIMILARITY_THRESHOLD:
        reasons.append("text_similarity")
        score += WEIGHT_TEXT_SIMILARITY

    if post_a.account_created_at and post_b.account_created_at:
        account_delta = abs(
            (_aware(post_a.account_created_at) - _aware(post_b.account_created_at)).total_seconds()
        )
        if account_delta <= ACCOUNT_CREATION_CLUSTER_SECONDS:
            reasons.append("account_creation_clustering")
            score += WEIGHT_ACCOUNT_CLUSTERING

    return reasons, round(score, 4)


def detect_coordinated_behavior(
    posts: list[CIBCheckPost], embedder: EmbeddingService | None = None
) -> CIBCheckResponse:
    """Deterministic CIB heuristic: flags and clusters posts on burst timing, text
    similarity, and account-creation clustering."""
    embedder = embedder or get_embedding_service()
    n = len(posts)

    embeddings = np.array(embedder.embed_batch([p.text for p in posts]))
    similarity_matrix = cosine_similarity(embeddings)

    uf = _UnionFind(n)
    edge_scores: dict[tuple[int, int], float] = {}
    edge_reasons: dict[tuple[int, int], list[str]] = {}

    for i in range(n):
        for j in range(i + 1, n):
            reasons, score = _pair_reasons_and_score(
                posts[i], posts[j], float(similarity_matrix[i][j])
            )
            if score >= PAIR_FLAG_THRESHOLD:
                uf.union(i, j)
                edge_scores[(i, j)] = score
                edge_reasons[(i, j)] = reasons

    groups: dict[int, list[int]] = {}
    for idx in range(n):
        groups.setdefault(uf.find(idx), []).append(idx)

    clusters: list[CIBCluster] = []
    involved_indices: set[int] = set()

    for members in groups.values():
        if len(members) < 2:
            continue
        member_set = set(members)
        cluster_edges = [
            (edge_scores[pair], edge_reasons[pair])
            for pair in edge_scores
            if pair[0] in member_set and pair[1] in member_set
        ]
        if not cluster_edges:
            continue

        avg_score = round(sum(s for s, _ in cluster_edges) / len(cluster_edges), 4)
        all_reasons = sorted({reason for _, reasons in cluster_edges for reason in reasons})
        involved_indices.update(members)

        clusters.append(
            CIBCluster(
                post_ids=[posts[i].id for i in members],
                author_ids=sorted({posts[i].author_id for i in members}),
                reason=all_reasons,
                coordination_score=avg_score,
            )
        )

    clusters.sort(key=lambda c: c.coordination_score, reverse=True)

    involvement_ratio = len(involved_indices) / n if n else 0.0
    max_cluster_score = max((c.coordination_score for c in clusters), default=0.0)
    overall_score = round(min(0.5 * involvement_ratio + 0.5 * max_cluster_score, 1.0), 4)

    return CIBCheckResponse(
        coordination_risk_score=overall_score,
        is_likely_coordinated=bool(clusters) and overall_score >= 0.5,
        clusters=clusters,
    )
