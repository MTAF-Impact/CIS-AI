"""DESTRUCTIVE - drops and recreates every table this service owns. Run manually:

    uv run python scripts/reset_schema.py

IMPORTANT: this database is shared with the Go backend (`cis_*` tables) - those are
explicitly excluded below and must never be touched by this script.
"""

import asyncio
import logging

from sqlalchemy import text

import app.models  # noqa: F401 - registers all ORM models on Base.metadata
from app.core.config import settings
from app.core.database import Base, engine
from app.core.logging_config import configure_logging

configure_logging(level=settings.LOG_LEVEL, json_format=False)
logger = logging.getLogger("reset_schema")

# Owned/migrated by the Go backend - must never be touched by this script.
FOREIGN_TABLE_PREFIXES = ("cis_",)


async def reset_schema() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

        # Query pg_tables directly rather than Base.metadata.drop_all() alone, which
        # leaves orphaned tables behind whenever a model file is deleted.
        all_tables = [
            row[0]
            for row in (
                await conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            )
        ]
        our_tables = [
            t for t in all_tables if not t.startswith(FOREIGN_TABLE_PREFIXES)
        ]
        skipped = [t for t in all_tables if t not in our_tables]
        if skipped:
            logger.info("Skipping %d foreign-service table(s): %s", len(skipped), skipped)
        if our_tables:
            logger.info("Dropping %d existing table(s): %s", len(our_tables), our_tables)
            for table_name in our_tables:
                await conn.execute(text(f'DROP TABLE IF EXISTS "{table_name}" CASCADE'))

        logger.info("Creating current tables...")
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Schema reset complete.")


if __name__ == "__main__":
    asyncio.run(reset_schema())
