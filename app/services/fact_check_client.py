"""Google Fact Check Tools API client: a secondary lookup for Falseness (F) scoring,
used when the OfficialSource match misses. Returns no results if GOOGLE_API_KEY is
unset or the request fails."""

import httpx

from app.core.config import settings

SEARCH_URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"
REQUEST_TIMEOUT_SECONDS = 10.0

# textualRating is free-form text set by each fact-checking org (e.g. "Salah",
# "False", "Hoax", "Palsu") - matched loosely since there's no fixed enum to key on.
FALSE_RATING_KEYWORDS = (
    "false", "hoax", "salah", "palsu", "keliru", "menyesatkan", "misleading", "fake",
)


async def search_fact_checks(query: str, language_code: str = "id") -> list[dict]:
    """Returns matching ClaimReview entries, or [] if disabled, no matches, or on
    any request error."""
    if not settings.GOOGLE_API_KEY:
        return []

    params = {
        "query": query,
        "languageCode": language_code,
        "key": settings.GOOGLE_API_KEY,
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.get(SEARCH_URL, params=params)
            resp.raise_for_status()
    except httpx.HTTPError:
        return []

    return resp.json().get("claims", [])


def has_false_rating(claims: list[dict]) -> bool:
    """True if any returned ClaimReview's textualRating reads as a false verdict."""
    for claim in claims:
        for review in claim.get("claimReview", []):
            rating = (review.get("textualRating") or "").lower()
            if any(keyword in rating for keyword in FALSE_RATING_KEYWORDS):
                return True
    return False
