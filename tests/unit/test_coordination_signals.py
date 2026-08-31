"""F5 Phase 1: candidate scope + the 5 signal computations (PRD 10.5.1/10.5.2). Small
hand-constructed fixtures with an obvious expected outcome, per the plan's verification
approach - cheaper and more precise than eyeballing real data this early."""

from datetime import UTC, datetime, timedelta

from app.services.coordination.scope import select_candidates
from app.services.coordination.signals.coamplification import compute_co_amplification
from app.services.coordination.signals.duplication import (
    compute_content_duplication,
    normalize_text,
)
from app.services.coordination.signals.provenance import compute_provenance_similarity
from app.services.coordination.signals.structural import compute_structural_overlap
from app.services.coordination.signals.temporal import compute_temporal_synchrony
from app.services.coordination.types import SignalAccount, SignalPost, pair_key

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _post(id_, account_id, text, offset_minutes=0, **kwargs):
    return SignalPost(
        id=id_,
        account_id=account_id,
        text=text,
        created_at=NOW + timedelta(minutes=offset_minutes),
        **kwargs,
    )


class TestPairKey:
    def test_order_independent(self):
        assert pair_key("b", "a") == pair_key("a", "b")


class TestSelectCandidates:
    def test_excludes_allowlisted_and_self_accounts(self):
        posts = [
            _post("1", "civic_org", "text"),
            _post("2", "real_user", "text"),
        ]
        result = select_candidates(posts, allowlisted_account_ids={"civic_org"})
        assert result.account_ids == ["real_user"]
        assert result.candidates_count == 1
        assert result.truncated is False

    def test_truncates_by_post_volume_and_records_it(self):
        posts = (
            [_post(f"a{i}", "heavy", "x") for i in range(10)]
            + [_post("b1", "light", "x")]
        )
        result = select_candidates(posts, a_max=1)
        assert result.account_ids == ["heavy"]
        assert result.candidates_count == 2  # true count, before truncation
        assert result.truncated is True
        assert all(p.account_id == "heavy" for p in result.posts)


class TestTemporalSynchrony:
    def test_synchronized_cluster_flagged_disjoint_accounts_not(self):
        bot_bin_offsets = [0, 5, 10, 15, 20]  # minutes; 60s bins -> distinct bins
        bots = [f"bot{i}" for i in range(5)]
        organics = [f"organic{i}" for i in range(5)]

        posts = []
        for bot in bots:
            for i, offset in enumerate(bot_bin_offsets):
                posts.append(_post(f"{bot}-{i}", bot, "post", offset_minutes=offset))
        for k, account in enumerate(organics):
            base = 100 + k * 10
            for i in range(5):
                posts.append(
                    _post(f"{account}-{i}", account, "post", offset_minutes=base + i)
                )

        result = compute_temporal_synchrony(posts)

        for i in range(5):
            for j in range(i + 1, 5):
                key = pair_key(bots[i], bots[j])
                assert key in result
                assert 0 < result[key] <= 1.0

        for i in range(5):
            for j in range(i + 1, 5):
                assert pair_key(organics[i], organics[j]) not in result
        for bot in bots:
            for organic in organics:
                assert pair_key(bot, organic) not in result

    def test_single_account_returns_empty(self):
        posts = [_post("1", "solo", "x")]
        assert compute_temporal_synchrony(posts) == {}


class TestContentDuplication:
    def test_normalize_text_strips_urls_mentions_and_punctuation_noise(self):
        raw = "Check THIS out!!! @someone https://example.com/x   now!!"
        normalized = normalize_text(raw)
        assert "https://" not in normalized
        assert "@someone" not in normalized
        assert "!!!" not in normalized
        assert normalized == normalized.lower()

    def test_near_duplicate_flagged(self):
        text_a = "The new congestion charge policy is nothing but a hidden tax on commuters"
        text_b = "The new congestion charge policy is nothing but a hidden tax on commuters!!"
        posts = [
            _post("1", "acct_a", text_a),
            _post("2", "acct_b", text_b, offset_minutes=1),
        ]
        result = compute_content_duplication(posts)
        assert result[pair_key("acct_a", "acct_b")] > 0

    def test_native_reshare_and_short_posts_excluded(self):
        text = "The new congestion charge policy is nothing but a hidden tax on commuters"
        posts = [
            _post("1", "acct_a", text),
            _post("2", "acct_b", text, offset_minutes=1, is_native_reshare=True),
            _post("3", "acct_c", "too short", offset_minutes=1),
        ]
        result = compute_content_duplication(posts)
        assert result == {}

    def test_cross_lingual_paraphrase_caught_by_embeddings_only(self, real_multilingual_embedder):
        text_en = "The new road pricing policy is a hidden tax on commuters."
        text_id = "Kebijakan tarif jalan baru ini adalah pajak tersembunyi bagi para komuter."
        posts = [
            _post("1", "acct_a", text_en),
            _post("2", "acct_b", text_id, offset_minutes=2),
        ]
        result = compute_content_duplication(posts, embedder=real_multilingual_embedder)
        assert result[pair_key("acct_a", "acct_b")] > 0

    def test_unrelated_posts_produce_no_pairs(self, real_multilingual_embedder):
        posts = [
            _post("1", "acct_a", "I love the new community garden on Maple Street here"),
            _post("2", "acct_b", "Does anyone know when the library reopens this month"),
        ]
        result = compute_content_duplication(posts, embedder=real_multilingual_embedder)
        assert result == {}


