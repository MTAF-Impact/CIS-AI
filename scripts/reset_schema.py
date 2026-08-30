"""Drop and recreate every table in the CIS AI Service schema.

DESTRUCTIVE - drops every table that physically exists in the `public` schema (not just
ones the current SQLAlchemy models know about - see note below) and recreates the current
set empty. Intended for pre-launch/demo use against a disposable database (this project has
no migrations tool; schema is Base.metadata.create_all-driven, same as
scripts/seed_demo_data.py's ensure_schema()). Run manually:

    uv run python scripts/reset_schema.py
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


async def reset_schema() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

        # Drop every table that physically exists, not just ones Base.metadata currently
        # has a model for. Base.metadata.drop_all() alone silently leaves orphaned tables
        # behind whenever a model file is deleted (e.g. Narrative/InterventionResponse
        # being superseded by Claim in the PRD v1.1 rearchitecture) - it has no way to
        # know those tables ever existed, so this queries pg_tables directly instead.
        existing_tables = [
            row[0]
            for row in (
                await conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            )
        ]
        if existing_tables:
            logger.info("Dropping %d existing table(s): %s", len(existing_tables), existing_tables)
            for table_name in existing_tables:
                await conn.execute(text(f'DROP TABLE IF EXISTS "{table_name}" CASCADE'))

        logger.info("Creating current tables...")
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Schema reset complete.")


if __name__ == "__main__":
    asyncio.run(reset_schema())
