"""Claim-type-aware business rules that don't fit cleanly in a Pydantic validator
because they depend on the claim's *persisted* claim_type, not the request payload.
Enforced here as the primary layer; a Postgres CHECK constraint on the claims table
(see app.models.claim.Claim) is the defense-in-depth backstop for any path that
bypasses this service (scripts, future admin tools, direct writes)."""

from app.models.enums import ClaimStatus, ClaimType


class InvalidClaimStatusError(ValueError):
    """Raised when a status is requested that the claim's type can never hold -
    EXISTING claims can never be Prebunk; NON_EXISTING claims can never be Debunk."""


def validate_status_transition(claim_type: ClaimType, new_status: ClaimStatus) -> None:
    if claim_type == ClaimType.EXISTING and new_status == ClaimStatus.PREBUNK:
        raise InvalidClaimStatusError(
            "An EXISTING claim can never be set to status 'prebunk'."
        )
    if claim_type == ClaimType.NON_EXISTING and new_status == ClaimStatus.DEBUNK:
        raise InvalidClaimStatusError(
            "A NON_EXISTING claim can never be set to status 'debunk'."
        )
