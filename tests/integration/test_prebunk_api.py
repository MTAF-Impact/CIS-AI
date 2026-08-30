from datetime import UTC

import pytest

pytestmark = pytest.mark.integration


class TestPredict:
    async def test_predict_returns_fake_llm_prediction_grounded_by_fault_lines(
        self, client, db_session, real_embedder
    ):
        from app.models.fault_line import FaultLine

        description = "Residents distrust city hall after repeated displacement."
        fault_line = FaultLine(
            community_name="District X",
            grievance_theme="Historical displacement distrust",
            description=description,
            embedding=real_embedder.embed(f"Historical displacement distrust: {description}"),
        )
        db_session.add(fault_line)
        await db_session.commit()

        response = await client.post(
            "/api/v1/prebunk/predict",
            json={
                "policy_title": "District X Housing Redevelopment",
                "policy_description": "The city will renovate public housing in District X with no planned displacement.",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["predicted_attack_angle"]
        assert "District X" in body["grounding_sources"]

    async def test_predict_rejects_empty_policy_description(self, client):
        response = await client.post(
            "/api/v1/prebunk/predict", json={"policy_description": ""}
        )
        assert response.status_code == 422


class TestCheckCIB:
    async def test_check_cib_flags_a_coordinated_pair_via_http(self, client):
        from datetime import datetime, timedelta

        now = datetime.now(UTC)
        account_created = (now - timedelta(days=2)).isoformat()

        response = await client.post(
            "/api/v1/prebunk/check-cib",
            json={
                "posts": [
                    {
                        "id": "1",
                        "text": "The new bus lane is a hidden tax on working families!",
                        "author_id": "botA",
                        "created_at": now.isoformat(),
                        "account_created_at": account_created,
                    },
                    {
                        "id": "2",
                        "text": "This bus lane is really just a hidden tax on working families!!",
                        "author_id": "botB",
                        "created_at": (now + timedelta(minutes=2)).isoformat(),
                        "account_created_at": (
                            now - timedelta(days=2) + timedelta(minutes=10)
                        ).isoformat(),
                    },
                ]
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["is_likely_coordinated"] is True
        assert len(body["clusters"]) == 1

    async def test_check_cib_rejects_fewer_than_two_posts(self, client):
        response = await client.post(
            "/api/v1/prebunk/check-cib",
            json={"posts": [{"id": "1", "text": "hi", "author_id": "u1", "created_at": "2026-01-01T00:00:00Z"}]},
        )
        assert response.status_code == 422
