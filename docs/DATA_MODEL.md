# Data Model Reference

Every table this service owns, column by column. This is the **authoritative** schema
reference — if anything here disagrees with the SQLAlchemy models under `app/models/`,
the code wins; file an issue/ping the AI team.

Schema is managed by `Base.metadata.create_all()` (see `scripts/reset_schema.py` /
`scripts/seed_demo_data.py`'s `ensure_schema()`), **not** a migrations tool — this is
pre-launch software. There is no Alembic history to consult; the model files themselves
are the single source of truth.

## Ownership boundary (read this first)

This service owns every table below. **The Go backend (CIS-Backend) only ever `SELECT`s
from them — it never inserts, updates, deletes, or migrates any of them.** A startup
guard on the backend's side refuses to boot if any of its own AutoMigrate models ever
targets a non-`cis_`-prefixed table, specifically because GORM cannot represent the
`pgvector` `embedding` columns present on five of these tables and would silently strip
them if it ever tried to manage one.

Symmetrically, this service must never write to any `cis_*` table — those are the
backend's exclusive territory (`cis_users`, `cis_refresh_tokens`, `cis_policies`,
`cis_claim_reviews`, `cis_claim_alerts`, `cis_claim_score_snapshots`, `cis_settings`).
`scripts/reset_schema.py` explicitly excludes anything prefixed `cis_` from its drop
step for exactly this reason. See `GO_INTEGRATION.md` for the full ownership contract
and the HTTP touchpoints that replace direct table access between the two services (8
endpoints the backend calls on this service, 2 in the reverse direction, as of that doc).

**One deliberate, narrow exception**: `app/services/config_service.py` `SELECT`s
`cis_settings` directly (never INSERT/UPDATE/DELETE) to read the dynamic scoring
parameters catalogued in `documentation/CIS/AI_DYNAMIC_PARAMETER.md` and `SCORING.md`'s
"Dynamic parameters" section — this is the mirror of the backend's own read-only access
to this service's tables, not a new ownership direction. It has no ORM model on this
side on purpose (a plain `text()` query), so it can never be created/dropped by
`Base.metadata.create_all()`/`reset_schema.py`.

All tables live in the same Supabase Postgres instance, `public` schema, with the
`vector` extension enabled (pgvector, for the `embedding` columns).

---

## `claims`

The central entity. One row per claim — either **Existing** (real content already
circulating, scored) or **Non-Existing** (AI-predicted, never scored). See `app/models/claim.py`.

`claim_type` is fixed permanently at creation by *which pipeline created the row* —
never reclassified later:
- Rows from `clustering_service.py` (real ingested/clustered content) are always `existing`.
- Rows from `claim_prediction_service.py` (the F2 prediction flow, no content exists yet)
  are always `non_existing`.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `uuid` | No | `uuid4()` | Primary key. **This is the value the Go backend stores as `cis_policies.ai_policy_id`'s analogue — for claims there's no equivalent backend-side echo, claims are read directly by `id`.** |
