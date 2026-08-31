# F5 Traceability Matrix — PRD v1.4 Section 10 → Implementation

**Superseded twice.** An earlier session built the full PRD v1.4 §10 spec inside this
service (all of US43-64, PDF/ZIP reports, F4 admin config). A review of a backend
summary document then rearchitected it down to "9 tables + 1 endpoint" based on a
loosely-sketched ownership split. A later review of the backend's **actual merged
code** (`CIS-Backend` `main`, commit `910cd82` — the real contract, not a summary of
it) found concrete mismatches against that guess: wrong endpoint paths/count, wrong
table names, wrong PK names, missing columns, a missing table, a different purge
design. This document now tracks the corrected, verified shape. See
`COORDINATION.md` for the current scope in full, and
`AI_INTEGRATION_RESPONSE_V1.md` (shared `documentation/CIS` repo) for the open
questions still outstanding to the backend team.

**Legend**: ✅ Done — ⚠️ Partial (works, but narrower than the spec text) — ❌ Not built — 🔀 Moved to backend (not this service's responsibility to trace)

---

## User Stories (10.7) — all moved to the backend

| Section | User stories | Status |
|---|---|---|
| 10.7.1 Network List | US43-48 | 🔀 Backend reads `coordinated_networks`/`network_claim_links` directly |
| 10.7.2 Network Detail | US49-54 | 🔀 Backend reads the 9 pipeline-output tables directly |
| 10.7.3 Account Annex | US55-57 | 🔀 Backend reads `network_accounts`/`coordination_accounts` directly; allowlist writes go to the backend's own table |
| 10.7.4 Report Generation | US58-60 | 🔀 PDF/ZIP generation is now the backend's; this service no longer stores generated reports |
| 10.7.5 Cross-Feature / F4 | US61-64 | 🔀 US61's claim-detail indicator, US62's config UI, US63's allowlist CRUD, and US64's export audit log are all backend-side now |

This service has no visibility into how completely the backend has implemented
US43-64 — that's tracked wherever the backend team tracks its own work, not here.

## Detection Pipeline (10.5.1 – 10.5.8) — still this service's responsibility

| Stage | Requirement | Status | Implementation |
|---|---|---|---|
| 10.5.1 Stage 0 | Candidate scope, allowlist/self-exclusion, candidate_cap truncation | ✅ | `scope.select_candidates`; allowlist sourced from the request's `exclusions.accounts` (sent inline by the backend, not read from a shared table) |
| 10.5.1 point 5 | Claim-scoped observation (never use activity outside the claim cluster) | ✅ | Enforced by `pipeline._load_candidate_posts`'s query (`claim_id` + `stance=SUPPORTING` filter) |
| 10.5.1a | Claim-relevance gate: anchoring, evidence-volume, link-strength; off-topic clusters logged not surfaced | ✅ | `relevance_gate.evaluate_claim_relevance`; `OfftopicCluster` persistence in `pipeline._persist_offtopic_cluster` |
| 10.5.1 point 6 | Multi-claim/topic batch runs | ✅ | `pipeline.run_detection` accepts `claim_ids: list[uuid]` and loops internally under one `detection_run` row; signals/metrics/relevance-gate remain independently computed per claim (the binding part of point 6) - candidate pooling across claims (the optional efficiency part) is not attempted |
| 10.5.2.1 Signal 1 | Temporal synchrony, mandatory null-model correction | ✅ (math) / ⚠️ (data) | `signals/temporal.py` — Poisson-binomial closed form (vectorized, not permutation-based, per spec's stated preference) is correct; runs on `ContentItem.created_at` (ingest time), not publish time — no `posted_at` field exists yet (backend integration doc's G1 point 1) |
| 10.5.2.2 Signal 2 | Content duplication, MinHash + semantic paraphrase, required exclusions | ✅ | `signals/duplication.py`. Common-phrase allowlist is empty by default (content curation, not code) |
| 10.5.2.3 Signal 3 | Co-amplification, inverse-popularity weighted | ✅ (math) / ⚠️ (data) | `signals/coamplification.py` correct; effectively always empty today — `ContentItem` has no reshare/quote/reply/outbound-link fields yet |
| 10.5.2.4 Signal 4 | Provenance & identity similarity, graceful degradation | ✅ (math) / ⚠️ (data) | `signals/provenance.py` correct; runs on handle/timing only — no ingestion path populates creation date/profile hash/bio yet |
| 10.5.2.5 Signal 5 | Structural overlap (Adamic-Adar), optional | ✅ (math) / ❌ (data) | `signals/structural.py` correct; always `None` — no follower-graph source exists |
| 10.5.3 Stage 2 | Fusion + multi-signal pruning rule | ✅ | `fusion.fuse_and_prune`; regression-tested (`test_single_strong_signal_alone_is_rejected`) |
| 10.5.4 Stage 3 | k-core + Leiden (not Louvain) | ✅ | `clustering.detect_communities` (`leidenalg`/`igraph`) |
| 10.5.5 Stage 4 | SY/DU/CO/PR/AU + composite CoordinationScore | ✅ | `cluster_metrics.compute_cluster_metrics` |
| 10.5.6 Stage 5 | Evidence snapshot: signal breakdown, burst timeline, content clusters, account annex, graph structure + layout, run metadata | ✅ | `evidence.build_evidence_snapshot`; layout via Fruchterman-Reingold (documented substitute for ForceAtlas2, not the literal named algorithm) |
| 10.5.7 Stage 6 | Recurrence: fingerprint + Jaccard-based matching, `parent_network_id` chaining | ✅ | `recurrence.py`; relevance re-evaluated per claim on recurrence (10.5.1a point 8) |
| 10.5.8 points 1-3 | Scheduled / velocity-crossing / on-demand triggers | 🔀 (decision) / ✅ (mechanism) | All three trigger *decisions* (when/whether to call, recorded via `trigger_source`) are the backend's now; this service exposes the one mechanism (`POST /api/v1/detection/runs`) all three route through — see COORDINATION.md |
| 10.5.8 complexity mitigations | Inverted index, LSH banding, sparse ops, A_max cap, 10-min target for 5k accounts/7 days | ⚠️ | A_max cap done; LSH banding done (MinHash/`datasketch`); the O(n²) temporal/provenance/co-amplification loops are vectorized (numpy) but not further optimized with an inverted bin index — untested at 5,000-account scale |

## Confidence, Transparency, Suppression (10.6) — unchanged, still this service

| Rule | Status | Implementation |
|---|---|---|
| Score transparency (never show composite without breakdown) | ✅ | Every persisted `CoordinatedNetwork` carries SY/DU/CO/PR/AU alongside `coordination_score` — enforced at the table level, not just an API convention, since the backend reads this table directly |
| Confidence banding (High needs score AND breadth) | ✅ | `confidence.determine_confidence_band`, regression-tested against the spec's own example |
| Suppression: below N_min never surfaced | ✅ | `clustering.detect_communities` retention filter |
| Suppression: Low confidence hidden by default | 🔀 | Now a backend list-view concern (this service persists `confidence_band`; hiding Low by default is a query the backend makes) |
| Suppression: ≥60% allowlisted → suppressed entirely | ✅ | `confidence.is_allowlist_suppressed`; persisted with `allowlist_suppressed=true` rather than dropped, so the backend can suppress it on every surface from a stable row |
| Suppression: truncated/≥2 unavailable → capped at Medium | ✅ | `confidence.determine_confidence_band` |

## Governance (10.9)

| Rule | Status | Implementation |
|---|---|---|
| Behaviour only, never viewpoint | ✅ | Verified by inspection — no signal reads stance/sentiment |
| No attribution | ✅ | No field anywhere stores identity/sponsorship |
| No account-level automation verdict | ✅ | AU is cluster-only; no `is_bot` field anywhere |
| Human review before escalation | 🔀 | Review workflow moved to the backend; this service has no report-gating logic left to enforce |
| No automated enforcement | ✅ | No platform-API call exists in the codebase |
| Public data only | N/A | No purchased-data code path exists to violate this |
| Minimum necessary retention | ✅ | `governance.purge_expired_evidence(db, network_ids)`, exposed as `POST /api/v1/detection/snapshots/purge`; the "except reported" exception is preserved - computed on the backend's side (only it can see `cis_network_reports`) and handed over as a list, not lost |
| Standing disclaimer, verbatim, on every report and the detail page | 🔀 | Text preserved in COORDINATION.md for the backend to reproduce; this service no longer renders it anywhere |
| False-positive management: dismissal logged, reviewable in aggregate | 🔀 | Dismissal/review logging moved to the backend entirely |

## Data Model (10.10)

**10 tables** in `app/models/coordination.py`, matching the backend's actual
reference schema (`docs/sql/01_f5_reference_schema.sql`) verbatim - table names
singular, PKs table-specific, several "BEYOND 10.10" columns added per the
backend's own gap analysis: `detection_run`, `account`, `coordinated_network`,
`network_account`, `network_edge`, `network_evidence_post`, `network_burst_bin`,
`network_claim_link`, `offtopic_cluster`, and `evidence_snapshot` (new - not in the
original 9, added for the PDF report's chain-of-custody section). The 4
human-action tables (review log, allowlist, generated reports, export audit log)
plus the F4 config table live entirely on the backend's side, under names that are
the backend team's own call. `coordinated_network.review_status` is not a column
here - nothing on this side could ever write it once review moved away.

## Out of Scope / Explicitly Excluded (10.3, 10.9.1) — confirmed untouched

- Detection over Non-Existing/Synthetic claims — not built (correct, PRD excludes it).
- Cross-city/cross-tenant correlation — not built (correct).
- Automated reporting to platforms via API — not built (correct, and permanently excluded).
- Attribution / real-identity inference — not built (correct, permanently excluded).

---

## Summary

- **User stories (US43-64)**: all on the backend; not this service's scope to trace anymore.
- **Pipeline stages (10.5.1-10.5.8)**: all 8 stages implemented, including multi-claim batch runs (point 6, previously not built); trigger *decisions* are the backend's; 3 data-availability gaps (w_amp, w_meta/PR, w_struct) remain honest signal-input limitations, not logic bugs.
- **Governance**: rules about the pipeline's own behavior remain fully enforced; review/reporting/disclaimer surfaces are the backend's; the retention-purge exception ("except reported") is fully preserved via the backend-driven network-id list, not lost.
- **Data model**: 10/10 tables from the backend's actual reference schema, verified present and matching column-for-column - a correction from an earlier guessed 9-table shape, not just an extension of it.
