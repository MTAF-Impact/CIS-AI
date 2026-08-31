from dataclasses import dataclass
from datetime import datetime


@dataclass
class Candidate:
    """Normalized shape every fetcher produces, regardless of source."""

    text: str
    source: str  # ContentSource-compatible value ("rss", "social", ...)
    author_id: str | None
    location: str | None
    external_ref: str
    created_at: datetime
    popularity_score: float
