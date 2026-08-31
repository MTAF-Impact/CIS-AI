"""Shared pytest fixtures. Integration tests need a real Postgres+pgvector at
TEST_DATABASE_URL; they never touch the real DATABASE_URL and never call a real LLM."""

import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.models  # noqa: F401 - registers all ORM models on Base.metadata
from app.core.database import Base, get_db, get_session_factory
from app.services.embedding_service import EmbeddingService, get_embedding_service
from app.services.llm_client import get_llm_client
from app.services.multilingual_embedding_service import (
    MultilingualEmbeddingService,
    get_multilingual_embedding_service,
)
from tests.fakes import FakeLLMClient

# app.main is imported lazily inside the `client` fixture so pure unit/DB-only tests
# can still collect without it.

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres"
)


@pytest.fixture(scope="session")
def real_embedder() -> EmbeddingService:
    """The real embedding model - used where genuine semantic similarity matters."""
    return EmbeddingService()


@pytest.fixture(scope="session")
def real_multilingual_embedder() -> MultilingualEmbeddingService:
    """The real F5 multilingual model - for the same reason as real_embedder above."""
    return MultilingualEmbeddingService()


@pytest_asyncio.fixture(scope="session")
async def test_engine() -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        # Stand-in for the backend-owned cis_coordination_allowlist table - the one
        # exception to "no shared-table access" (see pipeline.py's
        # _load_allowlisted_handles). Column names are an assumption pending backend
        # confirmation of the real DDL - see that function's docstring.
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS cis_coordination_allowlist "
                "(handle TEXT NOT NULL, removed_at TIMESTAMPTZ)"
            )
        )
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    session_maker = async_sessionmaker(bind=test_engine, expire_on_commit=False)
    async with session_maker() as session:
        yield session

    # Isolation via truncation (not rollback) since endpoints commit themselves.
    async with test_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
        await conn.execute(text("DELETE FROM cis_coordination_allowlist"))


@pytest_asyncio.fixture
async def client(
    test_engine: AsyncEngine,
    db_session: AsyncSession,
    real_embedder: EmbeddingService,
    real_multilingual_embedder: MultilingualEmbeddingService,
) -> AsyncGenerator[AsyncClient, None]:
    """httpx AsyncClient wired to the app, test DB, fake LLM, real embedders. Each
    simulated request gets its own fresh AsyncSession - asyncpg can't interleave two
    units of work on one session."""
    from app.main import app  # lazy - see module docstring/comment above

    request_session_maker = async_sessionmaker(bind=test_engine, expire_on_commit=False)

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with request_session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    # BackgroundTasks jobs can't use a request-scoped Depends(get_db).
    app.dependency_overrides[get_session_factory] = lambda: request_session_maker
    app.dependency_overrides[get_llm_client] = FakeLLMClient
    app.dependency_overrides[get_embedding_service] = lambda: real_embedder
    app.dependency_overrides[get_multilingual_embedding_service] = lambda: real_multilingual_embedder

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

    app.dependency_overrides.clear()
