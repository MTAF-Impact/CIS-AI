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
    # embedding model above (needed for Signal 2b, PRD 10.5.2.2/10.5.2.5).
    COORDINATION_MULTILINGUAL_MODEL_NAME: str = (
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )

    # F5 detection-pipeline tunables (PRD 10.11). Static defaults, not DB-backed - F4's
    # admin UI (CoordinationSettings) moved to the backend along with all F5 config
    # ownership; a caller can still override any of these per-run via the `overrides`
    # field on POST /coordination/detection-runs (see DetectionParams in pipeline.py).
    COORDINATION_DEFAULT_WINDOW_HOURS: float = 168.0  # 7 days, PRD 10.5.1 default
    COORDINATION_A_MAX: int = 5000
    COORDINATION_THETA_EDGE: float = 0.35
    COORDINATION_K_CORE: int = 3
    COORDINATION_LEIDEN_RESOLUTION: float = 1.0
    COORDINATION_N_MIN: int = 5
    COORDINATION_RHO_MIN: float = 0.30
    COORDINATION_MU_ANCHOR: float = 0.60
    COORDINATION_P_MIN: int = 20
    COORDINATION_OMEGA_MIN: float = 0.15
    COORDINATION_BIN_WIDTH_SECONDS: int = 60
    COORDINATION_NULL_MODEL_ALPHA: float = 0.01
    COORDINATION_TAU_DUP: float = 0.80
    COORDINATION_TAU_SEM: float = 0.90
    COORDINATION_L_MIN: int = 25
    COORDINATION_PROVENANCE_HALF_LIFE_HOURS: float = 36.0
    COORDINATION_SELF_EXCLUSION_HANDLES: list[str] = Field(default_factory=list)

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
