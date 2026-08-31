# Architecture

## What this service is

CIS AI Service is the decision-support backend for **F1 — Claim Repository Bank**, **F2 —
Public Policy Bank**, **F3 — Alert Page**, and **F4 — Admin Setting Page** of the CIS
(Climate Immune System) PRD. It detects and clusters climate-misinformation claims
circulating in public discourse, scores them via a transparent multi-factor formula,
predicts claims likely to emerge ahead of a policy announcement, drafts one-time
Debunk/Prebunk content for each, and automatically links every new policy to the claims
it relates to.

**This service does not talk to end users or the frontend directly.** The real request
path is FE → Go backend → this service. See `GO_INTEGRATION.md` for the full contract.

## Tech stack

| Layer | Choice |
|---|---|
| Web framework | FastAPI + Uvicorn (ASGI) |
| Validation / schemas | Pydantic v2 |
| ORM | SQLAlchemy 2.0, fully async (`asyncpg` driver) |
| Database | Supabase Postgres + `pgvector` extension |
| LLM | OpenAI SDK, Responses API (`client.responses.parse`), strict JSON `text_format` structured output — `gpt-5.6-luna` by default (`OPENAI_MODEL`) |
| Embeddings | `sentence-transformers/all-MiniLM-L6-v2`, 384-dim, local (no external API), English-only |
| Clustering | `hdbscan` (density-based, no fixed cluster count) |
| Similarity search | `pgvector` cosine distance (topics, official sources, fault lines, policy matching) |
| Coordination heuristic | deterministic — `scikit-learn` cosine similarity + union-find, no ML model |
| Package manager | `uv` |
| Schema management | `Base.metadata.create_all()` — **no migrations tool**. Pre-launch software; the model files are the single source of truth. |
| Deployment target | Google Cloud Run (structured JSON logging already wired for Cloud Logging) |

## Request lifecycle

```
Request ──▶ CORS middleware ──▶ logging middleware (X-Request-ID, timing)
        ──▶ router dispatch (app/api/v1/router.py, prefix /api/v1)
        ──▶ endpoint handler (Depends()-injected: DB session, LLMClient, EmbeddingService)
        ──▶ service layer (business logic, pure where possible)
        ──▶ response (Pydantic schema, auto-serialized)
```

Every endpoint handler gets its dependencies via FastAPI `Depends()`:
- `get_db` → a fresh `AsyncSession`, one per request, closed automatically after the
  response (`app/core/database.py`).
- `get_llm_client` → the singleton `LLMClient` (`@lru_cache`).
- `get_embedding_service` → the singleton `EmbeddingService` (`@lru_cache`; the
  sentence-transformers model is loaded once, lazily, on first use).
- `get_session_factory` → **only used by background tasks**, see below.

## The background-task pattern (important — read before touching any async work)

Several operations are slow (multiple sequential LLM calls) and shouldn't block the HTTP
response: F2's AI matchmaking pipeline, clustering triggered by ingestion. These run via
FastAPI's `BackgroundTasks`, which execute *after* the response has been sent but before
the connection is released.

**A background task cannot use a request-scoped `Depends(get_db)`** — by the time it
runs, the request that scheduled it has already returned and that session is closed.
Fixed via a separate overridable indirection:

```python
# app/core/database.py
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return AsyncSessionLocal
```

Every background-task-scheduling endpoint takes `session_factory: async_sessionmaker =
Depends(get_session_factory)` and threads it through to
`background_tasks.add_task(some_func, ..., session_factory=session_factory)`; the task
function itself opens its own session via `async with session_factory() as db:`. Tests
override `get_session_factory` the same way they override `get_db`
(`tests/conftest.py`), so background work in tests runs against the test database too.

**A background task must also receive its `llm`/`embedder` explicitly** — the same
reasoning applies. Calling the bare `get_llm_client()`/`get_embedding_service()`
functions from inside a background task bypasses FastAPI's `dependency_overrides`
entirely (that override machinery only intercepts `Depends()`-resolved parameters on a
route handler), so it would silently construct a **real** `LLMClient` even in tests where
`FakeLLMClient` is supposed to be used everywhere. Every endpoint that schedules a
background job resolves `llm`/`embedder` via its own `Depends()` and passes the already-
resolved instances through to `add_task(...)` — reusing the instance is safe since
neither `LLMClient` nor `EmbeddingService` holds request-scoped state. See
`tests/conftest.py`'s module docstring: integration tests must **never** call a real LLM;
this pattern is what makes that guarantee hold for background work too.

Background-task-scheduling endpoints today: `POST /ingest`, `POST /ingest/batch`
(clustering), `POST /policies` (matchmaking), `POST /matchmaking/policies` (matchmaking,
Go-triggered).

## Module map

