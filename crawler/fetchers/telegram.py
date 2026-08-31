"""Telegram public-channel fetcher via Telethon. Needs TELEGRAM_SESSION_STRING from a
one-time interactive login (see docs/CRAWLER.md) - returns nothing until that's set,
rather than failing the whole crawl run."""

import logging
from datetime import UTC, datetime, timedelta

from telethon import TelegramClient
from telethon.sessions import StringSession

from crawler.candidate import Candidate

logger = logging.getLogger(__name__)

MESSAGES_PER_CHANNEL_LIMIT = 200


async def fetch_telegram_candidates(
    api_id: int | None,
    api_hash: str,
    session_string: str,
    channels: list[str],
    window_hours: float,
) -> list[Candidate]:
    if not (api_id and api_hash and session_string and channels):
        return []

    candidates: list[Candidate] = []
    window_start = datetime.now(UTC) - timedelta(hours=window_hours)

    async with TelegramClient(StringSession(session_string), api_id, api_hash) as client:
        for channel in channels:
            try:
                async for message in client.iter_messages(
                    channel, limit=MESSAGES_PER_CHANNEL_LIMIT
                ):
                    if message.date < window_start:
                        break
                    if not message.text:
                        continue
                    candidates.append(
                        Candidate(
                            text=message.text,
                            source="social",
                            author_id=channel,
                            location=None,
                            external_ref=f"telegram:{channel}:{message.id}",
                            created_at=message.date,
                            popularity_score=(message.views or 0) + (message.forwards or 0) * 5,
                        )
                    )
            except Exception:
                logger.exception("Failed to fetch Telegram channel %s", channel)
    return candidates
