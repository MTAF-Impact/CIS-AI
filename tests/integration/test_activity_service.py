from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models.claim import Claim
from app.models.debunk_segment import ClaimDebunkSegment
from app.models.enums import ClaimStatus, ClaimType
from app.models.topic import Topic
from app.schemas.analysis import DebunkSegmentSchema
from app.services.activity_service import generate_and_cache_debunk_activity
from tests.fakes import FakeLLMClient

pytestmark = pytest.mark.integration


class _DuplicateSegmentNameLLMClient(FakeLLMClient):
    """Returns two segments sharing a name - the case claim_debunk_segments'
    UNIQUE(claim_id, segment_name) constraint exists to reject."""

    async def generate_debunk_segments(self, claim_statement, grounding_context, sample_texts):
        return [
            DebunkSegmentSchema(
                segment_name="Commuters", segment_rationale="First.", content="First draft."
            ),
            DebunkSegmentSchema(
                segment_name="Commuters", segment_rationale="Second.", content="Second draft."
            ),
            DebunkSegmentSchema(
                segment_name="Residents", segment_rationale="Third.", content="Third draft."
            ),
        ]


async def _make_claim(db_session) -> Claim:
    topic = Topic(name="Test Topic")
    db_session.add(topic)
    await db_session.flush()
    claim = Claim(
        claim_type=ClaimType.EXISTING,
        claim_statement="A test claim statement.",
        topic_id=topic.id,
        status=ClaimStatus.UNREVIEWED,
        first_caught_at=datetime.now(UTC),
    )
    db_session.add(claim)
    await db_session.flush()
    return claim


class TestDebunkSegmentGeneration:
    async def test_duplicate_segment_names_are_deduped_not_a_commit_failure(
        self, db_session, real_embedder
    ):
        claim = await _make_claim(db_session)

        await generate_and_cache_debunk_activity(
            db_session,
            claim,
            _DuplicateSegmentNameLLMClient(),
            real_embedder,
            supporting_texts=["A commuter complaint.", "A resident complaint."],
        )
        await db_session.commit()  # must not raise IntegrityError

        segments = (
            await db_session.execute(
                select(ClaimDebunkSegment)
                .where(ClaimDebunkSegment.claim_id == claim.id)
                .order_by(ClaimDebunkSegment.rank)
            )
        ).scalars().all()

        # Only the first "Commuters" survives, plus "Residents" - not three rows.
        assert [s.segment_name for s in segments] == ["Commuters", "Residents"]
        assert segments[0].content == "First draft."
        assert [s.rank for s in segments] == [0, 1]  # no gap left by the dropped duplicate
