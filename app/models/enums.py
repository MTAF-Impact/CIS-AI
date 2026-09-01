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
    """A post's stance relative to its claim - assessed only once clustered."""

    SUPPORTING = "supporting"
    OPPOSING = "opposing"
    NEUTRAL = "neutral"


class Sentiment(str, enum.Enum):
    """A content item's own emotional valence, independent of any claim (PRD v1.5
    6.6.1 - the Baseline Climate Sentiment half of F6's Climate Sentiment Index).

    Not the same axis as Stance: stance is a position relative to a specific claim
    and only exists once a post is clustered, while sentiment is assessed on every
    piece of content at ingestion time regardless of whether it ever gets clustered.
    Opposing a false claim is good for the city's information health, so reading
    stance as sentiment would invert the index for exactly the content the product
    most wants to see - the two must never be conflated."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class ClaimType(str, enum.Enum):
    """Fixed by pipeline of origin - Existing from clustering, Non-Existing from prediction."""

    EXISTING = "existing"
    NON_EXISTING = "non_existing"


class ClaimStatus(str, enum.Enum):
    """A single shared status set for both claim types."""

    UNREVIEWED = "unreviewed"
    ACTIVE = "active"
    INACTIVE = "inactive"
    ACTION_TAKEN = "action_taken"


class PolicyStatus(str, enum.Enum):
    """Derived, never stored - see Policy.status."""

    NOT_ROLLED_OUT = "not_rolled_out"
    ROLLED_OUT = "rolled_out"


class DetectionRunStatus(str, enum.Enum):
    """PENDING is the state the row is created in - synchronously, in the request
    handler, before the 202 response - so run_id is real and queryable immediately.
    The background task flips it to RUNNING once it actually starts."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ConfidenceBand(str, enum.Enum):
    """PRD 10.6.2 - a network's evidentiary strength, distinct from its raw score."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
