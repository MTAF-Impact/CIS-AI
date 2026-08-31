import pytest

from app.services.llm_client import get_llm_client
from tests.fakes import AlwaysFailingLLMClient, FakeLLMClient

pytestmark = pytest.mark.integration


class TestGenerateSynthetic:
    async def test_generates_and_persists_posts_through_the_normal_pipeline(self, client):
        response = await client.post(
            "/api/v1/ingest/generate-synthetic",
            json={"count": 5, "auto_cluster": False},
        )
        assert response.status_code == 201
        body = response.json()
        assert len(body["generated"]) == 5
        assert body["failed"] == []
        # Not auto-clustered - cluster stats stay unset.
        assert body["claims_created"] is None

        item = body["generated"][0]
        assert item["claim_id"] is None
        assert item["stance"] is None
        assert item["outrage_score"] is not None  # went through analyze_content, not a stub

    async def test_topic_hint_is_forwarded_to_generation(self, client):
        response = await client.post(
            "/api/v1/ingest/generate-synthetic",
            json={"count": 2, "topic_hint": "ERP road pricing", "auto_cluster": False},
        )
        assert response.status_code == 201
        body = response.json()
        assert all("ERP road pricing" in item["text"] for item in body["generated"])

    async def test_auto_cluster_runs_clustering_after_generation(self, client):
        response = await client.post(
            "/api/v1/ingest/generate-synthetic",
            json={"count": 3, "auto_cluster": True},
        )
        assert response.status_code == 201
        body = response.json()
        # Whether the near-duplicate fake posts actually form a claim is HDBSCAN's
        # concern (covered by test_clustering_service.py) - this only asserts that
        # auto_cluster actually triggered the clustering pass at all.
        assert body["claims_created"] is not None
        assert body["claims_updated"] is not None
        assert body["content_items_clustered"] is not None

    async def test_count_out_of_bounds_is_rejected(self, client):
        response = await client.post(
            "/api/v1/ingest/generate-synthetic", json={"count": 0}
        )
        assert response.status_code == 422

        response = await client.post(
            "/api/v1/ingest/generate-synthetic", json={"count": 51}
        )
        assert response.status_code == 422

    async def test_llm_not_configured_returns_503(self, client):
        from app.main import app

        app.dependency_overrides[get_llm_client] = AlwaysFailingLLMClient
        try:
            response = await client.post(
                "/api/v1/ingest/generate-synthetic", json={"count": 3}
            )
        finally:
            app.dependency_overrides[get_llm_client] = FakeLLMClient
        assert response.status_code == 503
