"""Shared pytest fixtures.

Unit tests (tests/unit/) exercise pure logic and need no fixtures from here beyond the
`real_embedder` session fixture (used only where genuine semantic similarity matters, e.g.
CIB detection).

Integration tests (tests/integration/) are marked `@pytest.mark.integration` and require a
real Postgres+pgvector instance reachable via TEST_DATABASE_URL (defaults to a local
`postgres/postgres@localhost:5432/postgres`, matching the `pgvector/pgvector:pg16` Docker
image used in CI - see .github/workflows/ci.yml). They NEVER touch the app's real
DATABASE_URL/Supabase instance, and NEVER call a real LLM - llm_client is always overridden
with tests.fakes.FakeLLMClient.
"""

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

import app.models
from app.core.database import Base, get_db
from app.main import app
from app.services.embedding_service import EmbeddingService, get_embedding_service
from app.services.llm_client import get_llm_client
from tests.fakes import FakeLLMClient

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres"
)


@pytest.fixture(scope="session")
def real_embedder() -> EmbeddingService:
    """The actual sentence-transformers model - used only by tests where semantic
    similarity genuinely matters (CIB detection, clustering, RAG matching)."""
    return EmbeddingService()


@pytest_asyncio.fixture(scope="session")
async def test_engine() -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    session_maker = async_sessionmaker(bind=test_engine, expire_on_commit=False)
    async with session_maker() as session:
        yield session

    # Endpoints call db.commit() themselves, so isolation between tests is via truncation
    # (not rollback) - this must run even if the test body raised.
    async with test_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


@pytest_asyncio.fixture
async def client(
    test_engine: AsyncEngine, db_session: AsyncSession, real_embedder: EmbeddingService
) -> AsyncGenerator[AsyncClient, None]:
    """An httpx AsyncClient wired to the FastAPI app with the DB pointed at the test
    database, the LLM client swapped for a deterministic fake (no network/API key), and
    the real embedding model (session-cached, so this costs nothing after the first test)
    so clustering/RAG-matching behavior is exercised with genuine semantics.

    Each simulated HTTP request gets its OWN fresh AsyncSession (mirroring how get_db
    works in production) rather than reusing the `db_session` fixture's session - asyncpg
    connections cannot interleave operations from two logical units of work, and reusing
    one session across multiple requests in a test triggers
    "cannot perform operation: another operation is in progress". `db_session` stays a
    fixture dependency here purely so its post-test truncation cleanup still runs; tests
    use it directly only for setup/assertions against the same underlying database.
    """
    request_session_maker = async_sessionmaker(bind=test_engine, expire_on_commit=False)

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with request_session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_llm_client] = FakeLLMClient
    app.dependency_overrides[get_embedding_service] = lambda: real_embedder

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

    app.dependency_overrides.clear()
