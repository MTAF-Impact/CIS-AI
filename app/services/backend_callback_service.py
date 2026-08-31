"""Flow 2 of the Go backend integration contract (docs/AI-INTEGRATION.md in the
CIS-Backend repo): reports the outcome of the US42 matchmaking pipeline back to the
backend. This is the only outbound call this service makes to the backend - everything
else is either the backend calling us (Flow 1, Flow 3) or plain reads of our tables."""

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
) -> None:
    """Best-effort: a failed callback is logged, never raised - the matchmaking work
    itself already happened and shouldn't be treated as failed just because the report
    didn't land. The backend's own retry job (up to 3x daily) and the operator-triggered
    POST /policies/:id/rematch exist precisely to cover this case."""
    if not settings.BACKEND_URL:
        logger.warning(
            "BACKEND_URL not configured - skipping matchmaking-result callback for "
            "backend policy %s (status=%s)",
            backend_policy_id,
            status,
        )
        return

    url = (
        f"{settings.BACKEND_URL.rstrip('/')}/api/v1/internal/policies/"
        f"{backend_policy_id}/matchmaking-result"
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
