"""Shared input types for the coordination signal computations - plain dataclasses,
not ORM models, so the signal functions stay pure and DB-independent."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class SignalPost:
    id: str
    account_id: str
    text: str
    created_at: datetime
    # Platform-native reshare/retweet: excluded from the duplication signal, since
    # identical text there is a platform artefact, not authored duplication.
    is_native_reshare: bool = False
    reshared_post_id: str | None = None
    quoted_post_id: str | None = None
    replied_to_post_id: str | None = None
    outbound_urls: tuple[str, ...] = field(default_factory=tuple)
    outbound_domains: tuple[str, ...] = field(default_factory=tuple)
    source: str | None = None


@dataclass(frozen=True)
class SignalAccount:
    account_id: str
    handle: str = ""
    created_at_platform: datetime | None = None
    profile_hash: str | None = None
    bio: str | None = None
    declared_location: str | None = None
    client_app: str | None = None


def pair_key(a: str, b: str) -> tuple[str, str]:
    """Canonical, order-independent key for an undirected account pair."""
    return (a, b) if a < b else (b, a)
