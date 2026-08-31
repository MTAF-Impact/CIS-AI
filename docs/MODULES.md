# Module-by-Module Reference

Deep dive into `app/services/` — the actual business logic. `DATA_MODEL.md` covers
`app/models/` in full; `API_REFERENCE.md` covers `app/api/v1/endpoints/` as an HTTP
contract. This file is the "how it actually works internally" layer between the two.

---

## `app/services/llm_client.py`

Thin async wrapper around the OpenAI SDK's Responses API, with **strict structured JSON
output** via Pydantic `text_format` schemas — every LLM call returns a validated,
typed Pydantic object, never raw text to parse.

**`LLMClient`** — construction is lazy and never raises: a missing/invalid
`OPENAI_API_KEY` only surfaces as `LLMNotConfiguredError` on the *first actual call*
(`_get_client`), not at import/instantiation time. `_generate_structured()` is the one
private method every public method funnels through: calls `client.responses.parse(model=...,
instructions=<system prompt>, input=<user prompt>, text_format=<schema>)`, with
automatic retry (up to 3×, `MAX_RATE_LIMIT_RETRIES`) on `openai.RateLimitError` —
honoring the API's own `retry-after` header/message if present, falling back to a 15s
default. Two rate-limit error codes (`insufficient_quota`, `credit_balance_exhausted`)
are treated as non-retryable — retrying a dead key just wastes ~45s before failing
anyway. Note: `temperature` is deliberately never passed — reasoning-tier models like
`gpt-5.6-luna` reject it (`400 Unsupported parameter`).

Public methods (each has its own system prompt constant in this file, and its own
Pydantic schema in `app/schemas/analysis.py`):

| Method | Purpose | Called from |
|---|---|---|
| `analyze_content(text)` | Ingestion-time: outrage score, moral foundation, extracted claim, underlying grievance. | `content_ingestion_service.analyze_and_build_item` |
| `summarize_claim(sample_texts)` | Synthesize a fresh `claim_statement` + candidate `topic_label` from a cluster of posts. | `clustering_service.build_claim_from_content_items` |
| `classify_stance(claim_statement, post_text)` | Single-post stance vs. a known claim. | `clustering_service` Pass 1 (attach to existing claim) |
| `classify_stances_batch(claim_statement, texts)` | Batched stance for a whole new cluster in one call. Raises `StanceCountMismatchError` if the model returns a different count than given — the caller must not silently zip a misaligned result. | `clustering_service` Pass 2 (new claim), falls back to per-item `classify_stance` on failure |
| `classify_harm(claim_statement, sample_supporting_texts)` | The 4 Harm sub-scores, against the detailed 5-band rubric (see `SCORING.md`). | `clustering_service.build_claim_from_content_items` |
| `generate_debunk(claim_statement, grounding_context)` | The Truth Sandwich (`core_fact`/`nuanced_flag`/`reiterated_fact`). | `activity_service.generate_and_cache_debunk_activity` |
| `predict_non_existing_claim(policy_title, policy_description, grounding_context)` | Predicted claim statement, topic, attack angle, framing, and the Prebunk explainer. | `claim_prediction_service.predict_non_existing_claim` |
| `generate_synthetic_posts(count, topic_hint, grounding_context)` | Fabricates realistic Jakarta posts (prototype crawler stand-in). | `ingestion.py`'s `/ingest/generate-synthetic`, `admin_service.generate_demo_existing_claim` |
| `confirm_policy_claim_matches(policy_title, policy_description, candidate_claim_statements)` | One boolean per candidate claim — is it genuinely about this policy? Raises `ValueError` on a count mismatch. | `policy_matchmaking_service._run` |

`get_llm_client()` — `@lru_cache`-singleton factory, the `Depends()` target everywhere.

---

## `app/services/embedding_service.py`

Wraps `sentence-transformers/all-MiniLM-L6-v2` (384-dim, local, no external API,
**English-only** — this is exactly why `SYNTHETIC_POSTS_SYSTEM_PROMPT` forces English
output even for a Jakarta-context prototype). `embed(text)` / `embed_batch(texts)`, both
L2-normalized (`normalize_embeddings=True`) so pgvector cosine distance and raw dot
product are equivalent. `get_embedding_service()` — `@lru_cache` singleton; the model
loads once, lazily, on first use.

---

## `app/services/content_ingestion_service.py`

Shared building blocks reused by real ingestion, synthetic generation, and the F4 demo
button:

- `build_grounding_context(db)` — pulls up to 5 fault lines + 10 topic names, renders
  them into a short plain-text block fed into generation prompts for thematic
  continuity. Not the same as `rag_service`'s grounding builder (see below) — this one
  is a lightweight "what's already active" summary for generation, not a targeted
  similarity retrieval.