| `claim_type` | `varchar(16)` | No | — | `existing` \| `non_existing`. See `ClaimType` enum. Immutable after creation. |
| `claim_statement` | `text` | No | — | One LLM-synthesized declarative sentence. For `existing`, synthesized fresh from a cluster of posts (never copied verbatim from one post). For `non_existing`, the LLM's predicted claim. |
| `topic_id` | `uuid` | No | — | FK → `topics.id`, `ON DELETE RESTRICT` (a topic can't be deleted while claims reference it). Every claim belongs to exactly one topic. |
| `status` | `varchar(16)` | No | `unreviewed` | `unreviewed` \| `active` \| `inactive` \| `action_taken`. Single shared set for both claim types (PRD v1.3 merged the old type-specific Prebunk/Debunk statuses). Purely a human review-state field — the AI pipeline never changes it after creation. |
| `policy_id` | `uuid` | Yes | `NULL` | FK → `policies.id`, `ON DELETE SET NULL`. **Only ever set for `non_existing` claims** (one-to-many: one policy → many predicted claims). `existing` claims correlate to policies via the separate `claim_policies` many-to-many junction table instead — this column stays `NULL` for them. |
| `embedding` | `vector(384)` | Yes | `NULL` | Embedding of `claim_statement` (`sentence-transformers/all-MiniLM-L6-v2`). Used for topic-assignment similarity and F2 policy-matchmaking cosine prefiltering. |
| `first_caught_at` | `timestamptz` | No | — | For `existing`: `min(content_item.created_at)` across the founding cluster. For `non_existing`: the prediction timestamp (no content exists to derive it from). |
| `reach_score` | `float` | Yes | `NULL` | **R**, 0–100. `existing` only — always `NULL` for `non_existing`. |
| `velocity_score` | `float` | Yes | `NULL` | **V**, 0–100. `existing` only. |
| `falseness_score` | `float` | Yes | `NULL` | **F**, 0–100, or `NULL` if neither the `official_sources` corpus match nor the live Google Fact Check API fallback finds anything. **`NULL` is a real, expected state for a novel never-fact-checked claim, not an error** — see `SCORING.md`. |
| `harm_score` | `float` | Yes | `NULL` | **H**, 0–100. Weighted composite of the 4 sub-scores below. |
| `harm_public_safety` | `float` | Yes | `NULL` | H sub-score, 0–100, AI-classified. |
| `harm_institutional_trust` | `float` | Yes | `NULL` | H sub-score, 0–100, AI-classified. |
| `harm_economic` | `float` | Yes | `NULL` | H sub-score, 0–100, AI-classified. |
| `harm_policy_disruption` | `float` | Yes | `NULL` | H sub-score, 0–100, AI-classified — deliberately the lowest-weighted (0.15) sub-score; scores *effect on policy execution*, never mere criticism/disagreement. |
| `harm_human_confirmed` | `boolean` | No | `false` | Flips to `true` only via `PATCH /claims/{id}/harm/confirm` — a human reviewer has confirmed/overridden the AI's harm classification. |
| `emotional_intensity_score` | `float` | Yes | `NULL` | **EI**, 0–100. Computed only from Supporting-stance content. |
| `emotional_intensity_opposing` | `float` | Yes | `NULL` | **EI_opposing**, 0–100. Same formula, Opposing-stance content. **Display-only — never enters `claim_score`/`npr`/`discount_factor`/`final_claim_score`.** |
| `claim_score` | `float` | Yes | `NULL` | Composite `0.15R + 0.15V + 0.30F + 0.30H + 0.10EI` (pre-discount). If `falseness_score` is `NULL`, F's weight is dropped and the rest renormalize — see `SCORING.md`. |
| `npr` | `float` | Yes | `NULL` | Net Pushback Ratio, 0–1. `NULL` when the claim is dormant (no Supporting+Opposing volume in the rolling window). |
| `discount_factor` | `float` | Yes | `NULL` | `1 − 0.5×npr`, range [0.5, 1]. `1.0` (no discount) when `npr` is `NULL` or total volume is below the reliability threshold (25 posts). |
| `final_claim_score` | `float` | Yes | `NULL` | `claim_score × discount_factor`, 0–100. **The ranking value** — what `GET /claims/existing` sorts by. |
| `is_dormant` | `boolean` | No | `false` | `true` when Supporting + Opposing volume is 0 in the rolling 24h window — flagged, never silently discounted. |
| `activity_content` | `text` | Yes | `NULL` | The single copyable Debunk (existing) / Prebunk (non-existing) content block. Generated once, cached forever — never regenerated on view. |
| `activity_generated_at` | `timestamptz` | Yes | `NULL` | When `activity_content` was generated. |
| `debunk_core_fact` | `text` | Yes | `NULL` | **`existing` only.** The Truth Sandwich's first block — the true, verified fact. Split out from `activity_content` so the FE can render 3 distinct labeled blocks instead of one paragraph. |
| `debunk_nuanced_flag` | `text` | Yes | `NULL` | **`existing` only.** Truth Sandwich's second block — a brief, neutral note that a false claim is circulating, without repeating its specific wording. |
| `debunk_reiterated_fact` | `text` | Yes | `NULL` | **`existing` only.** Truth Sandwich's third block — the fact restated in different words. |
| `created_at` | `timestamptz` | No | `now()` | |
| `updated_at` | `timestamptz` | No | `now()`, auto-updates on any column change | |

**No CHECK constraint** on `claim_type`/`status`/score-nullness combinations — PRD v1.3
simplified the status model to one shared set for both types, so the old type/status
CHECK constraint from an earlier PRD version was removed. Score-field nullness for
`non_existing` claims is enforced purely by the prediction pipeline never writing them,
not by a DB constraint.

**Relationships:** `topic` (many-to-one), `policy` (many-to-one, `non_existing` only),
`policy_links` (one-to-many `ClaimPolicy`, `existing` only), `content_items`
(one-to-many, `existing` only — `non_existing` claims have zero attached content by
construction), `debunk_segments` (one-to-many `ClaimDebunkSegment`, `existing` only —
see below).

---

## `content_items`

One row per raw piece of ingested content (a social post, forum message, RSS item, radio
transcript excerpt). See `app/models/content.py`.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `uuid` | No | `uuid4()` | |
| `text` | `text` | No | — | The raw content itself, original language. |
| `text_en` | `text` | Yes | `NULL` | LLM-translated English text, what's actually embedded (the embedding model is English-only). Echoes `text` when already English. |
| `source` | `varchar(32)` | No | `other` | `social` \| `rss` \| `radio` \| `forum` \| `other`. |
| `author_id` | `varchar(255)` | Yes | `NULL` | Whatever handle/identifier the source provides (e.g. `@driver_jkt`). Drives the Top 5 Accounts panel. |
| `location` | `varchar(255)` | Yes | `NULL` | Free-text, typically a neighborhood/landmark. |
| `outrage_score` | `float` | Yes | `NULL` | LLM-assessed, 0–1 (not 0–100 — this is a raw ratio, scaled into EI's 0–100 range downstream). |
| `moral_foundation` | `varchar(32)` | Yes | `NULL` | `fairness` \| `harm` \| `autonomy` \| `loyalty` \| `authority` \| `purity` \| `neutral`. LLM-assessed at ingestion. |
| `extracted_claim` | `text` | Yes | `NULL` | LLM's one-sentence summary of the core claim in this specific post (distinct from the claim cluster's own synthesized `claim_statement`). |
| `underlying_grievance` | `text` | Yes | `NULL` | LLM's short phrase for the deeper community concern this content taps into. |
| `sentiment` | `varchar(16)` | Yes | `NULL` | `positive` \| `negative` \| `neutral` (PRD v1.5 6.6.1). LLM-assessed at ingestion, on the content's own emotional valence — **not** derived from `stance`; see `Sentiment`'s docstring in `app/models/enums.py` for why the two axes must never be conflated. `NULL` only for rows ingested before this shipped; still counts toward F6's Climate Sentiment Index denominator. |
| `stance` | `varchar(16)` | Yes | `NULL` | `supporting` \| `opposing` \| `neutral`. **`NULL` until the item is clustered into a claim** — stance is only assessable relative to a specific claim statement, so it is never set at ingestion time, and never defaulted (always an explicit LLM call). |
| `impressions` | `integer` | Yes | `NULL` | Optional raw metric, feeds Reach (R). Populated by whatever upstream source provides it; `NULL`/absent is fine. |
| `positive_reaction_count` | `integer` | Yes | `NULL` | Optional raw metric, feeds Emotional Intensity (EI). |
| `negative_reaction_count` | `integer` | Yes | `NULL` | Optional raw metric, feeds Emotional Intensity (EI). |
| `embedding` | `vector(384)` | Yes | `NULL` | Embedding of `text_en` (falls back to `text` if translation failed). |
| `external_ref` | `varchar(512)` | Yes | `NULL`, **unique** | Dedup key for automated sources, e.g. `"telegram:<channel_id>:<message_id>"`, `"rss:<feed_url>:<guid>"`. `NULL` (and unenforced) for manual/synthetic ingestion — `POST /ingest`/`POST /ingest/batch` are idempotent per `external_ref` when it's set. |
| `claim_id` | `uuid` | Yes | `NULL` | FK → `claims.id`, `ON DELETE SET NULL`. `NULL` until clustered. |
| `created_at` | `timestamptz` | No | `now()` | The content's own timestamp (not the ingestion time — for real crawled content this should be the post's actual publish time). |

