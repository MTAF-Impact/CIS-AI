"""Flow 2: reports the matchmaking pipeline's outcome back to the Go backend."""

import logging
import uuid

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

CALLBACK_TIMEOUT_SECONDS = 15.0
MAX_ERROR_LENGTH = 2000  # backend's documented limit on the `error` field


async def report_matchmaking_result(
    backend_policy_id: uuid.UUID,
    status: str,
    ai_policy_id: uuid.UUID | None = None,
    matched_claim_count: int | None = None,
    generated_claim_count: int | None = None,
    error: str | None = None,
    callback_url: str | None = None,
) -> None:
    """Best-effort: a failed callback is logged, never raised - the backend's own retry
    job covers this case. callback_url (from the Flow 1 request body) is preferred over
    BACKEND_URL when present - it's what lets one AI deployment serve staging and
    production without either side hardcoding the other's host."""
    base = callback_url or settings.BACKEND_URL
    if not base:
        logger.warning(
            "No callback_url and BACKEND_URL not configured - skipping matchmaking-result "
            "callback for backend policy %s (status=%s)",
            backend_policy_id,
            status,
        )
        return

    # callback_url, when supplied, is already the full target URL; BACKEND_URL is just
    # the host and needs the path appended.
    url = base if callback_url else (
        f"{base.rstrip('/')}/api/v1/internal/policies/{backend_policy_id}/matchmaking-result"
    )
    body: dict = {"status": status}
    if ai_policy_id is not None:
        body["ai_policy_id"] = str(ai_policy_id)
    if matched_claim_count is not None:
        body["matched_claim_count"] = matched_claim_count
    if generated_claim_count is not None:
        body["generated_claim_count"] = generated_claim_count
    if error is not None:
        body["error"] = error[:MAX_ERROR_LENGTH]

    headers = {}
    if settings.INTERNAL_API_KEY:
        headers["X-Internal-Key"] = settings.INTERNAL_API_KEY

    try:
        async with httpx.AsyncClient(timeout=CALLBACK_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=body, headers=headers)
            response.raise_for_status()
    except Exception:
        logger.exception(
            "Failed to report matchmaking result to backend for policy %s", backend_policy_id
        )
