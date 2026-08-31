"""Shared guard for reset_schema.py/seed_demo_data.py - both scripts write
destructively to tables the Go backend holds dangling (no-FK) references into
(cis_claim_reviews.claim_id, cis_claim_alerts.claim_id, cis_policies.ai_policy_id,
etc). Running either against a backend-connected database silently orphans those
references - nothing errors, the backend UI just quietly shows wrong things. See
AI_REQUIREMENT_FOR_INTEGRATION_SUMMARY_V1.md, ask F1."""

import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_OVERRIDE_FLAG = "--i-know-the-backend-is-connected"


async def refuse_if_backend_connected(conn: AsyncConnection) -> None:
    if _OVERRIDE_FLAG in sys.argv:
        return
    result = await conn.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'cis\\_%' LIMIT 1")
    )
    if result.first() is not None:
        sys.exit(
            "Refusing to run: this database has cis_* tables, meaning the Go backend "
            "is connected to it. This script writes destructively to tables the "
            "backend holds dangling (no-FK) references into - claim_alerts, "
            "policies, claims - and running it will silently orphan those "
            "references (cis_claim_reviews.claim_id, cis_policies.ai_policy_id, "
            "etc). Nothing errors on either side; the backend UI just quietly shows "
            "wrong things afterward.\n\n"
            f"If you are certain this is safe (e.g. a scratch/local DB the backend "
            f"doesn't actually read), re-run with {_OVERRIDE_FLAG}."
        )
