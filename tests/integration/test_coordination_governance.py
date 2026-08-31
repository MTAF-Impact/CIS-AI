"""PRD 10.9.1 point 7 - evidence-artifact purge, now driven entirely by a
backend-supplied network_ids list (POST /api/v1/detection/snapshots/purge) rather
than an age computation on this side - the backend alone can see whether a report
was generated (cis_network_reports), which is what makes the "except reported"
exception possible again, just computed on their side instead of ours."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.claim import Claim
from app.models.content import ContentItem
from app.models.coordination import CoordinatedNetwork, EvidenceSnapshot, NetworkAccount
from app.models.enums import ClaimType, ContentSource, Stance
from app.models.topic import Topic
from tests.coordination_fixtures import detection_request

pytestmark = pytest.mark.integration

BOT_TEXT = "The new congestion charge policy is nothing but a hidden tax on hardworking commuters"


async def _seed_claim(db_session) -> Claim:
    topic = Topic(name="ERP Congestion Charge")
    db_session.add(topic)
    await db_session.flush()
    claim = Claim(
        claim_type=ClaimType.EXISTING,
        claim_statement="The ERP congestion charge is a hidden tax on commuters",
        topic_id=topic.id,
        first_caught_at=datetime.now(UTC),
    )
    db_session.add(claim)
    await db_session.commit()
    await db_session.refresh(claim)
    return claim


async def _seed_coordinated_cluster(db_session, claim_id, num_accounts=6, posts_per_account=4):
    now = datetime.now(UTC)
    for i in range(num_accounts):
        for j in range(posts_per_account):
            db_session.add(
                ContentItem(
                    text=BOT_TEXT + ("!" * j),
                    source=ContentSource.SOCIAL,
                    author_id=f"bot{i}",
                    claim_id=claim_id,
                    stance=Stance.SUPPORTING,
                    created_at=now - timedelta(minutes=j),
                )
            )
    await db_session.commit()


async def _seed_and_detect(client, db_session) -> CoordinatedNetwork:
    """Safe to call more than once in a single test - returns only the network from
    THIS call, not every network in the DB."""
    claim = await _seed_claim(db_session)
    await _seed_coordinated_cluster(db_session, claim.id)
    response = await client.post("/api/v1/detection/runs", json=detection_request([claim.id]))
    assert response.status_code == 202
    return (
        await db_session.execute(
            select(CoordinatedNetwork).order_by(CoordinatedNetwork.created_at.desc()).limit(1)
        )
    ).scalar_one()


class TestEvidenceSnapshotPurge:
    async def test_purges_named_networks_only(self, client, db_session):
        old_network = await _seed_and_detect(client, db_session)
        recent_network = await _seed_and_detect(client, db_session)

        response = await client.post(
            "/api/v1/detection/snapshots/purge", json={"network_ids": [str(old_network.id)]}
        )
        assert response.status_code == 200
        assert response.json()["snapshots_purged"] == 1

        # The audit record itself survives - only the evidence artifacts decay.
        still_exists = await db_session.get(CoordinatedNetwork, old_network.id)
        assert still_exists is not None

        old_accounts = (
            await db_session.execute(
                select(NetworkAccount).where(NetworkAccount.network_id == old_network.id)
            )
        ).scalars().all()
        assert old_accounts == []
        old_snapshot = (
            await db_session.execute(
                select(EvidenceSnapshot).where(EvidenceSnapshot.network_id == old_network.id)
            )
        ).scalar_one_or_none()
        assert old_snapshot is None

        recent_accounts = (
            await db_session.execute(
                select(NetworkAccount).where(NetworkAccount.network_id == recent_network.id)
            )
        ).scalars().all()
        assert len(recent_accounts) >= 6  # 6 members, plus whatever comparison accounts landed
        recent_snapshot = (
            await db_session.execute(
                select(EvidenceSnapshot).where(EvidenceSnapshot.network_id == recent_network.id)
            )
        ).scalar_one_or_none()
        assert recent_snapshot is not None

    async def test_empty_list_purges_nothing(self, client, db_session):
        response = await client.post("/api/v1/detection/snapshots/purge", json={"network_ids": []})
        assert response.status_code == 200
        assert response.json()["snapshots_purged"] == 0
