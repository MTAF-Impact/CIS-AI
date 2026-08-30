import enum


class ClassificationLabel(str, enum.Enum):
    LEGITIMATE_DEBATE = "legitimate_debate"
    MISINFORMATION = "misinformation"
    DISINFORMATION = "disinformation"
    SATIRE = "satire"
    UNKNOWN = "unknown"


class MoralFoundation(str, enum.Enum):
    FAIRNESS = "fairness"
    HARM = "harm"
    AUTONOMY = "autonomy"
    LOYALTY = "loyalty"
    AUTHORITY = "authority"
    PURITY = "purity"
    NEUTRAL = "neutral"


class ContentSource(str, enum.Enum):
    SOCIAL = "social"
    RSS = "rss"
    RADIO = "radio"
    FORUM = "forum"
    OTHER = "other"


class RiskLevel(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class NarrativeStatus(str, enum.Enum):
    ACTIVE = "active"
    MONITORING = "monitoring"
    RESOLVED = "resolved"
    ARCHIVED = "archived"


class ResponseType(str, enum.Enum):
    PREBUNK = "PREBUNK"
    TRUTH_SANDWICH = "TRUTH_SANDWICH"


class ResponseStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    EDITED = "EDITED"
    REJECTED = "REJECTED"
