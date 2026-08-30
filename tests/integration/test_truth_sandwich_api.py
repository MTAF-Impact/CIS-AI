import uuid

import pytest

from app.models.content import ContentItem
from app.models.enums import ClassificationLabel, ContentSource, MoralFoundation
from app.models.narrative import Narrative

pytestmark = pytest.mark.integration


async def _make_narrative(db_session, real_embedder) -> Narrative:
    narrative = Narrative(title="Bus Lane Congestion Charge Fears", summary="Test narrative summary")
    db_session.add(narrative)
    await db_session.flush()

    item = ContentItem(
        text="The city is secretly planning a hidden tax via the new bus lane.",
        source=ContentSource.SOCIAL,
        classification=ClassificationLabel.MISINFORMATION,
        confidence=0.9,
        outrage_score=0.7,
        moral_foundation=MoralFoundation.FAIRNESS,
        extracted_claim="The bus lane is secretly a congestion charge.",
        underlying_grievance="cost-of-living anxiety",
        embedding=real_embedder.embed("hidden tax bus lane"),
        narrative_id=narrative.id,
    )
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(narrative)
    return narrative


class TestGenerateResponse:
    async def test_generates_a_draft_truth_sandwich(self, client, db_session, real_embedder):
        narrative = await _make_narrative(db_session, real_embedder)

        response = await client.post(
            "/api/v1/response/generate", json={"narrative_id": str(narrative.id)}
        )

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "DRAFT"
        assert body["response_type"] == "TRUTH_SANDWICH"
        assert body["core_fact"]
        assert body["nuanced_flag"]
        assert body["reiterated_fact"]

    async def test_returns_404_for_unknown_narrative(self, client):
        response = await client.post(
            "/api/v1/response/generate", json={"narrative_id": str(uuid.uuid4())}
        )
        assert response.status_code == 404


class TestReviewResponse:
    async def test_approve_sets_status(self, client, db_session, real_embedder):
        narrative = await _make_narrative(db_session, real_embedder)
        generate = await client.post(
            "/api/v1/response/generate", json={"narrative_id": str(narrative.id)}
        )
        response_id = generate.json()["id"]

        review = await client.patch(
            f"/api/v1/response/{response_id}/review",
            json={"status": "APPROVED", "reviewer_notes": "Looks good"},
        )

        assert review.status_code == 200
        body = review.json()
        assert body["status"] == "APPROVED"
        assert body["reviewer_notes"] == "Looks good"

    async def test_edited_status_applies_edited_fields(self, client, db_session, real_embedder):
        narrative = await _make_narrative(db_session, real_embedder)
        generate = await client.post(
            "/api/v1/response/generate", json={"narrative_id": str(narrative.id)}
        )
        response_id = generate.json()["id"]

        review = await client.patch(
            f"/api/v1/response/{response_id}/review",
            json={"status": "EDITED", "core_fact": "A manually corrected core fact."},
        )

        assert review.status_code == 200
        body = review.json()
        assert body["status"] == "EDITED"
        assert body["core_fact"] == "A manually corrected core fact."

    async def test_returns_404_for_unknown_response(self, client):
        response = await client.patch(
            f"/api/v1/response/{uuid.uuid4()}/review", json={"status": "APPROVED"}
        )
        assert response.status_code == 404
