import uuid

import pytest

from app.models.enums import ContentSource, NarrativeStatus
from app.models.narrative import Narrative

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


async def _ingest(client, texts, location="Downtown"):
    for text in texts:
        response = await client.post(
            "/api/v1/ingest", json={"text": text, "source": ContentSource.SOCIAL.value, "location": location}
        )
        assert response.status_code == 201


class TestClusterNowAndList:
    async def test_cluster_now_groups_similar_posts_into_narratives(self, client):
        await _ingest(client, BUS_LANE_POSTS)
        await _ingest(client, TREE_REMOVAL_POSTS)

        cluster_response = await client.post("/api/v1/narratives/cluster-now")
        assert cluster_response.status_code == 200
        result = cluster_response.json()
        assert result["narratives_created"] == 2
        assert result["content_items_clustered"] == 5

        list_response = await client.get("/api/v1/narratives")
        assert list_response.status_code == 200
        narratives = list_response.json()
        assert len(narratives) == 2

    async def test_list_is_sorted_by_risk_score_descending(self, client):
        await _ingest(client, BUS_LANE_POSTS)
        await _ingest(client, TREE_REMOVAL_POSTS)
        await client.post("/api/v1/narratives/cluster-now")

        response = await client.get("/api/v1/narratives")
        scores = [n["overall_risk_score"] for n in response.json()]
        assert scores == sorted(scores, reverse=True)

    async def test_filter_by_risk_level(self, client):
        await _ingest(client, BUS_LANE_POSTS)
        await client.post("/api/v1/narratives/cluster-now")

        response = await client.get("/api/v1/narratives", params={"risk_level": "LOW"})
        assert response.status_code == 200
        for narrative in response.json():
            assert narrative["risk_level"] == "LOW"

    async def test_second_cluster_now_call_is_idempotent_on_already_clustered_items(self, client):
        await _ingest(client, BUS_LANE_POSTS)
        await client.post("/api/v1/narratives/cluster-now")

        second = await client.post("/api/v1/narratives/cluster-now")
        assert second.json()["content_items_clustered"] == 0


class TestNarrativeDetail:
    async def test_returns_404_for_unknown_narrative(self, client):
        response = await client.get(f"/api/v1/narratives/{uuid.uuid4()}")
        assert response.status_code == 404

    async def test_detail_includes_timeline_and_matched_fault_lines(
        self, client, db_session, real_embedder
    ):
        from app.models.fault_line import FaultLine

        description = (
            "Residents distrust new transit and bus lane projects, fearing they are a "
            "hidden tax in disguise."
        )
        fault_line = FaultLine(
            community_name="Downtown",
            grievance_theme="Cost-of-living anxiety",
            description=description,
            embedding=real_embedder.embed(f"Cost-of-living anxiety: {description}"),
        )
        db_session.add(fault_line)
        await db_session.commit()

        # HDBSCAN needs topic diversity to detect density contrast - see the comment in
        # clustering_service.py. TREE_REMOVAL_POSTS gives the bus-lane posts something to
        # contrast against so they reliably form a cluster.
        await _ingest(client, BUS_LANE_POSTS)
        await _ingest(client, TREE_REMOVAL_POSTS)
        await client.post("/api/v1/narratives/cluster-now")

        narratives = (await client.get("/api/v1/narratives")).json()
        assert len(narratives) == 2

        # FakeLLMClient's summarize_narrative always returns the same generic title, so
        # identify the bus-lane narrative by its actual content instead.
        bus_lane_detail = None
        for narrative in narratives:
            detail = (await client.get(f"/api/v1/narratives/{narrative['id']}")).json()
            if any("bus lane" in item["text"].lower() for item in detail["content_items"]):
                bus_lane_detail = detail
                break

        assert bus_lane_detail is not None
        assert len(bus_lane_detail["content_items"]) == 3
        assert bus_lane_detail["content_items"] == sorted(
            bus_lane_detail["content_items"], key=lambda item: item["created_at"]
        )
        # A fault line whose theme closely matches "hidden tax" bus-lane posts should surface.
        matched_names = [fl["community_name"] for fl in bus_lane_detail["matched_fault_lines"]]
        assert "Downtown" in matched_names


async def test_narrative_model_defaults(db_session):
    narrative = Narrative(title="Test narrative")
    db_session.add(narrative)
    await db_session.commit()
    await db_session.refresh(narrative)

    assert narrative.overall_risk_score == 0.0
    # Enum columns are plain String columns (not SQLAlchemy Enum), so after a DB round-trip
    # the attribute is a raw str, not a NarrativeStatus instance - Pydantic coerces it back
    # to the enum at the API boundary (see NarrativeRead), not the ORM layer.
    assert narrative.status == NarrativeStatus.ACTIVE.value