- `analyze_content_item(payload, llm)` — runs `llm.analyze_content` only (which also
  returns an English translation, `text_en` - the embedding model is English-only).
  Split out from embedding so callers ingesting many items can analyze concurrently,
  then embed all the resulting translations in one batched `embed_batch` call.
- `build_content_item(payload, analysis, embedding)` — pure, synchronous: assembles the
  `ContentItem` ORM object from a payload + its analysis + its (already-computed)
  embedding. Includes `payload.external_ref` for dedup (see `DATA_MODEL.md`).
- `analyze_and_build_item(payload, llm, embedder)` — single-item convenience wrapper:
  `analyze_content_item` → embed `analysis.text_en` → `build_content_item`. Used by
  `POST /ingest` and `admin_service.generate_demo_existing_claim`. Batch-oriented
  callers (`POST /ingest/batch`, `/ingest/generate-synthetic`) use the two split
  functions directly to preserve `embed_batch`'s performance win — see
  `ingestion.py::_analyze_and_build_batch`.

---

## `app/services/clustering_service.py`

**The core pipeline.** Two-pass clustering, dynamic topic assignment, stance
classification, and the full R/V/F/H/EI/NPR scoring orchestration for every claim
touched.

### `cluster_unclustered_content(db, llm=None, embedder=None) -> ClusterResult`

The entry point. Queries every `content_item` with `claim_id IS NULL AND embedding IS
NOT NULL`. If none, no-ops (`ClusterResult(0, 0, 0)`).

**Pass 1 — attach to existing claims:** for each unclustered item, cosine-compare
against every `existing` claim's embedding; if the best match is ≥
`CLAIM_ATTACH_THRESHOLD` (0.55), attach it (`item.claim_id = claim.id`) and classify its
stance via an **explicit LLM call** (never defaulted — see `ARCHITECTURE.md`). If the
stance is `supporting`, increments that hour's `topic_volume_buckets` row.

**Pass 2 — HDBSCAN the remainder:** whatever's left (label `-1` = noise stays
unclustered) is clustered via HDBSCAN (`MIN_CLUSTER_SIZE = 2`), and each formed cluster
becomes a brand-new `existing` claim via `build_claim_from_content_items` (below).

**Then:** for every topic touched in either pass, `renormalize_topic_reach` recomputes
Reach across every claim in that topic (a sibling's Reach change makes its own cached
score stale too), and every claim returned from that renormalization gets a full
`rescore_claim` pass.

### `build_claim_from_content_items(db, cluster_items, llm, embedder) -> Claim`

**Public and reused** by both Pass 2 above and `admin_service.generate_demo_existing_claim`
(the F4/Flow-3 demo-claim generator) — deliberately the *same* function, so a
demo-generated claim goes through identical construction logic to a real one, never a
parallel path that could drift.

