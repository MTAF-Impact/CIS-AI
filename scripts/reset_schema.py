"""Drop and recreate every table in the CIS AI Service schema.

DESTRUCTIVE - drops every table Base.metadata knows about and recreates them empty.
Intended for pre-launch/demo use against a disposable database (this project has no
migrations tool; schema is Base.metadata.create_all-driven, same as
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
        logger.info("Dropping all known tables...")
        await conn.run_sync(Base.metadata.drop_all)
        logger.info("Creating all tables...")
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Schema reset complete.")


if __name__ == "__main__":
    asyncio.run(reset_schema())
