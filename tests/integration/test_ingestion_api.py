import pytest

from app.models.fault_line import FaultLine
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


class TestTranslation:
    async def test_analysis_populates_text_en(self, client):
        response = await client.post(
            "/api/v1/ingest", json={"text": "The ERP charge is a hidden tax."}
        )
        assert response.status_code == 201
        assert response.json()["text_en"] == "The ERP charge is a hidden tax."


class TestSentiment:
    async def test_analysis_populates_sentiment(self, client):
        """PRD v1.5 6.6.1 - feeds F6's Climate Sentiment Index."""
        response = await client.post(
            "/api/v1/ingest", json={"text": "The ERP charge is a hidden tax."}
        )
        assert response.status_code == 201
        assert response.json()["sentiment"] == "negative"


class TestExternalRefDedup:
    async def test_single_ingest_is_idempotent_on_external_ref(self, client):
        payload = {"text": "Duplicate-prone crawled post.", "external_ref": "rss:feedA:guid-1"}

        first = await client.post("/api/v1/ingest", json=payload)
        assert first.status_code == 201
        first_id = first.json()["id"]

        second = await client.post("/api/v1/ingest", json=payload)
        assert second.status_code == 201
        assert second.json()["id"] == first_id  # same row, not a duplicate

    async def test_batch_ingest_skips_already_seen_external_refs(self, client):
        seed = await client.post(
            "/api/v1/ingest",
            json={"text": "Already-seen post.", "external_ref": "telegram:chan:123"},
        )
        assert seed.status_code == 201

        response = await client.post(
            "/api/v1/ingest/batch",
            json={
                "items": [
                    {"text": "Already-seen post.", "external_ref": "telegram:chan:123"},
                    {"text": "Brand-new post.", "external_ref": "telegram:chan:456"},
                ]
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["skipped"] == ["telegram:chan:123"]
        assert len(body["created"]) == 1
        assert body["created"][0]["external_ref"] == "telegram:chan:456"

    async def test_items_without_external_ref_are_never_deduped(self, client):
        payload = {"text": "No external ref on this one."}
        first = await client.post("/api/v1/ingest", json=payload)
        second = await client.post("/api/v1/ingest", json=payload)
        assert first.status_code == second.status_code == 201
        assert first.json()["id"] != second.json()["id"]


class TestFaultLinesEndpoint:
    async def test_lists_fault_lines(self, client, db_session):
        db_session.add(
            FaultLine(
                community_name="Kampung Pulo",
                grievance_theme="Eviction distrust",
                description="Historical eviction distrust.",
            )
        )
        await db_session.commit()

        response = await client.get("/api/v1/fault-lines")
        assert response.status_code == 200
        body = response.json()
        assert any(fl["community_name"] == "Kampung Pulo" for fl in body)
