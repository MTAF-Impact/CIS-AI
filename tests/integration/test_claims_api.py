import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.enums import ContentSource
from app.models.policy import Policy
from tests.jakarta_fixtures import ERP_POSTS, TREE_REMOVAL_POSTS

pytestmark = pytest.mark.integration


async def _ingest(client, texts, location="Sudirman"):
    for text in texts:
        response = await client.post(
            "/api/v1/ingest",
            json={"text": text, "source": ContentSource.SOCIAL.value, "location": location},
        )
        assert response.status_code == 201


async def _seed_two_claims(client):
    await _ingest(client, ERP_POSTS)
    await _ingest(client, TREE_REMOVAL_POSTS, location="Monas")
    cluster_response = await client.post("/api/v1/claims/cluster-now")
    assert cluster_response.status_code == 200
    assert cluster_response.json()["claims_created"] == 2


async def _seed_policy(db_session, **overrides) -> Policy:
    defaults = {
        "title": "Kampung Pulo Housing Redevelopment",
        "description": "Renovates public housing with no planned displacement.",
        "rolled_out_date": datetime.now(UTC).date() + timedelta(days=90),
        "processing": False,
    }
    defaults.update(overrides)
    policy = Policy(**defaults)
    db_session.add(policy)
    await db_session.commit()
    await db_session.refresh(policy)
    return policy


class TestIngestion:
    async def test_ingest_persists_a_claimless_stanceless_item(self, client):
        response = await client.post(
            "/api/v1/ingest", json={"text": "The ERP charge is secretly a hidden tax."}
        )
        assert response.status_code == 201
        body = response.json()
        assert body["claim_id"] is None
        assert body["stance"] is None
        assert len(body["id"]) > 0


class TestExistingClaimsList:
    async def test_envelope_has_fetched_at_and_total(self, client):
        await _seed_two_claims(client)

        response = await client.get("/api/v1/claims/existing")
        assert response.status_code == 200
        body = response.json()
        assert "fetched_at" in body
        assert body["total"] == 2
        assert len(body["items"]) == 2

    async def test_sorted_by_final_claim_score_descending(self, client):
        await _seed_two_claims(client)

        response = await client.get("/api/v1/claims/existing")
        scores = [item["final_claim_score"] for item in response.json()["items"]]
        assert scores == sorted(scores, reverse=True)

    async def test_search_by_claim_statement_text(self, client):
        await _seed_two_claims(client)

        all_claims = (await client.get("/api/v1/claims/existing")).json()["items"]
        target_text_fragment = all_claims[0]["claim_statement"][:20]

        response = await client.get(
            "/api/v1/claims/existing", params={"q": target_text_fragment}
        )
        assert response.status_code == 200
        assert len(response.json()["items"]) >= 1
        assert all(
            target_text_fragment.lower() in item["claim_statement"].lower()
            for item in response.json()["items"]
        )

    async def test_multi_topic_filter_merges_and_ranks_as_one_pool(self, client):
        await _seed_two_claims(client)

        topics = (await client.get("/api/v1/topics")).json()
        assert len(topics) == 2
        topic_ids = [t["id"] for t in topics]

        response = await client.get(
            "/api/v1/claims/existing", params={"topic_ids": topic_ids}
        )
        assert response.status_code == 200
        body = response.json()
        # Merged pool of both topics, ranked once (not top-N-per-topic) - both claims
        # should appear since each topic only has one claim.
        assert body["total"] == 2
        scores = [item["final_claim_score"] for item in body["items"]]
        assert scores == sorted(scores, reverse=True)

    async def test_status_filter(self, client):
        await _seed_two_claims(client)

        response = await client.get(
            "/api/v1/claims/existing", params={"status": "unreviewed"}
        )
        assert response.status_code == 200
        assert all(item["status"] == "unreviewed" for item in response.json()["items"])


class TestExistingClaimDetail:
    async def test_returns_404_for_unknown_claim(self, client):
        response = await client.get(f"/api/v1/claims/{uuid.uuid4()}")
        assert response.status_code == 404

    async def test_full_transparency_breakdown_present(self, client):
        await _seed_two_claims(client)
        claim_id = (await client.get("/api/v1/claims/existing")).json()["items"][0]["id"]

        response = await client.get(f"/api/v1/claims/{claim_id}")
        assert response.status_code == 200
        body = response.json()

        # Every individual score component must be present, never only the collapsed
        # final number (PRD Dashboard Transparency Requirement, Section 5.5).
        for field in (
            "reach_score",
            "velocity_score",
            "falseness_score",
            "harm_score",
            "emotional_intensity_score",
            "emotional_intensity_opposing",
            "claim_score",
            "npr",
            "discount_factor",
            "final_claim_score",
            "is_dormant",
        ):
            assert field in body

        assert body["supporting_statements"]
        assert body["opposing_statements"] == []
        assert body["neutral_statements"] == []
        assert body["activity_content"]


