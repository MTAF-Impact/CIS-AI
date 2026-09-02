# F5 — Coordinated-Network Detector

This service's F5 footprint matches the backend's **actual reference contract**,
pulled and reviewed from `CIS-Backend` `main` (commit `910cd82`) this session — not
an earlier guessed shape. That repo's `internal/aiclient/detection.go` +
`internal/aiclient/endpoints.go` (the client that calls us),
`internal/models/f5_ai_tables.go` (their GORM read-models for our tables),
`docs/sql/01_f5_reference_schema.sql` (the exact DDL), and `docs/AI-INTEGRATION.md`
"Flow 7"/"Flow 8" are the ground truth. This service owns the detection pipeline
(graph-based clustering, PRD v1.4 §10) plus 10 tables it writes, exposed through two
endpoints. Everything human-facing — network list/detail, review workflow, allowlist
CRUD, PDF/ZIP report generation, export audit log, F4 config — lives on the backend,
which reads these tables directly (same "backend `SELECT`s your tables, never writes
them" pattern already used for `claims`/`policies`/etc).

**History**: a full-spec build in an earlier session (13 tables, ~15 endpoints, PDF/
ZIP generation, F4 admin config) was rearchitected down to "9 tables + 1 endpoint"
based on a backend summary document that only sketched the contract loosely. Once the
backend's actual merged code was reviewed, several concrete mismatches surfaced —
wrong endpoint paths/count, wrong table names (plural vs. singular), wrong PK column
names, several missing columns, a missing table, a different purge design — and this
doc now describes the corrected, verified shape. See `AI_INTEGRATION_RESPONSE_V1.md`
(shared `documentation/CIS` repo) for the open questions sent back to the backend
team about a handful of remaining ambiguities.

This supersedes the old `POST /coordination/check-cib` heuristic
(`app/services/cib_detector.py`), which is still mounted (nothing has asked to
retire it yet, and it's unrelated to this rearchitecture) but is no longer what F5
actually runs on.

## Package layout

```
app/services/coordination/
├── types.py                 # SignalPost/SignalAccount - pure dataclasses, no ORM
├── scope.py                  # Stage 0: candidate selection, allowlist/self-exclusion, candidate_cap
├── signals/
│   ├── temporal.py             # Signal 1 (w_time) - null-model-corrected synchrony
│   ├── duplication.py           # Signal 2 (w_text) - MinHash + multilingual semantic paraphrase
│   ├── coamplification.py        # Signal 3 (w_amp) - inverse-popularity-weighted co-amplification
│   ├── provenance.py              # Signal 4 (w_meta) - creation-time/handle/image/bio similarity
│   └── structural.py               # Signal 5 (w_struct) - Adamic-Adar over follower sets, optional
├── fusion.py                 # Stage 2: weighted fusion + the multi-signal pruning rule
├── clustering.py              # Stage 3: k-core + Leiden community detection
├── relevance_gate.py           # Stage 3a: anchoring/evidence-volume/link-strength claim-relevance tests
├── cluster_metrics.py           # Stage 4: SY/DU/CO/PR/AU + composite CoordinationScore + raw_counts
├── confidence.py                 # Stage 4a: confidence banding + allowlist-majority suppression
├── evidence.py                    # Stage 5: burst timeline, duplicate-content grouping, account
│                                  #   annex (centrality), graph layout snapshot
├── recurrence.py                   # Stage 6: fingerprinting + Jaccard-based recurrence matching
├── governance.py                     # Evidence-snapshot purge (backend-driven, by network id)
└── pipeline.py                        # Orchestrator: create_pending_run, run_detection - the only
                                        #   module (besides governance.py) that touches the DB
```

Every `signals/*.py` and `fusion.py`/`clustering.py`/`relevance_gate.py`/
`cluster_metrics.py`/`confidence.py`/`evidence.py`/`recurrence.py` function is
**pure** — no DB access, fully unit-testable with hand-built fixtures. Only
`pipeline.py` and `governance.py` touch the database.

## Detection pipeline (PRD 10.5)

`pipeline.run_detection(run_id, claim_ids, window_start, window_end, parameters,
exclusions, ...)` runs the full sequence **per claim** in `claim_ids` (PRD 10.5.1
point 6 — multi-claim batch runs pool nothing across claims in this implementation;
signals/metrics/relevance are independently computed per claim either way, which is
the part of point 6 that's actually binding) and persists everything under the one
already-created `run_id`:

1. **Scope** (`scope.py`) — candidate accounts = everyone with ≥1 post in the
   claim's Supporting-side cluster within `[window_start, window_end]` (computed and
   sent by the backend — PRD 10.5.1's 50%-overlap-between-runs rule is enforced on
   their side). Allowlisted accounts (from the request's `exclusions.accounts`) are
   dropped before graph construction; the remaining set is capped at `candidate_cap`
   (default 5,000) by post volume.
2. **Signals** (`signals/`) — five independent pairwise similarity scores per
   account pair, each in `[0,1]`. `w_struct` is always `None` today (no
   follower-graph data source exists yet) — see "Known gaps" below.
3. **Fusion** (`fusion.py`) — weighted sum (`beta_time`/`beta_text`/`beta_amp`/
   `beta_meta`/`beta_struct`, defaults 0.30/0.25/0.20/0.15/0.10; an unavailable
   family's weight redistributes proportionally). An edge survives only if
   `w_total ≥ edge_threshold` **and** at least `min_signal_families` (default 2)
   distinct families independently score `≥ 0.25` — this multi-signal rule is the
   pipeline's primary false-positive control (verified by a dedicated regression
   test: a single signal at maximum strength can never form an edge alone).
4. **Clustering** (`clustering.py`) — k-core reduction (`k_core`, default 3), then
   **Leiden** (not Louvain). Retention filters: size ≥ `min_cluster_size` (5),
   internal density ≥ `min_internal_density` (0.30).
5. **Claim-relevance gate** (`relevance_gate.py`) — three tests: anchoring share ≥
   `anchor_share` (0.60), claim-cluster post count ≥ `min_claim_posts` (20),
   `overlap_ratio` ≥ `min_link_strength` (0.15). Clusters that fail become
   `offtopic_cluster` rows — real coordination, retained for recalibration review
   (the backend's, reading this table directly), never surfaced as a network.
6. **Cluster metrics + confidence** (`cluster_metrics.py`, `confidence.py`) — SY,
   DU, CO, PR, AU (each 0–100, plus the raw integer counts behind each — see
   `ClusterMetrics.raw_counts`) combine into `CoordinationScore` (0.25/0.25/0.20/
   0.15/0.15 weights). `SignalBreadth` = how many of the five independently score
   ≥50. Confidence band cutoffs (`high_score_cutoff`/`high_breadth_cutoff`/
   `medium_score_cutoff`/`medium_breadth_cutoff`) and `min_signal_families` are now
   backend-configurable parameters, not compile-time constants. A truncated run or
   ≥2 unavailable signal families caps the band at Medium regardless of score. A
   cluster ≥60% allowlisted is **persisted with `allowlist_suppressed=true`**, not
   silently dropped — the backend suppresses it from every surface but the audit
   trail stays intact and stable as the allowlist changes underneath it.
7. **Evidence snapshot** (`evidence.py`) — burst timeline (every 60s bin,
   z-scored), duplicate-content groups (union-find over the same pairwise
   duplicate flags, one canonical post per group, every post SHA-256'd, group id a
   deterministic UUID), account annex (degree/eigenvector centrality via `igraph`,
   plus per-account `layout_x`/`layout_y`), and up to `community_size` "comparison"
   accounts — genuine unclustered candidates from the same claim, for contrast
   (US51/PRD 10.8 item 5).
8. **Recurrence** (`recurrence.py`) — a fingerprint (hashed member-ID set + top
   terms) plus real member-set Jaccard matching (≥`recurrence_threshold`, default
   0.50) against prior *member* networks (comparison-role rows are excluded from
   the match pool) sets `parent_network_id`.
9. **Persistence** (`pipeline.py`) — `detection_run`, `coordinated_network` + its
   `network_account` (member and comparison rows)/`network_edge`/
   `network_evidence_post`/`network_burst_bin`/`network_claim_link` children, an
   `evidence_snapshot` row, or `offtopic_cluster` for relevance-gate failures.
   Failures anywhere in the run are caught and recorded as
   `DetectionRunStatus.FAILED` with `error` set, never silently lost.

## The two endpoints

Matching `internal/aiclient/endpoints.go`'s `pathDetectionRun`/`pathDetectionPurge`
exactly — **not** under `/coordination`, under `/detection`:

### `POST /api/v1/detection/runs` (API-key gated)

```json
{
  "claim_ids": ["<uuid>", "..."],
  "trigger_source": "scheduled | velocity | on_demand",
  "window_start": "<iso8601>", "window_end": "<iso8601>",
  "parameters": { "window_days": 7, "beta_time": 0.30, "...": "the full detector config" },
  "exclusions": {
    "accounts": [{"platform": "...", "platform_account_id": "...", "handle": "..."}],
    "phrases": ["..."]
  }
}
```

- `claim_ids` — always ≥1. The backend already rejects Non-Existing/Synthetic
  claims itself (422) before calling us, and already decided *when* to call this
  (all three PRD 10.5.8 trigger modes are its decision, recorded via
  `trigger_source`) — this service just runs the pipeline.
- `window_start`/`window_end` — computed by the backend, not this service.
- `parameters` — the **full** detector configuration, every call. There is no
  DB-backed config or partial-override concept left on this side.
- `exclusions` — the declared-coordination allowlist and common-phrase list,
  **sent inline**, not read from a shared table. (An earlier design had this
  service read a `cis_coordination_allowlist` table directly; the backend's actual
  contract sends the list with the request instead — simpler, and it means this
  integration currently has *no* AI→`cis_*` table read at all.)

Response: `202 {"run_id": "<uuid>", "status": "pending"}`, always — this is
fire-and-forget. The `detection_run` row is written **synchronously**, before the
202, so `run_id` is real and immediately queryable; the backend never polls, it
reads `detection_run.status` directly as the pipeline updates it in the background.
A `claim_id` that doesn't resolve (unknown, or not an Existing claim) is silently
skipped inside that claim's iteration — no partial failure of the whole run.

### `POST /api/v1/detection/snapshots/purge` (API-key gated)

```json
{"network_ids": ["<uuid>", "..."]}
→ {"snapshots_purged": 12}
```

PRD 10.9.1 point 7's retention purge. The backend computes *which* networks are
past retention — only it can see whether a report was generated from a network's
snapshot (`cis_network_reports`), which is what makes the "except reported"
exception possible — and hands over the list. This service just executes the
deletion, since the rows are AI-owned (`governance.purge_expired_evidence`).

## Demo/testing tooling

Not part of the backend's real contract above — for producing a populated
`coordinated_network` row without needing a real coordinated campaign to observe.
`app/services/coordination/demo_seed.py`'s `generate_demo_coordinated_network()` is
the shared generator behind two call sites:

- `POST /api/v1/admin/generate-coordinated-network` (optional `claim_id`,
  `topic_hint` query params) — no `verify_backend_api_key`, same as
  `/admin/generate-generic-claim`. Synthesizes an 8-account, 24-post coordinated
  burst (near-duplicate text, tightly packed timing) on a claim — a new demo claim
  if `claim_id` is omitted, else an existing one — then writes the `detection_run`
  row synchronously (`status=pending`) before returning 202, exactly like
  `POST /api/v1/detection/runs`, so a caller can poll immediately while detection
  runs in the background. Also rescores the claim after attaching the burst, so
  `claim_score_snapshots` gets a fresh point rather than staying static.
- `scripts/seed_demo_data.py` calls the same generator, then awaits
  `pipeline.run_detection()` directly (not backgrounded) so the script finishes with
  a fully completed network.

Deliberately drives only `w_time`/`w_text` — `w_amp`/`w_meta` are currently dead in
the real pipeline regardless of synthetic input (see "Known gaps" below), so a fake
account profile wouldn't move them. **The resulting network typically scores Low
confidence**, not Medium/High — not a bug: `compute_temporal_synchrony`'s null model
looks for a burst that's surprising *relative to the candidate pool's other
activity*, and since the whole candidate pool here (burst + the claim's own seed
posts) is generated within the same few seconds of wall-clock time, there's no quiet
baseline for it to stand out against. A real run has weeks of organic activity
providing that contrast. `du` (duplication) and `co` (cohesion) still land high.

## Data model

10 tables in `app/models/coordination.py`, names/columns matching
`docs/sql/01_f5_reference_schema.sql` verbatim (table names are **singular**, PKs
are table-specific, not a generic `id`):

| Table | PK | Purpose |
|---|---|---|
| `detection_run` | `run_id` | One pipeline execution: scope, trigger source, window, full parameter set, status, error. |
| `account` | `account_id` | Durable account identity — platform, platform_account_id (unique together), handle, created_at_platform, profile_hash, bio, declared_location, client_app. |
| `coordinated_network` | `network_id` | A detected network: scores, raw_counts, confidence band, comparison_account_count, fingerprint/parent for recurrence, allowlist_suppressed, relabelled. No review_status — backend-owned overlay. |
| `network_account` | `(network_id, account_id)` | Per-network membership (`membership_role` = member \| comparison) + contribution stats + `layout_x`/`layout_y`. |
| `network_edge` | `(network_id, account_a, account_b)` | Retained pairwise edges with full per-signal decomposition. No surrogate id. |
| `network_evidence_post` | `evidence_id` | Immutable captured post content + SHA-256, `posted_at`/`captured_at`, duplicate-group uuid, shared-span offsets (nullable, not yet populated). |
| `network_burst_bin` | `(network_id, bin_start)` | Per-bin post-volume series. No surrogate id. |
| `network_claim_link` | `(network_id, claim_id)` | Many-to-many network↔claim with `overlap_ratio`/`anchoring_share`/`is_primary_claim`. |
| `offtopic_cluster` | `cluster_id` | Real coordinated clusters that failed the relevance gate — never surfaced, kept for recalibration review. |
| `evidence_snapshot` | `snapshot_id` | One row per network: a hash + count for the PDF report's chain-of-custody section, `expires_at` for retention. |

**Moved to the backend** (not in this service at all): the review-log table, the
allowlist table, generated PDF/ZIP reports, the export audit log, the F4 config
table. Exact names on the backend side (`cis_*`) are the backend team's own.

## Configuration

All PRD 10.11 tunables — plus confidence-band cutoffs and `min_signal_families`,
which are backend-configurable too now — are sent in full on every
`POST /api/v1/detection/runs` call (`DetectorParameters`,
`app/schemas/coordination_network.py`). There is no static default, DB-backed
config, or per-run override concept on this side; the backend's own
`CISDetectorSettings.Validate()` guarantees a complete, cross-field-valid object
(fusion weights sum to 1.00, confidence-band ordering, cadence ≤ window/2) before
it's ever sent. Every `detection_run.parameters` row records the exact values in
force for that run, so a report generated months later can state the configuration
that produced it.

## Governance (PRD 10.9)

- **Behaviour only, never viewpoint** — no signal takes stance/sentiment/political
  position as an input anywhere in this package (verified by inspection).
- **No attribution, no per-account automation verdict** — no field anywhere
  stores an identity/sponsorship guess or an "is this account a bot" boolean.
  `AU` (Automation & behavioural anomaly) is a *cluster*-level score, never
  rendered against a single account.
- **Standing disclaimer** — moved with the report/detail rendering to the
  backend, which must reproduce this text verbatim on its own report and
  network-detail pages (PRD 10.9.2):

  > This report documents statistical patterns in publicly available account
  > behaviour – the timing, duplication, and provenance characteristics of a set
  > of accounts within a defined time window. It is not a determination that any
  > account is automated, inauthentic, or operated in bad faith, and it makes no
  > claim about the identity, affiliation, or intent of any account holder.
  > Coordinated posting behaviour has legitimate explanations, including
  > organised civic campaigns, newsroom syndication, and community mobilisation
  > in response to real events. Findings require human assessment before any
  > action is taken.

- **Minimum-necessary retention** — `governance.purge_expired_evidence(db,
  network_ids)` deletes evidence artifacts (posts, burst bins, edges, membership)
  and the `evidence_snapshot` row for exactly the networks the backend names;
  `coordinated_network` itself is kept permanently. The "except reported"
  exception (PRD 10.9.1 point 7) is preserved — computed on the backend's side
  (it alone can see `cis_network_reports`), not lost the way an earlier
  age-based-only design would have lost it.

## Known gaps (documented, not silently dropped)

- **`network_evidence_post.posted_at` is backfilled from `ContentItem.created_at`
  (ingest time)**, not real publish time — `content_items` has no separate
  `posted_at` column yet. Flagged by the backend's own gap analysis as
  load-bearing for every temporal signal (their "G1 point 1"); still open.
- **`w_amp` (co-amplification) is effectively empty** — `ContentItem` carries no
  reshare/quote/reply/outbound-link fields yet.
- **`w_meta`/`PR` run on a subset of their stated inputs** — no ingestion path
  populates `Account.bio`/`declared_location`/`client_app` yet (the columns exist,
  per the backend's schema; nothing writes them). Only `created_at_platform`/
  `profile_hash` are currently populated.
- **`w_struct` is always unavailable** — no follower-graph data source exists.
  All degrade honestly (`None`/unavailable or partial, never faked as complete).
  Per the backend's analysis, this caps every detected network at Medium
  confidence regardless of score — a real, currently-open consequence.
- **`shared_span_start`/`shared_span_end` are always `None`** — computing exactly
  where two near-duplicate posts overlap (for report/UI highlighting) needs a
  text-diff step this pipeline doesn't do yet; the columns exist, scoped as a
  follow-up.
- **`raw_counts_json` is a representative count per metric, not exhaustive** — SY/
  DU/PR reduce cleanly to a numerator/total; AU averages four per-account
  sub-signals and reports the cluster's combined circadian coverage as a stand-in,
  not a literal breakdown of all four.
- **`coordinated_network.relabelled` is never set** — see the open question to
  the backend team (`AI_INTEGRATION_RESPONSE_V1.md`) about how this is supposed
  to get triggered given neither side can write it after the fact under
  "no shared-table writes."
- **`network_edge`'s signal-weight columns are `NOT NULL DEFAULT 0`**, so an
  unavailable family is stored as `0.0` on a per-edge basis — `detection_run.
  signals_unavailable` is the actual source of truth for unavailability at the
  run level. Also an open question to the backend team.
- **Comparison accounts (`membership_role="comparison"`) aren't positioned in the
  graph layout** — `layout_x`/`layout_y` are `None` for them; only real cluster
  members get a computed position today.

## Testing

Unit tests (no DB) live alongside each pure module's logic:
`tests/unit/test_coordination_signals.py`,
`tests/unit/test_coordination_pipeline.py` (fusion/clustering/relevance/
confidence), `tests/unit/test_coordination_evidence.py`.

Integration tests (`tests/integration/`, `@pytest.mark.integration`, need a live
test Postgres) exercise the full HTTP path end to end against a synthetic
coordinated cluster, using the shared request-builder in
`tests/coordination_fixtures.py`: `test_coordination_pipeline_e2e.py`
(single-claim run), `test_coordination_triggers.py` (multi-claim batch runs, one
`detection_run` covering several claims), `test_coordination_governance.py`
(network-id-driven purge).

Notable regression coverage: the multi-signal rule (a single signal at maximum
strength can never form an edge alone —
`TestFuseAndPrune::test_single_strong_signal_alone_is_rejected`), and
self-exclusion actually removing an account from detection.
