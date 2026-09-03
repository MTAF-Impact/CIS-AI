# Go Backend Integration — Full Contract

This is this service's side of the integration documented in the CIS-Backend repo
(`docs/AI-INTEGRATION.md`, `docs/DATABASE.md`, `docs/api/internal.md`). Read those too —
this file expands on them with the actual implementation details on this side: exact
code locations, error handling, retry/idempotency guarantees, and config.

## The model, restated

**The shared Supabase Postgres database is never a write-surface between the two
services — coordination happens exclusively over HTTP.** That HTTP surface has grown
past this diagram's original "3 calls" framing as F4/F5/v1.5 landed; see the touchpoint
table right after it for the current, accurate count (8 backend → AI endpoints, 2 AI →
backend callbacks, plus the health probe).

```
                      ┌──────────────────────┐
   social media ─────▶│  this AI service      │
   (future crawler)   │  detect · cluster ·    │
                      │  score · generate      │
                      └──────────┬───────────┘
                                 │ owns / writes
                                 ▼
                   ┌───────────────────────────────┐
                   │   Supabase Postgres            │
                   │                                 │
                   │  AI-owned          backend-owned│
                   │  claims            cis_users     │
                   │  topics            cis_policies  │
                   │  policies          cis_claim_*   │
                   │  content_items     cis_settings  │
                   │  claim_policies                  │
                   │  fault_lines, official_sources    │
                   │  topic_volume_buckets              │
                   └───────────┬───────────────────────┘
                               │ backend: SELECT only from AI tables
                               │ AI service: NEVER touches cis_* tables
                               ▼
                      ┌──────────────────────┐
   frontend ◀────────▶│   Go backend          │
                      └──────────┬───────────┘
                                 │ every touchpoint below - no other channel
                                 ▼
                          this AI service
```

### Every touchpoint, current as of this doc

| # | Flow | Direction | Endpoint |
|---|---|---|---|
| 1 | Policy matchmaking (US42) | Backend → AI | `POST /api/v1/matchmaking/policies` |
| 2 | Matchmaking result | AI → Backend | `POST {BACKEND_URL}/.../matchmaking-result` (callback) |
| 3 | Generate Generic Claim (US33) | Backend → AI | `POST /api/v1/claims/generate-generic` |
| 4 | Harm confirmation | Backend → AI | `PATCH /api/v1/claims/{id}/harm/confirm` |
| 5 | Score re-evaluation | Backend → AI | `POST /api/v1/claims/rescore` |
| 6 | Sample content generation | Backend → AI | `POST /api/v1/ingest/generate-synthetic` |
| 6b | Cluster now | Backend → AI | `POST /api/v1/claims/cluster-now` |
| 7 | Detection run (F5) | Backend → AI | `POST /api/v1/detection/runs` |
| 8 | Evidence snapshot purge (F5) | Backend → AI | `POST /api/v1/detection/snapshots/purge` |
| — | Exclusion lists (F5), optional pull | AI → Backend | `GET {BACKEND_URL}/api/v1/internal/detection/exclusions` — the exclusions already travel inline on Flow 7's request body, so this is a fallback, not a mandatory call |
| — | Health probe | Backend → AI | `GET /health` |

Numbering matches the backend's own `docs/AI-INTEGRATION.md` flow table exactly, so a
flow number means the same thing in either repo. **8 endpoints the backend calls on this
service, 1 callback this service calls back on the backend, 1 optional pull in the same
reverse direction, plus the health probe** — not the "3" (nor the later "5") this doc
used to say before Flow 4/5/6/6b and F5's two endpoints were folded into the table above.

**This service owns and exclusively writes:** `claims`, `content_items`, `topics`,
`policies`, `claim_policies`, `topic_volume_buckets`, `fault_lines`, `official_sources`.
Full schema: `DATA_MODEL.md`.

