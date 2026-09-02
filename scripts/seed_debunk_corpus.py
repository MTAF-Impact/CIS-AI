"""Seed the Falseness (F) reference corpus (OfficialSource) from TurnBackHoax.id's
public RSS feed - Mafindo's own IFCN-certified hoax debunking site. Substitutes for
the `nlp-brin-id/fakenews-mafindo` HuggingFace dataset, which turned out to be
gated (manual owner approval, not obtainable same-day) - see the Data Pipeline &
Source Spec v1.0 audit this replaces.

The feed only exposes the ~10 most recent items - `?paged=N` returns the identical
page (verified: no working pagination on this route) - so this is a small,
freshness-biased corpus, not a historical archive. Safe and idempotent to re-run
periodically (skips items whose source_url is already stored) to accumulate more
over time; consider a daily cron once past tonight's deadline.

Only items labelled as an actual false/fraudulent claim are kept (title prefix
[SALAH]/[HOAX]/[PENIPUAN]/[DISINFORMASI]/[MANIPULASI]). Anything else (e.g. a
[KLARIFIKASI] or [FAKTA] correction-of-a-correction) is skipped rather than
guessed at: compute_falseness_score() (app/services/falseness_service.py) treats
every row here as "similarity to this = high Falseness", so a wrongly-included
true statement would poison the corpus.

Embeds the ENGLISH TRANSLATION of the claim text, not the raw Indonesian title -
the embedding model (app/services/embedding_service.py) is English-only, same
reason every ContentItem stores text_en alongside the original. Found by testing:
a claim closely paraphrasing a real seeded hoax topic (Pramono Anung / non-DKI
workers) scored 0.51 similarity against the untranslated Indonesian title - just
under DEFAULT_MATCH_THRESHOLD (0.55), a real match silently missed. Falls back to
embedding the raw Indonesian text (degraded, not broken) if the LLM call fails or
no key is configured - logged, never silent.

Usage:
    uv run python scripts/seed_debunk_corpus.py
"""

import asyncio
import logging
import re
from xml.etree import ElementTree

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.logging_config import configure_logging
from app.models.official_source import OfficialSource
from app.services.embedding_service import get_embedding_service
from app.services.llm_client import get_llm_client

configure_logging(level=settings.LOG_LEVEL, json_format=False)
logger = logging.getLogger("seed_debunk_corpus")

FEED_URL = "https://turnbackhoax.id/feed/"
HOAX_LABEL_RE = re.compile(r"^\[(SALAH|HOAX|PENIPUAN|DISINFORMASI|MANIPULASI)\]\s*", re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
CONTENT_CHAR_LIMIT = 4000


def _strip_html(html: str) -> str:
    text = HTML_TAG_RE.sub(" ", html)
    text = (
        text.replace("&amp;", "&")
        .replace("&nbsp;", " ")
        .replace("&quot;", '"')
        .replace("&#039;", "'")
    )
    return WHITESPACE_RE.sub(" ", text).strip()


async def _fetch_hoax_items() -> list[dict[str, str]]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CIS-AI debunk-corpus-seeder/1.0)"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=15.0) as client:
        resp = await client.get(FEED_URL)
        resp.raise_for_status()

    root = ElementTree.fromstring(resp.text)
    items = []
    for item in root.findall(".//item"):
        raw_title = (item.findtext("title") or "").strip()
        match = HOAX_LABEL_RE.match(raw_title)
        if not match:
            continue  # not a labelled false/fraudulent claim - skip rather than guess
        claim_text = HOAX_LABEL_RE.sub("", raw_title).strip()
        link = (item.findtext("link") or "").strip()
        description = _strip_html(item.findtext("description") or "")
        items.append({"claim_text": claim_text, "link": link, "description": description})
    return items


async def main() -> None:
    items = await _fetch_hoax_items()
    logger.info("Fetched %d labelled hoax items from %s", len(items), FEED_URL)
    if not items:
        logger.warning("Nothing to seed - feed returned no labelled hoax items.")
        return

    embedder = get_embedding_service()
    llm = get_llm_client()
    async with AsyncSessionLocal() as session:
        existing_urls = set(
            (await session.execute(select(OfficialSource.source_url))).scalars().all()
        )
        added = 0
        for entry in items:
            if entry["link"] in existing_urls:
                continue
            claim_text = entry["claim_text"]
            try:
                analysis = await llm.analyze_content(claim_text)
                embed_text = analysis.text_en or claim_text
            except Exception:  # noqa: BLE001 - keep seeding usable without a live OpenAI key
                logger.warning(
                    "Translation failed for %r - embedding raw Indonesian text instead",
                    claim_text[:60],
                )
                embed_text = claim_text
            content = entry["description"][:CONTENT_CHAR_LIMIT] or claim_text
            session.add(
                OfficialSource(
                    title=claim_text[:255],
                    content=content,
                    source_url=entry["link"] or None,
                    embedding=embedder.embed(embed_text),
                )
            )
            added += 1
        await session.commit()

    logger.info(
        "Seeded %d new known-hoax reference rows (skipped %d already present).",
        added,
        len(items) - added,
    )


if __name__ == "__main__":
    asyncio.run(main())
