"""Shared input types for F5's signal computations (PRD 10.5.2). Deliberately plain
dataclasses, not ORM models - signal functions are pure and DB-independent, matching
the existing app/services/cib_detector.py pattern; the pipeline orchestrator (later
phase) is responsible for querying ContentItem/Account and building these."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class SignalPost:
    id: str
    account_id: str
    text: str
    created_at: datetime
    # True for a platform-native reshare/retweet of another post - excluded from the
    # duplication signal (10.5.2.2 required exclusions); identical text there is a
    # platform artefact, not authored duplication.
    is_native_reshare: bool = False
    reshared_post_id: str | None = None
    quoted_post_id: str | None = None
    replied_to_post_id: str | None = None
    outbound_urls: tuple[str, ...] = field(default_factory=tuple)
    outbound_domains: tuple[str, ...] = field(default_factory=tuple)
    source: str | None = None  # e.g. ContentSource value - informational only, no signal reads it


@dataclass(frozen=True)
class SignalAccount:
    account_id: str
    handle: str = ""
    created_at_platform: datetime | None = None
    profile_hash: str | None = None  # hex pHash, computed upstream at ingestion
    bio: str | None = None
    declared_location: str | None = None
    client_app: str | None = None


def pair_key(a: str, b: str) -> tuple[str, str]:
    """Canonical, order-independent key for an undirected account pair."""
    return (a, b) if a < b else (b, a)
