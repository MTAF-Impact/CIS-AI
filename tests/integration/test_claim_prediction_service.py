from datetime import UTC, datetime, timedelta

import pytest

from app.models.enums import ClaimStatus, ClaimType
from app.models.policy import Policy
from app.services.claim_prediction_service import predict_non_existing_claim
from tests.fakes import FakeLLMClient

pytestmark = pytest.mark.integration


def _policy(**overrides) -> Policy:
    defaults = {
        "title": "Kampung Pulo Housing Redevelopment",
        "description": (
            "The city will renovate public housing in Kampung Pulo along the "
            "Ciliwung riverbank with no planned displacement."
        ),
        "rolled_out_date": datetime.now(UTC).date() + timedelta(days=90),
        "processing": False,
    }
    defaults.update(overrides)
    return Policy(**defaults)


class TestPredictNonExistingClaim:
    async def test_creates_an_unscored_non_existing_claim(self, db_session, real_embedder):
        policy = _policy()
        db_session.add(policy)
        await db_session.flush()

        prediction = await predict_non_existing_claim(
            db_session, policy, llm=FakeLLMClient(), embedder=real_embedder
        )
        await db_session.commit()

        claim = prediction.claim
        assert claim.claim_type == ClaimType.NON_EXISTING
        assert claim.status == ClaimStatus.UNREVIEWED
        assert claim.claim_statement
        assert claim.topic_id is not None
        assert claim.policy_id == policy.id
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

    async def test_already_covered_statements_are_forwarded_to_the_llm(
        self, db_session, real_embedder
    ):
        policy = _policy(title="ERP Road Pricing Expansion", description="Expands ERP gantries.")
        db_session.add(policy)
        await db_session.flush()

        llm = FakeLLMClient()
        await predict_non_existing_claim(
            db_session,
            policy,
            llm=llm,
            embedder=real_embedder,
            already_covered_claim_statements=["ERP is secretly a hidden tax"],
        )
        await db_session.commit()

        call = next(c for c in llm.calls if c[0] == "predict_non_existing_claim")
        _, _, grounding_context = call[1]
        assert "ERP is secretly a hidden tax" in grounding_context
