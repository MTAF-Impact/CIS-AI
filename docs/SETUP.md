# Setup, Testing & Deployment

## Requirements

- Python `>=3.11,<3.13`
- [`uv`](https://docs.astral.sh/uv/) as package manager
- A Postgres instance with the `pgvector` extension (Supabase in production; local
  Docker `pgvector/pgvector:pg16` for dev/test)
- An OpenAI API key (optional at boot — every LLM-dependent code path degrades to a
  clear `503`/log message without one, see `ARCHITECTURE.md`)

## Local dev

```bash
uv sync
cp .env.example .env   # fill in DATABASE_URL and OPENAI_API_KEY at minimum
uv run uvicorn app.main:app --reload --port 8000
```

Swagger UI: `http://localhost:8000/docs`. OpenAPI JSON: `http://localhost:8000/openapi.json`.

> **Windows:** set `PYTHONUTF8=1` in your shell before running the app or any script.
> Without it, Python falls back to the OS codepage (often `cp1252`, not UTF-8) for text
> decoding, which can corrupt non-ASCII characters (curly quotes, etc.) in LLM-generated
> text before it's ever stored. Already set in the Docker image (`ENV PYTHONUTF8=1` in
> the `Dockerfile`); local Windows runs need it set explicitly, e.g.
> `PYTHONUTF8=1 uv run uvicorn app.main:app --reload --port 8000` (bash) or
> `$env:PYTHONUTF8=1` beforehand (PowerShell) — it can't be set from inside a running
> Python process, only at interpreter startup.

### `--reload` caveat

`uvicorn --reload` (via `WatchFiles`) has been observed to catch its **first** file
change correctly and then silently stop watching for subsequent changes, leaving the
running process on stale code with no error or warning. If behavior doesn't match a code
change you just made, don't assume the bug is in your change — check whether the server
actually reloaded (look for a `WatchFiles detected changes in '...'. Reloading...` line
in its log), and if it didn't, kill and restart the process manually.

## Environment variables (`app/core/config.py`)

| Var | Default | Notes |
|---|---|---|
| `PROJECT_NAME` | `"CIS AI Service"` | |
| `ENV` | `"development"` | `"production"`/`"prod"` (case-insensitive) enables JSON logging by default. |
| `PORT` | `8000` | |
| `DATABASE_URL` | local Postgres | **Use Supabase's transaction pooler**, not the direct host — `db.<ref>.supabase.co` resolves IPv6-only, which fails outright on networks without real IPv6 routing. Format: `postgresql+asyncpg://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres`. |
| `OPENAI_API_KEY` | `""` | Every LLM code path is resilient to this being unset — see below. |
| `OPENAI_MODEL` | `"gpt-5.6-luna"` | `gpt-5.4-mini` also supported. |
| `EMBEDDING_MODEL_NAME` | `"sentence-transformers/all-MiniLM-L6-v2"` | |
| `EMBEDDING_DIM` | `384` | Must match the model's actual output dimension — this sizes the `vector(...)` columns. |
| `CORS_ORIGINS` | `["*"]` | JSON list, not comma-separated. |
| `LOG_LEVEL` | `"INFO"` | `"DEBUG"` also enables SQL echo. |
| `LOG_JSON` | `None` (auto: `True` iff `ENV` is production) | Override either way if needed. |
| `BACKEND_URL` | `""` | Go backend base URL, for the Flow 2 callback. See `GO_INTEGRATION.md`. |
| `AI_SERVICE_API_KEY` | `""` | Inbound auth for the 2 Go-facing endpoints, optional. |
| `INTERNAL_API_KEY` | `""` | Outbound `X-Internal-Key` on the Flow 2 callback, optional. |

## Seed demo data

```bash
uv run python scripts/seed_demo_data.py
```

Populates 4 real Jakarta community fault lines, 13 realistic posts across 4 emerging
Existing claims (ERP road pricing, MRT Fase 2 tree removal, ITF Sunter waste plant,
Ciliwung flood-control budget), and 2 predicted Non-Existing claims — then runs the full
production pipeline end to end: embed → classify (OpenAI) → persist → cluster → score →
cache Debunk/Prebunk. Topics form dynamically from the clusters, nothing hardcoded.

Resilient to a missing/rate-limited `OPENAI_API_KEY`: analysis/summarization calls fall
back to safe defaults rather than aborting the run, so embeddings + clustering + scoring
still populate the DB even without a live key (some claim statements will just be
truncated raw text instead of LLM-synthesized). LLM calls auto-retry on `429`s.

