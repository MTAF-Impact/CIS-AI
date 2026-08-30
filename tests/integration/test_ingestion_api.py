import pytest
from sqlalchemy import select

from app.models.content import ContentItem

pytestmark = pytest.mark.integration


class TestIngestSingle:
    async def test_ingest_persists_a_classified_embedded_item(self, client, db_session):
        response = await client.post(
            "/api/v1/ingest",
            json={
                "text": "The city council is secretly planning a hidden tax on drivers.",
                "source": "social",
                "author_id": "user_1",
                "location": "Downtown",
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["classification"] == "misinformation"  # FakeLLMClient keyword match
        assert body["location"] == "Downtown"
        assert body["narrative_id"] is None

        row = (
            await db_session.execute(select(ContentItem).where(ContentItem.id == body["id"]))
        ).scalar_one()
        assert row.embedding is not None
        assert len(row.embedding) == 384

    async def test_ingest_rejects_empty_text(self, client):
        response = await client.post("/api/v1/ingest", json={"text": ""})
        assert response.status_code == 422


class TestIngestBatch:
    async def test_batch_creates_all_valid_items(self, client, db_session):
        response = await client.post(
            "/api/v1/ingest/batch",
            json={
                "items": [
                    {"text": "First post about the bus lane.", "source": "social"},
                    {"text": "Second post about the tree removal.", "source": "forum"},
                ]
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert len(body["created"]) == 2
        assert body["failed"] == []

        count = (await db_session.execute(select(ContentItem))).scalars().all()
        assert len(count) == 2

    async def test_batch_rejects_empty_items_list(self, client):
        response = await client.post("/api/v1/ingest/batch", json={"items": []})
        assert response.status_code == 422