**Un-clustered backlog:** `WHERE claim_id IS NULL AND embedding IS NOT NULL` is exactly
the query `clustering_service.cluster_unclustered_content()` runs on every pass — any row
matching that predicate is fair game to attach to an existing claim or seed a new one.

---

## `topics`

A dynamically-formed grouping of claims — **not** a fixed/seeded taxonomy. Assigned via
embedding-centroid similarity at claim-creation time
(`clustering_service.assign_or_create_topic`). See `app/models/topic.py`.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `uuid` | No | `uuid4()` | |
| `name` | `varchar(255)` | No | — | LLM-synthesized short label (2–5 words), e.g. `"Road Pricing & Transit"`. |
| `description` | `text` | Yes | `NULL` | Not currently populated by any pipeline; available for manual `POST /topics` creation. |
| `embedding` | `vector(384)` | Yes | `NULL` | **Centroid** of every claim currently assigned to this topic — recomputed on every attach, not a static value. |
| `created_at` | `timestamptz` | No | `now()` | |
| `updated_at` | `timestamptz` | No | `now()`, auto-updates | |

A new claim attaches to the topic with the highest cosine similarity if that similarity
is ≥ `clustering.topic_attach_threshold` (defaults to `0.5`, dynamic via `cis_settings` -
see `SCORING.md`'s "Dynamic parameters" section); otherwise a brand-new topic is created. This applies
identically to both `existing` claims (Pass 2 of clustering) and `non_existing` claims
(the prediction flow) — no asymmetry between claim types.

---

## `policies`

A public policy the city is rolling out, authored either through this service's own
`POST /policies` (local/testing) or, in production, through the Go backend's Flow 1
webhook. See `app/models/policy.py`.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `uuid` | No | `uuid4()` | **This is `ai_policy_id` from the Go integration contract** — see `GO_INTEGRATION.md`. The backend stores this value on its own `cis_policies.ai_policy_id` and joins every claim↔policy correlation through it. |
| `title` | `varchar(255)` | No | — | |
| `description` | `text` | Yes | `NULL` | |
| `extracted_text` | `text` | Yes | `NULL` | Full text extracted from the uploaded PDF/Word document (`document_extraction.extract_text`) — the matchmaking pipeline's grounding input. `NULL` if extraction failed or no document was supplied (the pipeline then works from `title`/`description` alone). |
| `file_name` | `varchar(255)` | Yes | `NULL` | |
| `file_content_type` | `varchar(100)` | Yes | `NULL` | |
| `file_data` | `bytea` | Yes | `NULL` | The uploaded document's raw bytes, stored inline in Postgres — a deliberate MVP simplification (no separate object-storage credentials configured for this service). Served via `GET /policies/{id}/file`. |
| `rolled_out_date` | `date` | No | — | Drives the derived `status` property — never itself an editable "status" flag. |
| `embedding` | `vector(384)` | Yes | `NULL` | Embedding of `title + description + extracted_text[:4000]`, computed during matchmaking. |
| `processing` | `boolean` | No | `true` | `true` while the AI matchmaking background job is running. FE should not let a user act on this policy's correlated claims while `true`. |
| `backend_policy_id` | `uuid` | Yes | `NULL`, **unique** | Soft reference (no FK) to the Go backend's `cis_policies.id` — only set when this row was created via the Flow 1 webhook (`NULL` for policies created through this service's own `POST /policies`). Exists purely so a retried webhook call with the same `policy_id` is detected as a duplicate and re-reports the existing result instead of creating a second `Policy` row. |
| `created_at` | `timestamptz` | No | `now()` | |

