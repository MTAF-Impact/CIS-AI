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
