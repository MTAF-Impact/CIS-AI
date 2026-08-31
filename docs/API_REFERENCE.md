# API Reference

Base URL: `{AI_SERVICE_URL}/api/v1` (local dev: `http://localhost:8000/api/v1`).
Interactive Swagger UI (auto-generated from the exact same Pydantic schemas documented
here): `{AI_SERVICE_URL}/docs`. OpenAPI JSON: `{AI_SERVICE_URL}/openapi.json`.

**Auth:** none by default — this deployment assumes every caller reaches the AI service
over a private network. `POST /matchmaking/policies`, `POST /claims/generate-generic`,
`POST /ingest`, and `POST /ingest/batch` optionally accept `X-API-Key: <AI_SERVICE_API_KEY>`
or `Authorization: Bearer <AI_SERVICE_API_KEY>` if that env var is ever set — see
`GO_INTEGRATION.md`. Every other endpoint is fully open. `/ingest/generate-synthetic` is
deliberately excluded (manual/demo use, not an automated caller).

**Content type:** `application/json` for every request/response body except `POST
/policies` (`multipart/form-data`, file upload) and `GET /policies/{id}/file` (returns
the raw file bytes with its original `Content-Type`).

**Errors:** FastAPI's default shape, `{"detail": "..."}` for a single message or
`{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}` for Pydantic validation
errors (`422`). Status codes used throughout: `200` (read/update OK), `201` (created),
`202` (accepted, async work queued), `404` (not found), `422` (validation failed —
either Pydantic schema validation or a business-rule rejection), `502` (upstream LLM
returned an unusable result), `503` (`OPENAI_API_KEY` not configured).

**Pagination:** every list endpoint takes `limit`/`offset` and returns `total` (the full
matching count, independent of the page returned) so the caller can render "showing X of
Y" / build a pager without a second request.

---

## Health

### `GET /health` (and `GET /api/v1/health`)

Liveness check. No params, no DB touch.

**200:**
```json
{"status": "ok", "service": "CIS AI Service", "env": "production"}
```

---

## Ingestion

