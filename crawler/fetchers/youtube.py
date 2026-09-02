"""YouTube Data API v3 fetcher - comments on Indonesian video search results. Free,
self-serve key (console.cloud.google.com -> enable "YouTube Data API v3"), 10,000
quota units/day (search.list costs 100 units, commentThreads.list costs 1). Returns
nothing until GOOGLE_API_KEY is set, rather than failing the whole crawl run - same
posture as the Telegram fetcher's missing-credentials guard."""

import logging
from datetime import UTC, datetime

import httpx

from crawler.candidate import Candidate

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
COMMENT_THREADS_URL = "https://www.googleapis.com/youtube/v3/commentThreads"
VIDEOS_PER_QUERY = 5
COMMENTS_PER_VIDEO = 50
REQUEST_TIMEOUT_SECONDS = 15.0


async def _search_video_ids(client: httpx.AsyncClient, api_key: str, query: str) -> list[str]:
    params = {
        "key": api_key,
        "q": query,
        "part": "snippet",
        "type": "video",
        "order": "date",
        "relevanceLanguage": "id",
        "regionCode": "ID",
        "maxResults": VIDEOS_PER_QUERY,
    }
    resp = await client.get(SEARCH_URL, params=params)
    resp.raise_for_status()
    return [item["id"]["videoId"] for item in resp.json().get("items", [])]


async def _fetch_comments(
    client: httpx.AsyncClient, api_key: str, video_id: str
) -> list[Candidate]:
    params = {
        "key": api_key,
        "videoId": video_id,
        "part": "snippet",
        "order": "relevance",
        "maxResults": COMMENTS_PER_VIDEO,
        "textFormat": "plainText",
    }
    resp = await client.get(COMMENT_THREADS_URL, params=params)
    resp.raise_for_status()

    candidates: list[Candidate] = []
    for item in resp.json().get("items", []):
        top = item["snippet"]["topLevelComment"]["snippet"]
        text = (top.get("textDisplay") or "").strip()
        if not text:
            continue
        published = top.get("publishedAt")
        created_at = datetime.fromisoformat(published) if published else datetime.now(UTC)
        candidates.append(
            Candidate(
                text=text,
                source="social",
                author_id=(top.get("authorChannelId") or {}).get("value")
                or top.get("authorDisplayName"),
                location=None,
                external_ref=f"youtube:{video_id}:{item['id']}",
                created_at=created_at,
                popularity_score=float(top.get("likeCount", 0)),
            )
        )
    return candidates


async def fetch_youtube_candidates(api_key: str, search_queries: list[str]) -> list[Candidate]:
    if not (api_key and search_queries):
        return []

    candidates: list[Candidate] = []
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        for query in search_queries:
            try:
                video_ids = await _search_video_ids(client, api_key, query)
            except httpx.HTTPError:
                logger.exception("YouTube search failed for query %r", query)
                continue
            for video_id in video_ids:
                try:
                    candidates.extend(await _fetch_comments(client, api_key, video_id))
                except httpx.HTTPError:
                    # A 403 here usually just means comments are disabled on this
                    # video - common and expected, not worth a full traceback.
                    logger.warning(
                        "Could not fetch comments for video %s (comments may be disabled)",
                        video_id,
                    )
    return candidates