`status` (`rolled_out` / `not_rolled_out`) is **not a column** — it's a Python
`@property` computed live from `rolled_out_date` vs. wall-clock time on every read, so it
can never go stale the way a written-once flag would without a scheduled re-evaluation
job.

---

## `claim_policies`

Many-to-many junction, **Existing claims only**. Non-Existing claims use `claims.policy_id`
directly instead (one-to-many — exactly one policy each, by construction of the
prediction pipeline). See `app/models/policy.py`.

| Column | Type | Nullable | Notes |
|---|---|---|---|
| `claim_id` | `uuid` | No | FK → `claims.id`, `ON DELETE CASCADE`. Part of composite PK. |
| `policy_id` | `uuid` | No | FK → `policies.id`, `ON DELETE CASCADE`. Part of composite PK. |

Rows are created by `policy_matchmaking_service._run()` when the LLM confirms an
existing claim is genuinely about a policy (not just topically adjacent) — never
manually.

---

## `claim_alerts`

F3's watchlist. One row per claim a user has explicitly opted into ongoing monitoring
via the F1 bell icon. See `app/models/alert.py`.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `claim_id` | `uuid` | No | — | PK. FK → `claims.id`, `ON DELETE CASCADE`. |
| `added_at` | `timestamptz` | No | `now()` | Drives the watchlist's sort order (most-recently-added first). |

**Existing claims only** — enforced at the service layer (`POST /claims/{id}/alert`
returns `422` for a `non_existing` claim), not by a DB constraint (Postgres has no clean
way to express "`claim_type = 'existing'`" as an FK-level rule against another table's
column).

