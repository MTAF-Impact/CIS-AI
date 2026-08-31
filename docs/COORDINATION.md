# F5 — Coordinated-Network Detector

This service's F5 footprint per the backend integration doc's ownership split
(`AI_REQUIREMENT_FOR_INTEGRATION_SUMMARY_V1.md`, section G): the detection pipeline
itself (graph-based clustering, PRD v1.4 §10) plus the 9 tables it writes, plus a
single run-trigger endpoint. Everything human-facing — network list/detail, review
workflow, allowlist CRUD, PDF/ZIP report generation, export audit log, F4 config —
moved to the backend, which reads these 9 tables directly (same "backend `SELECT`s
your tables, never writes them" pattern already used for `claims`/`policies`/etc).

This was a full-spec build in an earlier session (all 8 original phases, ~15
endpoints, PDF/ZIP generation, F4 admin config — see git history) that was then
rearchitected down to this scope once the backend document's ownership split was
reviewed and the user chose to follow it. If you're looking for the removed
surface (network review, allowlist, reports), it now lives in the Go backend.

See `AI_INTEGRATION_RESPONSE_V1.md` (shared `documentation/CIS` repo) for the
formal reply to the backend's integration doc — what's implemented, the open
schema question (G1), and the proposed trigger-endpoint contract awaiting their
sign-off.

This supersedes the old `POST /coordination/check-cib` heuristic
(`app/services/cib_detector.py`), which is still mounted (nothing has asked to
retire it yet, and it's unrelated to this rearchitecture) but is no longer what F5
actually runs on.

## Package layout

```
app/services/coordination/
├── types.py                 # SignalPost/SignalAccount - pure dataclasses, no ORM
├── scope.py                  # Stage 0: candidate selection, allowlist/self-exclusion, A_max cap
├── signals/
│   ├── temporal.py             # Signal 1 (w_time) - null-model-corrected synchrony
│   ├── duplication.py           # Signal 2 (w_text) - MinHash + multilingual semantic paraphrase
│   ├── coamplification.py        # Signal 3 (w_amp) - inverse-popularity-weighted co-amplification
│   ├── provenance.py              # Signal 4 (w_meta) - creation-time/handle/image/bio similarity
│   └── structural.py               # Signal 5 (w_struct) - Adamic-Adar over follower sets, optional
├── fusion.py                 # Stage 2: weighted fusion + the multi-signal pruning rule
├── clustering.py              # Stage 3: k-core + Leiden community detection
├── relevance_gate.py           # Stage 3a: anchoring/evidence-volume/link-strength claim-relevance tests
├── cluster_metrics.py           # Stage 4: SY/DU/CO/PR/AU + composite CoordinationScore
├── confidence.py                 # Stage 4a: confidence banding + allowlist-majority suppression
├── evidence.py                    # Stage 5: burst timeline, duplicate-content grouping, account
│                                  #   annex (centrality), graph layout snapshot
├── recurrence.py                   # Stage 6: fingerprinting + Jaccard-based recurrence matching
├── governance.py                     # Evidence-retention purge (age-based)
└── pipeline.py                        # Orchestrator: run_detection_for_claim, run_scheduled_sweep,
                                        #   trigger_detection_run - the only module that touches
                                        #   the DB in this package's core logic
```

Every `signals/*.py` and `fusion.py`/`clustering.py`/`relevance_gate.py`/
`cluster_metrics.py`/`confidence.py`/`evidence.py`/`recurrence.py` function is
**pure** — no DB access, fully unit-testable with hand-built fixtures. Only
`pipeline.py` and `governance.py` touch the database. This mirrors the rest of the
service's separation of concerns (`scoring_engine.py` vs `clustering_service.py`).

## Detection pipeline (PRD 10.5)

One call to `pipeline.run_detection_for_claim(claim_id, ...)` runs the full
sequence and persists everything in one DB transaction:

1. **Scope** (`scope.py`) — candidate accounts = everyone with ≥1 post in the
   claim's Supporting-side cluster within window `W` (default 168h). Allowlisted
   and self-excluded accounts are dropped before graph construction; the remaining
   set is capped at `A_max` (default 5,000) by post volume.
2. **Signals** (`signals/`) — five independent pairwise similarity scores per
   account pair, each in `[0,1]`. `w_struct` is always `None` today (no
   follower-graph data source exists yet) — see "Known gaps" below.
3. **Fusion** (`fusion.py`) — weighted sum (`w_time` 0.30, `w_text` 0.25, `w_amp`
   0.20, `w_meta` 0.15, `w_struct` 0.10 by default; an unavailable family's weight
   redistributes proportionally). An edge survives only if `w_total ≥ θ_edge`
   **and** at least two distinct families independently score `≥ 0.25` — this
   multi-signal rule is the pipeline's primary false-positive control (verified by
   a dedicated regression test: a single signal at maximum strength can never form
   an edge alone).
4. **Clustering** (`clustering.py`) — k-core reduction (`k=3`), then **Leiden**
   (not Louvain — Louvain can produce internally disconnected communities, which
   would be indefensible as a "network"). Retention filters: size ≥ `N_min` (5),
   internal density ≥ `ρ_min` (0.30).
5. **Claim-relevance gate** (`relevance_gate.py`) — a cluster that passed
   community detection is not necessarily *about* the claim it was found under.
   Three tests: anchoring share ≥ `μ_anchor` (0.60), claim-cluster post count ≥
   `P_min` (20), `overlap_ratio` ≥ `ω_min` (0.15). Clusters that fail become
   `offtopic_cluster` rows — real coordination, retained for recalibration
   review (now the backend's, reading this table directly), never surfaced as a
   network.
6. **Cluster metrics + confidence** (`cluster_metrics.py`, `confidence.py`) — SY,
   DU, CO, PR, AU (each 0–100) combine into `CoordinationScore` (0.25/0.25/0.20/
   0.15/0.15 weights). `SignalBreadth` = how many of the five independently score
   ≥50. Confidence band: High needs score ≥70 **and** breadth ≥3 — a high score
   with breadth 1 can never reach High, by design (the signature of a false
   positive, not a campaign). A truncated run or ≥2 unavailable signal families
   caps the band at Medium regardless of score. A cluster ≥60% allowlisted is
   suppressed entirely.
7. **Evidence snapshot** (`evidence.py`) — burst timeline (every 60s bin,
   z-scored), duplicate-content groups (union-find over the same pairwise
   duplicate flags, one canonical post per group, every post SHA-256'd), account
   annex (degree/eigenvector centrality via `igraph`), and a force-directed graph
   layout (Fruchterman-Reingold — a documented substitute for the spec's named
   ForceAtlas2, not a second graph-layout dependency for cosmetic parity).
8. **Recurrence** (`recurrence.py`) — a fingerprint (hashed member-ID set + top
   terms) plus real member-set Jaccard matching (≥0.50) against prior networks
   sets `parent_network_id`. A recurring network still has to pass the
   claim-relevance gate against the *new* claim on its own merits.
9. **Persistence** (`pipeline.py`) — `detection_run`, `coordinated_network` +
   its `network_account`/`network_edge`/`network_evidence_post`/
   `network_burst_bin`/`network_claim_link` children, or `offtopic_cluster` for
   relevance-gate failures. Failures anywhere in the run are caught and recorded
   as `DetectionRunStatus.FAILED`, never silently lost.

## The one endpoint

`POST /coordination/detection-runs` (API-key gated) — the AI service's entire F5
API surface, per the backend doc's ownership table ("a run-trigger endpoint").

```json
{"claim_id": "<uuid> | null", "overrides": { /* optional, see below */ } | null}
```

- `claim_id` set → single-claim run. Covers what used to be two separate things
  (an on-demand endpoint under `/claims/{id}/detect-network`, and an
  automatic velocity-crossing trigger inside `POST /claims/rescore`) — both are
  now just the backend calling this endpoint with a claim_id, whenever it decides
  to (on-demand click, or its own velocity-crossing watch).
- `claim_id` omitted/null → full sweep across every `Active` Existing claim
  (replaces the old standalone scheduled-sweep endpoint). Also runs a housekeeping
  evidence-retention purge first (see Governance below) — folded into the sweep
  rather than its own endpoint, so this stays the *only* externally-triggered F5
  route.
- `overrides` — an optional partial map of the PRD 10.11 tunables for that run
  only (see Configuration below). Response is always `202 {"claim_id": ..., "status":
  "scheduled"}` — this is fire-and-forget (`BackgroundTasks`), same shape as F2
  matchmaking; there's no synchronous 404 for a bad `claim_id` anymore, it's
  silently skipped inside the background task.

All three PRD 10.5.8 trigger modes (scheduled/velocity/on-demand) are the
backend's decision now — *when* to call this endpoint, and how often. This
service just runs the pipeline when asked.

## Data model

Exactly the 9 pipeline-output tables from the backend doc's ownership table
(`app/models/coordination.py`) — nothing else:

| Table | Purpose |
|---|---|
| `detection_runs` | One pipeline execution: window, full parameter set, status, truncation flag. |
| `coordination_accounts` | Durable account identity (platform, platform_account_id, handle). |
| `coordinated_networks` | A detected, surfaced network: scores, confidence band, graph layout, fingerprint/parent for recurrence. No review-status field — that lives on the backend's side now (it can't write into this AI-owned table). |
| `network_accounts` | Per-network membership + this account's contribution stats (centrality, duplication rate, etc.). |
| `network_edges` | Retained pairwise edges with full per-signal decomposition. |
| `network_evidence_posts` | Immutable captured post content + SHA-256, duplicate-group membership. |
| `network_burst_bins` | Per-bin post-volume series backing the burst-timeline chart. |
| `network_claim_links` | Many-to-many network↔claim with `overlap_ratio`/`anchoring_share`/`is_primary_claim`. |
| `offtopic_clusters` | Real coordinated clusters that failed the relevance gate — never surfaced, kept for recalibration review. |

**Moved to the backend** (no longer in this service at all): the review-log
table, the allowlist table (see below — the AI now *reads* the backend's copy),
generated PDF/ZIP reports, the export audit log, and the F4 config table. Exact
names on the backend side are the backend team's call.

**The one shared-table read** (the integration doc's explicit, sole exception to
"no shared-table access"): `pipeline._load_allowlisted_handles()` reads the
backend-owned `cis_coordination_allowlist` table directly (read-only) before
candidate selection, so declared-legitimate coordination (US56/US63) still gets
excluded. **The column names assumed there (`handle`, `removed_at`) are a
placeholder pending confirmation of the backend's actual DDL** — the integration
doc names the table but not its schema. Reconcile before this reads real data.

## Configuration

All PRD 10.11 tunables are now static defaults in `app/core/config.py`
(`COORDINATION_*`), overridable per-run via the trigger endpoint's `overrides`
field. F4's old DB-backed `CoordinationSettings` singleton and its admin
CRUD endpoints moved to the backend along with the rest of F5 config ownership —
there's no persistent per-deployment override left on this side; a caller wanting
non-default parameters must pass them explicitly on every call.

| Setting | Default |
|---|---|
| `COORDINATION_MULTILINGUAL_MODEL_NAME` | `paraphrase-multilingual-MiniLM-L12-v2` |
| `COORDINATION_DEFAULT_WINDOW_HOURS` | `168.0` |
| `COORDINATION_A_MAX` | `5000` |
| `COORDINATION_THETA_EDGE` | `0.35` |
| `COORDINATION_K_CORE` | `3` |
| `COORDINATION_LEIDEN_RESOLUTION` | `1.0` |
| `COORDINATION_N_MIN` | `5` |
| `COORDINATION_RHO_MIN` | `0.30` |
| `COORDINATION_MU_ANCHOR` | `0.60` |
| `COORDINATION_P_MIN` | `20` |
| `COORDINATION_OMEGA_MIN` | `0.15` |
| `COORDINATION_BIN_WIDTH_SECONDS` | `60` |
| `COORDINATION_NULL_MODEL_ALPHA` | `0.01` |
| `COORDINATION_TAU_DUP` | `0.80` |
| `COORDINATION_TAU_SEM` | `0.90` |
| `COORDINATION_L_MIN` | `25` |
| `COORDINATION_PROVENANCE_HALF_LIFE_HOURS` | `36.0` |
| `COORDINATION_SELF_EXCLUSION_HANDLES` | `[]` |

Every `detection_run.parameters` row still records the exact values in force for
that run (defaults + any overrides applied), so a report generated months later
can state the configuration that produced it — this didn't change.

**Still compile-time-only, no override path at all** (unchanged from before):
confidence-band score/breadth cutoffs and the allowlist-majority-suppression
threshold (`app/services/coordination/confidence.py`), the multi-signal-rule
constants (`app/services/coordination/fusion.py`).

## Governance (PRD 10.9)

- **Behaviour only, never viewpoint** — no signal takes stance/sentiment/political
  position as an input anywhere in this package (verified by inspection).
- **No attribution, no per-account automation verdict** — no field anywhere
  stores an identity/sponsorship guess or an "is this account a bot" boolean.
  `AU` (Automation & behavioural anomaly) is a *cluster*-level score, never
  rendered against a single account.
- **Standing disclaimer** — moved with the report/detail rendering to the
  backend, which must reproduce this text verbatim on its own report and
  network-detail pages (PRD 10.9.2, previously `governance.STANDING_DISCLAIMER`):

  > This report documents statistical patterns in publicly available account
  > behaviour – the timing, duplication, and provenance characteristics of a set
  > of accounts within a defined time window. It is not a determination that any
  > account is automated, inauthentic, or operated in bad faith, and it makes no
  > claim about the identity, affiliation, or intent of any account holder.
  > Coordinated posting behaviour has legitimate explanations, including
  > organised civic campaigns, newsroom syndication, and community mobilisation
  > in response to real events. Findings require human assessment before any
  > action is taken.

- **Minimum-necessary retention** — `governance.purge_expired_evidence` deletes
  evidence artifacts (posts, burst bins, edges, membership) for networks older
  than the retention window (default 24 months); the `coordinated_network` audit
  row itself is kept permanently. Runs automatically at the start of every
  scheduled sweep — **not** exposed as its own endpoint (folded in so the AI
  service still exposes exactly one F5 route), and **no longer skips reported
  networks** — "reported" status lives in the backend's tables now, invisible to
  this service under "no shared-table writes." This is a deliberate change from
  the original PRD 10.9.1 point 7 wording; worth surfacing to the backend team
  since they're the ones tracking report status.

## Known gaps (documented, not silently dropped)

See `AI_INTEGRATION_RESPONSE_V1.md` (in the shared `documentation/CIS` repo) for the
full account of these gaps against the backend's own "minimum ask" schema list
(their integration doc's G1) — this section is the short version.

- **`w_time` (temporal synchrony) runs on ingest time, not publish time** —
  `ContentItem.created_at` is when this service ingested the post, not when it was
  actually posted; there's no separate `posted_at` field yet. Ingestion lag can
  smear or fabricate apparent synchrony. Flagged by the backend's own integration
  doc (their G1, point 1) as load-bearing for every temporal signal — not yet fixed.
- **`w_amp` (co-amplification) is effectively empty** — `ContentItem` carries no
  reshare/quote/reply/outbound-link fields yet, so there's nothing for this
  signal to compute over until ingestion captures them.
- **`w_meta`/`PR` run on handle-and-timing data only** — no ingestion path
  populates `Account.bio`/`declared_location`/`client_app` yet (backend's minimum
  ask); `created_at_platform`/`profile_hash` are the only fields currently populated.
- **`w_struct` is always unavailable** — no follower-graph data source exists.
  All four degrade honestly (`None`/unavailable or running on partial data, never
  faked as complete) rather than being worked around. Per the backend's own
  analysis, three of five signal families being degraded caps every detected
  network at Medium confidence regardless of score — a real, currently-open
  consequence, not a hypothetical one.
- **Recurrence count (if the backend surfaces one) should use exact
  `fingerprint_hash` matching as a display proxy**, not a full traversal of the
  fuzzy-Jaccard `parent_network_id` chain — the underlying recurrence
  *detection* (which does use real Jaccard matching) is unaffected either way.
- **`NetworkAccount.score_contribution`** (per-metric contribution breakdown,
  not just aggregate stats) is `{}` — a future refinement.
- **`cis_coordination_allowlist` column names are an assumption** (see Data
  model above) — needs backend DDL confirmation.
- **The run-trigger endpoint's exact contract is this service's own design
  choice**, not something the integration doc specifies — it only says "a
  run-trigger endpoint" without a shape. Worth confirming with the backend team
  that `POST /coordination/detection-runs`'s `{claim_id, overrides}` body matches
  what they intend to call.

## Testing

Unit tests (no DB) live alongside each pure module's logic:
`tests/unit/test_coordination_signals.py`,
`tests/unit/test_coordination_pipeline.py` (fusion/clustering/relevance/
confidence), `tests/unit/test_coordination_evidence.py`.

Integration tests (`tests/integration/`, `@pytest.mark.integration`, need a live
test Postgres) exercise the full HTTP path end to end against a synthetic
coordinated cluster: `test_coordination_pipeline_e2e.py` (single-claim run via
the unified trigger endpoint), `test_coordination_triggers.py` (sweep via the
same endpoint with no `claim_id`), `test_coordination_governance.py` (retention
purge).

Notable regression coverage: the multi-signal rule (a single signal at maximum
strength can never form an edge alone —
`TestFuseAndPrune::test_single_strong_signal_alone_is_rejected`), and
self-exclusion actually removing an account from detection.
