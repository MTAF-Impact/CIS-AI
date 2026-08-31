from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    PROJECT_NAME: str = "CIS AI Service"
    ENV: str = "development"
    PORT: int = 8000

    # Database (Supabase Postgres + pgvector)
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres"

    # OpenAI
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-5.6-luna"

    # Embeddings
    EMBEDDING_MODEL_NAME: str = "sentence-transformers/all-MiniLM-L6-v2"
    EMBEDDING_DIM: int = 384

    # F5 coordination detection - multilingual, separate from the English-only
    # embedding model above (needed for Signal 2b, PRD 10.5.2.2/10.5.2.5). This is
    # the only F5 setting left here - every detection-pipeline tunable (PRD 10.11)
    # is sent in full by the backend on every POST /api/v1/detection/runs call now
    # (DetectorParameters), not read from static config or a DB row.
    COORDINATION_MULTILINGUAL_MODEL_NAME: str = (
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )

    # CORS
    CORS_ORIGINS: list[str] = Field(default_factory=lambda: ["*"])

    # Logging - JSON logs are auto-enabled in production (Cloud Run/Cloud Logging reads
    # the `severity`/`message` fields from stdout JSON directly); override either if needed.
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool | None = None

    # Go backend integration (see docs/AI-INTEGRATION.md in the CIS-Backend repo - the
    # shared Postgres DB is read-only from this side; all coordination is these 3 HTTP
    # touchpoints). Both keys are optional shared secrets, empty by default (private
    # network only, no key exchanged) - see that doc's "Configuration" section.
    BACKEND_URL: str = ""  # e.g. https://cis-backend-465014351308.europe-west1.run.app
    AI_SERVICE_API_KEY: str = ""  # validates inbound X-API-Key from the backend, if set
    INTERNAL_API_KEY: str = ""  # sent as X-Internal-Key on our callback to the backend, if set

    @property
    def is_production(self) -> bool:
        return self.ENV.lower() in {"prod", "production"}

    @property
    def log_json(self) -> bool:
        return self.is_production if self.LOG_JSON is None else self.LOG_JSON


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
