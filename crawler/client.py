"""Thin HTTP client to the AI service. The crawler never touches Postgres directly -
every other component in this system talks to the AI service over HTTP only, and the
crawler follows the same rule."""

import httpx

from crawler.config import CrawlerSettings


class AIServiceClient:
    def __init__(self, settings: CrawlerSettings) -> None:
        self._base_url = settings.AI_SERVICE_URL
        self._headers = (
            {"X-API-Key": settings.AI_SERVICE_API_KEY} if settings.AI_SERVICE_API_KEY else {}
        )

    async def fetch_exemplars(self) -> list[str]:
        """The relevance-filter corpus - sourced from fault_lines, not a separately
        curated list, so it stays in sync as fault_lines grows."""
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as client:
            response = await client.get("/api/v1/fault-lines")
            response.raise_for_status()
            fault_lines = response.json()
        return [f"{fl['grievance_theme']}: {fl['description'] or ''}" for fl in fault_lines]

    async def fetch_topic_names(self) -> list[str]:
        """Active topic labels - used to seed YouTube search queries dynamically
        (see main.py) so the query list grows with what the system is actually
        tracking, instead of staying a fixed hand-picked list forever."""
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as client:
            response = await client.get("/api/v1/topics")
            response.raise_for_status()
            topics = response.json()
        return [t["name"] for t in topics]

    async def submit_batch(self, items: list[dict]) -> dict:
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=120.0, headers=self._headers
        ) as client:
            response = await client.post("/api/v1/ingest/batch", json={"items": items})
            response.raise_for_status()
            return response.json()
