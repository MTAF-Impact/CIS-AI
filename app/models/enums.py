import enum


class ContentSource(str, enum.Enum):
    SOCIAL = "social"
    RSS = "rss"
    RADIO = "radio"
    FORUM = "forum"
    OTHER = "other"


class MoralFoundation(str, enum.Enum):
    FAIRNESS = "fairness"
    HARM = "harm"
    AUTONOMY = "autonomy"
    LOYALTY = "loyalty"
    AUTHORITY = "authority"
    PURITY = "purity"
    NEUTRAL = "neutral"


class Stance(str, enum.Enum):
    """A post's stance relative to the claim it has been clustered into.
    Assessed only once a claim exists - never at ingestion time."""

    SUPPORTING = "supporting"
    OPPOSING = "opposing"
    NEUTRAL = "neutral"


class ClaimType(str, enum.Enum):
    """Fixed by pipeline of origin: EXISTING claims come from real ingested/clustered
    content; NON_EXISTING claims come from the prediction flow and have no content."""

    EXISTING = "existing"
    NON_EXISTING = "non_existing"


class ClaimStatus(str, enum.Enum):
    """Business rule (enforced in app.services.claim_service and via a DB CHECK
    constraint on the claims table): EXISTING claims can never be PREBUNK;
    NON_EXISTING claims can never be DEBUNK."""

    UNREVIEWED = "unreviewed"
    ACTIVE = "active"
    INACTIVE = "inactive"
    PREBUNK = "prebunk"
    DEBUNK = "debunk"
