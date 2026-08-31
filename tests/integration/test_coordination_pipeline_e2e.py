"""End-to-end detection pipeline, triggered via the AI service's one F5 endpoint
(POST /coordination/detection-runs, PRD 10.5.8 point 3 shape), verified against real
persisted rows."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.claim import Claim
from app.models.content import ContentItem
from app.models.coordination import (
    Account,
    CoordinatedNetwork,
    DetectionRun,
    NetworkAccount,
    NetworkClaimLink,
)
from app.models.enums import ClaimType, ContentSource, DetectionRunStatus, Stance
from app.models.topic import Topic

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
        account_id = f"bot{i}"
        for j in range(posts_per_account):
            db_session.add(
                ContentItem(
                    text=BOT_TEXT + ("!" * j),
                    source=ContentSource.SOCIAL,
                    author_id=account_id,
                    claim_id=claim_id,
                    stance=Stance.SUPPORTING,
                    created_at=now - timedelta(minutes=j),  # past, not future - see window_end filter
                )
            )
    await db_session.commit()


async def _seed_organic_noise(db_session, claim_id):
    """A handful of unrelated, unsynchronized posts on the same claim - should never
    end up inside the detected network."""
    now = datetime.now(UTC)
    texts = [
        "I think the new park is lovely, my kids enjoyed it this weekend",
        "Traffic was actually fine on my commute today, no complaints",
        "Does anyone know when the town hall meeting on this is scheduled",
    ]
    for i, text in enumerate(texts):
        db_session.add(
            ContentItem(
                text=text,
                source=ContentSource.SOCIAL,
                author_id=f"resident{i}",
                claim_id=claim_id,
                stance=Stance.SUPPORTING,
                created_at=now - timedelta(hours=i * 10),
            )
        )
    await db_session.commit()


class TestDetectionRunsEndpoint:
    async def test_unknown_claim_creates_no_run(self, client, db_session):
        """No synchronous 404 anymore - this is a fire-and-forget trigger, matching
        the sweep call's shape. An unknown claim_id is silently skipped inside the
        background task (run_detection_for_claim's own not-found/not-Existing check)."""
        response = await client.post(
            "/api/v1/coordination/detection-runs",
            json={"claim_id": f"{'0' * 8}-0000-0000-0000-000000000000"},
        )
        assert response.status_code == 202

        runs = (await db_session.execute(select(DetectionRun))).scalars().all()
        assert runs == []

    async def test_non_existing_claim_creates_no_run(self, client, db_session):
        topic = Topic(name="T")
        db_session.add(topic)
        await db_session.flush()
        claim = Claim(
            claim_type=ClaimType.NON_EXISTING,
            claim_statement="predicted claim",
            topic_id=topic.id,
            first_caught_at=datetime.now(UTC),
        )
        db_session.add(claim)
        await db_session.commit()
        await db_session.refresh(claim)

        response = await client.post(
            "/api/v1/coordination/detection-runs", json={"claim_id": str(claim.id)}
        )
        assert response.status_code == 202

        runs = (await db_session.execute(select(DetectionRun))).scalars().all()
        assert runs == []

    async def test_detects_and_persists_a_coordinated_network(self, client, db_session):
        claim = await _seed_claim(db_session)
        await _seed_coordinated_cluster(db_session, claim.id)
        await _seed_organic_noise(db_session, claim.id)

        response = await client.post(
            "/api/v1/coordination/detection-runs", json={"claim_id": str(claim.id)}
        )
        assert response.status_code == 202
        assert response.json()["status"] == "scheduled"

        runs = (await db_session.execute(select(DetectionRun))).scalars().all()
        assert len(runs) == 1
        run = runs[0]
        assert run.status == DetectionRunStatus.COMPLETED
        assert run.candidates_count == 9  # 6 bots + 3 organic accounts
        assert run.completed_at is not None

        networks = (await db_session.execute(select(CoordinatedNetwork))).scalars().all()
        assert len(networks) == 1
        network = networks[0]
        assert network.run_id == run.id
        assert 0 <= network.coordination_score <= 100
        assert network.account_count == 6
        assert network.signal_breadth >= 2  # multi-signal rule guarantees this

        network_accounts = (
            await db_session.execute(
                select(Account.platform_account_id)
                .join(NetworkAccount, NetworkAccount.account_id == Account.id)
                .where(NetworkAccount.network_id == network.id)
            )
        ).scalars().all()
        assert set(network_accounts) == {f"bot{i}" for i in range(6)}
        assert not set(network_accounts) & {"resident0", "resident1", "resident2"}

        link = (
            await db_session.execute(
                select(NetworkClaimLink).where(NetworkClaimLink.network_id == network.id)
            )
        ).scalar_one()
        assert link.claim_id == claim.id
        assert link.is_primary_claim is True
        assert link.passed_relevance_gate is True

    async def test_sparse_unrelated_activity_produces_no_network(self, client, db_session):
        claim = await _seed_claim(db_session)
        await _seed_organic_noise(db_session, claim.id)

        response = await client.post(
            "/api/v1/coordination/detection-runs", json={"claim_id": str(claim.id)}
        )
        assert response.status_code == 202

        run = (await db_session.execute(select(DetectionRun))).scalar_one()
        assert run.status == DetectionRunStatus.COMPLETED

        networks = (await db_session.execute(select(CoordinatedNetwork))).scalars().all()
        assert networks == []