**This service never writes any `cis_*` table.** An earlier version of this
integration (before the contract above was documented) dual-wrote directly into
`cis_claim_reviews`/`cis_claim_alerts`/`cis_claim_score_snapshots`/`cis_settings` — that
was wrong and has been fully removed (`app/services/go_backend_sync.py` no longer
exists). It specifically violated the backend's own stated design intent:
`cis_claim_reviews` is deliberately the backend's own reviewer-status overlay, so a
pipeline re-run on this side can never silently overwrite a human's decision. If you're
reading old context (chat history, an old commit) that mentions dual-writing into
`cis_*` tables, it's stale — ignore it.

`scripts/reset_schema.py` on this side explicitly excludes any table prefixed `cis_`
from its drop step, so a schema reset here can never destroy the backend's tables
either — this is enforced in both directions, not just documented.

---

## Flow 1 — Policy matchmaking (US42)

**Backend → this service.** Fires when an operator uploads a policy through F2.

```
POST {AI_SERVICE_URL}/api/v1/matchmaking/policies
Content-Type: application/json
X-API-Key: {AI_SERVICE_API_KEY}          (optional, see Config below)
Authorization: Bearer {AI_SERVICE_API_KEY}  (optional, same key, either header works)
```

Implementation: `app/api/v1/endpoints/matchmaking.py` →
`app/services/policy_matchmaking_service.py::run_matchmaking_webhook`.

### Request body

See `API_REFERENCE.md`'s Matchmaking section for the exact field table. In short:
`policy_id` (the backend's `cis_policies.id` — **not** this service's own `Policy.id`),
`name`, `description`, `rolled_out_date`, `status` (informational, ignored for scoring
purposes), `file_name`, `file_mime_type`, `document_url`, plus two fields covered under
Idempotency below: `force` (bool, default false) and `callback_url` (optional, see
Flow 2).

`document_url` is a **time-limited signed URL** (Supabase Storage, default ~1h
validity). This service fetches it with a plain `httpx.AsyncClient.get()` —
`follow_redirects=True` is set, since some signed-URL/CDN flows redirect to the actual
object location. No special auth header is sent; the signature is expected to already be
embedded in the URL's query string, exactly how Supabase Storage signed URLs work.
30-second fetch timeout (`DOCUMENT_FETCH_TIMEOUT_SECONDS`).

If `document_url` is absent, unreachable, times out, or the fetched bytes aren't a
supported type (PDF/`.docx`), this service **does not fail the request** — it proceeds
working from `name`/`description` alone, per the backend's own documented fallback
("work from the name alone"). This is a best-effort try/except in
`policy_matchmaking_service._fetch_and_extract`, split into two independent halves: a
fetch failure loses the file entirely (nothing downloaded to keep), an *extraction*
failure keeps the downloaded bytes and drops only the text. The extraction half is
deliberately broad (any exception from `extract_text`, not just the expected
"unsupported file type" case) — a real incident showed why: an AES-encrypted PDF (a
common permission-only scheme for government documents, no password needed to read)
made `pypdf` raise `DependencyError` because the `cryptography` package it needs
wasn't installed. That's now a real dependency, so AES-encrypted PDFs extract
normally; a narrower failure mode (this or anything else `extract_text` might throw)
degrades to "no extracted text" instead of crashing the whole Flow 1 job before the
`Policy` row is even created — which is what happened, and which looks indistinguishable
from the request never arriving at all (no row, no error, no Flow 2 callback).

### Response

**Always `202 {"status": "processing"}`, immediately, before any real work happens.**
The actual matchmaking pipeline runs afterward as a `BackgroundTasks` job — the real
result is reported later via Flow 2, never in this response.

### What happens in the background

1. A new `Policy` row is created on this side (`policies` table), with
   `backend_policy_id` set to the incoming `policy_id` — this is the correlation key for
   idempotency (see below). **This service's own `Policy.id` becomes `ai_policy_id`** in
   the Flow 2 callback.
