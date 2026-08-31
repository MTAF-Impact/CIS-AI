# Go Backend Integration — Full Contract

This is this service's side of the integration documented in the CIS-Backend repo
(`docs/AI-INTEGRATION.md`, `docs/DATABASE.md`, `docs/api/internal.md`). Read those too —
this file expands on them with the actual implementation details on this side: exact
code locations, error handling, retry/idempotency guarantees, and config.

## The model, restated

**The shared Supabase Postgres database is never a write-surface between the two
services — coordination happens exclusively over 3 HTTP calls.**

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
                                 │ the only 3 HTTP calls between the services
                                 ▼
                          this AI service
```

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
purposes), `file_name`, `file_mime_type`, `document_url`.

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
`policy_matchmaking_service._fetch_and_extract`.

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
`SELECT * FROM policies WHERE backend_policy_id = :policy_id`. If found, it **does not
re-run the pipeline at all** — it recomputes `matched_claim_count`
(`COUNT(*) FROM claim_policies WHERE policy_id = <existing ai_policy_id>`) and
`generated_claim_count` (`COUNT(*) FROM claims WHERE policy_id = <existing ai_policy_id>
AND claim_type = 'non_existing'`) from the already-persisted state, and reports those via
Flow 2 with `status: "completed"`. This guarantees zero duplicate `Policy` rows and zero
duplicate generated claims on any retry, no matter how many times the backend calls
this endpoint with the same `policy_id`.

---

## Flow 2 — Reporting the result

**This service → backend.** Always fires after Flow 1's background work finishes —
success, failure, or the idempotent-retry short-circuit above.

```
POST {BACKEND_URL}/api/v1/internal/policies/{policy_id}/matchmaking-result
Content-Type: application/json
X-Internal-Key: {INTERNAL_API_KEY}   (only sent if INTERNAL_API_KEY is set)
```

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
behavior.

Response is deliberately minimal (`claim_id`, `claim_statement`, `topic_id`, `message`)
— matches the backend's documented shape exactly. For a fuller response with the
complete scored detail object in one call, see `POST /admin/generate-generic-claim`
(same underlying pipeline, richer response, used by this service's own admin panel —
not part of the Go contract).

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

## Config (this service's `.env`)

| Var | Default | Purpose |
|---|---|---|
| `BACKEND_URL` | `""` (unset) | Base URL for the Flow 2 outbound callback. Skipped (logged, not fatal) if unset. |
| `AI_SERVICE_API_KEY` | `""` (unset) | If set, `POST /matchmaking/policies` and `POST /claims/generate-generic` require a matching `X-API-Key` or `Authorization: Bearer` header (`app/core/security.py::verify_backend_api_key`). If unset (the current deployment's default), every request is accepted with no header at all — both services are assumed reachable only over a private network. |
| `INTERNAL_API_KEY` | `""` (unset) | Sent as `X-Internal-Key` on the Flow 2 callback, only if set. Must match whatever the backend's own `INTERNAL_API_KEY` expects (currently unset on the backend too). |

Set either pair of keys on **both** sides together if the network boundary ever stops
being private-only.

---

## Coordinated-Network Detector (F5) — a 4th touchpoint, and the one read exception

F5 is built (PRD v1.4 §10), scoped per the backend integration doc's ownership split
(`AI_REQUIREMENT_FOR_INTEGRATION_SUMMARY_V1.md`, section G): this service keeps only
the detection pipeline + its 9 output tables + one trigger endpoint. Everything
human-facing (network list/detail/review, allowlist CRUD, PDF/ZIP reports, F4 config,
export audit log) is the backend's — it reads the 9 tables directly, same as every
other AI-owned table above.

- **`POST /coordination/detection-runs`** (a 4th HTTP touchpoint beyond the 3 flows
  above) — the backend calls this whenever it decides to run detection (its own
  schedule, its own velocity watch, or an analyst's on-demand click); this service
  just runs the pipeline. See `docs/COORDINATION.md` for the request shape.
- **The one exception to "never a write-surface... between the two services" above,
  in the opposite direction**: this service *reads* the backend-owned
  `cis_coordination_allowlist` table (read-only, this table only) before candidate
  selection, so declared-legitimate coordination stays excluded. Column names
  assumed on this side (`handle`, `removed_at`) are a placeholder pending
  confirmation of the actual DDL — see `COORDINATION.md`'s data-model section.
- The old `POST /coordination/check-cib` stateless heuristic (posts supplied directly
  in the request, no DB read/write) still exists, predates the real pipeline, and is
  unrelated to it — not retired, just superseded.

## What's explicitly out of scope / deferred

- **`cis_policies.file_path`:** this service never reads or needs to know the backend's
  own permanent storage path for the uploaded document — it only ever consumes the
  time-limited `document_url` given in the Flow 1 request, once, immediately.
