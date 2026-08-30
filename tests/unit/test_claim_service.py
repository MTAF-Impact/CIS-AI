import pytest

from app.models.enums import ClaimStatus, ClaimType
from app.services.claim_service import (
    InvalidClaimStatusError,
    validate_status_transition,
)


class TestValidateStatusTransition:
    def test_existing_claim_can_never_be_prebunk(self):
        with pytest.raises(InvalidClaimStatusError):
            validate_status_transition(ClaimType.EXISTING, ClaimStatus.PREBUNK)

    def test_non_existing_claim_can_never_be_debunk(self):
        with pytest.raises(InvalidClaimStatusError):
            validate_status_transition(ClaimType.NON_EXISTING, ClaimStatus.DEBUNK)

    @pytest.mark.parametrize(
        "status",
        [ClaimStatus.UNREVIEWED, ClaimStatus.ACTIVE, ClaimStatus.INACTIVE, ClaimStatus.DEBUNK],
    )
    def test_existing_claim_accepts_all_other_statuses(self, status):
        validate_status_transition(ClaimType.EXISTING, status)  # should not raise

    @pytest.mark.parametrize(
        "status",
        [ClaimStatus.UNREVIEWED, ClaimStatus.ACTIVE, ClaimStatus.INACTIVE, ClaimStatus.PREBUNK],
    )
    def test_non_existing_claim_accepts_all_other_statuses(self, status):
        validate_status_transition(ClaimType.NON_EXISTING, status)  # should not raise
