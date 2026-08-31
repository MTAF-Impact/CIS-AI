"""F5 Phase 2: signal fusion, community detection, claim-relevance gate, confidence
banding, and cluster-level metrics (PRD 10.5.3-10.5.5, 10.6)."""

from datetime import UTC, datetime, timedelta

from app.models.enums import ConfidenceBand
from app.services.coordination.cluster_metrics import compute_cluster_metrics
from app.services.coordination.clustering import DetectedCommunity, detect_communities
from app.services.coordination.confidence import (
    compute_signal_breadth,
    determine_confidence_band,
    is_allowlist_suppressed,
)
from app.services.coordination.fusion import fuse_and_prune
from app.services.coordination.relevance_gate import evaluate_claim_relevance
from app.services.coordination.types import SignalAccount, SignalPost

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _post(id_, account_id, offset_minutes=0, text="post"):
    return SignalPost(
        id=id_, account_id=account_id, text=text, created_at=NOW + timedelta(minutes=offset_minutes)
    )


class TestFuseAndPrune:
    def test_single_strong_signal_alone_is_rejected(self):
        # One signal family, however strong, can never satisfy the multi-signal rule.
        signals = {"w_time": {("a", "b"): 0.9}}
        edges, unavailable = fuse_and_prune(signals, weights={"w_time": 1.0})
        assert edges == []
        assert unavailable == []

    def test_two_strong_signals_together_are_retained(self):
        signals = {
            "w_time": {("a", "b"): 0.9},
            "w_text": {("a", "b"): 0.3},
        }
        edges, _ = fuse_and_prune(signals, weights={"w_time": 0.6, "w_text": 0.4})
        assert len(edges) == 1
        assert edges[0].signal_count == 2
        assert edges[0].w_total == round(0.6 * 0.9 + 0.4 * 0.3, 4)

    def test_moderate_signal_below_multi_signal_floor_is_rejected(self):
        # Both signals clear theta_edge combined, but neither individually reaches
        # the 0.25 "strong contribution" floor - still not enough.
        signals = {
            "w_time": {("a", "b"): 0.5},
            "w_text": {("a", "b"): 0.2},
        }
        edges, _ = fuse_and_prune(
            signals, weights={"w_time": 0.5, "w_text": 0.5}, theta_edge=0.30
        )
        # total = 0.5*0.5 + 0.5*0.2 = 0.35 >= theta_edge, but w_text=0.2 < 0.25 floor
        # and only w_time clears it -> strong_families = 1.
        assert edges == []

    def test_unavailable_family_weight_is_redistributed(self):
        signals = {
            "w_time": {("a", "b"): 0.9},
            "w_text": {("a", "b"): 0.9},
            "w_amp": {},
            "w_meta": {},
            "w_struct": None,
        }
        edges, unavailable = fuse_and_prune(signals)
        assert unavailable == ["w_struct"]
        assert len(edges) == 1
        # w_struct's 0.10 redistributed proportionally across the other 4 (sum 0.90).
        redistributed_time_weight = 0.30 + (0.30 / 0.90) * 0.10
        redistributed_text_weight = 0.25 + (0.25 / 0.90) * 0.10
        expected_total = round(redistributed_time_weight * 0.9 + redistributed_text_weight * 0.9, 4)
        assert edges[0].w_total == expected_total
        assert edges[0].per_signal["w_time"] == 0.9  # per_signal stores the raw signal value

    def test_empty_signals_produce_no_edges(self):
        edges, unavailable = fuse_and_prune({"w_time": {}, "w_text": {}, "w_amp": {}, "w_meta": {}, "w_struct": {}})
        assert edges == []
        assert unavailable == []


class TestDetectCommunities:
    def test_finds_dense_cluster_and_drops_sparse_pair(self):
        from app.services.coordination.fusion import FusedEdge

        edges = [
            FusedEdge("a", "b", 0.9, {"w_time": 0.9}, 1),
            FusedEdge("a", "c", 0.9, {"w_time": 0.9}, 1),
            FusedEdge("b", "c", 0.9, {"w_time": 0.9}, 1),
            FusedEdge("a", "d", 0.6, {"w_time": 0.6}, 1),
            FusedEdge("b", "d", 0.6, {"w_time": 0.6}, 1),
            FusedEdge("c", "d", 0.6, {"w_time": 0.6}, 1),
            FusedEdge("x", "y", 0.9, {"w_time": 0.9}, 1),  # isolated pair, k-core too low
        ]
        communities = detect_communities(
            edges, account_ids=["a", "b", "c", "d", "x", "y"], k_core=2, n_min=3
        )
        assert len(communities) == 1
        assert set(communities[0].account_ids) == {"a", "b", "c", "d"}
        assert 0 < communities[0].internal_density <= 1.0
        assert 0 <= communities[0].conductance <= 1.0

    def test_no_edges_produces_no_communities(self):
        assert detect_communities([], account_ids=["a", "b", "c"]) == []


