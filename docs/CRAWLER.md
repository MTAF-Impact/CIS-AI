# Crawler

A separate, stateless Cloud Run **Job** (not a Service) that feeds real content into the
AI service, replacing/supplementing `/ingest/generate-synthetic`. Lives in `crawler/`,
its own dependency group and `Dockerfile.crawler` - see `ARCHITECTURE.md` for how this
fits the rest of the system and why it talks to the AI service only over HTTP (no direct
Postgres access, same rule as the Go backend).

```
crawler/
├── config.py            # CrawlerSettings - all env vars
├── candidate.py          # the common shape every fetcher normalizes into
├── client.py              # HTTP client to the AI service (GET /fault-lines, POST /ingest/batch)
├── relevance_filter.py     # local multilingual embedding-similarity + location keyword gate
├── fetchers/
│   ├── rss.py               # feedparser-based
│   └── telegram.py           # Telethon-based, public channels only
└── main.py                # orchestrates: fetch -> filter -> rank top-N -> submit
```

## Local run

```bash
uv sync --group crawler
uv run python -m crawler.main --dry-run   # logs candidates, submits nothing
uv run python -m crawler.main             # actually submits to AI_SERVICE_URL
```

`--dry-run` is the way to safely tune `RELEVANCE_THRESHOLD` and eyeball candidate
quality before ever writing anything - see `ARCHITECTURE.md`'s verification section.

## Environment variables

| Var | Default | Notes |
|---|---|---|
| `AI_SERVICE_URL` | `http://localhost:8000` | |
| `AI_SERVICE_API_KEY` | `""` | Sent as `X-API-Key` if set - must match the AI service's own `AI_SERVICE_API_KEY`. |
| `RSS_FEED_URLS` | `[]` | JSON list of feed URLs. Empty by default - needs curation, see Phase 2 below. |
| `TELEGRAM_CHANNELS` | `[]` | JSON list of public channel usernames. |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | unset | From my.telegram.org - see Phase 2. |
| `TELEGRAM_SESSION_STRING` | `""` | From a one-time interactive login - see Phase 2. Telegram fetch is skipped entirely (not an error) until this is set. |
| `TOP_N_PER_SOURCE` | `20` | Popularity-ranked cap per source, per run. |
| `CRAWL_WINDOW_HOURS` | `6.0` | How far back Telegram messages are considered. |
| `RELEVANCE_THRESHOLD` | `0.35` | Cosine similarity cutoff against `fault_lines` exemplars. Tune via `--dry-run`. |
| `RELEVANCE_MODEL_NAME` | `paraphrase-multilingual-MiniLM-L12-v2` | Local-only, used purely for filtering - separate from the AI service's own English-only embedding model. |
| `LOCATION_KEYWORDS` | Jakarta place names (see `config.py`) | Cheap pre-filter before the relevance model even runs. |

## Phase 2 — manual setup (not automatable)

1. **Telegram credentials**: register an app at [my.telegram.org](https://my.telegram.org)
   to get `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`, then generate a `TELEGRAM_SESSION_STRING`
   via a one-time interactive login (phone number + OTP) using Telethon's
   `StringSession` - this needs a human with a real phone number, can't be scripted end
   to end. Store the resulting string as a Secret Manager secret, never commit it.
2. **Telegram channel list**: which public Jakarta community/local channels to follow -
   needs local knowledge, not something to guess.
3. **RSS feed list**: which Indonesian/Jakarta news outlets' feeds to pull from.

Both lists go into `RSS_FEED_URLS`/`TELEGRAM_CHANNELS` (JSON-encoded env vars).

## Deployment (bundled with the AI service redeploy, not incremental)

1. Set `AI_SERVICE_API_KEY` on the AI service's own Cloud Run env - this is what
   actually turns on the auth check already wired on `POST /ingest`/`/ingest/batch`.
2. Build + push `Dockerfile.crawler` to Artifact Registry.
3. Create the `cis-crawler` Cloud Run **Job** from that image, with `AI_SERVICE_URL`,
   the same `AI_SERVICE_API_KEY`, and the Telegram secret wired in.
4. Create a Cloud Scheduler cron job targeting the Cloud Run Jobs execute API (OIDC
   auth) at the agreed cadence.
5. Trigger the Job manually once first (don't wait for the schedule) - confirm via
   `GET /claims/existing` that real data flows through end to end before enabling the
   cadence.
