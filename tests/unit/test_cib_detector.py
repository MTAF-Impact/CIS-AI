"""CIB detector tests use the real embedding model (via the `real_embedder` session
fixture) because the heuristic depends on genuine text-similarity semantics - a random/
hash-based fake embedder would not preserve "near-duplicate text -> high cosine similarity",
which is the exact thing this detector needs to catch."""

from datetime import UTC, datetime, timedelta

from app.schemas.response import CIBCheckPost
from app.services.cib_detector import detect_coordinated_behavior


def _post(id_, text, author_id, created_at, account_created_at=None):
    return CIBCheckPost(
        id=id_,
        text=text,
        author_id=author_id,
        created_at=created_at,
        account_created_at=account_created_at,
    )


class TestDetectCoordinatedBehavior:
    def test_flags_a_coordinated_bot_pair(self, real_embedder):
        now = datetime.now(UTC)
        account_created = now - timedelta(days=2)

        posts = [
            _post(
                "1",
                "The new bus lane policy is a hidden tax on working families!",
                "botA",
                now,
                account_created,
            ),
            _post(
                "2",
                "This bus lane policy is really just a hidden tax on working families!!",
                "botB",
                now + timedelta(minutes=2),
                account_created + timedelta(minutes=10),
            ),
            _post(
                "3",
                "I think the new park is lovely, my kids enjoyed it this weekend",
                "realuser",
                now - timedelta(hours=5),
                now - timedelta(days=900),
            ),
        ]

        result = detect_coordinated_behavior(posts, embedder=real_embedder)

        assert result.is_likely_coordinated is True
        assert len(result.clusters) == 1
        cluster = result.clusters[0]
        assert set(cluster.post_ids) == {"1", "2"}
        assert set(cluster.author_ids) == {"botA", "botB"}
        assert "burst_timing" in cluster.reason
        assert "text_similarity" in cluster.reason
        assert "account_creation_clustering" in cluster.reason
        assert cluster.coordination_score == 1.0

    def test_unrelated_posts_produce_no_clusters(self, real_embedder):
        now = datetime.now(UTC)
        posts = [
            _post("1", "I love the new community garden on Maple Street", "alice", now),
            _post(
                "2",
                "Traffic on the highway was terrible this morning",
                "bob",
                now - timedelta(hours=3),
            ),
            _post(
                "3",
                "Does anyone know when the library reopens after renovation?",
                "carol",
                now - timedelta(days=1),
            ),
        ]

        result = detect_coordinated_behavior(posts, embedder=real_embedder)

        assert result.clusters == []
        assert result.is_likely_coordinated is False
        assert result.coordination_risk_score < 0.5

    def test_similar_text_alone_without_burst_timing_is_not_enough(self, real_embedder):
        # Two heuristics required to flag a pair (score >= 0.6); text similarity alone
        # is only worth 0.40 of the 1.0 weight budget.
        now = datetime.now(UTC)
        posts = [
            _post(
                "1",
                "The new bus lane policy is a hidden tax on working families!",
                "userA",
                now,
            ),
            _post(
                "2",
                "This bus lane policy is really just a hidden tax on working families!!",
                "userB",
                now - timedelta(days=3),
            ),
        ]

        result = detect_coordinated_behavior(posts, embedder=real_embedder)

        assert result.clusters == []
