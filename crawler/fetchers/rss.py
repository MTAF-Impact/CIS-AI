"""RSS fetcher - normalizes feed entries into the common Candidate shape. RSS has no
native popularity signal, so popularity_score approximates it via recency + cross-outlet
corroboration (the same story appearing in multiple feeds at once)."""

import logging
from datetime import UTC, datetime

import feedparser

from crawler.candidate import Candidate

logger = logging.getLogger(__name__)


def fetch_rss_candidates(feed_urls: list[str]) -> list[Candidate]:
    parsed_feeds = []
    for url in feed_urls:
        try:
            parsed_feeds.append((url, feedparser.parse(url)))
        except Exception:
            logger.exception("Failed to fetch RSS feed %s", url)

    title_counts: dict[str, int] = {}
    for _, parsed in parsed_feeds:
        for entry in parsed.entries:
            key = entry.get("title", "").strip().lower()
            title_counts[key] = title_counts.get(key, 0) + 1

    candidates: list[Candidate] = []
    now = datetime.now(UTC)
    for url, parsed in parsed_feeds:
        for entry in parsed.entries:
            title = entry.get("title", "")
            text = f"{title}. {entry.get('summary', '')}".strip()
            if not text:
                continue

            published = entry.get("published_parsed")
            created_at = datetime(*published[:6], tzinfo=UTC) if published else now
            recency_hours = max((now - created_at).total_seconds() / 3600, 0.0)
            corroboration = title_counts.get(title.strip().lower(), 1)

            candidates.append(
                Candidate(
                    text=text,
                    source="rss",
                    author_id=None,
                    location=None,
                    external_ref=f"rss:{url}:{entry.get('id') or entry.get('link') or title}",
                    created_at=created_at,
                    popularity_score=corroboration * 10 - recency_hours,
                )
            )
    return candidates