---

## `claim_score_snapshots`

Append-only history of `final_claim_score` over time, powering the F3 trend chart. `claims`
itself only ever holds the *current* score — this is the only history of how it moved.
See `app/models/alert.py`.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `uuid` | No | `uuid4()` | |
| `claim_id` | `uuid` | No | — | FK → `claims.id`, `ON DELETE CASCADE`. Non-unique — many rows per claim over time. |
| `final_claim_score` | `float` | No | — | |
| `recorded_at` | `timestamptz` | No | `now()` | |

A row is appended every time `clustering_service.rescore_claim()` runs (clustering, the
standalone `POST /claims/rescore`, or a harm confirmation) — **for every claim touched**,
not just alerted ones, since scoring itself doesn't know which claims are watchlisted.

---

## `claim_debunk_segments`

PRD v1.5 US12. One tailored Debunk Activity draft per audience segment, replacing the
single generic draft (`claims.activity_content`) with 1–4 segment-specific ones.
Generated once, alongside `activity_content`, from the claim's Supporting-side sample —
never regenerated on view, same rule as `activity_content` itself. See
`app/models/debunk_segment.py`.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `uuid` | No | `uuid4()` | |
| `claim_id` | `uuid` | No | — | FK → `claims.id`, `ON DELETE CASCADE`. |
| `segment_name` | `varchar(255)` | No | — | Card label, e.g. `"Commuters"`. LLM-inferred from the sample of posts spreading the claim. |
| `segment_rationale` | `text` | Yes | `NULL` | Why this segment is exposed / what framing makes the claim land differently for them. Rendered as the card's subtitle. |
| `content` | `text` | No | — | The tailored corrective message for this segment, following the same Truth Sandwich discipline as `activity_content`. |
| `rank` | `integer` | No | `0` | Card order, most-exposed segment first — the LLM response's own ordering, deduped (see below), not re-sorted. |
| `generated_at` | `timestamptz` | No | `now()` | |

**Unique constraint** `(claim_id, segment_name)` — matches the backend's proposed DDL
(`CIS-Backend/docs/sql/02_f6_reference_schema.sql`) exactly, since this schema is that
proposal, adopted as-is. `activity_service._generate_debunk_segments` dedupes on
`segment_name` **before** inserting: the LLM's "distinct segments" instruction is a
prompt-level ask, not a guarantee, and a repeated name would otherwise only surface as an
`IntegrityError` at the caller's eventual commit — for clustering's Pass 2, one
transaction shared by every claim created in that run, so one naming collision would roll
back claims that have nothing to do with it.

**Optional by construction, from the backend's side:** an empty result set for a claim
falls back to `activity_content` exactly as PRD v1.4 behaved. A generation failure here
(caught in `activity_service`) never blocks or invalidates the generic draft above it.

---

## `admin_settings`

F4's single global config row. Not a key-value table — there is currently only one
setting, so it's one row, upserted in place. See `app/models/admin_setting.py`.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `integer` | No | `1` | Always `1` (`SINGLETON_ID` in code). There is exactly one row. |
| `over_threshold` | `float` | No | `70.0` | The global Over/Under Threshold cutoff (0–100) governing F3's `threshold_status` for every claim. |
| `updated_at` | `timestamptz` | No | `now()`, auto-updates | |

---

## `fault_lines`

