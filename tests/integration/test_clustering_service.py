import pytest
from sqlalchemy import select

from app.models.claim import Claim
from app.models.content import ContentItem
from app.models.enums import ClaimStatus, ClaimType, ContentSource, Stance
from app.models.topic import Topic
from app.models.topic_volume_bucket import TopicVolumeBucket
from app.services.clustering_service import cluster_unclustered_content
from tests.fakes import FakeLLMClient
from tests.jakarta_fixtures import ERP_POSTS, TREE_REMOVAL_POSTS

pytestmark = pytest.mark.integration


async def _insert_items(db_session, real_embedder, texts, location="Sudirman"):
    for text in texts:
        db_session.add(
            ContentItem(
                text=text,
                source=ContentSource.SOCIAL,
                embedding=real_embedder.embed(text),
                outrage_score=0.6,
                location=location,
            )
        )
    await db_session.commit()


class TestClusterUnclusteredContent:
    async def test_no_unclustered_items_is_a_noop(self, db_session):
        result = await cluster_unclustered_content(db_session, llm=FakeLLMClient())
        assert result.claims_created == 0
        assert result.content_items_clustered == 0

    async def test_forms_distinct_claims_by_topic(self, db_session, real_embedder):
        # HDBSCAN needs topic diversity to detect density contrast - TREE_REMOVAL_POSTS
        # gives the ERP posts something to contrast against so both reliably cluster.
        await _insert_items(db_session, real_embedder, ERP_POSTS)
        await _insert_items(db_session, real_embedder, TREE_REMOVAL_POSTS, location="Monas")

        result = await cluster_unclustered_content(db_session, llm=FakeLLMClient())

        assert result.claims_created == 2
        assert result.content_items_clustered == 5

        claims = (await db_session.execute(select(Claim))).scalars().all()
        assert len(claims) == 2

        topics = (await db_session.execute(select(Topic))).scalars().all()
        assert len(topics) == 2  # ERP and tree-removal are semantically distinct

        for claim in claims:
            assert claim.claim_type == ClaimType.EXISTING
            assert claim.status == ClaimStatus.UNREVIEWED
            assert claim.claim_statement.startswith("Claim: ")
            assert claim.topic_id is not None
            assert claim.embedding is not None

            # First run, single claim per topic, all-recent content, empty OfficialSource
            # corpus, and FakeLLMClient's fixed harm values -> everything is deterministic.
            assert claim.reach_score == pytest.approx(50.0)  # lone claim in its topic
            assert claim.velocity_score == pytest.approx(50.0)  # no baseline history yet
            assert claim.falseness_score is None  # empty OfficialSource corpus
            assert claim.harm_score == pytest.approx(0.35 * 40 + 0.30 * 50 + 0.20 * 30 + 0.15 * 20)
            assert claim.emotional_intensity_score == pytest.approx(30.0)  # 50*0.6 + 50*0
            assert claim.emotional_intensity_opposing is None  # no Opposing content yet
            assert claim.npr == pytest.approx(0.0)  # all-Supporting, no Opposing content yet
            assert claim.is_dormant is False
            assert claim.discount_factor == pytest.approx(1.0)  # below reliability threshold
            assert claim.claim_score is not None
            assert claim.final_claim_score == pytest.approx(claim.claim_score)  # discount=1.0
            assert claim.activity_content is not None  # generated eagerly at creation
            assert claim.activity_generated_at is not None

        # Every item in each cluster got an explicit (never-defaulted) stance.
        items = (await db_session.execute(select(ContentItem))).scalars().all()
        assert all(item.stance == Stance.SUPPORTING for item in items)
        assert all(item.claim_id is not None for item in items)

        buckets = (await db_session.execute(select(TopicVolumeBucket))).scalars().all()
        assert len(buckets) == 2  # one bucket per topic, incremented by Supporting items

    async def test_a_single_orphan_post_is_left_unclustered(self, db_session, real_embedder):
        await _insert_items(db_session, real_embedder, ERP_POSTS[:1])

        result = await cluster_unclustered_content(db_session, llm=FakeLLMClient())

        assert result.claims_created == 0
        item = (await db_session.execute(select(ContentItem))).scalar_one()
        assert item.claim_id is None
        assert item.stance is None  # never assessed - no claim exists to compare against

    async def test_new_similar_post_attaches_to_existing_claim(self, db_session, real_embedder):
        await _insert_items(db_session, real_embedder, ERP_POSTS)
        await _insert_items(db_session, real_embedder, TREE_REMOVAL_POSTS, location="Monas")
        first_result = await cluster_unclustered_content(db_session, llm=FakeLLMClient())
        assert first_result.claims_created == 2

        await _insert_items(
            db_session,
            real_embedder,
            ["Another hidden tax complaint about the ERP road pricing plan surfaced today."],
        )
        second_result = await cluster_unclustered_content(db_session, llm=FakeLLMClient())

        assert second_result.claims_created == 0
        assert second_result.claims_updated >= 1
        assert second_result.content_items_clustered == 1

        claims = (await db_session.execute(select(Claim))).scalars().all()
        assert len(claims) == 2  # attached, not a new claim

        # The attached claim's Reach should reflect the extra content item now.
        erp_claim = None
        for candidate in claims:
            candidate_items = (
                await db_session.execute(
                    select(ContentItem).where(ContentItem.claim_id == candidate.id)
                )
            ).scalars().all()
            if any(item.text.startswith("The new ERP") for item in candidate_items):
                erp_claim = candidate
                erp_items = candidate_items
                break

        assert erp_claim is not None
        assert len(erp_items) == 4  # 3 original + 1 newly attached

    async def test_stance_classification_never_defaults_on_llm_failure(self, db_session, real_embedder):
        class _AlwaysFailingStanceClient(FakeLLMClient):
            async def classify_stance(self, claim_statement, post_text):
                raise RuntimeError("simulated LLM failure")

        await _insert_items(db_session, real_embedder, ERP_POSTS)
        await _insert_items(db_session, real_embedder, TREE_REMOVAL_POSTS, location="Monas")
        await cluster_unclustered_content(db_session, llm=FakeLLMClient())

        # A new item that would otherwise attach to the existing ERP claim, but stance
        # classification fails - it must stay unclustered, never fabricated as Supporting.
        await _insert_items(
            db_session,
            real_embedder,
            ["Another hidden tax complaint about the ERP road pricing plan surfaced today."],
        )
        result = await cluster_unclustered_content(db_session, llm=_AlwaysFailingStanceClient())

        assert result.content_items_clustered == 0
        new_item = (
            await db_session.execute(
                select(ContentItem).where(ContentItem.text.startswith("Another hidden tax"))
            )
        ).scalar_one()
        assert new_item.claim_id is None
        assert new_item.stance is None
