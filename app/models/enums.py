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
    """PRD v1.3: a single shared status set for both claim types - the old type-specific
    PREBUNK/DEBUNK statuses (and the business rule barring EXISTING from PREBUNK /
    NON_EXISTING from DEBUNK) have been merged into one shared ACTION_TAKEN status."""

    UNREVIEWED = "unreviewed"
    ACTIVE = "active"
    INACTIVE = "inactive"
    ACTION_TAKEN = "action_taken"


class PolicyStatus(str, enum.Enum):
    """Derived, never stored as a plain flag - see app.models.policy.Policy.status
    (a computed property from rolled_out_date vs. wall-clock time), so it can never go
    stale the way a written-once column would without a scheduled re-evaluation job."""

    NOT_ROLLED_OUT = "not_rolled_out"
    ROLLED_OUT = "rolled_out"
