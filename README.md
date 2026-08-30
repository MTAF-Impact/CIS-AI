# CIS AI Service

Decision-support AI service for **CIS (Climate Immune System / ClimaResonance)**: detects
climate misinformation, maps community fault lines, predicts backlash, and drafts structured
"Truth Sandwich" inoculations.

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

### Seed demo data

Populates 4 community fault lines and 13 realistic urban-climate-policy posts, then runs the
same pipeline production traffic triggers: embed -> classify (OpenAI) -> persist -> cluster
into narratives -> score risk.

```bash
uv run python scripts/seed_demo_data.py
```

The script is resilient to a missing/rate-limited `OPENAI_API_KEY`: classification and
narrative-labeling calls fall back to safe defaults (`unknown` / truncated post text) rather
than aborting the whole run, so embeddings + clustering + risk scoring still populate the
database even without a live key. LLM calls also auto-retry on `429` rate limits using the
API's suggested `retry-after` delay.

> **Note:** if you're on a rate-limited/free-tier key, seeding fires ~13-17 LLM calls back to
> back; you may see some narratives fall back to a truncated title once the quota is hit.
> Everything else (embeddings, clustering, risk scoring, all non-LLM endpoints) is unaffected.

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

- `tests/unit/` - pure logic (risk engine, CIB detector, RAG text-building, schema validation).
  No DB, no LLM key required. The CIB detector tests use the real embedding model (via the
  session-scoped `real_embedder` fixture) since the heuristic depends on genuine text-similarity
  semantics that a fake/random embedder wouldn't preserve.
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
├── api/v1/endpoints/   # ingestion, narratives, prebunk, truth_sandwich, health
├── core/               # config (Pydantic Settings), async SQLAlchemy engine/session
├── models/             # ContentItem, Narrative, FaultLine, InterventionResponse (+ enums)
├── schemas/            # Pydantic request/response + LLM structured-output contracts
└── services/
    ├── llm_client.py           # OpenAI Responses API wrapper, strict JSON schema output
    ├── embedding_service.py    # sentence-transformers MiniLM, 384-dim
    ├── clustering_service.py   # HDBSCAN clustering + LLM narrative labeling
    ├── risk_engine.py          # deterministic risk formula + sub-score heuristics
    ├── cib_detector.py         # deterministic coordinated-inauthentic-behavior heuristic
    ├── rag_service.py          # pgvector similarity search over fault lines (grounding)
    └── truth_sandwich_service.py
```

**Risk formula** (`app/services/risk_engine.py`):

```
Risk = 0.35 * GrowthVelocity + 0.25 * EmotionalIntensity + 0.15 * GeoConcentration + 0.25 * FaultLineRelevance
```
`LOW` < 0.4, `MEDIUM` 0.4-0.7, `HIGH` > 0.7.

**CIB heuristic** (`app/services/cib_detector.py`): flags post pairs on burst timing (<10 min),
text similarity (>0.80 cosine), and account-creation clustering (<24h apart), then groups
flagged pairs into coordinated clusters via union-find.

## API reference

Base URL: `http://localhost:8000/api/v1`

### Health

```bash
curl http://localhost:8000/health
curl http://localhost:8000/api/v1/health
```

### Ingestion (Feature 1 - Sentiment Radar)

```bash
curl -X POST http://localhost:8000/api/v1/ingest \
  -H "Content-Type: application/json" \
  -d '{
        "text": "The new bus lane is a hidden tax on working families!",
        "source": "social",
        "author_id": "user_123",
        "location": "Downtown"
      }'

curl -X POST http://localhost:8000/api/v1/ingest/batch \
  -H "Content-Type: application/json" \
  -d '{
        "items": [
          {"text": "First post text...", "source": "social", "location": "Downtown"},
          {"text": "Second post text...", "source": "forum", "location": "Riverside"}
        ]
      }'
```

### Narratives (risk ranking, clustering)

```bash
curl http://localhost:8000/api/v1/narratives
curl "http://localhost:8000/api/v1/narratives?risk_level=HIGH&limit=10"
curl http://localhost:8000/api/v1/narratives/{narrative_id}
curl -X POST http://localhost:8000/api/v1/narratives/cluster-now
```

### Prebunk & CIB detection (Feature 2)

```bash
curl -X POST http://localhost:8000/api/v1/prebunk/predict \
  -H "Content-Type: application/json" \
  -d '{
        "policy_title": "Downtown Bus Lane Expansion",
        "policy_description": "The city will add a dedicated bus lane on 5th Ave to cut commute times, funded by the existing transit budget with no new fees planned."
      }'

curl -X POST http://localhost:8000/api/v1/prebunk/check-cib \
  -H "Content-Type: application/json" \
  -d '{
        "posts": [
          {"id": "1", "text": "This bus lane is a hidden tax on working families!", "author_id": "botA", "created_at": "2026-08-30T12:00:00Z", "account_created_at": "2026-08-28T00:00:00Z"},
          {"id": "2", "text": "This bus lane is really just a hidden tax on working families!!", "author_id": "botB", "created_at": "2026-08-30T12:02:00Z", "account_created_at": "2026-08-28T00:10:00Z"},
          {"id": "3", "text": "I like the new park, my kids enjoyed it this weekend", "author_id": "realuser", "created_at": "2026-08-30T07:00:00Z", "account_created_at": "2020-01-01T00:00:00Z"}
        ]
      }'
```

### Truth Sandwich recovery (Feature 3)

```bash
curl -X POST http://localhost:8000/api/v1/response/generate \
  -H "Content-Type: application/json" \
  -d '{"narrative_id": "<narrative-uuid-from-/narratives>"}'

# Human-in-the-loop review
curl -X PATCH http://localhost:8000/api/v1/response/{response_id}/review \
  -H "Content-Type: application/json" \
  -d '{"status": "APPROVED", "reviewer_notes": "Looks good, publish as-is."}'

curl -X PATCH http://localhost:8000/api/v1/response/{response_id}/review \
  -H "Content-Type: application/json" \
  -d '{
        "status": "EDITED",
        "core_fact": "Edited core fact text...",
        "reviewer_notes": "Tightened the wording."
      }'
```

## Verified

- Live-tested against a real Supabase Postgres instance: `pgvector` extension enabled, all 4
  tables created via `Base.metadata.create_all`, full seed -> embed -> classify -> cluster ->
  risk-score pipeline runs end-to-end.
- All 11 API routes registered and smoke-tested (`/health`, ingest, ingest/batch, narratives
  list/detail/cluster-now, prebunk predict/check-cib, response generate/review).
- CIB detector unit-verified: correctly isolates a coordinated bot pair (burst timing + near-
  duplicate text + clustered account age) from a genuine unrelated post.
- Risk engine formula and LOW/MEDIUM/HIGH bucketing unit-verified.
