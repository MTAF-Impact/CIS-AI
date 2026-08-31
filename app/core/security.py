"""Optional auth for the Go backend's inbound webhooks. No-op if AI_SERVICE_API_KEY is unset."""

import hmac

from fastapi import Header, HTTPException

from app.core.config import settings


def _matches(candidate: str) -> bool:
    return hmac.compare_digest(candidate, settings.AI_SERVICE_API_KEY)


async def verify_backend_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> None:
    if not settings.AI_SERVICE_API_KEY:
        return

    bearer = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None
    if (x_api_key and _matches(x_api_key)) or (bearer and _matches(bearer)):
        return

    raise HTTPException(status_code=401, detail="Missing or invalid API key")
