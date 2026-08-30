from datetime import UTC, date, datetime, timedelta

import pytest

from app.models.enums import ContentSource
from tests.jakarta_fixtures import ERP_POSTS, TREE_REMOVAL_POSTS
from tests.policy_fixtures import DOCX_CONTENT_TYPE, make_test_docx_bytes

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


def _upload(name: str, rolled_out_date: date, text: str | None = None) -> dict:
    files = {
        "file": ("policy.docx", make_test_docx_bytes(text or f"Policy document about {name}"), DOCX_CONTENT_TYPE)
    }
    data = {"name": name, "rolled_out_date": rolled_out_date.isoformat()}
    return {"files": files, "data": data}


class TestCreatePolicy:
    async def test_extracts_text_and_derives_not_rolled_out_status(self, client):
        response = await client.post(
            "/api/v1/policies",
            **_upload("Future Policy", datetime.now(UTC).date() + timedelta(days=30)),
        )
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "not_rolled_out"
        assert body["file_name"] == "policy.docx"

    async def test_past_rolled_out_date_derives_rolled_out_status(self, client):
        response = await client.post(
            "/api/v1/policies",
            **_upload("Past Policy", datetime.now(UTC).date() - timedelta(days=30)),
        )
        assert response.status_code == 201
        assert response.json()["status"] == "rolled_out"

    async def test_unsupported_file_type_is_rejected(self, client):
        response = await client.post(
            "/api/v1/policies",
            files={"file": ("policy.txt", b"plain text", "text/plain")},
            data={"name": "Bad Policy", "rolled_out_date": datetime.now(UTC).date().isoformat()},
        )
        assert response.status_code == 422

    async def test_download_file_roundtrip(self, client):
        raw = make_test_docx_bytes("Roundtrip content")
        create = await client.post(
            "/api/v1/policies",
            files={"file": ("policy.docx", raw, DOCX_CONTENT_TYPE)},
            data={"name": "Download Policy", "rolled_out_date": datetime.now(UTC).date().isoformat()},
        )
        policy_id = create.json()["id"]

        download = await client.get(f"/api/v1/policies/{policy_id}/file")
        assert download.status_code == 200
        assert download.content == raw


class TestAIMatchmaking:
    async def test_links_existing_claim_and_predicts_a_non_existing_claim(self, client):
        await _seed_two_claims(client)

        response = await client.post(
            "/api/v1/policies",
            **_upload(
                "ERP Congestion Charge Rollout",
                datetime.now(UTC).date() + timedelta(days=60),
                text="This policy expands the ERP congestion charge program citywide.",
            ),
        )
        assert response.status_code == 201
        policy_id = response.json()["id"]

        detail = (await client.get(f"/api/v1/policies/{policy_id}")).json()
        # By the time the ASGI call returns, the BackgroundTasks matchmaking job has
        # already run (see Starlette's Response.__call__ awaiting background tasks
        # before the ASGI callable returns) - no polling needed.
        assert detail["processing"] is False
        assert len(detail["existing_claims"]) >= 1
        assert len(detail["non_existing_claims"]) == 1
        assert detail["non_existing_claims"][0]["final_claim_score"] is None


class TestListPolicies:
    async def test_filters_by_year_and_search(self, client):
        await client.post("/api/v1/policies", **_upload("Year2020 Policy", date(2020, 1, 1)))
        await client.post("/api/v1/policies", **_upload("Year2030 Policy", date(2030, 1, 1)))

        by_year = (await client.get("/api/v1/policies", params={"years": [2020]})).json()
        assert all(item["rolled_out_date"].startswith("2020") for item in by_year["items"])

        by_search = (
            await client.get("/api/v1/policies", params={"q": "Year2030"})
        ).json()
        assert any(item["title"] == "Year2030 Policy" for item in by_search["items"])