Steps, in order: LLM-synthesize `claim_statement`+`topic_label` (`summarize_claim`,
falls back to the first sample post's truncated text on failure) → embed the statement →
`assign_or_create_topic` → create the `Claim` row (`status=UNREVIEWED`) → flush →
batch-classify every item's stance (`classify_stances_batch`, falls back to per-item
`classify_stance` on failure) → attach items + increment volume buckets for
Supporting-stance items → AI-classify Harm (`classify_harm`) and compute `harm_score` →
`generate_and_cache_debunk_activity`. Returns the claim **not yet scored on R/V/F/EI/NPR**
— that happens in the caller (`cluster_unclustered_content`'s renormalize+rescore step,
or `admin_service`'s own explicit renormalize+rescore call).

### `assign_or_create_topic(db, claim_embedding, candidate_label) -> Topic`

Dynamic topic assignment, identical for both claim types. Cosine-compares the new
claim's embedding against every topic's centroid embedding; if the best match ≥
`TOPIC_ATTACH_THRESHOLD` (0.5), attaches and **recomputes that topic's centroid** as the
mean of every sibling claim's embedding plus this one (`_centroid`, L2-normalized).
Otherwise creates a new `Topic` with `embedding = claim_embedding` (a single-claim topic
is its own centroid).

### `renormalize_topic_reach(db, topic_id) -> list[Claim]`

Recomputes raw Reach (`scoring_engine.raw_reach`) for every `existing` claim in a topic
from its current Supporting-side `content_items` aggregates, then min-max normalizes the
whole set together (`scoring_engine.normalize_minmax_per_topic`) and writes
`claim.reach_score`. Returns the touched claims so the caller can also rescore them.

### `rescore_claim(db, claim) -> None`

Computes Velocity (24h-window volume delta, z-scored against the topic's baseline from
`topic_volume_buckets`), Falseness (`falseness_service.compute_falseness_score`), NPR +
dormancy + discount factor (Supporting/Opposing volume counts in the rolling window),
`claim_score`, `final_claim_score` — writes all of them, and appends a
`ClaimScoreSnapshot` row.

### `rescore_all_existing_claims(db) -> int`

`POST /claims/rescore`'s implementation — calls `rescore_claim` for every `existing`
claim, unconditionally (no clustering re-run). Necessary because NPR/discount can drift
purely from wall-clock time.

### `cluster_unclustered_content_task(llm=None, embedder=None, session_factory=None) -> None`

The `BackgroundTasks`-compatible wrapper around `cluster_unclustered_content` — opens its
own session, logs the result, swallows and logs any exception (a background task has no
HTTP response to fail). See `ARCHITECTURE.md`'s background-task pattern section for why
this exists and why `llm`/`embedder` must be passed explicitly by the caller.

---

## `app/services/scoring_engine.py`

Pure functions, zero I/O, fully unit-tested — see `SCORING.md` for the formulas
themselves and the exact constants (`WEIGHT_R`, `GAMMA`, `RELIABILITY_THRESHOLD`, etc.).

---

## `app/services/falseness_service.py`

`compute_falseness_score(db, claim_embedding, threshold=0.55) -> float | None` — top-1
pgvector cosine-similarity match against `official_sources`. Deliberately a **separate
module** from `rag_service.py`: RAG grounding (below) is soft/best-effort context for LLM
prompts; this is hard-thresholded and must never fabricate a value.

---

## `app/services/rag_service.py`

`retrieve_relevant_fault_lines(db, query_text, embedder=None, top_k=3)` — pgvector
cosine-distance top-k search over `fault_lines`. `build_grounding_context(fault_lines,
extra_notes=None)` — renders retrieved fault lines into a plain-text block for a
Debunk/Prebunk generation prompt. Used to ground both `activity_service`'s Debunk
drafting and `claim_prediction_service`'s Prebunk prediction in real local context, so
generated content references genuine community grievances rather than generic language.

---

## `app/services/claim_prediction_service.py`

`predict_non_existing_claim(db, policy, llm, embedder, already_covered_claim_statements=None)
-> NonExistingClaimPrediction` — the D2 prediction flow. Retrieves fault-line grounding
for the policy, calls `llm.predict_non_existing_claim`, embeds the result,
`assign_or_create_topic`s it, creates the `Claim` row (`claim_type=NON_EXISTING,
policy_id=policy.id`), renders the Prebunk activity (`activity_service.render_prebunk_activity`
— just the LLM's `inoculation_explainer` directly, no further processing needed). If
`already_covered_claim_statements` is given (from F2 matchmaking's own existing-claim
matches), the LLM is explicitly asked to predict something distinct from those rather
than duplicating an already-covered aspect. `db.flush()`, not `db.commit()` — the caller
controls the transaction boundary.

---

## `app/services/activity_service.py`

`generate_and_cache_debunk_activity(db, claim, llm, embedder)` — **Existing** claims'
Debunk Activity. Idempotent guard (`if claim.activity_content is not None: return`) —
never regenerates. Retrieves fault-line grounding for `claim.claim_statement`, calls
`llm.generate_debunk`, writes the concatenated `activity_content` **and** the 3 split
Truth Sandwich fields (`debunk_core_fact`/`debunk_nuanced_flag`/`debunk_reiterated_fact`)
— the concatenation is still the single copyable block the PRD requires; the 3 fields let
the FE render them as distinct labeled UI blocks without having to guess where to split
the combined text. On LLM failure, logs and leaves `activity_content` unset (never
partially populated).

`render_prebunk_activity(inoculation_explainer)` — trivial: **Non-Existing** claims'
Prebunk Activity is just the `inoculation_explainer` field verbatim, no further
structure. `predicted_attack_angle`/`likely_framing` are useful analyst context but are
never persisted as columns.

---

## `app/services/policy_matchmaking_service.py`

The F2 AI matchmaking pipeline (US42) — see `GO_INTEGRATION.md` for the full contract
this implements from the Go-backend-facing side.

- `_run(db, policy, llm, embedder) -> tuple[int, int]` — embeds the policy, cosine-
  prefilters existing claims (`CLAIM_MATCH_PREFILTER_THRESHOLD = 0.35`, top 20
  candidates, `MAX_MATCH_CANDIDATES`), sends the survivors to
  `llm.confirm_policy_claim_matches` in one batched call, creates `ClaimPolicy` rows for
  confirmed matches, then calls `predict_non_existing_claim` (passing the matched
  claims' statements so the prediction avoids duplicating already-covered ground).
  Returns `(matched_claim_count, generated_claim_count)`.
- `match_and_predict_claims_for_policy(policy_id, llm=None, embedder=None,
  session_factory=None)` — the background-task wrapper for this service's own `POST
  /policies` (local/testing) upload flow.
- `_fetch_and_extract(file_name, file_mime_type, document_url)` — best-effort document
  download (`httpx`, `follow_redirects=True`, 30s timeout) + text extraction; any failure
  (expired signed URL, unsupported type, network error) falls back to `(None, None)`
  rather than aborting.
- `run_matchmaking_webhook(backend_policy_id, name, description, rolled_out_date,
  file_name, file_mime_type, document_url, llm=None, embedder=None, session_factory=None)`
  — the Flow 1 handler. Idempotency check first (`SELECT ... WHERE backend_policy_id =
  :id`); if an existing `Policy` is found, short-circuits to re-reporting the existing
  counts via Flow 2 without re-running anything. Otherwise: fetch+extract, create the
  `Policy` row (`backend_policy_id` set), run `_run`, flip `processing = False`, always
  call `backend_callback_service.report_matchmaking_result` (success or failure).

---

## `app/services/backend_callback_service.py`

`report_matchmaking_result(backend_policy_id, status, ai_policy_id=None,
matched_claim_count=None, generated_claim_count=None, error=None)` — the Flow 2 outbound
call. Best-effort: any failure to reach the backend is logged, never raised or retried
(see `GO_INTEGRATION.md` for why). Skips entirely (warning-logged) if `BACKEND_URL` is
unset.

---

## `app/services/document_extraction.py`

`extract_text(filename, content_type, data) -> str` — PDF (`pypdf`) or Word (`python-docx`)
only, detected by `content_type` or filename extension. Raises
`UnsupportedDocumentTypeError` for anything else.

---

## `app/services/admin_service.py`

F4's two features:

- `get_settings(db)` / `set_threshold(db, over_threshold)` — the singleton
  `AdminSetting` row (creates it with the default `70.0` on first access if missing).
- `generate_demo_existing_claim(db, llm, embedder, topic_hint=None) -> Claim` — fabricates
  `DEMO_CLAIM_POST_COUNT = 6` synthetic posts (`llm.generate_synthetic_posts`), runs each
  through `analyze_and_build_item`, then hands the whole batch to
  `clustering_service.build_claim_from_content_items` — **the same construction pipeline
  real clustering uses**, followed by the same renormalize+rescore step
  `cluster_unclustered_content` would do. This is what backs both `POST
  /claims/generate-generic` (Flow 3, minimal response) and `POST
  /admin/generate-generic-claim` (full detail response) — same function, different
  endpoint wraps its result differently.

---

## `app/services/cib_detector.py`

`detect_coordinated_behavior(posts, embedder=None) -> CIBCheckResponse` — pure,
deterministic, stateless (no DB read/write). For every post pair, flags on 3 independent
signals (each contributes a fixed weight if triggered, `PAIR_FLAG_THRESHOLD = 0.60` to
count as a flagged edge):

| Signal | Threshold | Weight |
|---|---|---|
| Burst timing | posted ≤10 min apart (`BURST_WINDOW_SECONDS`) | 0.35 |
| Text similarity | cosine ≥0.80 (`TEXT_SIMILARITY_THRESHOLD`) | 0.40 |
| Account-creation clustering | accounts created ≤24h apart (`ACCOUNT_CREATION_CLUSTER_SECONDS`) | 0.25 |

Flagged pairs are unioned into clusters via a small `_UnionFind` implementation; each
cluster's score is the mean of its member-pair scores. Overall
`coordination_risk_score = 0.5×(fraction of posts involved in any cluster) +
0.5×(highest single cluster score)`, clamped to `[0,1]`. `is_likely_coordinated` requires
both `overall_score ≥ 0.5` and at least one cluster. Groundwork for F5 — see
`ARCHITECTURE.md`/`GO_INTEGRATION.md` for why this isn't wired to run automatically yet.

---

## `app/core/`

- **`config.py`** — `Settings` (Pydantic Settings, loads `.env`). Every env var this
  service reads lives here, nowhere else — see `SETUP.md` for the full table.
- **`database.py`** — the async engine (`connect_args={"statement_cache_size": 0}` —
  required for Supabase's transaction-mode PgBouncer pooler, which doesn't support
  asyncpg's named prepared statements; harmless against a direct connection too),
  `AsyncSessionLocal`, `get_db()`, `get_session_factory()` (see `ARCHITECTURE.md`).
- **`logging_config.py`** — `configure_logging()` sets up either a JSON formatter
  (Cloud Logging-compatible `severity`/`message`/`timestamp` fields, auto-enabled in
  production) or a human-readable formatter (local dev). `Timer` — a tiny context
  manager used by `main.py`'s request-logging middleware.
- **`security.py`** — `verify_backend_api_key` — the optional `X-API-Key`/`Bearer` check
  on the 2 Go-backend-facing endpoints. No-ops entirely if `AI_SERVICE_API_KEY` is unset.
