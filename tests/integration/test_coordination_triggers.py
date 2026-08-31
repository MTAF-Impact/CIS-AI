"""Scheduled sweep (PRD 10.5.8 point 1), via the same unified trigger endpoint as the
single-claim run (claim_id omitted -> sweep every Active claim). The velocity-crossing
trigger (10.5.8 point 2) moved to the backend - it watches velocity_score directly and
decides when to call this endpoint; there's nothing left in this service to test for
that decision, only that the sweep itself only touches Active claims."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models.claim import Claim
from app.models.coordination import DetectionRun
from app.models.enums import ClaimStatus, ClaimType
from app.models.topic import Topic

pytestmark = pytest.mark.integration


async def _seed_claim(db_session, status: ClaimStatus = ClaimStatus.UNREVIEWED) -> Claim:
    topic = Topic(name="Trigger Test Topic")
    db_session.add(topic)
    await db_session.flush()
    claim = Claim(
        claim_type=ClaimType.EXISTING,
        claim_statement="claim under test",
        topic_id=topic.id,
        status=status,
        first_caught_at=datetime.now(UTC),
    )
    db_session.add(claim)
    await db_session.commit()
    await db_session.refresh(claim)
    return claim


class TestScheduledSweep:
    async def test_only_sweeps_active_claims(self, client, db_session):
        active_claim = await _seed_claim(db_session, status=ClaimStatus.ACTIVE)
        await _seed_claim(db_session, status=ClaimStatus.UNREVIEWED)

        response = await client.post("/api/v1/coordination/detection-runs", json={})
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "scheduled"
        assert body["claim_id"] is None

        runs = (await db_session.execute(select(DetectionRun))).scalars().all()
        assert len(runs) == 1
        assert runs[0].scope_claim_ids == [str(active_claim.id)]

    async def test_no_active_claims_produces_no_runs(self, client, db_session):
        await _seed_claim(db_session, status=ClaimStatus.UNREVIEWED)

        response = await client.post("/api/v1/coordination/detection-runs", json={})
        assert response.status_code == 202

        runs = (await db_session.execute(select(DetectionRun))).scalars().all()
        assert runs == []
