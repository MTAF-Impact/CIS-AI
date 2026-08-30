import pytest
from sqlalchemy import select

from app.models.enums import ClaimStatus, ClaimType
from app.models.policy import Policy
from app.services.claim_prediction_service import predict_non_existing_claim
from tests.fakes import FakeLLMClient

pytestmark = pytest.mark.integration


class TestPredictNonExistingClaim:
    async def test_creates_an_unscored_non_existing_claim(self, db_session, real_embedder):
        prediction = await predict_non_existing_claim(
            db_session,
            policy_title="Kampung Pulo Housing Redevelopment",
            policy_description=(
                "The city will renovate public housing in Kampung Pulo along the "
                "Ciliwung riverbank with no planned displacement."
            ),
            llm=FakeLLMClient(),
            embedder=real_embedder,
        )

        claim = prediction.claim
        assert claim.claim_type == ClaimType.NON_EXISTING
        assert claim.status == ClaimStatus.UNREVIEWED
        assert claim.claim_statement
        assert claim.topic_id is not None
        assert claim.policy_id is not None
        assert claim.activity_content
        assert claim.activity_generated_at is not None

        # NON_EXISTING claims are never scored (PRD: "Non-existing claims are not scored").
        assert claim.reach_score is None
        assert claim.velocity_score is None
        assert claim.falseness_score is None
        assert claim.harm_score is None
        assert claim.emotional_intensity_score is None
        assert claim.claim_score is None
        assert claim.final_claim_score is None
        assert claim.npr is None

        assert prediction.predicted_attack_angle
        assert prediction.likely_framing

    async def test_reuses_an_existing_policy_with_the_same_title(self, db_session, real_embedder):
        first = await predict_non_existing_claim(
            db_session,
            policy_title="ERP Road Pricing Expansion",
            policy_description="Expands ERP gantries to more corridors.",
            llm=FakeLLMClient(),
            embedder=real_embedder,
        )
        second = await predict_non_existing_claim(
            db_session,
            policy_title="ERP Road Pricing Expansion",
            policy_description="Expands ERP gantries to more corridors.",
            llm=FakeLLMClient(),
            embedder=real_embedder,
        )

        assert first.claim.policy_id == second.claim.policy_id

        policies = (
            await db_session.execute(
                select(Policy).where(Policy.title == "ERP Road Pricing Expansion")
            )
        ).scalars().all()
        assert len(policies) == 1