class TestCoAmplification:
    def test_obscure_shared_target_scores_higher_than_viral_one(self):
        # Each of a/b/c/d also has 2 private (unique) targets, so IDF weighting on the
        # *shared* dimension actually moves the cosine - with only one shared target
        # and nothing else, cosine is 1.0 regardless of weighting (pure parallel
        # vectors), which would hide the effect this signal exists to capture.
        posts = [
            _post("1", "a", "x", reshared_post_id="obscure_post"),
            _post("1b", "a", "x", quoted_post_id="priv_a1"),
            _post("1c", "a", "x", quoted_post_id="priv_a2"),
            _post("2", "b", "x", reshared_post_id="obscure_post"),
            _post("2b", "b", "x", quoted_post_id="priv_b1"),
            _post("2c", "b", "x", quoted_post_id="priv_b2"),
            _post("3", "c", "x", reshared_post_id="viral_post"),
            _post("3b", "c", "x", quoted_post_id="priv_c1"),
            _post("3c", "c", "x", quoted_post_id="priv_c2"),
            _post("4", "d", "x", reshared_post_id="viral_post"),
            _post("4b", "d", "x", quoted_post_id="priv_d1"),
            _post("4c", "d", "x", quoted_post_id="priv_d2"),
        ] + [
            _post(f"v{i}", f"crowd{i}", "x", reshared_post_id="viral_post") for i in range(8)
        ]
        result = compute_co_amplification(posts)
        assert result[pair_key("a", "b")] > result[pair_key("c", "d")]

    def test_no_shared_targets_produces_no_pairs(self):
        posts = [
            _post("1", "a", "x", reshared_post_id="post_a"),
            _post("2", "b", "x", reshared_post_id="post_b"),
        ]
        assert compute_co_amplification(posts) == {}


class TestProvenanceSimilarity:
    def test_matching_creation_time_and_handle_template_scores_high(self):
        accounts = [
            SignalAccount(
                account_id="bot1",
                handle="bot_alpha01",
                created_at_platform=NOW,
            ),
            SignalAccount(
                account_id="bot2",
                handle="bot_beta02",
                created_at_platform=NOW + timedelta(hours=1),
            ),
        ]
        result = compute_provenance_similarity(accounts)
        assert result[pair_key("bot1", "bot2")] > 0.3

    def test_no_metadata_at_all_produces_no_pair(self):
        accounts = [SignalAccount(account_id="x"), SignalAccount(account_id="y")]
        assert compute_provenance_similarity(accounts) == {}

    def test_exact_location_match_only(self):
        accounts = [
            SignalAccount(account_id="a", declared_location="Jakarta"),
            SignalAccount(account_id="b", declared_location="Jakarta"),
        ]
        result = compute_provenance_similarity(accounts)
        assert result[pair_key("a", "b")] == 1.0


class TestStructuralOverlap:
    def test_none_when_no_follower_data(self):
        assert compute_structural_overlap(None) is None
        assert compute_structural_overlap({}) is None

    def test_shared_followers_score_in_unit_range(self):
        follower_sets = {
            "a": {"f1", "f2", "f3"},
            "b": {"f1", "f2", "f4"},
            "c": {"f5", "f6"},
        }
        result = compute_structural_overlap(follower_sets)
        assert pair_key("a", "b") in result
        assert 0 < result[pair_key("a", "b")] <= 1.0
        assert pair_key("a", "c") not in result
