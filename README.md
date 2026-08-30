# CIS AI Service

Backend for **CIS (Climate Immune System)** (PRD v1.3, F1-F4): detects and scores
climate-misinformation **Claims** circulating in public discourse (Existing/Generic claims),
predicts claims that might emerge ahead of a policy announcement (Non-Existing/Synthetic
claims), drafts a structured Debunk/Prebunk Activity for each, and automatically links every
newly-registered **Public Policy** to the claims it relates to via an AI matchmaking pipeline
(F2). Also covers the F3 Alert watchlist and F4 admin/threshold config. F5 (Coordinated-Network
Detector) is out of scope per the PRD - `/coordination/check-cib` is kept as a standalone
capability from the earlier design, ahead of F5's own future spec.

> **Integration note (2026-08-31 demo):** the real request path is **FE -> Go backend -> this
> service**, and as of 2026-08-30 they share **the same Supabase Postgres database**. The Go
> backend owns its own tables, all prefixed `cis_` (`cis_users`, `cis_refresh_tokens`,
> `cis_settings`, `cis_policies`, `cis_claim_alerts`, `cis_claim_reviews`,
> `cis_claim_score_snapshots`) - this service must **never** touch those tables except via the
> deliberate dual-writes below (`scripts/reset_schema.py` explicitly excludes anything prefixed
> `cis_` from its drop step).
>
> This service still owns full persistence for its own domain (claims, topics, policies,
> alerts, admin_settings) - Go's `cis_*` tables largely mirror a subset of that data for the
> FE, so **every write this service makes that has a Go-side counterpart is dual-written** via
> `app/services/go_backend_sync.py`, best-effort and never allowed to fail the primary write
> (each dual-write runs in its own `SAVEPOINT`):
>
> | This service writes | Also dual-written to | Trigger |
> |---|---|---|
> | `Claim.status` | `cis_claim_reviews` (upsert by `claim_id`) | claim creation (initial `unreviewed`), `PATCH /claims/{id}/status` |
> | `ClaimAlert` add/remove | `cis_claim_alerts` | `POST`/`DELETE /claims/{id}/alert` |
> | `ClaimScoreSnapshot` (final score only) | `cis_claim_score_snapshots` (full R/V/F/H/EI/NPR/discount breakdown) | every `rescore_claim()` call (clustering, standalone rescore, harm confirm) |
> | `AdminSetting.over_threshold` | `cis_settings` (`key='alert_threshold'`, upsert) | `PUT /admin/settings` |
>
> **Not yet wired: `cis_policies`.** Its shape (`file_path`/`file_mime_type`/`file_size_bytes`,
> `processing_status`/`processing_error`/`processing_attempts`, and an `ai_policy_id`
> back-reference) describes a different workflow than a simple mirror: Go owns the upload and
> file storage, and expects this service to react to a `cis_policies` row and write back
> `ai_policy_id` + `processing_status` once matchmaking finishes - not receive the file
> directly the way `POST /policies` currently does. This needs an explicit handoff contract
> with the Go team (how do we get notified of a new `cis_policies` row - a direct call passing
> its id, or do we poll for `processing_status='pending'`? where does `file_path` actually
> point?) before it's safe to implement - confirm this first during integration.
>
> Beyond that, several pieces built here (F3 Alert CRUD, F4 Admin threshold CRUD, F2's file
> upload/storage/list/search/download) have no actual AI/ML in them and are natural
> Go-backend territory now that `cis_*` tables for them already exist - only the AI-specific
> parts (embeddings, LLM calls, clustering, the Claim Scoring System, F2's matchmaking step)
> strictly need to live here. Re-check this split during integration rather than assuming the
> current "both sides persist, dual-written" arrangement is the final shape.

- **Framework:** FastAPI + Uvicorn, Pydantic v2
- **DB/ORM:** SQLAlchemy 2.0 (async, `asyncpg`) against Supabase Postgres with `pgvector`
- **LLM:** OpenAI SDK (Responses API), `gpt-5.6-luna` by default (`gpt-5.4-mini` also supported),
  strict JSON `text_format` structured output
- **Embeddings/ML:** `sentence-transformers/all-MiniLM-L6-v2` (384-dim), scikit-learn, HDBSCAN
- **Package manager:** [`uv`](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync
cp .env.example .env   # then fill in DATABASE_URL and OPENAI_API_KEY
uv run uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000/docs for interactive Swagger UI.

> **Windows local dev:** set `PYTHONUTF8=1` in your shell before running the app or the
> seed script. Without it, Python falls back to the OS codepage (often `cp1252`, not
> UTF-8) for text decoding, which can corrupt non-ASCII characters (curly quotes, etc.)
> in LLM-generated text before it's ever stored. This was found and fixed for the
> Docker image (`ENV PYTHONUTF8=1` in the `Dockerfile`); it can't be set from inside a
> running Python process since it only takes effect at interpreter startup, so local
> Windows runs need it set in the environment beforehand, e.g.
> `PYTHONUTF8=1 uv run uvicorn app.main:app --reload --port 8000` (bash) or
> `$env:PYTHONUTF8=1` (PowerShell, before running).

### Seed demo data

Populates 4 real Jakarta community fault lines (Kampung Pulo eviction distrust, Penjaringan
tidal flooding, Muara Angke reclamation distrust, Sunter waste-plant pollution distrust), 13
realistic posts across 4 emerging Existing claims grounded in actual Jakarta policies (ERP
road pricing, MRT Fase 2 tree removal, the ITF Sunter waste-to-energy plant, Ciliwung
flood-control budget), and 2 predicted Non-Existing claims (ahead of an MRT extension and a
Ciliwung Phase 3 announcement). Topics form dynamically from the clusters - nothing is
hardcoded. Then runs the same pipeline production traffic triggers: embed -> classify
(OpenAI) -> persist -> cluster into claims -> score (Reach/Velocity/Falseness/Harm/Emotional
Intensity + Net Pushback Ratio discount) -> cache Debunk/Prebunk Activity.

```bash
uv run python scripts/seed_demo_data.py
```

The script is resilient to a missing/rate-limited `OPENAI_API_KEY`: analysis and
claim-summarization calls fall back to safe defaults rather than aborting the whole run, so
embeddings + clustering + scoring still populate the database even without a live key. LLM
calls also auto-retry on `429` rate limits using the API's suggested `retry-after` delay.

> **Note:** if you're on a rate-limited/free-tier key, seeding fires ~20 LLM calls back to
> back; you may see some claims fall back to a truncated statement once the quota is hit.
> Everything else (embeddings, clustering, scoring, all non-LLM endpoints) is unaffected.

Schema changes are applied via `Base.metadata.create_all` (no migrations tool yet - this is
pre-launch software). To fully reset the schema (drops and recreates every table):

```bash
uv run python scripts/reset_schema.py
```

### Docker

```bash
docker build -t cis-ai-service .
docker run --env-file .env -p 8000:8000 cis-ai-service
```

`torch` is pinned to the CPU-only wheel index (`[tool.uv.sources]` in `pyproject.toml`) -
PyPI's default Linux wheels bundle full CUDA/nvidia-\* libraries that are never used (this
service only ever runs embeddings on CPU, including on Cloud Run). That keeps the image at
~3.4GB instead of ~10GB; skipping it would still work, just build/deploy slower.

## Testing

```bash
# Unit tests only - no external services needed (uses the real embedding model, but no DB/LLM)
uv run pytest tests/unit

# Full suite (unit + integration) - needs a local Postgres with pgvector:
docker run -d --name cis-ai-test-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 pgvector/pgvector:pg16
uv run pytest
```

- `tests/unit/` - pure logic (scoring engine, claim status-transition rule, CIB detector, RAG
  text-building, schema validation). No DB, no LLM key required. The CIB detector tests use
  the real embedding model (via the session-scoped `real_embedder` fixture) since the
  heuristic depends on genuine text-similarity semantics that a fake/random embedder wouldn't
  preserve.
- `tests/integration/` (marked `@pytest.mark.integration`) - hits the FastAPI app over HTTP
  (`httpx.AsyncClient` + `ASGITransport`) against a real Postgres+pgvector database, pointed at
  by `TEST_DATABASE_URL` (defaults to `postgresql+asyncpg://postgres:postgres@localhost:5432/postgres`,
  matching the `pgvector/pgvector:pg16` container above and the same image CI uses). These tests
  **never** touch the real `DATABASE_URL`/Supabase instance and **never** call a real LLM -
  `app.services.llm_client.get_llm_client` is always overridden with `tests.fakes.FakeLLMClient`.
- Each simulated HTTP request in a test gets its own fresh DB session (mirroring how `get_db`
  works in production) rather than reusing one session across requests - asyncpg connections
  can't interleave two logical units of work, which is also why the test suite runs on a single
  session-scoped event loop (`asyncio_default_fixture_loop_scope = "session"` in
  `pyproject.toml`) rather than pytest-asyncio's default per-test loop.

## CI/CD

`.github/workflows/ci.yml` runs on every push/PR to `main`/`dev`:
- **lint-and-test**: `ruff check`, then the full test suite against a `pgvector/pgvector:pg16`
  service container (same image as local testing above). The MiniLM model is cached across runs
  via `actions/cache` so it isn't re-downloaded every run.
- **docker-build**: builds the Dockerfile (no push) to catch deployability regressions early.

No deploy step yet - the plan is to deploy to **Google Cloud Run**, which is why logging is
already structured JSON in production (`app/core/logging_config.py` emits `severity`/`message`
fields Cloud Logging parses natively) and the Dockerfile is a standard multi-stage
`uvicorn`-on-`$PORT`-style build ready for `gcloud run deploy`.

## Architecture

```
app/
├── api/v1/endpoints/   # ingestion, claims, topics, policies, coordination, health
├── core/               # config (Pydantic Settings), async SQLAlchemy engine/session
├── models/             # ContentItem, Claim, Topic, Policy, OfficialSource, FaultLine (+ enums)
├── schemas/            # Pydantic request/response + LLM structured-output contracts
└── services/
    ├── llm_client.py             # OpenAI Responses API wrapper, strict JSON schema output
    ├── embedding_service.py      # sentence-transformers MiniLM, 384-dim
    ├── clustering_service.py     # HDBSCAN clustering, dynamic topic assignment, stance,
    │                             #   R/V/F/H/EI/NPR scoring orchestration
    ├── scoring_engine.py         # pure Claim Scoring System math (PRD Section 5)
    ├── falseness_service.py      # pgvector similarity match against OfficialSource corpus
    ├── claim_prediction_service.py  # Non-Existing claim prediction (D2)
    ├── claim_service.py          # claim-type/status business rule
    ├── activity_service.py       # Debunk/Prebunk Activity generation + caching
    ├── cib_detector.py           # deterministic coordinated-inauthentic-behavior heuristic
    └── rag_service.py            # pgvector similarity search over fault lines (grounding)
```

### Claim Scoring System (`app/services/scoring_engine.py`, PRD Section 5)

Existing claims only - Non-Existing claims are never scored.

```
ClaimScore = 0.15*R + 0.15*V + 0.30*F + 0.30*H + 0.10*EI
FinalClaimScore = ClaimScore * (1 - 0.5*NPR)      # NPR = Net Pushback Ratio, capped discount
```

- **R (Reach):** log-weighted impressions/authors/content-count/platform-spread, min-max
  normalized per-topic.
- **V (Velocity):** the claim's own growth rate, z-scored against its topic's historical
  baseline (cold-start gracefully defaults to a neutral 50).
- **F (Falseness):** cosine-similarity match against a verified `OfficialSource` corpus -
  starts empty, so most claims read `null` until real documents are loaded; a missing F
  renormalizes the remaining weights rather than being scored as 0 (would wrongly assert
  "confirmed true").
- **H (Harm):** `0.35*PublicSafety + 0.30*InstitutionalTrust + 0.20*Economic + 0.15*PolicyDisruption`,
  AI-classified then human-confirmable via `PATCH /claims/{id}/harm/confirm`.
- **EI (Emotional Intensity):** outrage-word density + negative-reaction ratio, computed only
  from Supporting-stance content. `EI_opposing` is the same formula on Opposing-stance content
  - display-only, never fed into scoring (PRD Section 5.4.6).
- Every individual component is returned on `GET /claims/{id}` - never just the collapsed
  `final_claim_score` (PRD's Dashboard Transparency Requirement).

**CIB heuristic** (`app/services/cib_detector.py`): flags post pairs on burst timing (<10 min),
text similarity (>0.80 cosine), and account-creation clustering (<24h apart), then groups
flagged pairs into coordinated clusters via union-find. Groundwork for the PRD's D3
(Coordinated-Network Detector) dashboard, which is explicitly deferred to a future iteration.

## API reference

Base URL: `http://localhost:8000/api/v1`

### Health

```bash
curl http://localhost:8000/health
curl http://localhost:8000/api/v1/health
```

### Ingestion

```bash
curl -X POST http://localhost:8000/api/v1/ingest \
  -H "Content-Type: application/json" \
  -d '{
        "text": "The new ERP congestion charge on Sudirman is a hidden tax on working families!",
        "source": "social",
        "author_id": "user_123",
        "location": "Sudirman"
      }'

curl -X POST http://localhost:8000/api/v1/ingest/batch \
  -H "Content-Type: application/json" \
  -d '{
        "items": [
          {"text": "First post text...", "source": "social", "location": "Sudirman"},
          {"text": "Second post text...", "source": "forum", "location": "Kampung Pulo"}
        ]
      }'
```

Live crawling isn't wired up for this prototype - a scheduler would normally feed `/ingest`.
Until then, `/ingest/generate-synthetic` fabricates realistic Jakarta posts via the LLM and
runs them through the exact same embed + analyze + persist pipeline (meant to be triggered
on demand, e.g. a "Generate sample data" button in the FE):

```bash
curl -X POST http://localhost:8000/api/v1/ingest/generate-synthetic \
  -H "Content-Type: application/json" \
  -d '{"count": 10, "topic_hint": null, "auto_cluster": true}'
```

### Claims - D1 Existing (ranked, scored)

```bash
curl http://localhost:8000/api/v1/claims/existing
curl "http://localhost:8000/api/v1/claims/existing?status=unreviewed&limit=10"
curl "http://localhost:8000/api/v1/claims/existing?topic_ids=<id1>&topic_ids=<id2>"   # merged pool, ranked once
curl "http://localhost:8000/api/v1/claims/existing?q=hidden+tax"                       # search by claim text

curl http://localhost:8000/api/v1/claims/{claim_id}          # full R/V/F/H/EI/NPR breakdown

curl -X POST http://localhost:8000/api/v1/claims/cluster-now
curl -X POST http://localhost:8000/api/v1/claims/rescore      # time-based NPR/Velocity refresh

curl -X PATCH http://localhost:8000/api/v1/claims/{claim_id}/status \
  -H "Content-Type: application/json" -d '{"status": "active"}'

curl -X PATCH http://localhost:8000/api/v1/claims/{claim_id}/harm/confirm \
  -H "Content-Type: application/json" -d '{"public_safety": 90.0}'
```

Claim `status` is a single shared 4-value set for both claim types (PRD v1.3 merged the old
type-specific Prebunk/Debunk into one shared value): `unreviewed`, `active`, `inactive`,
`action_taken`.

Bell-icon watchlist toggle (Existing claims only - see F3 below):

```bash
curl -X POST http://localhost:8000/api/v1/claims/{claim_id}/alert      # add to watchlist
curl -X DELETE http://localhost:8000/api/v1/claims/{claim_id}/alert    # remove from watchlist
```

### Claims - D2 Non-Existing (predicted, unscored)

```bash
# policy_id must reference an already-registered F2 policy (see below) - the automatic
# path is F2's AI matchmaking pipeline, which predicts claims on policy creation without
# needing this endpoint at all. This is the manual/ad-hoc trigger.
curl -X POST http://localhost:8000/api/v1/claims/non-existing/predict \
  -H "Content-Type: application/json" -d '{"policy_id": "<policy-uuid>"}'

curl http://localhost:8000/api/v1/claims/non-existing
```

### Coordination (CIB check)

```bash
curl -X POST http://localhost:8000/api/v1/coordination/check-cib \
  -H "Content-Type: application/json" \
  -d '{
        "posts": [
          {"id": "1", "text": "This ERP congestion charge is a hidden tax on working families!", "author_id": "botA", "created_at": "2026-08-30T12:00:00Z", "account_created_at": "2026-08-28T00:00:00Z"},
          {"id": "2", "text": "This ERP congestion charge is really just a hidden tax on working families!!", "author_id": "botB", "created_at": "2026-08-30T12:02:00Z", "account_created_at": "2026-08-28T00:10:00Z"},
          {"id": "3", "text": "I like the new park, my kids enjoyed it this weekend", "author_id": "realuser", "created_at": "2026-08-30T07:00:00Z", "account_created_at": "2020-01-01T00:00:00Z"}
        ]
      }'
```

### Topics

```bash
curl http://localhost:8000/api/v1/topics
curl -X POST http://localhost:8000/api/v1/topics -d '{"name": "Road Pricing & Transit"}'
```

### F2 - Public Policy Bank

Creating a policy is `multipart/form-data` (file upload), not JSON - the only 3 fields
the "Add Public Policy" modal collects (US40). `rolled_out_date` is a plain `YYYY-MM-DD`.
`status` (`rolled_out` / `not_rolled_out`) is derived from `rolled_out_date` vs. wall-clock
time on every read - never a stale stored flag.

```bash
curl -X POST http://localhost:8000/api/v1/policies \
  -F "file=@policy.pdf" \
  -F "name=MRT Fase 2 Bundaran HI-Kota Extension" \
  -F "rolled_out_date=2026-12-01"
```

The response returns immediately with `"processing": true` - creation kicks off the **AI
matchmaking pipeline** (US42) in the background: it embeds the policy (title + description +
extracted document text), links any Existing claims that an LLM confirms are genuinely about
this policy (many-to-many), and predicts one new Non-Existing claim for whatever the policy
covers that isn't already matched (one-to-many). Poll the detail endpoint until
`"processing": false`:

```bash
curl http://localhost:8000/api/v1/policies/{policy_id}          # correlated claim lists once processing completes
curl http://localhost:8000/api/v1/policies/{policy_id}/file     # download the original uploaded document

curl "http://localhost:8000/api/v1/policies?years=2026&years=2025"   # multi-select year filter
curl "http://localhost:8000/api/v1/policies?q=ERP"                    # search by policy name
```

### F3 - Alert Page (watchlist)

Existing claims only - added/removed via the bell icon on `/claims/{claim_id}/alert` (see
above), never directly here.

```bash
curl http://localhost:8000/api/v1/alerts                                    # [C3] watchlist table
curl "http://localhost:8000/api/v1/alerts?q=hidden+tax"                     # search by claim text

# [C1]/[C2] - FinalClaimScore trend for whichever watched claims are currently checked in the FE
curl "http://localhost:8000/api/v1/alerts/chart?claim_ids=<id1>&claim_ids=<id2>&granularity=week"
```

`threshold_status` (`over_threshold` / `under_threshold`) on each watchlist row is derived by
comparing the claim's `final_claim_score` against the single global threshold set via F4.

### F4 - Admin Setting Page

```bash
curl http://localhost:8000/api/v1/admin/settings
curl -X PUT http://localhost:8000/api/v1/admin/settings -d '{"over_threshold": 70.0}'

# One-click, fully-scored sample Existing claim for demo/testing (US33) - fabricates a
# small internally-consistent post cluster via the LLM and runs it through the exact same
# claim-construction + scoring pipeline real clustering uses.
curl -X POST http://localhost:8000/api/v1/admin/generate-generic-claim
```

## Known assumptions / gaps (PRD v1.3)

- **US12 "Top 5 Accounts" interpretation**: the PRD itself flags this as an unconfirmed
  assumption. Implemented here as "top 5 accounts by post-volume contribution to the claim's
  Supporting side" (the PRD's own stated reading) - revisit if the PM confirms a different
  interpretation (e.g. top 5 opposing, by engagement, or bot-like).
- **Policy file storage**: uploaded documents are stored inline in Postgres (`bytea`), not a
  separate object-storage bucket - a deliberate MVP simplification (no other storage
  credentials configured), fine at demo scale but worth revisiting before real production load.
- **F2 AI matchmaking** currently predicts exactly one new Non-Existing claim per policy (the
  PRD allows "one or more"); the existing-claim match step is a cosine-similarity prefilter
  (threshold 0.35) followed by one batched LLM confirmation call, not a full pairwise LLM
  review of every claim in the databank.
- **F5 Coordinated-Network Detector** is explicitly out of scope in PRD v1.3 (placeholder only);
  `/coordination/check-cib` remains available from the earlier design but isn't part of the
  current F1-F4 spec.

## Verified

- Live-tested end-to-end against the real Supabase Postgres instance: `reset_schema.py`
  drops/recreates all tables cleanly (queries `pg_tables` directly, so it can't leave orphaned
  tables behind the way relying on `Base.metadata.drop_all()` alone can), `seed_demo_data.py`
  runs the full pipeline with a real OpenAI key.
- F2's AI matchmaking pipeline live-tested against the real DB/LLM: uploading a policy document
  correctly linked an existing matching claim and predicted a genuinely distinct (non-duplicate)
  new Non-Existing claim, both persisted correctly, `processing` flipping to `false` once done.
- F3 (alert add/remove, watchlist, threshold-status derivation, score-history chart) and F4
  (global threshold, one-click fully-scored demo claim generation) smoke-tested end-to-end
  against the real DB/LLM.
- 99 automated tests passing (unit + integration against a live pgvector container), ruff
  clean. Coverage includes: the full Claim Scoring System's edge cases (dormant/below-
  reliability-threshold NPR, missing-Falseness weight renormalization, numerically-stable
  Velocity z-score), the shared status model working uniformly across both claim types, stance
  classification never defaulting on LLM failure, dynamic topic formation/merging, the Score
  Transparency Requirement (every score component present, never just the collapsed number),
  the CIB detector isolating a coordinated bot pair from a genuine post, and F2's create/list/
  detail/download/matchmaking flow (including a real, valid in-memory `.docx` round-trip
  through the actual PDF/Word text-extraction parser, not a mocked one).
- Found and fixed a real cross-cutting bug during this build: the F2 AI matchmaking
  BackgroundTasks job originally imported `AsyncSessionLocal` directly, which would have
  silently pointed at the real production database during tests instead of the test database
  (background tasks can't use a request-scoped `Depends(get_db)`, which is how every other
  test-vs-prod DB split in this codebase works). Fixed via a new overridable
  `get_session_factory()` indirection in `app/core/database.py`, threaded through
  `BackgroundTasks.add_task(...)` and overridden in `tests/conftest.py` the same way `get_db`
  already is.
- Also found and fixed (pre-PRD-v1.3 work, still relevant): a numeric overflow in the Velocity
  z-score's sigmoid squash for large negative inputs, and a Windows-specific text-encoding
  corruption (`locale.getpreferredencoding()` defaulting to `cp1252`) that mangled non-ASCII
  characters in LLM-generated text before storage - fixed for the Docker image via
  `PYTHONUTF8=1`, documented above for local Windows dev.
