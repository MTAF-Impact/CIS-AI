from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


# SQL echo is opt-in via LOG_LEVEL=DEBUG (not tied to is_production) - SQLAlchemy sets its
# own logger level/handler when echo=True, independent of app.core.logging_config, so this
# is the only reliable way to control it without duplicate/uncontrolled log lines.
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.LOG_LEVEL.upper() == "DEBUG",
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a request-scoped async DB session."""
    async with AsyncSessionLocal() as session:
        yield session


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """FastAPI-overridable indirection for code that can't use a request-scoped
    Depends(get_db) - namely BackgroundTasks jobs (see
    app.services.policy_matchmaking_service), which run detached from the request after
    its own session has closed. Tests override this the same way they override get_db
    (see tests/conftest.py's client fixture) so background jobs run against the test
    database instead of silently falling back to the real DATABASE_URL engine."""
    return AsyncSessionLocal
