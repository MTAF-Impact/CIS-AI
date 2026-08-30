import pytest
from sqlalchemy import select

from app.models.content import ContentItem
from app.models.enums import ContentSource
from app.models.narrative import Narrative
from app.services.clustering_service import cluster_unclustered_content
from tests.fakes import FakeLLMClient

pytestmark = pytest.mark.integration

BUS_LANE_POSTS = [
    "The new bus lane is a hidden tax on working families!",
    "This bus lane project is really just a hidden tax on drivers, wake up.",
    "I can't believe the city snuck in a hidden tax through this bus lane plan.",
]
TREE_REMOVAL_POSTS = [
    "The city is removing 500 mature trees for a parking structure, environmental betrayal.",
    "500 trees gone for a parking lot?! Absolute hypocrisy from city council.",
]


async def _insert_items(db_session, real_embedder, texts):
    for text in texts:
        db_session.add(
            ContentItem(
                text=text,
                source=ContentSource.SOCIAL,
                embedding=real_embedder.embed(text),
                outrage_score=0.6,
                location="Downtown",
            )
        )
    await db_session.commit()


class TestClusterUnclusteredContent:
    async def test_no_unclustered_items_is_a_noop(self, db_session):
        result = await cluster_unclustered_content(db_session, llm=FakeLLMClient())
        assert result.narratives_created == 0
        assert result.content_items_clustered == 0

    async def test_forms_distinct_narratives_by_topic(self, db_session, real_embedder):
        await _insert_items(db_session, real_embedder, BUS_LANE_POSTS)
        await _insert_items(db_session, real_embedder, TREE_REMOVAL_POSTS)

        result = await cluster_unclustered_content(db_session, llm=FakeLLMClient())

        assert result.narratives_created == 2
        assert result.content_items_clustered == 5

        narratives = (await db_session.execute(select(Narrative))).scalars().all()
        assert len(narratives) == 2
        for narrative in narratives:
            assert narrative.title == "Fake Narrative Title"  # from FakeLLMClient
            assert 0.0 <= narrative.overall_risk_score <= 1.0

    async def test_a_single_orphan_post_is_left_unclustered(self, db_session, real_embedder):
        await _insert_items(db_session, real_embedder, BUS_LANE_POSTS[:1])

        result = await cluster_unclustered_content(db_session, llm=FakeLLMClient())

        assert result.narratives_created == 0
        item = (await db_session.execute(select(ContentItem))).scalar_one()
        assert item.narrative_id is None

    async def test_new_similar_post_attaches_to_existing_narrative(
        self, db_session, real_embedder
    ):
        # HDBSCAN needs topic diversity to detect density contrast - see the comment in
        # clustering_service.py. TREE_REMOVAL_POSTS gives the bus-lane posts something to
        # contrast against so they reliably form a cluster on the first pass.
        await _insert_items(db_session, real_embedder, BUS_LANE_POSTS)
        await _insert_items(db_session, real_embedder, TREE_REMOVAL_POSTS)
        first_result = await cluster_unclustered_content(db_session, llm=FakeLLMClient())
        assert first_result.narratives_created == 2

        await _insert_items(
            db_session,
            real_embedder,
            ["Another hidden tax complaint about the bus lane plan surfaced today."],
        )
        second_result = await cluster_unclustered_content(db_session, llm=FakeLLMClient())

        assert second_result.narratives_created == 0
        assert second_result.narratives_updated == 1
        assert second_result.content_items_clustered == 1

        narratives = (await db_session.execute(select(Narrative))).scalars().all()
        assert len(narratives) == 2
