"""F5 Phase 3: evidence extraction/snapshotting and recurrence tracking
(PRD 10.5.6/10.5.7)."""

from datetime import UTC, datetime, timedelta

from app.services.coordination.clustering import DetectedCommunity
from app.services.coordination.evidence import (
    build_account_annex,
    build_burst_timeline,
    build_evidence_snapshot,
    build_graph_snapshot,
    build_representative_content,
)
from app.services.coordination.fusion import FusedEdge
from app.services.coordination.recurrence import (
    RecurrenceCandidate,
    compute_fingerprint,
    find_recurrence_parent,
    member_jaccard,
)
from app.services.coordination.types import SignalAccount, SignalPost

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _post(id_, account_id, offset_minutes=0, text="post"):
    return SignalPost(
        id=id_, account_id=account_id, text=text, created_at=NOW + timedelta(minutes=offset_minutes)
    )


class TestBurstTimeline:
    def test_marks_a_genuine_spike_as_anomalous(self):
        # Baseline: 1 post every 10 minutes for a while, then a 6-post spike in one bin.
        posts = [_post(f"base-{i}", "a", offset_minutes=i * 10) for i in range(20)]
        posts += [_post(f"spike-{i}", "a", offset_minutes=500, text=f"spike{i}") for i in range(6)]
        bins = build_burst_timeline(posts)
        assert any(b.is_anomalous for b in bins)
        spike_bin = next(b for b in bins if b.is_anomalous)
        assert spike_bin.post_count >= 6
        assert spike_bin.zscore > 0

    def test_empty_posts_returns_no_bins(self):
        assert build_burst_timeline([]) == []


class TestRepresentativeContent:
    def test_duplicate_group_has_exactly_one_canonical(self, real_multilingual_embedder):
        text = "The new congestion charge policy is nothing but a hidden tax on commuters"
        posts = [
            _post("1", "a", 0, text),
            _post("2", "b", 1, text + "!"),
            _post("3", "c", 2, text + "!!"),
            _post("4", "d", 3, "completely unrelated text about a community garden"),
        ]
        evidence = build_representative_content(posts, embedder=real_multilingual_embedder)
        by_id = {e.post_id: e for e in evidence}

        group_ids = {e.duplicate_group_id for e in evidence if e.duplicate_group_id}
        assert len(group_ids) == 1
        duplicated = [e for e in evidence if e.duplicate_group_id]
        assert len(duplicated) == 3
        assert sum(1 for e in duplicated if e.is_canonical) == 1
        assert by_id["1"].is_canonical is True  # earliest post in the group
        assert by_id["4"].duplicate_group_id is None

        # Every post is captured with a hash regardless of group membership.
        assert all(len(e.content_sha256) == 64 for e in evidence)

    def test_no_duplicates_leaves_every_post_ungrouped(self, real_multilingual_embedder):
        posts = [
            _post("1", "a", 0, "I love the new community garden on Maple Street"),
            _post("2", "b", 1, "Does anyone know when the library reopens this month"),
        ]
        evidence = build_representative_content(posts, embedder=real_multilingual_embedder)
        assert all(e.duplicate_group_id is None for e in evidence)


class TestAccountAnnex:
    def test_hub_account_has_higher_centrality_than_a_leaf(self):
        community = DetectedCommunity(account_ids=["a", "b", "c", "d"], internal_density=0.8, conductance=0.1)
        edges = [
            FusedEdge("a", "b", 0.9, {"w_time": 0.9}, 1),
            FusedEdge("a", "c", 0.9, {"w_time": 0.9}, 1),
            FusedEdge("a", "d", 0.9, {"w_time": 0.9}, 1),
        ]
        posts = [_post(f"{acc}-0", acc, i) for i, acc in enumerate(["a", "b", "c", "d"])]
        accounts = [SignalAccount(account_id=acc, handle=acc) for acc in ["a", "b", "c", "d"]]

        annex = build_account_annex(community, posts, accounts, edges)
        by_id = {e.account_id: e for e in annex}

        assert by_id["a"].degree_centrality > by_id["b"].degree_centrality
        assert len(annex) == 4
        assert by_id["a"].handle == "a"

    def test_missing_account_metadata_still_produces_an_entry(self):
        community = DetectedCommunity(account_ids=["a"], internal_density=0.0, conductance=0.0)
        posts = [_post("1", "a")]
        annex = build_account_annex(community, posts, accounts=[], edges=[])
        assert len(annex) == 1
        assert annex[0].handle == ""