class TestRelevanceGate:
    def _members_with_posts(self, n_members, posts_each):
        members = [f"m{i}" for i in range(n_members)]
        posts = [
            _post(f"{m}-{i}", m, offset_minutes=i) for m in members for i in range(posts_each)
        ]
        return members, posts

    def test_passes_all_three_tests(self):
        members, posts = self._members_with_posts(5, 5)  # 25 posts total, all anchored
        result = evaluate_claim_relevance(members, posts, total_posts_by_account={})
        assert result.passed is True
        assert result.failed_test is None
        assert result.claim_cluster_post_count == 25

    def test_fails_anchoring_when_most_members_have_one_post(self):
        members, posts = self._members_with_posts(5, 1)
        # pad volume so it wouldn't fail on evidence_volume/link_strength instead
        posts += [_post(f"pad-{i}", members[0], offset_minutes=100 + i) for i in range(20)]
        result = evaluate_claim_relevance(members, posts, total_posts_by_account={})
        assert result.passed is False
        assert result.failed_test == "anchoring"

    def test_fails_evidence_volume_below_p_min(self):
        members, posts = self._members_with_posts(5, 3)  # anchored, but only 15 posts
        result = evaluate_claim_relevance(members, posts, total_posts_by_account={}, p_min=20)
        assert result.passed is False
        assert result.failed_test == "evidence_volume"

    def test_fails_link_strength_when_diluted_by_other_activity(self):
        members, posts = self._members_with_posts(5, 5)
        # Each member has 100x more activity elsewhere -> low overlap_ratio.
        total_posts = {m: 2500 for m in members}
        result = evaluate_claim_relevance(members, posts, total_posts_by_account=total_posts)
        assert result.passed is False
        assert result.failed_test == "link_strength"


class TestConfidenceBanding:
    def test_high_confidence_requires_score_and_breadth(self):
        assert determine_confidence_band(75, 3) == ConfidenceBand.HIGH
        assert determine_confidence_band(75, 1) != ConfidenceBand.HIGH  # the spec's own example

    def test_medium_confidence(self):
        assert determine_confidence_band(60, 2) == ConfidenceBand.MEDIUM

    def test_low_confidence_fallback(self):
        assert determine_confidence_band(40, 1) == ConfidenceBand.LOW

    def test_degraded_run_caps_high_at_medium(self):
        assert determine_confidence_band(90, 5, run_truncated=True) == ConfidenceBand.MEDIUM
        assert determine_confidence_band(90, 5, unavailable_signal_count=2) == ConfidenceBand.MEDIUM
        assert determine_confidence_band(90, 5, unavailable_signal_count=1) == ConfidenceBand.HIGH

    def test_signal_breadth_counts_families_at_or_above_50(self):
        assert compute_signal_breadth({"sy": 60, "du": 49.9, "co": 50, "pr": 10, "au": 90}) == 3


class TestAllowlistSuppression:
    def test_majority_allowlisted_is_suppressed(self):
        members = ["a", "b", "c", "d", "e"]
        assert is_allowlist_suppressed(members, {"a", "b", "c"}) is True  # 60%

    def test_minority_allowlisted_is_not_suppressed(self):
        members = ["a", "b", "c", "d", "e"]
        assert is_allowlist_suppressed(members, {"a", "b"}) is False  # 40%


class TestClusterMetrics:
    def test_coordinated_looking_cluster_scores_higher_than_organic(self, real_multilingual_embedder):
        community = DetectedCommunity(
            account_ids=["bot0", "bot1", "bot2", "bot3", "bot4"],
            internal_density=0.9,
            conductance=0.05,  # tight, isolated -> high CO
        )
        bot_text = "The new congestion charge policy is nothing but a hidden tax on commuters"
        cluster_posts = [
            _post(f"bot{i}-{j}", f"bot{i}", offset_minutes=j, text=bot_text + ("!" * j))
            for i in range(5)
            for j in range(3)
        ]
        w_time = {
            (f"bot{i}", f"bot{j}"): 0.8 for i in range(5) for j in range(i + 1, 5)
        }
        accounts = [
            SignalAccount(
                account_id=f"bot{i}",
                handle=f"bot_alpha{i:02d}",
                created_at_platform=NOW,
            )
            for i in range(5)
        ]

        organic_community = DetectedCommunity(
            account_ids=["u0", "u1", "u2", "u3", "u4"], internal_density=0.1, conductance=0.9
        )
        organic_texts = [
            "I love the new community garden on Maple Street",
            "Does anyone know when the library reopens this month",
            "Traffic was terrible this morning on the highway",
            "My kids enjoyed the park renovation a lot",
            "The bus schedule changed again this week apparently",
        ]
        organic_posts = [
            _post(f"u{i}-0", f"u{i}", offset_minutes=i * 500, text=organic_texts[i])
            for i in range(5)
        ]
        organic_accounts = [
            SignalAccount(
                account_id=f"u{i}",
                handle=f"resident{i}",
                created_at_platform=NOW - timedelta(days=400 + i * 100),
            )
            for i in range(5)
        ]

        bot_metrics = compute_cluster_metrics(
            community,
            cluster_posts,
            accounts,
            w_time,
            window_hours=1.0,
            now=NOW,
            embedder=real_multilingual_embedder,
        )
        organic_metrics = compute_cluster_metrics(
            organic_community,
            organic_posts,
            organic_accounts,
            {},
            window_hours=(500 * 4) / 60,
            now=NOW,
            embedder=real_multilingual_embedder,
        )

        assert 0 <= bot_metrics.coordination_score <= 100
        assert 0 <= organic_metrics.coordination_score <= 100
        assert bot_metrics.coordination_score > organic_metrics.coordination_score
        assert bot_metrics.du > organic_metrics.du  # near-duplicate text, unlike organic
        assert bot_metrics.co > organic_metrics.co  # tight/isolated vs sparse/leaky