2. The policy is embedded (`title + description + extracted_text[:4000]`).
3. **Existing-claim matching:** every `existing` claim with a non-null embedding is
   cosine-scored against the policy; the top 20 candidates scoring ≥ 0.35 are sent to the
   LLM in one batched call (`confirm_policy_claim_matches`) asking "is this claim
   genuinely about this policy, not just topically adjacent?". Confirmed matches get a
   `claim_policies` row.
4. **Non-existing claim prediction:** exactly one new predicted claim is generated
   (`predict_non_existing_claim`), explicitly told which existing claims were already
   matched so it predicts something *not* already covered.
5. `Policy.processing` flips to `false`.
6. The Flow 2 callback fires — always, whether steps 2–4 succeeded or raised.

Any exception in steps 2–4 is caught, logged, and turned into `status: "failed"` +
`error: str(exc)` on the Flow 2 callback rather than propagating (a background task has
no HTTP response to fail).

### Idempotency

**Required by the backend's own contract** (`docs/api/internal.md`: "Make the endpoint
idempotent for a given `policy_id` — do not duplicate synthetic claims on a retry"),
because the backend retries a failed matchmaking up to 3× via a daily job, and an
operator can trigger `POST /policies/:id/rematch` manually.

Implementation: before creating anything, `run_matchmaking_webhook` looks up
`SELECT * FROM policies WHERE backend_policy_id = :policy_id`. What happens next
depends on `force` and whether the previous run actually succeeded:

- **No existing row** → normal path: create a new `Policy`, run the pipeline.
- **Existing row, `force` is false, and the previous run genuinely succeeded**
  (`policies.last_matchmaking_error IS NULL`) → **short-circuit**, no re-run.
  Recomputes `matched_claim_count`
  (`COUNT(*) FROM claim_policies WHERE policy_id = <existing ai_policy_id>`) and
  `generated_claim_count` (`COUNT(*) FROM claims WHERE policy_id = <existing
  ai_policy_id> AND claim_type = 'non_existing'`) from the already-persisted state
  and reports those via Flow 2 with `status: "completed"` — this is what makes the
  backend's retry sweep cheap when the only thing that was actually lost was the
  Flow 2 report itself.
- **Existing row, and either `force` is true OR the previous run failed**
  (`last_matchmaking_error` is set) → **re-run against the same row.** `Policy.id`
  (and therefore `ai_policy_id`) never changes — the pipeline supersedes the prior
  `claim_policies` links and predicted claim rather than duplicating them
  (`DELETE ... WHERE policy_id = :id` on both, before re-running). This is what lets
  a failed run actually recover, and lets `PUT /policies/:id/file` (a replaced
  document) re-match against the new content instead of silently reporting the old
  result forever.

`last_matchmaking_error` (nullable text on `policies`) is the field that makes the
second and third cases distinguishable: `NULL` means "never run, or the last run
succeeded"; anything else is the last failure's message, and its mere presence is
what triggers an automatic re-run even without `force`. It's set in a `finally`
block on every run, success or failure, so it's never stale.

This guarantees zero duplicate `Policy` rows on any retry, whether or not it
re-runs — the correlation is always `backend_policy_id`, one row per, forever.

---

## Flow 2 — Reporting the result

**This service → backend.** Always fires after Flow 1's background work finishes —
success, failure, or the idempotent-retry short-circuit above.

```
POST {callback_url, or BACKEND_URL/api/v1/internal/policies/{policy_id}/matchmaking-result}
Content-Type: application/json
X-Internal-Key: {INTERNAL_API_KEY}   (only sent if INTERNAL_API_KEY is set)
```

If Flow 1's request body carried a `callback_url`, that full URL is used as-is and
preferred over `BACKEND_URL` — this is what lets one AI deployment serve multiple
backend environments (staging/production) without either one hardcoded on this side.
Falls back to `BACKEND_URL` + the path above when `callback_url` is absent.

Implementation: `app/services/backend_callback_service.py::report_matchmaking_result`.

### Body