class TestGraphSnapshot:
    def test_layout_has_one_coordinate_per_member(self):
        community = DetectedCommunity(account_ids=["a", "b", "c"], internal_density=0.9, conductance=0.05)
        edges = [
            FusedEdge("a", "b", 0.9, {"w_time": 0.9}, 1),
            FusedEdge("b", "c", 0.9, {"w_time": 0.9}, 1),
        ]
        snapshot = build_graph_snapshot(community, edges)
        assert set(snapshot.layout.keys()) == {"a", "b", "c"}
        assert len(snapshot.edges) == 2

    def test_excludes_edges_to_non_members(self):
        community = DetectedCommunity(account_ids=["a", "b"], internal_density=1.0, conductance=0.0)
        edges = [
            FusedEdge("a", "b", 0.9, {"w_time": 0.9}, 1),
            FusedEdge("a", "outsider", 0.9, {"w_time": 0.9}, 1),
        ]
        snapshot = build_graph_snapshot(community, edges)
        assert len(snapshot.edges) == 1
        assert "outsider" not in snapshot.layout


class TestBuildEvidenceSnapshot:
    def test_produces_all_four_artifacts(self, real_multilingual_embedder):
        community = DetectedCommunity(account_ids=["a", "b"], internal_density=1.0, conductance=0.0)
        text = "The new congestion charge policy is nothing but a hidden tax on commuters"
        posts = [_post("1", "a", 0, text), _post("2", "b", 1, text + "!")]
        accounts = [SignalAccount(account_id="a", handle="a"), SignalAccount(account_id="b", handle="b")]
        edges = [FusedEdge("a", "b", 0.9, {"w_time": 0.9, "w_text": 0.9}, 2)]

        snapshot = build_evidence_snapshot(
            community, posts, accounts, edges, embedder=real_multilingual_embedder
        )
        assert len(snapshot.burst_timeline) >= 1
        assert len(snapshot.representative_content) == 2
        assert len(snapshot.account_annex) == 2
        assert set(snapshot.graph.layout.keys()) == {"a", "b"}
        # The account annex's duplication_rate should reflect the near-duplicate pair.
        assert all(e.duplication_rate > 0 for e in snapshot.account_annex)


class TestRecurrence:
    def test_fingerprint_is_order_independent(self):
        fp1 = compute_fingerprint(["a", "b", "c"], ["climate", "flood"])
        fp2 = compute_fingerprint(["c", "a", "b"], ["flood", "climate"])
        assert fp1 == fp2

    def test_fingerprint_changes_with_membership(self):
        fp1 = compute_fingerprint(["a", "b"], ["x"])
        fp2 = compute_fingerprint(["a", "b", "c"], ["x"])
        assert fp1 != fp2

    def test_member_jaccard_bounds(self):
        assert member_jaccard({"a", "b"}, {"a", "b"}) == 1.0
        assert member_jaccard({"a"}, {"b"}) == 0.0
        assert member_jaccard(set(), {"a"}) == 0.0

    def test_finds_best_matching_recurrence_above_threshold(self):
        candidates = [
            RecurrenceCandidate("old-weak", {"a", "x", "y", "z"}),  # jaccard 1/7 ~ 0.14
            RecurrenceCandidate("old-strong", {"a", "b", "c", "q"}),  # jaccard 3/5 = 0.6
        ]
        parent = find_recurrence_parent({"a", "b", "c", "d"}, candidates)
        assert parent == "old-strong"

    def test_no_match_below_threshold_returns_none(self):
        candidates = [RecurrenceCandidate("old", {"x", "y", "z"})]
        assert find_recurrence_parent({"a", "b", "c"}, candidates) is None