```
app/
├── main.py                      # FastAPI app, CORS, request-logging middleware, lifespan
├── core/
│   ├── config.py                 # Pydantic Settings — every env var, single source of truth
│   ├── database.py                # async engine/session factory, get_db, get_session_factory
│   ├── logging_config.py           # structured JSON logging (Cloud Logging-compatible) + Timer
│   └── security.py                  # optional X-API-Key/Bearer check for Go-backend-facing endpoints
├── models/                       # SQLAlchemy ORM — see DATA_MODEL.md for full column reference
│   ├── enums.py                   # ContentSource, MoralFoundation, Stance, ClaimType, ClaimStatus, PolicyStatus
│   ├── content.py                  # ContentItem
│   ├── claim.py                     # Claim
│   ├── topic.py                      # Topic
│   ├── policy.py                      # Policy, ClaimPolicy
│   ├── alert.py                        # ClaimAlert, ClaimScoreSnapshot
│   ├── admin_setting.py                 # AdminSetting (singleton)
│   ├── fault_line.py                     # FaultLine
│   ├── official_source.py                 # OfficialSource
│   └── topic_volume_bucket.py              # TopicVolumeBucket
├── schemas/                      # Pydantic request/response contracts + LLM structured-output schemas
│   ├── content.py, claim.py, topic.py, policy.py, alert.py, admin.py, coordination.py, matchmaking.py
│   └── analysis.py                # every LLM structured-output schema (ContentAnalysisSchema, etc.)
├── api/v1/
│   ├── router.py                  # mounts every endpoint router under /api/v1
│   └── endpoints/                  # one file per resource — see API_REFERENCE.md
│       ├── health.py, ingestion.py, claims.py, topics.py, policies.py,
│       │   alerts.py, admin.py, coordination.py, matchmaking.py
└── services/                     # business logic — see MODULES.md for a per-file walkthrough
    ├── llm_client.py               # OpenAI wrapper, every system prompt, retry/rate-limit handling
    ├── embedding_service.py         # sentence-transformers wrapper
    ├── content_ingestion_service.py  # shared embed+analyze+build helpers, RAG grounding-context builder
    ├── clustering_service.py          # the core pipeline: 2-pass clustering, topic assignment,
    │                                  #   stance, scoring orchestration, background-task wrapper
    ├── scoring_engine.py                # pure Claim Scoring System math — see SCORING.md
    ├── falseness_service.py              # Falseness (F) pgvector match against official_sources
    ├── rag_service.py                     # pgvector similarity search over fault_lines (grounding)
    ├── claim_prediction_service.py         # Non-Existing claim prediction (D2)
    ├── activity_service.py                  # Debunk/Prebunk generation + one-time caching
    ├── policy_matchmaking_service.py         # F2 AI matchmaking pipeline + Go webhook handler
    ├── backend_callback_service.py            # outbound Flow 2 callback to the Go backend
    ├── document_extraction.py                  # PDF/.docx text extraction
    ├── admin_service.py                         # F4 threshold + one-click demo-claim generation
    └── cib_detector.py                           # deterministic CIB heuristic (F5 groundwork)
```

See `MODULES.md` for what every one of these files does at the function level.

## Key design decisions worth knowing before you extend this service

- **`claim_type` is fixed by pipeline of origin, never reclassified.** A claim from
  `clustering_service.py` is always `existing`; a claim from
  `claim_prediction_service.py` is always `non_existing`. There is no code path that
  flips one into the other.
- **Stance is never defaulted.** A `content_item` only gets a `stance` via an explicit
  LLM call, made exactly once, when it's clustered into a specific claim (stance is only
  meaningful relative to a specific claim statement). Centroid-proximity attachment
  (Pass 1) does **not** imply Supporting stance — a rebuttal can be just as semantically
  close to the original claim as agreement.
- **Activity content (Debunk/Prebunk) is generated exactly once, cached forever.** Both
  `activity_service.generate_and_cache_debunk_activity` and
  `claim_prediction_service.predict_non_existing_claim` check
  `if claim.activity_content is not None: return` before ever calling the LLM — no
  endpoint re-generates it on view.
- **Topics are dynamic, never a fixed/seeded taxonomy.** New claims attach to the
  nearest topic by embedding-centroid cosine similarity (≥ 0.5) or spawn a new topic —
  identical logic for both Existing and Non-Existing claims
  (`clustering_service.assign_or_create_topic`).
- **Missing Falseness (F) is `NULL`, never `0`.** Zero would assert "confirmed true".
  See `SCORING.md`.
- **The LLM client never raises on construction, only on first real call.** A
  missing/invalid `OPENAI_API_KEY` doesn't crash the app at startup — `LLMNotConfiguredError`
  only surfaces when a generation call is actually attempted (`LLMClient._get_client`),
  so the rest of the app still starts and non-LLM code paths still work without a key.
- **No table this service owns is ever migrated to include a `cis_`-prefixed column or
  vice versa** — the ownership boundary with the Go backend is enforced by both sides
  independently (`scripts/reset_schema.py` here, an AutoMigrate guard there). See
  `GO_INTEGRATION.md`.
