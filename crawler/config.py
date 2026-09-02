from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CrawlerSettings(BaseSettings):
    """Env-driven config for the crawler Cloud Run Job. Deliberately its own settings
    class, not app.core.config.Settings - this is a separate deployable unit with no
    Postgres/OpenAI credentials of its own, only the AI service's HTTP API."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=True, extra="ignore"
    )

    AI_SERVICE_URL: str = "http://localhost:8000"
    AI_SERVICE_API_KEY: str = ""

    # JSON list, matching app.core.config.Settings.CORS_ORIGINS's convention.
    RSS_FEED_URLS: list[str] = Field(default_factory=list)
    TELEGRAM_CHANNELS: list[str] = Field(default_factory=list)

    # console.cloud.google.com -> enable "YouTube Data API v3" on this project. One
    # key, shared with the main AI service's GOOGLE_API_KEY (app/core/config.py,
    # where it stays optional) - same Google Cloud project, "Fact Check Tools API"
    # also enabled on it there. REQUIRED here - unlike Telegram, YouTube is not a
    # best-effort source; main.py's run() raises immediately if this is unset rather
    # than silently returning zero YouTube candidates.
    GOOGLE_API_KEY: str = ""
    # Fixed floor/seed queries - always searched. Real breadth comes from
    # main.py appending one query per active AI-service topic on top of these
    # (see AIServiceClient.fetch_topic_names), so this list only needs to cover
    # the cold-start case before any topics exist yet.
    YOUTUBE_SEARCH_QUERIES: list[str] = Field(default_factory=list)
    # Caps total queries per run (static + topic-derived) to protect the
    # 10,000 unit/day quota - each query costs 100 units (search.list) plus
    # ~1 unit per video's comment page.
    YOUTUBE_MAX_QUERIES: int = 15

    # Telethon session string generated via a one-time interactive login - see
    # docs/CRAWLER.md's Phase 2 setup instructions. Empty = Telegram fetch is skipped.
    TELEGRAM_API_ID: int | None = None
    TELEGRAM_API_HASH: str = ""
    TELEGRAM_SESSION_STRING: str = ""

    TOP_N_PER_SOURCE: int = 20
    CRAWL_WINDOW_HOURS: float = 6.0

    RELEVANCE_MODEL_NAME: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    RELEVANCE_THRESHOLD: float = 0.35

    LOCATION_KEYWORDS: list[str] = Field(
        default_factory=lambda: [
            "jakarta", "dki", "sudirman", "thamrin", "kampung pulo", "penjaringan",
            "muara angke", "muara baru", "sunter", "cakung", "kota tua", "monas",
            "blok m", "kampung melayu", "ciliwung",
        ]
    )


@lru_cache
def get_settings() -> CrawlerSettings:
    return CrawlerSettings()