Known community grievances / historical fault lines, used as RAG grounding context for
Prebunk predictions and Debunk drafting (`rag_service.py`). Seeded manually
(`scripts/seed_demo_data.py`'s `DEMO_FAULT_LINES`) — no ingestion pipeline writes these.
See `app/models/fault_line.py`.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `uuid` | No | `uuid4()` | |
| `community_name` | `varchar(255)` | No | — | e.g. `"Kampung Pulo"`. |
| `grievance_theme` | `varchar(255)` | No | — | e.g. `"Eviction distrust"`. |
| `description` | `text` | Yes | `NULL` | |
| `embedding` | `vector(384)` | Yes | `NULL` | Embedding of `f"{grievance_theme}: {description}"`, used for cosine-similarity retrieval (top-3 by default). |
| `created_at` | `timestamptz` | No | `now()` | |

---

## `official_sources`

The verified reference corpus Falseness (F) scoring matches claims against. Seeded from
TurnBackHoax.id's public feed via `scripts/seed_debunk_corpus.py` (safe to re-run
periodically - skips rows whose `source_url` is already stored) - see `docs/SOURCES.md`
for why this substitutes for the originally-planned `nlp-brin-id/fakenews-mafindo`
HuggingFace dataset (turned out to be gated). `content`/`title` hold the **hoax
claim text itself** (original Indonesian), not a corrected fact - a new claim needs
to resemble the false narrative to match, not its correction. `embedding` is computed
from an **English translation** of that claim text, not the raw Indonesian - the
embedding model is English-only, and embedding the untranslated text measurably
degraded match quality (a claim closely paraphrasing a real seeded hoax topic scored
0.51 similarity pre-fix, just under `DEFAULT_MATCH_THRESHOLD` 0.55 - a real match
silently missed; 0.90 after). Deliberately kept **separate** from
`policies` — this is an independently-managed reference corpus (fact-checks, official
statements) with no guaranteed 1:1 relationship to a specific Policy row; conflating them
would force every debunk-reference document to also be a "Policy". See
`app/models/official_source.py`.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `uuid` | No | `uuid4()` | |
| `title` | `varchar(255)` | No | — | |
| `content` | `text` | No | — | |
| `source_url` | `varchar(1024)` | Yes | `NULL` | |
| `embedding` | `vector(384)` | Yes | `NULL` | |
| `created_at` | `timestamptz` | No | `now()` | |

---

## `topic_volume_buckets`

Lightweight hourly rolling-history table backing Velocity's z-score baseline. Incremented
whenever a Supporting-stance `content_item` attaches to (or creates) an Existing claim in
a topic. See `app/models/topic_volume_bucket.py`.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `uuid` | No | `uuid4()` | |
| `topic_id` | `uuid` | No | — | FK → `topics.id`, `ON DELETE CASCADE`. |
| `bucket_start` | `timestamptz` | No | — | Floored to the hour. |
| `supporting_volume` | `integer` | No | `0` | Count of Supporting-stance items attached in this hour. |

**Unique constraint** `(topic_id, bucket_start)` — one row per topic per hour, upserted
(incremented) rather than duplicated.

---

## Enums (`app/models/enums.py`)

Stored as plain `varchar` columns (not Postgres native `ENUM` types), so adding a new
value never requires a migration — just a new string constant.

| Enum | Values | Used on |
|---|---|---|
| `ContentSource` | `social`, `rss`, `radio`, `forum`, `other` | `content_items.source` |
| `MoralFoundation` | `fairness`, `harm`, `autonomy`, `loyalty`, `authority`, `purity`, `neutral` | `content_items.moral_foundation` |
| `Sentiment` | `positive`, `negative`, `neutral` | `content_items.sentiment` — independent axis from `Stance`, never derived from it. |
| `Stance` | `supporting`, `opposing`, `neutral` | `content_items.stance` |
| `ClaimType` | `existing`, `non_existing` | `claims.claim_type` — matches the Go backend's canonical vocabulary exactly (see `GO_INTEGRATION.md`'s alias table). |
| `ClaimStatus` | `unreviewed`, `active`, `inactive`, `action_taken` | `claims.status` |
| `PolicyStatus` | `not_rolled_out`, `rolled_out` | Never stored — always the live-computed `Policy.status` property. |

## Entity-relationship summary

```
                    ┌──────────┐
                    │  topics  │
                    └────┬─────┘
                         │ 1
                         │
                         │ *
   ┌──────────┐    ┌─────┴──────┐    *      *    ┌──────────┐
   │ policies │◄───┤   claims   ├────────────────►│ policies │  (via claim_policies,
   └────┬─────┘  1 └─────┬──────┘  claim_policies └──────────┘   existing claims only)
        │ *              │ 1                            (same table, shown twice
        │(non_existing   │                               for the two relationship
        │ claims only)   │ *                              shapes)
        │           ┌────┴─────────┐
        │           │ content_items│
        │           └──────────────┘
        │
        │ 1
        │ *
   (own row per Policy, no separate table)

   claims 1───* claim_alerts (existing only, PK = claim_id)
   claims 1───* claim_score_snapshots
   claims 1───* claim_debunk_segments (existing only)
   topics 1───* topic_volume_buckets
```

Standalone, unreferenced by FK from anywhere else: `fault_lines`, `official_sources`,
`admin_settings` (all read/written independently, joined only at query time via
similarity search or global-singleton lookup).