class TestStatusUpdate:
    async def test_valid_transition_succeeds(self, client):
        await _seed_two_claims(client)
        claim_id = (await client.get("/api/v1/claims/existing")).json()["items"][0]["id"]

        response = await client.patch(
            f"/api/v1/claims/{claim_id}/status", json={"status": "active"}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "active"

    async def test_action_taken_is_a_shared_status_for_both_claim_types(self, client, db_session):
        # PRD v1.3: Prebunk/Debunk merged into one shared Action Taken status, usable
        # by both Existing and Non-Existing claims - no more type-specific rejection.
        await _seed_two_claims(client)
        existing_claim_id = (await client.get("/api/v1/claims/existing")).json()["items"][0]["id"]
        existing_response = await client.patch(
            f"/api/v1/claims/{existing_claim_id}/status", json={"status": "action_taken"}
        )
        assert existing_response.status_code == 200
        assert existing_response.json()["status"] == "action_taken"

        policy = await _seed_policy(db_session)
        predict_response = await client.post(
            "/api/v1/claims/non-existing/predict", json={"policy_id": str(policy.id)}
        )
        non_existing_claim_id = predict_response.json()["claim"]["id"]
        non_existing_response = await client.patch(
            f"/api/v1/claims/{non_existing_claim_id}/status", json={"status": "action_taken"}
        )
        assert non_existing_response.status_code == 200
        assert non_existing_response.json()["status"] == "action_taken"

    async def test_returns_404_for_unknown_claim(self, client):
        response = await client.patch(
            f"/api/v1/claims/{uuid.uuid4()}/status", json={"status": "active"}
        )
        assert response.status_code == 404


class TestNonExistingClaimPrediction:
    async def test_predicts_an_unscored_claim(self, client, db_session):
        policy = await _seed_policy(
            db_session,
            title="ITF Sunter Expansion",
            description="Expands the ITF Sunter waste-to-energy plant capacity.",
        )
        response = await client.post(
            "/api/v1/claims/non-existing/predict", json={"policy_id": str(policy.id)}
        )
        assert response.status_code == 201
        body = response.json()
        assert body["claim"]["claim_type"] == "non_existing"
        assert body["claim"]["activity_content"]
        assert body["predicted_attack_angle"]
        assert body["likely_framing"]

        list_response = await client.get("/api/v1/claims/non-existing")
        assert list_response.json()["total"] == 1
        # D2 cards never carry a score - NON_EXISTING claims are never scored.
        assert list_response.json()["items"][0]["final_claim_score"] is None


class TestHarmConfirm:
    async def test_overrides_recompute_downstream_scores(self, client):
        await _seed_two_claims(client)
        claim_id = (await client.get("/api/v1/claims/existing")).json()["items"][0]["id"]
        before = (await client.get(f"/api/v1/claims/{claim_id}")).json()

        response = await client.patch(
            f"/api/v1/claims/{claim_id}/harm/confirm", json={"public_safety": 90.0}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["harm_human_confirmed"] is True
        assert body["harm_public_safety"] == 90.0
        assert body["harm_score"] != before["harm_score"]
        assert body["final_claim_score"] != before["final_claim_score"]


class TestRescore:
    async def test_rescores_every_existing_claim(self, client):
        await _seed_two_claims(client)

        response = await client.post("/api/v1/claims/rescore")
        assert response.status_code == 200
        assert response.json()["claims_rescored"] == 2


class TestCoordinationCheck:
    async def test_check_cib_via_http(self, client):
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        response = await client.post(
            "/api/v1/coordination/check-cib",
            json={
                "posts": [
                    {
                        "id": "1",
                        "text": "The ERP charge is a hidden tax on working families!",
                        "author_id": "botA",
                        "created_at": now.isoformat(),
                        "account_created_at": (now - timedelta(days=2)).isoformat(),
                    },
                    {
                        "id": "2",
                        "text": "This ERP charge is really just a hidden tax on working families!!",
                        "author_id": "botB",
                        "created_at": (now + timedelta(minutes=2)).isoformat(),
                        "account_created_at": (now - timedelta(days=2) + timedelta(minutes=10)).isoformat(),
                    },
                ]
            },
        )
        assert response.status_code == 200
        assert response.json()["is_likely_coordinated"] is True


class TestTopics:
    async def test_create_and_list_topic(self, client):
        create = await client.post("/api/v1/topics", json={"name": "Test Topic"})
        assert create.status_code == 201

        listing = await client.get("/api/v1/topics")
        assert any(t["name"] == "Test Topic" for t in listing.json())


class TestAlertBell:
    async def test_add_and_remove_alert(self, client):
        await _seed_two_claims(client)
        claim_id = (await client.get("/api/v1/claims/existing")).json()["items"][0]["id"]

        add_response = await client.post(f"/api/v1/claims/{claim_id}/alert")
        assert add_response.status_code == 201
        assert add_response.json()["is_alerted"] is True

        watchlist = (await client.get("/api/v1/alerts")).json()
        assert watchlist["total"] == 1
        assert watchlist["items"][0]["claim_id"] == claim_id

        remove_response = await client.delete(f"/api/v1/claims/{claim_id}/alert")
        assert remove_response.status_code == 200
        assert remove_response.json()["is_alerted"] is False

        watchlist_after = (await client.get("/api/v1/alerts")).json()
        assert watchlist_after["total"] == 0

    async def test_non_existing_claim_cannot_be_alerted(self, client, db_session):
        policy = await _seed_policy(db_session)
        predict_response = await client.post(
            "/api/v1/claims/non-existing/predict", json={"policy_id": str(policy.id)}
        )
        claim_id = predict_response.json()["claim"]["id"]

        response = await client.post(f"/api/v1/claims/{claim_id}/alert")
        assert response.status_code == 422


class TestAdminSettings:
    async def test_get_and_update_threshold(self, client):
        response = await client.put("/api/v1/admin/settings", json={"over_threshold": 55.0})
        assert response.status_code == 200
        assert response.json()["over_threshold"] == 55.0

        response = await client.get("/api/v1/admin/settings")
        assert response.json()["over_threshold"] == 55.0

    async def test_generate_generic_claim_is_fully_scored(self, client):
        response = await client.post("/api/v1/admin/generate-generic-claim")
        assert response.status_code == 201
        claim = response.json()["claim"]
        assert claim["final_claim_score"] is not None
        assert claim["activity_content"]