| Field | Sent when |
|---|---|
| `status` | Always — `"completed"` or `"failed"` (this service never sends `"processing"` here; that's covered by Flow 1's own `202` ack). |
| `ai_policy_id` | Always, except on the rare path where the initial `Policy` row creation itself failed (e.g. a DB outage) before an id even existed. |
| `matched_claim_count` | Always. |
| `generated_claim_count` | Always. |
| `error` | Only when `status: "failed"` — the exception message, truncated to 2000 chars. |

### Failure handling — this callback is best-effort

If the callback itself fails to reach the backend (network error, backend down, `4xx`/`5xx`
response), this is **logged and swallowed, never retried by this service**. The reasoning:
the matchmaking work already happened and succeeded/failed on its own terms — that
shouldn't be treated as failed just because the *report* didn't land. The backend's own
retry job (up to 3× daily) and the operator-triggered rematch endpoint exist precisely to
cover exactly this case: if the backend never received a callback, its `cis_policies` row
stays in `processing_status: "pending"` past its own timeout expectations, and it will
retry Flow 1 again — which this service's idempotency handling (above) then answers
correctly, re-sending the already-computed result.

If `BACKEND_URL` is unset, the callback is skipped entirely (logged as a warning) rather
than raising — this lets local dev / a standalone deployment run Flow 1 without a
backend to call back to.

---

## Flow 3 — Generate Generic Claim (US33)

**Backend → this service.** The F4 test/demo button — the backend can't satisfy this
itself since it never writes to `claims`.

```
POST {AI_SERVICE_URL}/api/v1/claims/generate-generic
X-API-Key: {AI_SERVICE_API_KEY}   (optional)
```

Full request/response shape: `API_REFERENCE.md`. Implementation:
`app/api/v1/endpoints/claims.py::generate_generic_claim_webhook` →
`app/services/admin_service.py::generate_demo_existing_claim`, which runs a small
LLM-fabricated post cluster through the **exact same** construction + scoring pipeline
real HDBSCAN clustering uses (`clustering_service.build_claim_from_content_items`) —
never a separate "fake claim" path that could drift out of sync with real scoring
behavior. The generated claim is also linked to a `Policy` (`claim_policies`) —
nearest by embedding cosine similarity among policies that have one, else the most
recently created `Policy`, no-op only if no `Policy` exists at all — so the F1 detail
page's Related Policies panel is populated for demo claims too, not left empty.

Response is deliberately minimal (`claim_id`, `claim_statement`, `topic_id`, `message`)
— matches the backend's documented shape exactly. For a fuller response with the
complete scored detail object in one call, see `POST /admin/generate-generic-claim`
(same underlying pipeline, richer response, used by this service's own admin panel —
not part of the Go contract).

---

## Flow 4 — Harm confirmation

**Backend → this service.** An analyst on the F1 detail page confirms or overrides the
AI-classified Harm sub-scores (US23). The backend proxies this immediately rather than
applying it itself — `harm_*`, `harm_human_confirmed`, and every score derived from them
live on this service's `claims` table.

```
PATCH {AI_SERVICE_URL}/api/v1/claims/{claim_id}/harm/confirm
```

```json
{ "public_safety": 90.0, "institutional_trust": null, "economic": null, "policy_disruption": null }
```

All four fields optional, `0.0–100.0` each (`HarmConfirmRequest`). An omitted field keeps
the AI's own classification; an empty body is the legitimate "I reviewed these and
they're right" case — it still flips `harm_human_confirmed` to `true`.

Implementation: `app/api/v1/endpoints/claims.py::confirm_harm`. Recomputes `harm_score`
(`scoring_engine.harm_score`) from the (possibly partial) new sub-scores, then calls the
same `rescore_claim` every other path uses to cascade
`claim_score → discount_factor → final_claim_score` and append a `claim_score_snapshots`
row. Returns the full `ExistingClaimDetailRead`; the backend documents that it ignores
this body and re-reads the claim itself, so the response shape only matters to this
service's own consumers.

**404** for an unknown `claim_id` or a Synthetic (`non_existing`) claim — the lookup is
scoped to Existing claims only, since Synthetic claims carry no harm scores to confirm.