The entry point for raw content — what a live crawler/scheduler will eventually feed.
**All 3 endpoints below auto-trigger clustering in the background after a successful
write** (fire-and-forget `BackgroundTasks`, no polling needed on the caller's part — new
claims simply appear in `GET /claims/existing` shortly after, same "no separate manual
step" pattern as F2's matchmaking).

### `POST /ingest`

Ingest one piece of content: runs it through LLM analysis (which also returns an English
translation), embeds the translation, persists it. **Auth:** optional `X-API-Key`/`Bearer`
(see top of this doc) — this and `/ingest/batch` are the ones a crawler hits.

**Request body** (`ContentItemCreate`):

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `text` | string | yes | `min_length=1` | Original language - translated internally, not by the caller. |
| `source` | string enum | no | `social`\|`rss`\|`radio`\|`forum`\|`other` | Default `other`. |
| `author_id` | string \| null | no | | |
| `location` | string \| null | no | | |
| `impressions` | int \| null | no | | Feeds Reach (R). |
| `positive_reaction_count` | int \| null | no | | Feeds Emotional Intensity (EI). |
| `negative_reaction_count` | int \| null | no | | Feeds Emotional Intensity (EI). |
| `external_ref` | string \| null | no | `max_length=512` | Dedup key for automated sources (e.g. `"telegram:<channel_id>:<message_id>"`). If omitted, no dedup applies to this item. |

**201** (`ContentItemRead`):
```json
{
  "id": "uuid", "text": "...", "text_en": "...", "source": "social", "author_id": null,
  "location": "Sudirman", "outrage_score": 0.62, "moral_foundation": "fairness",
  "extracted_claim": "The ERP charge is a hidden tax", "underlying_grievance": "cost-of-living anxiety",
  "stance": null, "impressions": null, "positive_reaction_count": null, "negative_reaction_count": null,
  "external_ref": null, "claim_id": null, "created_at": "2026-08-31T10:00:00Z"
}
```
`stance` and `claim_id` are always `null` at ingest time — stance is only assessable
once a claim exists, and clustering runs asynchronously after this response returns.

**Idempotent on `external_ref`:** if a non-null `external_ref` was already ingested, this
returns the **existing** item (still `201`, not an error) instead of creating a duplicate
or spending another LLM call.

**503:** `{"detail": "OPENAI_API_KEY is not configured..."}` if no LLM key is set.

### `POST /ingest/batch`

Same as above, N items in one call. **Dedup runs first** — any item whose `external_ref`
already exists is skipped *before* it costs an LLM call; the rest are analyzed
concurrently, then embedded in one batched call. **Auth:** same optional key as above.

**Request body** (`ContentItemBatchCreate`): `{"items": [ContentItemCreate, ...]}` (`min_length=1`).

**201** (`ContentItemBatchResult`):
```json
{
  "created": [ContentItemRead, ...],
  "failed": [{"text": "...", "error": "..."}],
  "skipped": ["telegram:chan:123"]
}
```
`skipped` lists the `external_ref`s that already existed (no LLM call spent on them).
Per-item analysis failures (e.g. one item's LLM call errors) don't abort the whole batch
— they land in `failed` instead, and everything else still commits.

### `POST /ingest/generate-synthetic`

**Prototype-only.** Live crawling isn't wired up for this deployment yet — this fabricates
realistic Jakarta posts via the LLM and runs them through the exact same
embed→analyze→persist pipeline real content would. Intended for an on-demand "Generate
sample data" button, not production traffic.

**Request body** (`SyntheticIngestRequest`):

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `count` | int | no | `1 ≤ count ≤ 50` | Default `10`. |
| `topic_hint` | string \| null | no | `max_length=255` | Optional steer; blank = the LLM's judgement across realistic Jakarta topics. |
| `auto_cluster` | bool | no | | Default `true`. If `true`, clustering runs **synchronously** before this call returns (unlike `/ingest`'s background trigger) so the response can report resulting claim counts immediately. |

**201** (`SyntheticIngestResult`):
```json
{
  "generated": [ContentItemRead, ...], "failed": [],
  "claims_created": 2, "claims_updated": 0, "content_items_clustered": 10
}
```
The 3 `claims_*` fields are `null` if `auto_cluster` was `false`.

**502:** if the LLM returns zero posts. **422:** `count` out of `[1, 50]`. **503:** no LLM key.

---

## Fault Lines

### `GET /fault-lines`

Read-only listing of every `fault_lines` row. No params, no auth. This is the "living
exemplar corpus" a relevance-filtering crawler is expected to fetch each run (compare a
candidate post's embedding against these via cosine similarity, rather than keyword
matching) — see `ARCHITECTURE.md`.

**200:**
```json
[{
  "id": "uuid", "community_name": "Kampung Pulo",
  "grievance_theme": "Historical eviction distrust (Ciliwung normalization)",
  "description": "...", "created_at": "2026-08-31T00:56:10Z"
}]
```

---

## Claims — Existing (D1: ranked, scored)

### `GET /claims/existing`

**Query params:**

| Param | Type | Notes |
|---|---|---|
| `topic_ids` | `uuid[]` (repeat the param) | Multi-select. 2+ topics are merged into one pool and ranked once — not top-N-per-topic. |
| `status` | string enum \| null | `unreviewed`\|`active`\|`inactive`\|`action_taken` |
| `q` | string \| null | `ILIKE '%q%'` against `claim_statement`. |
| `limit` | int | Default `10`, max `1000`. |
| `offset` | int | Default `0`. |

Sorted by `final_claim_score DESC NULLS LAST`.

**200** (`ClaimListEnvelope`):
```json
{
  "fetched_at": "2026-08-31T10:00:00Z",
  "total": 4,
  "items": [
    {
      "id": "uuid", "claim_type": "existing", "claim_statement": "...",
      "topic": {"id": "uuid", "name": "Road Pricing & Transit"},
      "status": "unreviewed", "first_caught_at": "2026-08-30T08:00:00Z",
      "positive_statement_count": 5, "negative_statement_count": 2,
      "final_claim_score": 62.4, "is_alerted": false
    }
  ]
}
```
`fetched_at` is wall-clock at response time — satisfies the "last fetched" UI requirement
with zero server-side state to maintain.

### `GET /claims/non-existing`

Identical shape and query params to the above, but: sorted by `created_at DESC`
(most-recently-predicted first, not by score — non-existing claims are never scored),
and every card's `final_claim_score` is always `null`, `is_alerted` is always `false`
(F3's watchlist is existing-claims-only).

### `GET /claims/{claim_id}`

Returns one of two shapes depending on `claim_type` — check the `claim_type` field in
the response to discriminate.

**200, `claim_type: "existing"`** (`ExistingClaimDetailRead`) — every score component
individually, never just the collapsed number:
```json
{
  "id": "uuid", "claim_type": "existing", "claim_statement": "...",
  "topic": {"id": "uuid", "name": "..."}, "status": "unreviewed",
  "first_caught_at": "...", "created_at": "...", "updated_at": "...",

  "reach_score": 71.2, "velocity_score": 55.0, "falseness_score": null,
  "harm_score": 48.3,
  "harm_public_safety": 40.0, "harm_institutional_trust": 50.0,
  "harm_economic": 30.0, "harm_policy_disruption": 20.0, "harm_human_confirmed": false,
  "emotional_intensity_score": 62.0, "emotional_intensity_opposing": 30.5,
  "claim_score": 58.9, "npr": 0.15, "discount_factor": 0.925, "final_claim_score": 54.5,
  "is_dormant": false, "is_alerted": true,

  "activity_content": "Full concatenated Debunk text...",
  "activity_generated_at": "...",
  "debunk_core_fact": "The true fact stated plainly...",
  "debunk_nuanced_flag": "A claim suggesting otherwise has circulated...",
  "debunk_reiterated_fact": "The fact restated...",

  "top_accounts": [{"account_handle": "@user_ax7", "contribution_count": 4}],
  "supporting_statements": [ContentItemRead, ...],
  "opposing_statements": [ContentItemRead, ...],
  "neutral_statements": [ContentItemRead, ...],
  "policies": [{"id": "uuid", "title": "..."}]
}
```
See `SCORING.md` for what every score field means and its valid range.
`falseness_score: null` is expected — the reference corpus (`official_sources`) is
currently empty in this deployment.

**200, `claim_type: "non_existing"`** (`NonExistingClaimDetailRead`) — no score fields at
all:
```json
{
  "id": "uuid", "claim_type": "non_existing", "claim_statement": "...",
  "topic": {"id": "uuid", "name": "..."}, "status": "unreviewed",
  "first_caught_at": "...", "created_at": "...", "updated_at": "...",
  "policy": {"id": "uuid", "title": "..."},
  "activity_content": "The Prebunk explainer text...",
  "activity_generated_at": "..."
}
```

**404:** unknown `claim_id`.

### `PATCH /claims/{claim_id}/status`

**Request body:** `{"status": "active"}` (any `ClaimStatus` value). Applies to both
claim types — one shared status set, no type-specific business rule.

**200:** the full detail object (existing or non-existing shape, per above), reflecting
the new status. **404:** unknown `claim_id`.

### `PATCH /claims/{claim_id}/harm/confirm`

**Existing claims only.** Human confirms/overrides the AI-classified Harm sub-scores;
recomputes `harm_score` → `claim_score` → `final_claim_score` from the result and
appends a new `claim_score_snapshots` row.

**Request body** (`HarmConfirmRequest`, all fields optional, `0.0–100.0` each): any
provided field overrides the AI value; omitted fields keep the AI's original
classification.
```json
{"public_safety": 90.0, "institutional_trust": null, "economic": null, "policy_disruption": null}
```

**200:** `ExistingClaimDetailRead` with `harm_human_confirmed: true` and the
recomputed scores. **404:** unknown `claim_id`, or the claim is `non_existing`.

### `POST /claims/{claim_id}/alert` / `DELETE /claims/{claim_id}/alert`

Bell-icon add/remove. **Existing claims only.**

**POST 201 / DELETE 200** (`ClaimListItemRead`, `is_alerted` reflecting the new state).

**POST 422:** the claim is `non_existing` (`"Only Existing claims can be alerted"`).
**404:** unknown `claim_id` (both verbs). Adding an already-alerted claim, or removing a
non-alerted one, is a no-op that still returns `200`/`201` with the correct current state
— not an error.

### `POST /claims/cluster-now`

Manually trigger an immediate clustering pass over any not-yet-clustered `content_items`.
Normally unnecessary — ingestion already auto-triggers this in the background — but
useful to force a pass without waiting, or after an ingest whose background task hasn't
finished yet.

**200** (`ClusterNowResponse`): `{"claims_created": 2, "claims_updated": 1, "content_items_clustered": 8}`.

### `POST /claims/rescore`

Time-based NPR/Velocity/discount/final re-evaluation for **every** existing claim,
independent of clustering. Necessary because NPR can drift purely from wall-clock time
(old Opposing posts aging out of the rolling window) with zero new content ingested.

**200** (`RescoreResponse`): `{"claims_rescored": 4}`.

---

## Claims — Non-Existing prediction (D2)

### `POST /claims/non-existing/predict`

Manual/ad-hoc trigger for an already-registered policy. **The automatic path is F2's AI
matchmaking pipeline**, which runs this exact same underlying logic on policy creation
without needing this endpoint — use this only to force an additional prediction pass on
a policy that's already been through matchmaking once.

**Request body:** `{"policy_id": "uuid"}` — must reference an existing `policies.id`.

**201** (`NonExistingClaimPredictResponse`):
```json
{
  "claim": NonExistingClaimDetailRead,
  "predicted_attack_angle": "Fear of income loss for informal workers",
  "likely_framing": "government overreach"
}
```
`predicted_attack_angle`/`likely_framing` are LLM reasoning context, useful to an
analyst — they are **not** persisted anywhere on the `Claim` row, only returned in this
one response.

**404:** unknown `policy_id`. **503:** no LLM key.

### `POST /claims/generate-generic`

**One of the 3 Go-backend integration touchpoints — see `GO_INTEGRATION.md` for the full
contract.** The F4 "Generate Generic Claim" test/demo button (US33): fabricates a small,
internally-consistent cluster of posts and runs it through the *exact same* claim
construction + scoring pipeline real clustering uses (never a parallel "fake claim" code
path that could drift out of sync).

**Request body** (`GenerateGenericClaimWebhookRequest`):

| Field | Type | Required | Notes |
|---|---|---|---|
| `claim_type` | string \| null | no | Must be one of the "existing/generic" aliases (`existing`, `generic`, `existing_claim`, `generic_claim`) if provided — this endpoint only ever generates Existing/Generic claims. `422` for anything else. |
| `topic_id` | uuid \| null | no | If given, the generated claim is force-assigned to this topic (overriding the normal embedding-similarity auto-assignment). `404` if the topic doesn't exist. |

**201** (`GenerateGenericClaimWebhookResponse`):
```json
{"claim_id": "uuid", "claim_statement": "...", "topic_id": "uuid", "message": "generated"}
```
This is the **minimal** shape the Go contract specifies. For the full scored detail
object instead, use `POST /admin/generate-generic-claim` (identical underlying pipeline,
richer response) — see below.

**503:** no LLM key. Runtime: ~30–60s (multiple sequential LLM calls: post generation,
analysis, clustering, harm classification, debunk drafting).

---

## Topics

### `GET /topics`

No params. Returns every topic, alphabetical by `name`.

**200:** `[{"id": "uuid", "name": "...", "description": null, "created_at": "..."}]`

### `POST /topics`

Manual creation, outside the normal dynamic clustering-driven path. Rarely needed in
practice — topics are usually created automatically by `assign_or_create_topic`.

**Request body:** `{"name": "...", "description": null}` (`name` required, 1–255 chars).

**201:** the created `TopicRead`.

---

## Policies — F2 Public Policy Bank

### `POST /policies`

**Local/testing use — not the production integration path.** In production, the Go
backend owns policy upload/storage and calls `POST /matchmaking/policies` instead (see
`GO_INTEGRATION.md`). This endpoint still works for local dev/demos: it accepts the file
directly.

**Request:** `multipart/form-data`, 3 fields (matching the "Add Public Policy" modal
exactly — US40):

| Field | Type | Required | Notes |
|---|---|---|---|
| `file` | file | yes | PDF or `.docx` only. No file-size limit enforced server-side. |
| `name` | string (form field) | yes | 1–255 chars. |
| `rolled_out_date` | date (form field) | yes | `YYYY-MM-DD`. |

**201** (`PolicyRead`):
```json
{
  "id": "uuid", "title": "MRT Fase 2 Bundaran HI-Kota Extension", "description": null,
  "rolled_out_date": "2026-12-01", "status": "not_rolled_out",
  "file_name": "policy.pdf", "processing": true, "created_at": "..."
}
```
Returns **immediately** with `processing: true` — the AI matchmaking pipeline
(`policy_matchmaking_service.py`) runs afterward as a background task. Poll `GET
/policies/{id}` until `processing` flips to `false`.

**422:** unsupported file type (not PDF/`.docx`).

### `GET /policies`

**Query params:** `years` (`int[]`, repeat the param — multi-select year filter on
`rolled_out_date`), `q` (string \| null, search by `title`), `limit` (default `10`, max
`1000`), `offset` (default `0`).

Sorted by the **latest created date among any claim linked to the policy** (not the
policy's own creation date) — a policy with no linked claims yet falls back to its own
`created_at`, placed after every policy that does have linked-claim activity.

**200** (`PolicyListResult`): `{"total": 3, "items": [PolicyRead, ...]}`.

### `GET /policies/{policy_id}`

Full detail including every correlated claim — reuses the *exact same* card shape/fields
as `GET /claims/existing`/`GET /claims/non-existing`'s list items, no policy-specific
variant.

**200** (`PolicyDetailRead`):
```json
{
  ...PolicyRead fields...,
  "existing_claims": [ClaimListItemRead, ...],
  "non_existing_claims": [ClaimListItemRead, ...]
}
```
Both lists are empty while `processing: true`. **404:** unknown `policy_id`.

### `GET /policies/{policy_id}/file`

Downloads the original uploaded document, original `Content-Type`,
`Content-Disposition: attachment; filename="..."`.

**200:** raw file bytes. **404:** unknown `policy_id`, or no file was ever attached
(e.g. the policy was created via the webhook with no `document_url`).

---

## Alerts — F3 watchlist

### `GET /alerts`

**Query params:** `q` (string \| null, search `claim_statement`), `limit` (default `50`,
max `1000`), `offset` (default `0`). Sorted most-recently-added first.

**200** (`AlertListResult`):
```json
{
  "total": 2,
  "items": [{
    "claim_id": "uuid", "claim_statement": "...", "claim_created_date": "...",
    "final_claim_score": 62.4, "threshold_status": "under_threshold",
    "added_at": "2026-08-31T09:00:00Z"
  }]
}
```
`threshold_status` (`over_threshold` \| `under_threshold`) is derived live by comparing
`final_claim_score` against the single global `admin_settings.over_threshold` — never
stored, so it can never disagree with the current threshold.

### `GET /alerts/chart`

**Query params:** `claim_ids` (`uuid[]`, repeat the param — required for a non-empty
result; the chart-visibility selection is FE-local state, not persisted server-side, so
the FE must always pass its currently-checked claim IDs), `granularity` (`day` \| `week`
\| `month` \| `year`, default `week`).

Only IDs that are actually on the watchlist are honored; anything else is silently
dropped from the result.

**200:** `list[ChartSeries]`
```json
[{
  "claim_id": "uuid", "claim_statement": "...",
  "points": [{"recorded_at": "2026-08-24T00:00:00Z", "final_claim_score": 58.1}, ...]
}]
```
Points are bucketed by the requested granularity and averaged within each bucket (built
from every `claim_score_snapshots` row for that claim, appended on every rescore).

---

## Admin — F4

### `GET /admin/settings` / `PUT /admin/settings`

**GET 200:** `{"over_threshold": 70.0}`.

**PUT request body:** `{"over_threshold": 80.0}` (`0.0–100.0`). **PUT 200:** the updated
setting.

### `POST /admin/generate-generic-claim`

Same underlying pipeline as `POST /claims/generate-generic` (see the Go integration
touchpoint above), but returns the **full scored detail object** instead of the minimal
webhook shape — use this one for the F4 admin-panel button / any internal tooling that
wants the complete claim immediately rather than a follow-up `GET`.

**Query param:** `topic_hint` (string \| null) — a candidate topic label steer for the
fabricated post cluster (not a topic *id* override, unlike `/claims/generate-generic`'s
`topic_id` — this one influences dynamic topic *assignment*, doesn't force it).

**201** (`GenerateGenericClaimResponse`): `{"claim": ExistingClaimDetailRead}` — the same
full shape as `GET /claims/{id}` for an existing claim.

**503:** no LLM key.

---

## Detection — F5 pipeline trigger + purge

Two endpoints, matching the backend's actual reference contract verbatim
(`CIS-Backend` `internal/aiclient/endpoints.go`) — see `docs/COORDINATION.md` for
the full pipeline this triggers, the 10 tables it writes, and why everything else
(network list/detail/review/allowlist/reports/config) lives on the backend.

### `POST /api/v1/detection/runs`

**Request body**: `{"claim_ids": ["<uuid>", ...], "trigger_source": "scheduled |
velocity | on_demand", "window_start": "<iso8601>", "window_end": "<iso8601>",
"parameters": {...full detector config...}, "exclusions": {"accounts": [...],
"phrases": [...]}}`. Always returns `202 {"run_id": "<uuid>", "status": "pending"}`
(fire-and-forget `BackgroundTasks`; the `detection_run` row is written
synchronously before the response, so `run_id` is immediately queryable).

### `POST /api/v1/detection/snapshots/purge`

**Request body**: `{"network_ids": ["<uuid>", ...]}` → `{"snapshots_purged": N}`.
The backend computes which networks are past retention; this service just deletes
the named evidence.

## Coordination — CIB check (legacy, unrelated to F5)

### `POST /coordination/check-cib`

Deterministic heuristic for Coordinated Inauthentic Behavior across a list of posts
**you supply directly in the request** — this endpoint is stateless, reads nothing from
the database, and persists nothing. Predates the full F5 pipeline above and is
unrelated to it; still mounted, not retired.

**Request body** (`CIBCheckRequest`): `{"posts": [CIBCheckPost, ...]}` (`min_length=2`).

`CIBCheckPost`:

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | Caller-assigned; echoed back in the response. |
| `text` | string | yes | |
| `author_id` | string | yes | |
| `created_at` | datetime | yes | |
| `account_created_at` | datetime \| null | no | Third detection signal; omit if unknown. |

**200** (`CIBCheckResponse`):
```json
{
  "coordination_risk_score": 0.875,
  "is_likely_coordinated": true,
  "clusters": [{
    "post_ids": ["p1", "p2", "p3"], "author_ids": ["@a", "@b", "@c"],
    "reason": ["account_creation_clustering", "burst_timing", "text_similarity"],
    "coordination_score": 1.0
  }]
}
```
Flags pairs on 3 signals — burst timing (posted ≤10 min apart), text similarity (≥0.80
cosine), account-creation clustering (accounts created ≤24h apart) — unions flagged pairs
into clusters, and computes an overall risk score from cluster involvement + peak cluster
score. `is_likely_coordinated` requires both `overall_score ≥ 0.5` **and** at least one
cluster. See `ARCHITECTURE.md` for the exact weight/threshold constants.

---

## Matchmaking — Go backend integration (Flow 1)

### `POST /matchmaking/policies`

**The primary integration entry point — see `GO_INTEGRATION.md` for the full 3-flow
contract, retry/idempotency semantics, and the outbound callback this triggers.** The Go
backend calls this after a policy is uploaded through F2; this service never calls it
itself.

**Auth:** optional `X-API-Key` / `Authorization: Bearer` (see top of this doc).

**Request body** (`PolicyMatchmakingWebhookRequest`):

| Field | Type | Required | Notes |
|---|---|---|---|
| `policy_id` | uuid | yes | The backend's `cis_policies.id`. Echoed back in the Flow 2 callback. |
| `name` | string | yes | |
| `description` | string \| null | no | |
| `rolled_out_date` | date | yes | |
| `status` | string \| null | no | Informational only — this service computes its own status from `rolled_out_date`. |
| `file_name` | string \| null | no | |
| `file_mime_type` | string \| null | no | |
| `document_url` | string \| null | no | A time-limited signed URL (Supabase Storage). If absent (signing failed on the backend's side) or unreachable, this service proceeds working from `name`/`description` alone. |

**202** (`PolicyMatchmakingAckResponse`): `{"status": "processing"}` — always, immediately,
before any actual matchmaking work happens. The real result arrives later via the Flow 2
callback (`POST {BACKEND_URL}/api/v1/internal/policies/{policy_id}/matchmaking-result`),
never as this response.

**Idempotent per `policy_id`** — a repeat call with the same `policy_id` (the backend's
own retry job, or an operator-triggered rematch) is detected via `policies.backend_policy_id`
and just re-reports the existing result via the callback, never duplicating the `Policy`
row or the generated Non-Existing claim.
