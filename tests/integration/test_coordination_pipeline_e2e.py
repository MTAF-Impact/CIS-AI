"""End-to-end detection pipeline, triggered via POST /api/v1/detection/runs - the
backend's actual reference contract (claim_ids/trigger_source/window/parameters/
exclusions), verified against real persisted rows."""

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
        """The backend already rejects a bad claim_id before calling us (422 on its
        side) - our endpoint doesn't re-validate claim existence, it just runs
        whatever's in claim_ids and silently skips ones that don't resolve."""
        response = await client.post(
            "/api/v1/detection/runs",
            json=detection_request([f"{'0' * 8}-0000-0000-0000-000000000000"]),
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]

        run = await db_session.get(DetectionRun, run_id)
        assert run.status == DetectionRunStatus.COMPLETED
        networks = (await db_session.execute(select(CoordinatedNetwork))).scalars().all()
        assert networks == []

    async def test_non_existing_claim_creates_no_network(self, client, db_session):
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

        response = await client.post("/api/v1/detection/runs", json=detection_request([claim.id]))
        assert response.status_code == 202
        run_id = response.json()["run_id"]

        run = await db_session.get(DetectionRun, run_id)
        assert run.status == DetectionRunStatus.COMPLETED
        networks = (await db_session.execute(select(CoordinatedNetwork))).scalars().all()
        assert networks == []

    async def test_detects_and_persists_a_coordinated_network(self, client, db_session):
        claim = await _seed_claim(db_session)
        await _seed_coordinated_cluster(db_session, claim.id)
        await _seed_organic_noise(db_session, claim.id)

        response = await client.post("/api/v1/detection/runs", json=detection_request([claim.id]))
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "pending"
        run_id = body["run_id"]

        run = await db_session.get(DetectionRun, run_id)
        assert run.status == DetectionRunStatus.COMPLETED
        assert run.scope_claim_ids == [str(claim.id)]
        assert run.trigger_source == "on_demand"
        assert run.candidates_count == 9  # 6 bots + 3 organic accounts
        assert run.completed_at is not None
        assert run.library_version  # non-empty

        networks = (
            await db_session.execute(select(CoordinatedNetwork).where(CoordinatedNetwork.run_id == run_id))
        ).scalars().all()
        assert len(networks) == 1
        network = networks[0]
        assert 0 <= network.coordination_score <= 100
        assert network.account_count == 6
        assert network.signal_breadth >= 2  # multi-signal rule guarantees this
        assert network.allowlist_suppressed is False
        assert network.raw_counts  # populated, not None

        member_accounts = (
            await db_session.execute(
                select(Account.platform_account_id)
                .join(NetworkAccount, NetworkAccount.account_id == Account.id)
                .where(NetworkAccount.network_id == network.id, NetworkAccount.membership_role == "member")
            )
        ).scalars().all()
        assert set(member_accounts) == {f"bot{i}" for i in range(6)}
        assert not set(member_accounts) & {"resident0", "resident1", "resident2"}

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

        response = await client.post("/api/v1/detection/runs", json=detection_request([claim.id]))
        assert response.status_code == 202
        run_id = response.json()["run_id"]

        run = await db_session.get(DetectionRun, run_id)
        assert run.status == DetectionRunStatus.COMPLETED

        networks = (
            await db_session.execute(select(CoordinatedNetwork).where(CoordinatedNetwork.run_id == run_id))
        ).scalars().all()
        assert networks == []