---

## Flow 5 — Score re-evaluation

**Backend → this service.** A claim's score moves with wall-clock time even when
nothing new is ingested — NPR drifts as Opposing posts age out of the rolling window.
Nothing on this service's side re-evaluates that on a schedule (no cron here), so the
backend's hourly snapshot job calls this first and captures the result immediately after.

```
POST {AI_SERVICE_URL}/api/v1/claims/rescore
```

No request body. Response: `{ "claims_rescored": 4 }`.

Implementation: `app/api/v1/endpoints/claims.py::rescore` →
`clustering_service.rescore_all_existing_claims` — renormalizes Reach per topic, then
rescores every Existing claim in it (NPR, discount, `final_claim_score`), each appending
its own `claim_score_snapshots` row. Also exposed identically as `POST /admin/rescore`
for this service's own admin panel.

---

## Flow 6 — Sample content generation / clustering

**Backend → this service**, from the F4 "Generate sample data" button. Until a live
crawler exists, this is the only way `content_items` — and therefore Existing claims —
come into being outside of Flow 1's predicted claims and Flow 3's single demo claim.

```
POST {AI_SERVICE_URL}/api/v1/ingest/generate-synthetic
```

```json
{ "count": 10, "topic_hint": "road pricing", "auto_cluster": true }
```

All fields optional (defaults: 10 items, auto-clustered); `count` capped at 50.
Implementation: `app/api/v1/endpoints/ingestion.py::generate_synthetic_ingest` —
fabricates `count` realistic posts via `llm.generate_synthetic_posts`, runs them through
the normal analyze → embed → persist pipeline, then (if `auto_cluster`) clusters
synchronously rather than waiting for the next pass. This is why the backend documents
this call on its long timeout budget, not the short one.

**Flow 6b — `POST /api/v1/claims/cluster-now`** — the same clustering step alone, for
content that was ingested but whose background clustering pass hasn't run yet
(`app/api/v1/endpoints/claims.py::cluster_now`). No request body.

---

## Flow 7 & 8 — Coordinated-Network Detector (F5), no shared-table read

F5 is built (PRD v1.4 §10), scoped and verified against the backend's actual merged
code (`CIS-Backend` `main`, commit `910cd82`, pulled and reviewed this session) —
not an earlier guessed contract. This service owns the detection pipeline + 10
output tables + two endpoints. Everything human-facing (network list/detail/review,
allowlist CRUD, PDF/ZIP reports, F4 config, export audit log) is the backend's — it
reads the 10 tables directly, same as every other AI-owned table above.

- **`POST /api/v1/detection/runs`** (Flow 7) and **`POST /api/v1/detection/snapshots/purge`**
  (Flow 8) — the two touchpoints counted in the table at the top of this doc — the backend
  calls the first whenever it decides to run detection (its own schedule, its own
  velocity watch, or an analyst's on-demand click) and the second whenever it's
  computed which networks are past retention. See `docs/COORDINATION.md` for both
  request shapes.
- **No shared-table read after all**: an earlier design had this service read a
  `cis_coordination_allowlist` table directly (the one place the read direction
  would have reversed). The backend's actual contract sends the allowlist +
  common-phrase exclusions inline in the detection-run request instead — simpler,
  and it means the "backend `SELECT`s your tables, never writes them, and you never
  read theirs" rule above holds with **zero** exceptions in either direction.
- The old `POST /coordination/check-cib` stateless heuristic (posts supplied directly
  in the request, no DB read/write) still exists, predates the real pipeline, and is
  unrelated to it — not retired, just superseded.

---

## `claim_type` vocabulary

This service's `ClaimType` enum values are `existing` / `non_existing` — these already
match the backend's canonical values exactly (`NormalizeClaimType` on the backend's
side accepts several aliases, but the canonical/emitted values from this service need no
translation). See `DATA_MODEL.md`'s enum table.

---

## Score value ranges — what the backend can assume

