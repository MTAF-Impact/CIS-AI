"""POST /api/v1/admin/generate-coordinated-network - demo/testing tool (not part of
the backend's real F5 contract). Covers item 3 (placeholder row written before the
202 response) and item 4 (attaching content to a claim appends a fresh
claim_score_snapshots row, not a single static point)."""

import pytest
from sqlalchemy import select

from app.models.alert import ClaimScoreSnapshot
from app.models.coordination import CoordinatedNetwork, DetectionRun
from app.models.enums import DetectionRunStatus
from app.services.coordination import demo_seed

pytestmark = pytest.mark.integration


class TestGenerateCoordinatedNetwork:
    async def test_creates_a_new_demo_claim_and_a_completed_network(self, client, db_session):
        response = await client.post("/api/v1/admin/generate-coordinated-network")
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "pending"
        claim_id, run_id = body["claim_id"], body["run_id"]

        run = await db_session.get(DetectionRun, run_id)
        assert run.status == DetectionRunStatus.COMPLETED
        assert run.scope_claim_ids == [str(claim_id)]

        networks = (
            await db_session.execute(select(CoordinatedNetwork).where(CoordinatedNetwork.run_id == run_id))
        ).scalars().all()
        assert len(networks) == 1
        # Only the coordinated fraction of the generated pool should ever surface as
        # a network - see demo_seed's module docstring for why 100% coordinated
        # would be an unrealistic (and less convincing) demo.
        expected_coordinated_count = round(
            demo_seed.COORD_DEMO_ACCOUNT_COUNT * demo_seed.COORD_DEMO_COORDINATED_RATIO
        )
        assert networks[0].account_count == expected_coordinated_count
        assert networks[0].signal_breadth >= 2  # multi-signal rule

    async def test_appends_a_fresh_score_snapshot_not_a_single_point(self, client, db_session):
        generic = await client.post("/api/v1/admin/generate-generic-claim")
        claim_id = generic.json()["claim"]["id"]
        before = (
            await db_session.execute(
                select(ClaimScoreSnapshot).where(ClaimScoreSnapshot.claim_id == claim_id)
            )
        ).scalars().all()
        assert len(before) >= 1  # generate-generic-claim already rescores once

        response = await client.post(
            "/api/v1/admin/generate-coordinated-network", params={"claim_id": claim_id}
        )
        assert response.status_code == 202
        assert response.json()["claim_id"] == claim_id

        after = (
            await db_session.execute(
                select(ClaimScoreSnapshot).where(ClaimScoreSnapshot.claim_id == claim_id)
            )
        ).scalars().all()
        assert len(after) > len(before)

    async def test_unknown_claim_id_is_404(self, client):
        response = await client.post(
            "/api/v1/admin/generate-coordinated-network",
            params={"claim_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert response.status_code == 404
