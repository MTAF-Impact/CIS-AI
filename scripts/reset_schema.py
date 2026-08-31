"""Drop and recreate every CIS AI Service table in the shared Supabase Postgres database.

DESTRUCTIVE - drops every table that physically exists in the `public` schema and belongs
to THIS service (not just ones the current SQLAlchemy models know about - see note below)
and recreates the current set empty. Intended for pre-launch/demo use against a disposable
database (this project has no migrations tool; schema is Base.metadata.create_all-driven,
same as scripts/seed_demo_data.py's ensure_schema()). Run manually:

    uv run python scripts/reset_schema.py

IMPORTANT: this database is SHARED with the Go backend, which owns its own tables (the
`cis_` prefix - cis_users, cis_policies, cis_claim_alerts, etc.). Those are explicitly
excluded below - this script must never touch another service's tables.
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

# Tables outside this prefix are owned/migrated by another service (currently just the Go
# backend's `cis_` tables) and must never be touched by this script.
FOREIGN_TABLE_PREFIXES = ("cis_",)


async def reset_schema() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

        # Drop every table that physically exists AND belongs to us, not just ones
        # Base.metadata currently has a model for. Base.metadata.drop_all() alone
        # silently leaves orphaned tables behind whenever a model file is deleted (e.g.
        # Narrative/InterventionResponse being superseded by Claim in the PRD v1.1
        # rearchitecture) - it has no way to know those tables ever existed, so this
        # queries pg_tables directly instead. Foreign-service tables are filtered out
        # before anything is dropped - see FOREIGN_TABLE_PREFIXES above.
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
