"""Seed the CIS AI Service with realistic demo data for the hackathon walkthrough.

Populates:
  - 4 community Fault Lines (historical grievances used for RAG grounding / risk scoring)
  - 13 realistic urban-climate-policy posts across 4 emerging narratives
Then runs the same pipeline production traffic would trigger: embed -> classify (OpenAI)
-> persist -> cluster into Narratives -> score risk.

Usage:
    uv run python scripts/seed_demo_data.py
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.core.config import settings
from app.core.database import AsyncSessionLocal, Base, engine
from app.core.logging_config import configure_logging
from app.models.content import ContentItem
from app.models.enums import (
    ClassificationLabel,
    ContentSource,
    MoralFoundation,
)
from app.models.fault_line import FaultLine
from app.services.clustering_service import cluster_unclustered_content
from app.services.embedding_service import EmbeddingService, get_embedding_service
from app.services.llm_client import LLMClient, get_llm_client

configure_logging(level=settings.LOG_LEVEL, json_format=False)
logger = logging.getLogger("seed_demo_data")

NOW = datetime.now(UTC)


def _hours_ago(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


DEMO_FAULT_LINES = [
    {
        "community_name": "District X",
        "grievance_theme": "Historical displacement distrust",
        "description": (
            "Residents of District X were displaced twice in the last 20 years by "
            "infrastructure projects with little compensation or consultation. Any new "
            "city project touching housing, transit, or land use is viewed through this "
            "lens of distrust toward city hall."
        ),
    },
    {
        "community_name": "North Ward",
        "grievance_theme": "Flooding infrastructure neglect",
        "description": (
            "North Ward has flooded three times in five years while requests for storm "
            "drain upgrades went unfunded. Residents believe the city prioritizes "
            "wealthier wards for infrastructure spending."
        ),
    },
    {
        "community_name": "Riverside",
        "grievance_theme": "Cost-of-living and gentrification anxiety",
        "description": (
            "Riverside has seen rents rise sharply after recent green-space and transit "
            "investment, which residents associate with displacement of longtime "
            "working-class tenants rather than genuine environmental benefit."
        ),
    },
    {
        "community_name": "Eastgate",
        "grievance_theme": "Industrial pollution and health distrust",
        "description": (
            "Eastgate sits next to a former industrial corridor with a documented history "
            "of unreported chemical spills. Residents are highly sensitive to any claim "
            "involving toxic emissions, fires, or waste processing near their homes."
        ),
    },
]

# (text, source, author_id, location, hours_ago)
DEMO_POSTS: list[tuple[str, ContentSource, str, str, float]] = [
    # --- Narrative A: Bus lane / congestion charge backlash (Downtown, District X) ---
    (
        "New downtown bus lane is just a backdoor way to bring in a congestion charge "
        "next year, watch what happens to our commute costs.",
        ContentSource.SOCIAL,
        "user_marco",
        "Downtown",
        3.0,
    ),
    (
        "Heard the city is planning a $50/day congestion charge once the bus lane is "
        "finished. Nobody voted for this, they're just sneaking it in.",
        ContentSource.SOCIAL,
        "user_priya",
        "Downtown",
        2.5,
    ),
    (
        "So the bus lane project is basically a hidden tax on working families who still "
        "need to drive to their jobs across town.",
        ContentSource.FORUM,
        "user_deshawn",
        "District X",
        1.0,
    ),
    (
        "Reminder: after the bus lane opened on 5th Ave last year, the city added paid "
        "parking meters within six months. History repeating itself.",
        ContentSource.SOCIAL,
        "user_ana_t",
        "District X",
        0.5,
    ),
    (
        "I get why people are nervous about the bus lane given what happened with the "
        "5th Ave meters, but has the city actually confirmed a congestion charge is "
        "planned, or is this speculation?",
        ContentSource.FORUM,
        "user_kwame",
        "Downtown",
        0.25,
    ),
    # --- Narrative B: Tree removal backlash (Riverside) ---
    (
        "City quietly approved removing 500 mature trees along the riverside corridor "
        "for a new parking structure. This is an environmental betrayal.",
        ContentSource.SOCIAL,
        "user_lena_g",
        "Riverside",
        6.0,
    ),
    (
        "500 trees gone for a parking lot?! And they call this a 'climate resilience' "
        "plan? Absolute hypocrisy from city council.",
        ContentSource.SOCIAL,
        "user_oliver_p",
        "Riverside",
        5.5,
    ),
    (
        "The riverside tree removal is the same playbook as the last redevelopment: "
        "green branding while actually displacing longtime residents and green space.",
        ContentSource.FORUM,
        "user_sofia_m",
        "Riverside",
        5.0,
    ),
    # --- Narrative C: Recycling plant fire claims (Eastgate) ---
    (
        "Sources say the recycling plant fire last week released toxic smoke and the "
        "city is covering it up to avoid a lawsuit. Check your air quality apps.",
        ContentSource.SOCIAL,
        "user_ray_h",
        "Eastgate",
        10.0,
    ),
    (
        "They're saying the Eastgate recycling fire was 'minor' but three of my "
        "neighbors had breathing problems that night. Nobody believes the official line "
        "here anymore.",
        ContentSource.SOCIAL,
        "user_tanya_b",
        "Eastgate",
        9.0,
    ),
    (
        "Can anyone confirm if the fire department actually tested air quality after "
        "the Eastgate recycling fire, or are we just going off rumors at this point?",
        ContentSource.FORUM,
        "user_devon_l",
        "Eastgate",
        8.5,
    ),
    # --- Narrative D: North Ward flood infrastructure debate (legitimate debate + satire) ---
    (
        "Genuinely torn on the new storm drain budget for North Ward - it's expensive, "
        "but we've flooded three times in five years. What's the actual cost of doing "
        "nothing?",
        ContentSource.FORUM,
        "user_grace_w",
        "North Ward",
        20.0,
    ),
    (
        "BREAKING: City council to replace all storm drains with giant curly straws, "
        "sources (my cat) confirm. /s obviously, but seriously when is North Ward "
        "getting real flood relief?",
        ContentSource.SOCIAL,
        "user_felix_r",
        "North Ward",
        19.0,
    ),
]


async def ensure_schema() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)


async def clear_demo_data(session) -> None:
    await session.execute(text("DELETE FROM intervention_responses"))
    await session.execute(text("DELETE FROM content_items"))
    await session.execute(text("DELETE FROM narratives"))
    await session.execute(text("DELETE FROM fault_lines"))
    await session.commit()


async def seed_fault_lines(session, embedder: EmbeddingService) -> list[FaultLine]:
    created = []
    for fl in DEMO_FAULT_LINES:
        embedding = embedder.embed(f"{fl['grievance_theme']}: {fl['description']}")
        obj = FaultLine(
            community_name=fl["community_name"],
            grievance_theme=fl["grievance_theme"],
            description=fl["description"],
            embedding=embedding,
        )
        session.add(obj)
        created.append(obj)
    await session.commit()
    return created


async def _analyze_with_fallback(llm: LLMClient, text_content: str):
    try:
        return await llm.analyze_content(text_content)
    except Exception:  # noqa: BLE001 - keep seeding usable without a live OpenAI key
        logger.warning(
            "OpenAI analysis failed (missing/invalid OPENAI_API_KEY?) - using fallback "
            "classification for: %.60s...",
            text_content,
        )

        class _Fallback:
            classification = ClassificationLabel.UNKNOWN
            confidence = 0.0
            outrage_score = 0.5
            moral_foundation = MoralFoundation.NEUTRAL
            extracted_claim = text_content[:200]
            underlying_grievance = ""

        return _Fallback()


async def seed_content_items(session, embedder: EmbeddingService, llm: LLMClient):
    created = []
    for post_text, source, author_id, location, hours_ago in DEMO_POSTS:
        embedding = embedder.embed(post_text)
        analysis = await _analyze_with_fallback(llm, post_text)
        item = ContentItem(
            text=post_text,
            source=source,
            author_id=author_id,
            location=location,
            classification=analysis.classification,
            confidence=analysis.confidence,
            outrage_score=analysis.outrage_score,
            moral_foundation=analysis.moral_foundation,
            extracted_claim=analysis.extracted_claim,
            underlying_grievance=analysis.underlying_grievance,
            embedding=embedding,
            created_at=_hours_ago(hours_ago),
        )
        session.add(item)
        created.append(item)
        logger.info("Prepared content item [%s]: %.60s...", location, post_text)

    await session.commit()
    return created


async def main() -> None:
    logger.info("Ensuring schema (pgvector extension + tables)...")
    await ensure_schema()

    embedder = get_embedding_service()
    llm = get_llm_client()

    async with AsyncSessionLocal() as session:
        logger.info("Clearing previous demo data...")
        await clear_demo_data(session)

        logger.info("Seeding %d fault lines...", len(DEMO_FAULT_LINES))
        fault_lines = await seed_fault_lines(session, embedder)
        logger.info("Created fault lines: %s", [fl.community_name for fl in fault_lines])

        logger.info("Seeding %d content items (embedding + OpenAI analysis)...", len(DEMO_POSTS))
        content_items = await seed_content_items(session, embedder, llm)
        logger.info("Created %d content items", len(content_items))

        logger.info("Triggering narrative clustering...")
        result = await cluster_unclustered_content(session, llm=llm)
        logger.info(
            "Clustering complete: %d narratives created, %d updated, %d items clustered",
            result.narratives_created,
            result.narratives_updated,
            result.content_items_clustered,
        )

    logger.info("Seed complete. Try: GET /api/v1/narratives")


if __name__ == "__main__":
    asyncio.run(main())
