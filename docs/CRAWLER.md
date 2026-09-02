# Crawler

Feeds real content into the AI service, replacing/supplementing
`/ingest/generate-synthetic`. Lives in `crawler/`, its own dependency group (`uv sync
--group crawler`) - see `ARCHITECTURE.md` for why it talks to the AI service only over
HTTP (no direct Postgres access, same rule as the Go backend), even though it now runs
inside that same service's process (see Deployment below).

**Deployment shape**: originally designed as a separate, stateless Cloud Run Job
triggered by Cloud Scheduler. For the hackathon build this was folded into the main AI
service instead - one deployment, one image, triggered via `POST /admin/run-crawler` -
since standing up a second Cloud Run resource (Job + Scheduler + its own build
pipeline) wasn't worth the operational overhead for this stage. If this ever needs to
split back out (e.g. the crawler's resource usage starts contending with the AI
service's own), see git history around this doc's "merged into the AI service" section
for the original standalone-Job setup (a separate `Dockerfile.crawler` + Cloud Run Job
+ Cloud Scheduler) to resurrect rather than redesigning from scratch.

```
crawler/
├── config.py            # CrawlerSettings - all env vars
├── candidate.py          # the common shape every fetcher normalizes into
├── client.py              # HTTP client to the AI service (GET /fault-lines, POST /ingest/batch)
├── relevance_filter.py     # local multilingual embedding-similarity + location keyword gate
├── fetchers/
│   ├── rss.py               # feedparser-based
│   ├── telegram.py           # Telethon-based, public channels only
│   └── youtube.py             # YouTube Data API v3 - video search + comment threads
└── main.py                # orchestrates: fetch -> filter -> rank top-N -> submit
```

See `docs/SOURCES.md` for the full source registry (feasibility/impact of every
candidate source, not just what's wired up here) and how this fits the Falseness (F)
and Harm (H) grounding sources that live in the main AI service instead of the
crawler (`scripts/seed_debunk_corpus.py`, `app/services/fact_check_client.py`,
`app/services/hazard_context_service.py`).

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
| `AI_SERVICE_URL` | `http://localhost:8000` | Only matters for **direct CLI use** (`python -m crawler.main`, local dev). Via `POST /admin/run-crawler` (the deployed path), this is force-overridden to `http://localhost:$PORT` regardless of what's set - don't configure it on Cloud Run, it's ignored there. |
| `AI_SERVICE_API_KEY` | `""` | Sent as `X-API-Key` if set - must match the AI service's own `AI_SERVICE_API_KEY`. |
| `RSS_FEED_URLS` | `[]` | JSON list of feed URLs. Inherently a fixed publisher list (RSS has no query/topic API to search against, unlike YouTube) - "wider" here means verifying more outlet feeds, not making it algorithmically dynamic. 5 verified as of this doc: Antara (terkini + metro), CNN Indonesia, Republika, Tempo. Kompas, Detik, and beritajakarta.id's feed paths didn't resolve on a quick check - not guessed at, see `docs/SOURCES.md`. |
| `TELEGRAM_CHANNELS` | `[]` | JSON list of public channel usernames. |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | unset | From my.telegram.org - see Phase 2. |
| `TELEGRAM_SESSION_STRING` | `""` | From a one-time interactive login - see Phase 2. Telegram fetch is skipped entirely (not an error) until this is set. |
| `GOOGLE_API_KEY` | `""` | **Required** - console.cloud.google.com -> enable "YouTube Data API v3" -> Credentials -> API key. Self-serve, free, 10,000 quota units/day. `run()` (and `POST /admin/run-crawler`) refuse to start without it - YouTube is a required source, not a best-effort one. Shared with the main AI service's own `GOOGLE_API_KEY` (`app/core/config.py`), which stays optional there - same project, "Fact Check Tools API" also enabled on it. |
| `YOUTUBE_SEARCH_QUERIES` | `[]` | Fixed floor/seed queries, always searched (covers cold start before any topics exist). Real breadth is dynamic: `main.py` calls `AIServiceClient.fetch_topic_names()` (`GET /api/v1/topics`) and appends one `"{topic name} jakarta"` query per active topic on top of these, so the query list grows with what the system is actually tracking instead of staying hand-picked forever. |
| `YOUTUBE_MAX_QUERIES` | `15` | Caps total queries (static + topic-derived) per run - each costs 100 quota units (`search.list`) plus ~1/video comment page. |
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

## Deployment - merged into the AI service (current)

The crawler ships inside the AI service's own image now (`Dockerfile` installs
`--group crawler` too) - no separate Cloud Run resource.

1. Redeploy the AI service as normal (whatever pipeline already builds/deploys it -
   e.g. a PR merge to `main` if Cloud Build continuous deployment is set up).
2. Set the crawler's env vars on that **same** Cloud Run service/revision -
   `RSS_FEED_URLS`, `GOOGLE_API_KEY`, `YOUTUBE_SEARCH_QUERIES`, and Telegram's if
   configured (see the table above). Don't set `AI_SERVICE_URL` - the endpoint below
   resolves it to itself (`http://localhost:$PORT`) at call time.
3. Trigger a run: `POST /admin/run-crawler` (no body, 202 response, runs in the
   background - watch Cloud Logging for progress/errors, same log lines as a local
   `--dry-run`). Trigger this manually before a demo, or point a Cloud Scheduler HTTP
   job at it (with OIDC auth, same as any other authenticated Cloud Run endpoint) for
   a recurring cadence - much simpler than the old Job+Scheduler combination since
   it's just a normal HTTP route on an existing service now.
