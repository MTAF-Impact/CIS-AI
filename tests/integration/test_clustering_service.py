import pytest
from sqlalchemy import select

from app.models.content import ContentItem
from app.models.enums import ContentSource
from app.models.narrative import Narrative
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
        assert result.narratives_created == 0
        assert result.content_items_clustered == 0

    async def test_forms_distinct_narratives_by_topic(self, db_session, real_embedder):
        await _insert_items(db_session, real_embedder, ERP_POSTS)
        await _insert_items(db_session, real_embedder, TREE_REMOVAL_POSTS, location="Monas")

        result = await cluster_unclustered_content(db_session, llm=FakeLLMClient())

        assert result.narratives_created == 2
        assert result.content_items_clustered == 5

        narratives = (await db_session.execute(select(Narrative))).scalars().all()
        assert len(narratives) == 2
        for narrative in narratives:
            assert narrative.title == "Fake Narrative Title"  # from FakeLLMClient
            assert 0.0 <= narrative.overall_risk_score <= 1.0

    async def test_a_single_orphan_post_is_left_unclustered(self, db_session, real_embedder):
        await _insert_items(db_session, real_embedder, ERP_POSTS[:1])

        result = await cluster_unclustered_content(db_session, llm=FakeLLMClient())

        assert result.narratives_created == 0
        item = (await db_session.execute(select(ContentItem))).scalar_one()
        assert item.narrative_id is None

    async def test_new_similar_post_attaches_to_existing_narrative(
        self, db_session, real_embedder
    ):
        # HDBSCAN needs topic diversity to detect density contrast - see the comment in
        # clustering_service.py. TREE_REMOVAL_POSTS gives the ERP posts something to
        # contrast against so they reliably form a cluster on the first pass.
        await _insert_items(db_session, real_embedder, ERP_POSTS)
        await _insert_items(db_session, real_embedder, TREE_REMOVAL_POSTS, location="Monas")
        first_result = await cluster_unclustered_content(db_session, llm=FakeLLMClient())
        assert first_result.narratives_created == 2

        await _insert_items(
            db_session,
            real_embedder,
            ["Another hidden tax complaint about the ERP road pricing plan surfaced today."],
        )
        second_result = await cluster_unclustered_content(db_session, llm=FakeLLMClient())

        assert second_result.narratives_created == 0
        assert second_result.narratives_updated == 1
        assert second_result.content_items_clustered == 1

        narratives = (await db_session.execute(select(Narrative))).scalars().all()
        assert len(narratives) == 2