`clear_demo_data()` (called at the top of the seed script) wipes `content_items`,
`claim_alerts`, `claim_score_snapshots`, `claims`, `topic_volume_buckets`, `topics`,
`policies`, `fault_lines`, `official_sources` — in FK-safe order — and explicitly
**preserves** `admin_settings` (not demo content, an operator-configured value).

## Schema reset

```bash
uv run python scripts/reset_schema.py
```

Drops and recreates every table **this service owns** (queries `pg_tables` directly
rather than relying on `Base.metadata.drop_all()` alone, which silently leaves orphaned
tables behind whenever a model file is deleted). **Explicitly excludes every table
prefixed `cis_`** — the Go backend's tables — so this can never be run destructively
against the backend's data, even by accident. See `GO_INTEGRATION.md`.

There is no migrations tool. Any schema change is applied by editing the relevant
`app/models/*.py` file and either running this reset script (destructive — full data
loss on this service's own tables) or a manual, additive `ALTER TABLE ... ADD COLUMN`
against the live database for a non-destructive rollout (this is how
`claims.debunk_core_fact`/`debunk_nuanced_flag`/`debunk_reiterated_fact` and
`policies.backend_policy_id` were added to the live Supabase instance without a reseed).

## Docker

```bash
docker build -t cis-ai-service .
docker run --env-file .env -p 8000:8000 cis-ai-service
```

`torch` is pinned to the CPU-only wheel index (`[tool.uv.sources]` in `pyproject.toml`)
— PyPI's default Linux wheels bundle full CUDA/`nvidia-*` libraries that are never used
(this service only ever runs embeddings on CPU, including on Cloud Run). Keeps the image
at ~3.4GB instead of ~10GB.

## Testing

```bash
# Unit only - no external services (uses the real embedding model, no DB/LLM)
uv run pytest tests/unit

# Full suite - needs a local Postgres+pgvector
docker run -d --name cis-ai-test-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 pgvector/pgvector:pg16
uv run pytest
```

- **`tests/unit/`** — pure logic: the scoring engine (every formula + every documented
  edge case), the CIB detector (uses the real embedding model via the session-scoped
  `real_embedder` fixture — the heuristic depends on genuine similarity semantics a
  fake/random embedder wouldn't preserve), RAG grounding-text building, schema
  validation. No DB, no LLM key required.
- **`tests/integration/`** (marked `@pytest.mark.integration`) — hits the FastAPI app
  over HTTP (`httpx.AsyncClient` + `ASGITransport`) against a real Postgres+pgvector
  database, pointed at by `TEST_DATABASE_URL` (defaults to
  `postgresql+asyncpg://postgres:postgres@localhost:5432/postgres`). **Never** touches
  the real `DATABASE_URL`/Supabase instance and **never** calls a real LLM —
  `get_llm_client` is always overridden with `tests.fakes.FakeLLMClient`, and (critically)
  every `BackgroundTasks` job is threaded its `llm`/`embedder` explicitly by the endpoint
  that schedules it, so background work is faked too — see `ARCHITECTURE.md`'s
  background-task section. If you add a new endpoint that schedules a background task,
  you **must** pass `llm=`/`embedder=` through explicitly or this guarantee silently
  breaks (this exact bug shipped once — the whole suite quietly took 5–6 real minutes
  hitting the real OpenAI API instead of ~20 seconds against the fake, invisible locally
  because `.env` had a real key, and only surfaced as a hard failure in CI where no key
  exists).
- Each simulated HTTP request gets its **own fresh** `AsyncSession` (mirroring
  production's `get_db`) rather than reusing one session across requests — asyncpg
  connections can't interleave two logical units of work. This is also why the whole
  test suite runs on a single session-scoped event loop
  (`asyncio_default_fixture_loop_scope = "session"` in `pyproject.toml`) rather than
  pytest-asyncio's per-test default — asyncpg connections are bound to the loop they
  were created in.

## CI/CD (`.github/workflows/ci.yml`)

Runs on every push/PR to `main`/`dev`:
- **lint-and-test** — `ruff check`, then the full suite against a
  `pgvector/pgvector:pg16` service container (same image as local testing). The MiniLM
  model is cached across runs (`actions/cache`) so it isn't re-downloaded every run.
  **No `OPENAI_API_KEY` secret is configured or needed** — see the `FakeLLMClient`
  guarantee above.
- **docker-build** — builds the `Dockerfile` (no push), catches deployability
  regressions early.

No deploy step yet. Target is **Google Cloud Run** — logging is already structured JSON
in production (`app/core/logging_config.py` emits `severity`/`message`/`timestamp`
fields Cloud Logging parses natively), and the `Dockerfile` is a standard multi-stage
`uvicorn`-on-`$PORT` build ready for `gcloud run deploy`.
