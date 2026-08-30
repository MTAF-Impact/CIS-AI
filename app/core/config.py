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

    # CORS
    CORS_ORIGINS: list[str] = Field(default_factory=lambda: ["*"])

    # Logging - JSON logs are auto-enabled in production (Cloud Run/Cloud Logging reads
    # the `severity`/`message` fields from stdout JSON directly); override either if needed.
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool | None = None

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
