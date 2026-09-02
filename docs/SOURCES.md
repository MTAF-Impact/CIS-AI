# Data sources

Companion to `docs/CRAWLER.md` (Tier A collectors) - this is the full registry: what's
wired up, what's deferred, and why. Derived from an external "Data Pipeline & Source
Spec v1.0" audit (PM-supplied, Sept 2026) cross-checked against this codebase's actual
state, not the spec's own assumptions - several sources it names as simple turned out
to need more setup, and one turned out to be blocked outright. See git history around
this file's introduction for the full comparison if useful.

## Wired up

| Source | Where | Notes |
|---|---|---|
| RSS (Antara terkini + metro, CNN Indonesia, Republika, Tempo) | `crawler/fetchers/rss.py`, `RSS_FEED_URLS` | 5 verified working feed URLs. Inherently a fixed publisher list - RSS has no search/topic API, so "wider" means verifying more outlets, not going dynamic. Kompas, Detik, and beritajakarta.id's feed paths didn't resolve on a quick check - not wired, not guessed at. |
| Telegram | `crawler/fetchers/telegram.py` | Needs manual one-time setup (credentials + channel list) - see `docs/CRAWLER.md` Phase 2. |
| YouTube Data API v3 | `crawler/fetchers/youtube.py`, `GOOGLE_API_KEY`/`YOUTUBE_SEARCH_QUERIES`/`YOUTUBE_MAX_QUERIES` | Comment threads on video search results. **Dynamic, not just a static list**: `crawler/main.py` fetches active topic names from the AI service (`GET /api/v1/topics`) and appends a `"{topic} jakarta"` query per topic on top of the fixed seed queries, capped at `YOUTUBE_MAX_QUERIES` to protect the daily quota - so coverage grows with what the system is actually tracking. **`GOOGLE_API_KEY` is required for the crawler** (`crawler/main.py::run()` and `POST /admin/run-crawler` both refuse to start without it) - shared with the Fact Check Tools API below, which stays optional. |
| TurnBackHoax.id (Mafindo) | `scripts/seed_debunk_corpus.py` -> `official_sources` table | **Substitutes for the `nlp-brin-id/fakenews-mafindo` HuggingFace dataset, which is gated (manual owner approval - not obtainable same-day).** Pulls the ~10 most recent labelled hoaxes from the public RSS feed (`turnbackhoax.id/feed`) - no working pagination on that route, so this is a small, freshness-biased corpus. Safe to re-run periodically to accumulate more over time. Translates each claim to English before embedding (the embedding model is English-only) - found by testing: skipping this step measurably degraded match quality (0.51 similarity on a real topical match, just under the 0.55 threshold - a silent miss). |
| Google Fact Check Tools API | `app/services/fact_check_client.py`, wired into `falseness_service.compute_falseness_score` as a live fallback when the OfficialSource match misses | Needs `GOOGLE_API_KEY` (self-serve, free) - same key as YouTube above, just enable both APIs on one project. Silently skipped when unset. |
| BMKG weather forecast | `app/services/hazard_context_service.py`, wired into `LLMClient.classify_harm` via `grounding_context` | Public, no auth. Scoped to `BMKG_ADM4_CODES` (kelurahan-level codes) - only one is verified (Kemayoran, central Jakarta). Extend against Kepmendagri 100.1.1-6117/2022, don't guess codes. |

## Deferred (not this pass)

| Source | Why not tonight | Revisit when |
|---|---|---|
| PetaBencana | Public API endpoint didn't resolve on a quick check (docs page 403'd) - needs real investigation, not a guessed URL. | Flood-specific claims become a priority demo scenario. |
| NASA FIRMS | Needs a separately-registered `MAP_KEY` (free, self-serve, but another manual credential). | Fire-detection claims (e.g. the Cakung scenario) become a priority. |
| Bluesky Jetstream | Architecturally different from the crawler's scheduled-pull shape (streaming firehose, not a periodic fetch) - needs a consumer-process design decision first. Also the only source that could light up F5's `w_meta`/`w_struct` signal families (currently dead - see `docs/COORDINATION.md`). | F5 demo needs signal families beyond `w_time`/`w_text`, or there's time to decide the consumer architecture. |
| Reddit | Free tier is non-commercial-only; needs an institutional/academic affiliation or a Reddit for Researchers application before any code is worth writing. | The licensing path is resolved - this is a partnership decision, not an engineering one. |
| Mastodon, Kaskus, GDELT/CC-NEWS | Low ROI at this stage (low Indonesian volume, high maintenance, or backfill-only value that doesn't matter until cold-start becomes the actual bottleneck). | Volume from the wired sources above turns out insufficient. |

## Known gap this doesn't fix

Adding sources does not, by itself, make the Coordinated-Network Detector's (F5)
`w_amp` (co-amplification) or `w_meta` (provenance) signal families real - both are
dead today because `crawler/candidate.py` and `app/models/content.py::ContentItem`
carry no reshare/quote/reply-target or account-creation-time fields, regardless of
which platform a post came from. Bluesky (full follow graph, exact account creation
via the PLC directory) and an extended Telegram fetcher (`forward_from`) are the two
sources worth the schema work if F5 signal richness becomes the priority - see
`docs/COORDINATION.md` for the existing writeup of this gap.
