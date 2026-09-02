"""Entry point - fetch -> location filter -> relevance filter -> rank -> submit.

    python -m crawler.main [--dry-run]
"""

import argparse
import asyncio
import logging
from collections import defaultdict

from crawler.candidate import Candidate
from crawler.client import AIServiceClient
from crawler.config import get_settings
from crawler.fetchers.rss import fetch_rss_candidates
from crawler.fetchers.telegram import fetch_telegram_candidates
from crawler.fetchers.youtube import fetch_youtube_candidates
from crawler.relevance_filter import RelevanceFilter

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
)
logger = logging.getLogger("crawler")


def _top_n_per_source(candidates: list[Candidate], top_n: int) -> list[Candidate]:
    by_source: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        by_source[candidate.source].append(candidate)

    selected: list[Candidate] = []
    for source_candidates in by_source.values():
        source_candidates.sort(key=lambda c: c.popularity_score, reverse=True)
        selected.extend(source_candidates[:top_n])
    return selected


async def run(dry_run: bool) -> None:
    settings = get_settings()
    ai_client = AIServiceClient(settings)

    logger.info("Fetching relevance exemplars from %s", settings.AI_SERVICE_URL)
    exemplars = await ai_client.fetch_exemplars()
    relevance = RelevanceFilter(settings, exemplars)

    logger.info("Fetching RSS (%d feeds)...", len(settings.RSS_FEED_URLS))
    candidates = fetch_rss_candidates(settings.RSS_FEED_URLS)

    logger.info("Fetching Telegram (%d channels)...", len(settings.TELEGRAM_CHANNELS))
    candidates += await fetch_telegram_candidates(
        settings.TELEGRAM_API_ID,
        settings.TELEGRAM_API_HASH,
        settings.TELEGRAM_SESSION_STRING,
        settings.TELEGRAM_CHANNELS,
        settings.CRAWL_WINDOW_HOURS,
    )

    topic_queries = [f"{name} jakarta" for name in await ai_client.fetch_topic_names()]
    # Static seed queries first (guaranteed present even with zero topics yet -
    # cold start), then topic-derived ones, de-duplicated, capped to protect quota.
    youtube_queries = list(dict.fromkeys(settings.YOUTUBE_SEARCH_QUERIES + topic_queries))
    youtube_queries = youtube_queries[: settings.YOUTUBE_MAX_QUERIES]

    logger.info(
        "Fetching YouTube (%d queries: %d static + %d topic-derived, capped at %d)...",
        len(youtube_queries),
        len(settings.YOUTUBE_SEARCH_QUERIES),
        len(topic_queries),
        settings.YOUTUBE_MAX_QUERIES,
    )
    candidates += await fetch_youtube_candidates(settings.GOOGLE_API_KEY, youtube_queries)
    logger.info("Fetched %d raw candidates", len(candidates))

    candidates = [c for c in candidates if relevance.location_matches(c.text)]
    logger.info("%d candidates after location filter", len(candidates))

    candidates = [c for c in candidates if relevance.is_relevant(c.text)]
    logger.info("%d candidates after relevance filter", len(candidates))

    candidates = _top_n_per_source(candidates, settings.TOP_N_PER_SOURCE)
    logger.info("%d candidates selected (top-%d per source)", len(candidates), settings.TOP_N_PER_SOURCE)

    if dry_run:
        for candidate in candidates:
            logger.info(
                "[DRY RUN] %s | score=%.1f | %s",
                candidate.source, candidate.popularity_score, candidate.text[:100],
            )
        return

    if not candidates:
        logger.info("Nothing to submit.")
        return

    items = [
        {
            "text": c.text,
            "source": c.source,
            "author_id": c.author_id,
            "location": c.location,
            "external_ref": c.external_ref,
        }
        for c in candidates
    ]
    result = await ai_client.submit_batch(items)
    logger.info(
        "Submitted %d items: %d created, %d skipped, %d failed",
        len(items),
        len(result.get("created", [])),
        len(result.get("skipped", [])),
        len(result.get("failed", [])),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Log candidates instead of submitting")
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