Every field the backend reads is already clamped to its documented range before being
written — the backend "clamps values defensively on output... but does not correct them"
per its own docs, i.e. it trusts this service to send values already in range:

| Field(s) | Range |
|---|---|
| `reach_score`, `velocity_score`, `falseness_score`, `harm_score`, `emotional_intensity_score`, `emotional_intensity_opposing`, `claim_score`, `final_claim_score` | 0–100 (or `null`) |
| `npr` | 0–1 (or `null`, when dormant) |
| `discount_factor` | 0.5–1 |
| Every `harm_*` sub-score | 0–100 |

**Synthetic (non-existing) claims are unscored** — every score column stays `NULL`.
`activity_content`/`activity_generated_at` are generated exactly once and cached
forever; the backend should never expect these to change on a re-fetch of an
already-generated claim.

---

## PRD v1.5 additions — asks #8 and #9 from `docs/AI-INTEGRATION.md`

Both implemented. Same "generated once, cached forever" rule as `activity_content`
above applies to both — neither is ever regenerated on a re-fetch.

**`content_items.sentiment`** (ask #8) — `positive` / `negative` / `neutral`, set by
`LLMClient.analyze_content()` at ingestion time, on every ingestion path (single,
batch, and synthetic all funnel through `content_ingestion_service.build_content_item`).
`NULL` only for rows ingested before this shipped. **Not derived from `stance`** — see
`app/models/enums.py::Sentiment`'s docstring; the two axes are assessed independently
and must never be conflated, per the backend's own note in `AI-INTEGRATION.md`.

**`claim_debunk_segments`** (ask #9) — one row per audience segment, generated
alongside `activity_content` (same call site, same once-only guard) from the claim's
Supporting-side sample. `LLMClient.generate_debunk_segments()` returns 1–5 segments,
deduped then capped to `ai.debunk_segment_max_count` (dynamic via `cis_settings`,
defaults to `3` — see `AI_DYNAMIC_PARAMETER.md` AP-21); `rank` is assignment order
(most-exposed first, per the LLM's own ordering, preserved through the cap). Flow 3's
demo claim populates this too, since it goes through the same
`clustering_service.build_claim_from_content_items` construction path. On any
generation failure the row set is simply empty and `activity_content` still exists —
matches the fallback the backend already documents.

Schema matches `docs/sql/02_f6_reference_schema.sql` from the backend repo exactly
(`app/models/debunk_segment.py`), so no sign-off round-trip should be needed on the
DDL itself.

Deliberately not implemented yet: **`content_items.city`** (ask #10) — deferred until
a second city is actually configured; today's single-city Jakarta deployment gets no
benefit from partitioning on a column that would only ever hold one value.

---

## Config (this service's `.env`)

| Var | Default | Purpose |
|---|---|---|
| `BACKEND_URL` | `""` (unset) | Base URL for the Flow 2 outbound callback. Skipped (logged, not fatal) if unset. |
| `AI_SERVICE_API_KEY` | `""` (unset) | If set, `POST /matchmaking/policies`, `POST /claims/generate-generic`, `POST /detection/runs`, and `POST /detection/snapshots/purge` require a matching `X-API-Key` or `Authorization: Bearer` header (`app/core/security.py::verify_backend_api_key`). If unset (the current deployment's default), every request is accepted with no header at all — both services are assumed reachable only over a private network. Flow 4/5/6/6b are not gated by this dependency at all yet, regardless of this setting — see their sections above. |
| `INTERNAL_API_KEY` | `""` (unset) | Sent as `X-Internal-Key` on the Flow 2 callback, only if set. Must match whatever the backend's own `INTERNAL_API_KEY` expects (currently unset on the backend too). |

Set either pair of keys on **both** sides together if the network boundary ever stops
being private-only.

---

## What's explicitly out of scope / deferred

- **`cis_policies.file_path`:** this service never reads or needs to know the backend's
  own permanent storage path for the uploaded document — it only ever consumes the
  time-limited `document_url` given in the Flow 1 request, once, immediately.
