"""Live hazard grounding for the Harm (H) classifier (LLMClient.classify_harm) -
Data Pipeline & Source Spec v1.0, D3 ("ground the judgment, don't just guess it").

Scoped to BMKG only for tonight's build - PetaBencana's public API endpoint
couldn't be confirmed in time and NASA FIRMS needs a separately-registered MAP_KEY;
both are noted as follow-ups in docs/SOURCES.md rather than guessed at here.

Free, no auth. Silently returns "" on any failure (unreachable, bad response, no
configured codes) - a live external dependency must degrade, never raise, into the
scoring path, same posture as fact_check_client.py.
"""

import httpx

from app.core.config import settings

BMKG_URL = "https://api.bmkg.go.id/publik/prakiraan-cuaca"
REQUEST_TIMEOUT_SECONDS = 8.0
FORECAST_ENTRIES_LOOKAHEAD = 8  # ~24h at BMKG's 3-hourly cadence
HAZARD_KEYWORDS = ("hujan", "petir", "badai", "angin kencang")


async def _fetch_one(adm4: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.get(BMKG_URL, params={"adm4": adm4})
            resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError):
        return None


async def fetch_bmkg_context() -> str:
    """One line per configured BMKG_ADM4_CODES location, summarizing the next
    ~24h forecast and flagging any hazard-adjacent conditions (rain/storm/high
    wind). "" if every location is unreachable or none are configured."""
    lines: list[str] = []
    for adm4 in settings.BMKG_ADM4_CODES:
        payload = await _fetch_one(adm4)
        if not payload or not payload.get("data"):
            continue

        lokasi = payload["lokasi"]
        place = f"{lokasi.get('kecamatan', '?')}, {lokasi.get('kotkab', '?')}"
        forecasts = [
            entry for day in payload["data"][0]["cuaca"] for entry in day
        ][:FORECAST_ENTRIES_LOOKAHEAD]
        hazardous = list(
            dict.fromkeys(
                f["weather_desc"]
                for f in forecasts
                if any(k in f["weather_desc"].lower() for k in HAZARD_KEYWORDS)
            )
        )
        if hazardous:
            lines.append(f"- [BMKG] {place}: {', '.join(hazardous)} forecast in the next ~24h.")
        else:
            lines.append(f"- [BMKG] {place}: no significant weather hazard forecast in the next ~24h.")

    return "\n".join(lines)
