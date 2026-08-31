"""Multi-claim batch runs (PRD 10.5.1 point 6): the backend sends one or many
claim_ids in a single POST /api/v1/detection/runs call - it decides the scope and
cadence (scheduled/velocity/on-demand, PRD 10.5.8) entirely on its own now; there is
no "sweep active claims" behaviour left on this side to test. What's still ours to
verify: one call with N claim_ids produces one detection_run scoped to all of them,
and each claim's signals/relevance gate are still evaluated independently (10.5.1
point 6 - pooling is optional, per-claim correctness is not)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.claim import Claim
from app.models.content import ContentItem
from app.models.coordination import CoordinatedNetwork, DetectionRun, NetworkClaimLink
from app.models.enums import ClaimType, ContentSource, DetectionRunStatus, Stance
from app.models.topic import Topic
from tests.coordination_fixtures import detection_request

pytestmark = pytest.mark.integration

BOT_TEXT = "The new congestion charge policy is nothing but a hidden tax on hardworking commuters"


async def _seed_claim(db_session, name: str) -> Claim:
    topic = Topic(name=name)
    db_session.add(topic)
    await db_session.flush()
    claim = Claim(
        claim_type=ClaimType.EXISTING,
        claim_statement=f"claim under test - {name}",
        topic_id=topic.id,
        first_caught_at=datetime.now(UTC),
    )
    db_session.add(claim)
    await db_session.commit()
    await db_session.refresh(claim)
    return claim


async def _seed_coordinated_cluster(db_session, claim_id, prefix: str, num_accounts=6, posts_per_account=4):
    now = datetime.now(UTC)
    for i in range(num_accounts):
        for j in range(posts_per_account):
            db_session.add(
                ContentItem(
                    text=BOT_TEXT + ("!" * j),
                    source=ContentSource.SOCIAL,
                    author_id=f"{prefix}{i}",
                    claim_id=claim_id,
                    stance=Stance.SUPPORTING,
                    created_at=now - timedelta(minutes=j),
                )
            )
    await db_session.commit()


class TestMultiClaimBatchRun:
    async def test_one_call_two_claims_produces_one_run_with_both_scoped(self, client, db_session):
        claim_a = await _seed_claim(db_session, "Claim A")
        claim_b = await _seed_claim(db_session, "Claim B")
        await _seed_coordinated_cluster(db_session, claim_a.id, prefix="bota")
        await _seed_coordinated_cluster(db_session, claim_b.id, prefix="botb")

        response = await client.post(
            "/api/v1/detection/runs",
            json=detection_request([claim_a.id, claim_b.id], trigger_source="scheduled"),
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]

        run = await db_session.get(DetectionRun, run_id)
        assert run.status == DetectionRunStatus.COMPLETED
        assert set(run.scope_claim_ids) == {str(claim_a.id), str(claim_b.id)}
        assert run.trigger_source == "scheduled"

        networks = (
            await db_session.execute(select(CoordinatedNetwork).where(CoordinatedNetwork.run_id == run_id))
        ).scalars().all()
        assert len(networks) == 2  # one network per claim's own coordinated cluster

        linked_claim_ids = {
            row[0]
            for row in (
                await db_session.execute(
                    select(NetworkClaimLink.claim_id).where(
                        NetworkClaimLink.network_id.in_([n.id for n in networks])
                    )
                )
            ).all()
        }
        assert linked_claim_ids == {claim_a.id, claim_b.id}

    async def test_one_claim_with_no_cluster_does_not_block_the_other(self, client, db_session):
        quiet_claim = await _seed_claim(db_session, "Quiet Claim")
        loud_claim = await _seed_claim(db_session, "Loud Claim")
        await _seed_coordinated_cluster(db_session, loud_claim.id, prefix="botc")

        response = await client.post(
            "/api/v1/detection/runs",
            json=detection_request([quiet_claim.id, loud_claim.id], trigger_source="velocity"),
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]

        run = await db_session.get(DetectionRun, run_id)
        assert run.status == DetectionRunStatus.COMPLETED

        networks = (
            await db_session.execute(select(CoordinatedNetwork).where(CoordinatedNetwork.run_id == run_id))
        ).scalars().all()
        assert len(networks) == 1
