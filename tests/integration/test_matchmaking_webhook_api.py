"""Flow 1 webhook (POST /matchmaking/policies) - the force/short-circuit fix (B1/B2)
from AI_REQUIREMENT_FOR_INTEGRATION_SUMMARY_V1.md. Previously ANY existing
backend_policy_id short-circuited unconditionally and `force` was never read, so a
failed run could never recover and a document replacement never re-matched."""

import uuid
from datetime import date

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.claim import Claim
from app.models.enums import ClaimType, ContentSource
from app.models.policy import Policy
from tests.jakarta_fixtures import ERP_POSTS, TREE_REMOVAL_POSTS

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_live_callback(monkeypatch):
    # These tests exercise the matchmaking pipeline itself, not the Flow 2 callback -
    # blank BACKEND_URL makes report_matchmaking_result short-circuit with a log line
    # instead of making a real outbound call to the production backend.
    monkeypatch.setattr(settings, "BACKEND_URL", "")


async def _seed_claims(client):
    for text in ERP_POSTS:
        response = await client.post(
            "/api/v1/ingest", json={"text": text, "source": ContentSource.SOCIAL.value, "location": "Sudirman"}
        )
        assert response.status_code == 201
    for text in TREE_REMOVAL_POSTS:
        response = await client.post(
            "/api/v1/ingest", json={"text": text, "source": ContentSource.SOCIAL.value, "location": "Monas"}
        )
        assert response.status_code == 201
    await client.post("/api/v1/claims/cluster-now")


def _payload(backend_policy_id: uuid.UUID, force: bool = False) -> dict:
    return {
        "policy_id": str(backend_policy_id),
        "name": "ERP Congestion Charge Rollout",
        "description": "Expands the ERP congestion charge program citywide.",
        "rolled_out_date": date(2026, 6, 1).isoformat(),
        "force": force,
    }


async def _get_policy(db_session, backend_policy_id: uuid.UUID) -> Policy:
    # The background task writes via a different session on the same connection pool -
    # expire this session's identity map first so we read fresh column values, not a
    # cached object from an earlier query in the same test.
    db_session.expunge_all()
    return (
        await db_session.execute(select(Policy).where(Policy.backend_policy_id == backend_policy_id))
    ).scalar_one()


async def _predicted_claim_id(db_session, policy_id: uuid.UUID) -> uuid.UUID:
    db_session.expunge_all()
    return (
        await db_session.execute(
            select(Claim.id).where(Claim.policy_id == policy_id, Claim.claim_type == ClaimType.NON_EXISTING)
        )
    ).scalar_one()


class TestFlow1ForceAndShortCircuit:
    async def test_fresh_run_creates_policy_and_matches(self, client, db_session):
        await _seed_claims(client)
        backend_policy_id = uuid.uuid4()

        response = await client.post("/api/v1/matchmaking/policies", json=_payload(backend_policy_id))
        assert response.status_code == 202

        policy = await _get_policy(db_session, backend_policy_id)
        assert policy.processing is False
        assert policy.last_matchmaking_error is None
        # Match quality/count is HDBSCAN-clustering-timing-dependent and already
        # covered by test_policies_api.py::TestAIMatchmaking - this test only needs to
        # prove a fresh (no prior row) Flow 1 call reaches a clean completed state.
        assert await _predicted_claim_id(db_session, policy.id) is not None

    async def test_retry_without_force_after_success_short_circuits(self, client, db_session):
        await _seed_claims(client)
        backend_policy_id = uuid.uuid4()

        await client.post("/api/v1/matchmaking/policies", json=_payload(backend_policy_id))
        policy = await _get_policy(db_session, backend_policy_id)
        first_predicted_id = await _predicted_claim_id(db_session, policy.id)

        # Same backend_policy_id, no force - must NOT re-run the pipeline.
        response = await client.post("/api/v1/matchmaking/policies", json=_payload(backend_policy_id))
        assert response.status_code == 202

        second_predicted_id = await _predicted_claim_id(db_session, policy.id)
        assert second_predicted_id == first_predicted_id  # unchanged -> proves no re-run happened

        policies = (
            await db_session.execute(select(Policy).where(Policy.backend_policy_id == backend_policy_id))
        ).scalars().all()
        assert len(policies) == 1  # never duplicated

    async def test_force_true_reruns_and_supersedes_without_duplicating_the_row(self, client, db_session):
        await _seed_claims(client)
        backend_policy_id = uuid.uuid4()

        await client.post("/api/v1/matchmaking/policies", json=_payload(backend_policy_id))
        policy_before = await _get_policy(db_session, backend_policy_id)
        first_predicted_id = await _predicted_claim_id(db_session, policy_before.id)

        response = await client.post(
            "/api/v1/matchmaking/policies", json=_payload(backend_policy_id, force=True)
        )
        assert response.status_code == 202

        policy_after = await _get_policy(db_session, backend_policy_id)
        assert policy_after.id == policy_before.id  # same ai_policy_id - the backend's join key

        second_predicted_id = await _predicted_claim_id(db_session, policy_after.id)
        assert second_predicted_id != first_predicted_id  # old prediction superseded, not duplicated

        # Exactly one predicted claim survives - the old one was deleted, not left behind.
        remaining = (
            await db_session.execute(
                select(Claim).where(
                    Claim.policy_id == policy_after.id, Claim.claim_type == ClaimType.NON_EXISTING
                )
            )
        ).scalars().all()
        assert len(remaining) == 1

    async def test_previous_failure_reruns_even_without_force(self, client, db_session):
        await _seed_claims(client)
        backend_policy_id = uuid.uuid4()

        await client.post("/api/v1/matchmaking/policies", json=_payload(backend_policy_id))
        policy = await _get_policy(db_session, backend_policy_id)
        policy_id = policy.id
        first_predicted_id = await _predicted_claim_id(db_session, policy_id)

        # Simulate a prior run that failed (can't reliably force a real pipeline
        # exception through the fake LLM/embedder test setup, so set the precondition
        # directly - this exercises the exact same branch a real failure would hit).
        # Re-fetch first: _predicted_claim_id's expunge_all() above detached `policy`,
        # so mutating that stale reference wouldn't persist on commit.
        policy = await _get_policy(db_session, backend_policy_id)
        policy.last_matchmaking_error = "simulated failure for B2 coverage"
        await db_session.commit()

        response = await client.post("/api/v1/matchmaking/policies", json=_payload(backend_policy_id))
        assert response.status_code == 202

        policy_after = await _get_policy(db_session, backend_policy_id)
        assert policy_after.last_matchmaking_error is None  # cleared by the successful re-run

        second_predicted_id = await _predicted_claim_id(db_session, policy_id)
        assert second_predicted_id != first_predicted_id  # re-ran, didn't just re-report
