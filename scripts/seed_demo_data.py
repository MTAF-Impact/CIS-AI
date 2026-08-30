"""Seed the CIS AI Service with realistic Jakarta demo data for the hackathon walkthrough.

Populates:
  - 4 community Fault Lines (real Jakarta historical grievances used for RAG grounding /
    risk scoring)
  - 13 realistic urban-climate-policy posts across 4 emerging narratives, grounded in
    actual Jakarta policies and places (ERP road pricing, MRT Fase 2 tree removal, ITF
    Sunter waste-to-energy plant, Ciliwung flood-control budget)
Then runs the same pipeline production traffic would trigger: embed -> classify (OpenAI)
-> persist -> cluster into Narratives -> score risk.

Note: post text is kept in English on purpose even though this is Jakarta data - the
embedding model (sentence-transformers/all-MiniLM-L6-v2) is English-only, so English text
with real Jakarta place/policy names gives the most reliable clustering. Swap to a
multilingual embedding model (e.g. paraphrase-multilingual-MiniLM-L12-v2, also 384-dim) if
Bahasa Indonesia post text is needed later.

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
        "community_name": "Kampung Pulo",
        "grievance_theme": "Historical eviction distrust (Ciliwung normalization)",
        "description": (
            "Kampung Pulo and Bukit Duri residents were forcibly evicted in 2015-2016 for "
            "the Ciliwung river normalization project, many without adequate compensation "
            "or resettlement. Any new city project touching the riverbanks, housing, or "
            "flood infrastructure is viewed through this lens of deep distrust toward city "
            "hall."
        ),
    },
    {
        "community_name": "Penjaringan",
        "grievance_theme": "Land subsidence and tidal flooding (rob) neglect",
        "description": (
            "North Jakarta, including Penjaringan and Muara Baru, is sinking up to 25cm a "
            "year in places, causing chronic tidal flooding (banjir rob). Residents feel "
            "the promised giant sea wall (NCICD) has moved too slowly while wealthier "
            "areas get faster infrastructure investment."
        ),
    },
    {
        "community_name": "Muara Angke",
        "grievance_theme": "Land reclamation (reklamasi) distrust",
        "description": (
            "Fishing communities in Muara Angke fought the Jakarta Bay reclamation project "
            "for years, fearing loss of livelihood and worsened flooding. Even after parts "
            "of the project were halted, residents remain deeply skeptical of any coastal "
            "or waterfront development framed as environmental improvement."
        ),
    },
    {
        "community_name": "Sunter",
        "grievance_theme": "Industrial waste and air pollution distrust",
        "description": (
            "Residents near the ITF Sunter waste-to-energy incinerator project are highly "
            "sensitive to any claim involving toxic emissions, fires, or waste processing, "
            "given the plant's history of delays, cost overruns, and disputed emissions "
            "data."
        ),
    },
]

# (text, source, author_id, location, hours_ago)
DEMO_POSTS: list[tuple[str, ContentSource, str, str, float]] = [
    # --- Narrative A: ERP (Electronic Road Pricing) backlash (Sudirman-Thamrin corridor) ---
    (
        "The new ERP gantries on Sudirman are just a backdoor way to bring in a full "
        "congestion charge next year, watch what happens to our commute costs.",
        ContentSource.SOCIAL,
        "user_budi_s",
        "Sudirman",
        3.0,
    ),
    (
        "Heard the city is planning a Rp50,000/day ERP charge once the pilot on Thamrin "
        "ends. Nobody voted for this, they're just sneaking it in.",
        ContentSource.SOCIAL,
        "user_prita_w",
        "Thamrin",
        2.5,
    ),
    (
        "So the ERP rollout is basically a hidden tax on commuters who still need to drive "
        "into the CBD for work every day.",
        ContentSource.FORUM,
        "user_dedi_k",
        "Blok M",
        1.0,
    ),
    (
        "Reminder: after the busway lane expanded on Sudirman last year, the city added "
        "paid parking meters within six months. History repeating itself with this ERP "
        "plan.",
        ContentSource.SOCIAL,
        "user_ani_t",
        "Sudirman",
        0.5,
    ),
    (
        "I get why people are nervous about ERP given what happened with the Sudirman "
        "parking meters, but has the city actually confirmed a full congestion charge is "
        "planned, or is this speculation?",
        ContentSource.FORUM,
        "user_kevin_h",
        "Thamrin",
        0.25,
    ),
    # --- Narrative B: MRT Fase 2 tree removal backlash (Kota Tua / Monas corridor) ---
    (
        "City quietly approved removing dozens of mature trees along the MRT Fase 2 route "
        "near Monas for construction staging. This is an environmental betrayal.",
        ContentSource.SOCIAL,
        "user_lina_g",
        "Monas",
        6.0,
    ),
    (
        "Trees gone for MRT construction staging areas?! And they call this a green "
        "transit project? Absolute hypocrisy from the city.",
        ContentSource.SOCIAL,
        "user_oscar_p",
        "Monas",
        5.5,
    ),
    (
        "The Kota Tua tree removal is the same playbook as the last redevelopment: green "
        "branding while actually clearing heritage streetscape for construction "
        "convenience.",
        ContentSource.FORUM,
        "user_sari_m",
        "Kota Tua",
        5.0,
    ),
    # --- Narrative C: ITF Sunter waste plant fire claims (Sunter / Cakung) ---
    (
        "Sources say the ITF Sunter waste plant test-burn last week released toxic smoke "
        "and the city is covering it up to avoid a lawsuit. Check your air quality apps.",
        ContentSource.SOCIAL,
        "user_reza_h",
        "Sunter",
        10.0,
    ),
    (
        "They're saying the Sunter incinerator test was 'minor' but three of my neighbors "
        "had breathing problems that night. Nobody believes the official line here "
        "anymore.",
        ContentSource.SOCIAL,
        "user_tania_b",
        "Sunter",
        9.0,
    ),
    (
        "Can anyone confirm if the environment agency actually tested air quality after "
        "the ITF Sunter test-burn, or are we just going off rumors at this point?",
        ContentSource.FORUM,
        "user_devan_l",
        "Cakung",
        8.5,
    ),
    # --- Narrative D: Ciliwung flood budget debate (Kampung Melayu, legitimate + satire) ---
    (
        "Genuinely torn on the new Ciliwung normalization budget for Kampung Melayu - it's "
        "expensive, but we've flooded three times in five years. What's the actual cost of "
        "doing nothing?",
        ContentSource.FORUM,
        "user_grace_w",
        "Kampung Melayu",
        20.0,
    ),
    (
        "BREAKING: City council to replace all Ciliwung floodgates with giant curly "
        "straws, sources (my cat) confirm. /s obviously, but seriously when is Kampung "
        "Melayu getting real flood relief?",
        ContentSource.SOCIAL,
        "user_felix_r",
        "Kampung Melayu",
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
