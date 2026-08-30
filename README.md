# CIS AI Service

Backend for **CIS (Climate Immune System)**'s Claim Repository Bank (PRD v1.1, F1): detects
and scores climate-misinformation **Claims** circulating in public discourse (Existing
claims), predicts claims that might emerge ahead of a policy announcement (Non-Existing
claims), and drafts a structured Debunk/Prebunk Activity for each.

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

### Claims - D2 Non-Existing (predicted, unscored)

```bash
curl -X POST http://localhost:8000/api/v1/claims/non-existing/predict \
  -H "Content-Type: application/json" \
  -d '{
        "policy_title": "Kampung Pulo Housing Redevelopment",
        "policy_description": "The city will renovate public housing in Kampung Pulo along the Ciliwung riverbank, funded by the existing housing budget with no planned displacement."
      }'

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

### Topics & Policies

```bash
curl http://localhost:8000/api/v1/topics
curl -X POST http://localhost:8000/api/v1/topics -d '{"name": "Road Pricing & Transit"}'

curl http://localhost:8000/api/v1/policies
curl -X POST http://localhost:8000/api/v1/policies -d '{"title": "ERP Road Pricing Expansion"}'
```

## Verified

- Live-tested end-to-end against the real Supabase Postgres instance: `reset_schema.py`
  drops/recreates all 8 tables cleanly, `seed_demo_data.py` runs the full pipeline with a real
  OpenAI key - 4 Existing claims correctly scored and ranked, 2 Non-Existing claims correctly
  predicted and left unscored, 5 topics formed dynamically (including a Non-Existing
  prediction correctly matching into an existing Existing-claim topic).
- All 15 API routes registered and smoke-tested via a live server against real data.
- 96 automated tests passing (unit + integration against a live pgvector container), ruff
  clean. Coverage includes: the full Claim Scoring System's edge cases (dormant/below-
  reliability-threshold NPR, missing-Falseness weight renormalization, numerically-stable
  Velocity z-score), the claim-type/status business rule (both directions, via HTTP 422),
  stance classification never defaulting on LLM failure, dynamic topic formation/merging, the
  Dashboard Transparency Requirement (every score component present, never just the collapsed
  number), and the CIB detector isolating a coordinated bot pair from a genuine post.
- Found and fixed two real bugs during verification: a numeric overflow in the Velocity
  z-score's sigmoid squash for large negative inputs, and a Windows-specific text-encoding
  corruption (`locale.getpreferredencoding()` defaulting to `cp1252`) that mangled non-ASCII
  characters in LLM-generated text before storage - fixed for the Docker image via
  `PYTHONUTF8=1`, documented above for local Windows dev.
