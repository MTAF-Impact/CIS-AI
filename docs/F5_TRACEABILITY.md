# F5 Traceability Matrix — PRD v1.4 Section 10 → Implementation

**Superseded by the G1 ownership rearchitecture.** An earlier session built the full
PRD v1.4 §10 spec inside this service (all of US43-64, PDF/ZIP reports, F4 admin
config — the original version of this document tracked that build item-by-item). A
later review of the backend integration doc
(`AI_REQUIREMENT_FOR_INTEGRATION_SUMMARY_V1.md`, section G) found that document
assigns F5 ownership differently, and the user chose to follow it: this service keeps
only the detection pipeline + 9 output tables + one run-trigger endpoint; everything
else (US43-64's list/detail/review/allowlist/report/config surface) moved to the
backend. That earlier per-US-story detail is git history now, not current scope — see
`COORDINATION.md` for what's actually here today.

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
| 10.5.1 Stage 0 | Candidate scope, allowlist/self-exclusion, A_max truncation | ✅ | `scope.select_candidates`; allowlist now sourced from the backend-owned `cis_coordination_allowlist` (see COORDINATION.md) |
| 10.5.1 point 5 | Claim-scoped observation (never use activity outside the claim cluster) | ✅ | Enforced by `pipeline._load_candidate_posts`'s query (`claim_id` + `stance=SUPPORTING` filter) |
| 10.5.1a | Claim-relevance gate: anchoring, evidence-volume, link-strength; off-topic clusters logged not surfaced | ✅ | `relevance_gate.evaluate_claim_relevance`; `OfftopicCluster` persistence in `pipeline._persist_offtopic_cluster` |
| 10.5.1 point 6 | Multi-claim/topic batch runs | ❌ | Not built — every run is single-claim (`run_detection_for_claim(claim_id, ...)`). `run_scheduled_sweep` calls it once per Active claim rather than a true pooled batch run |
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
| 10.5.8 points 1-3 | Scheduled / velocity-crossing / on-demand triggers | 🔀 (decision) / ✅ (mechanism) | All three trigger *decisions* (when/whether to call) are the backend's now; this service exposes the one mechanism (`POST /coordination/detection-runs`) all three route through — see COORDINATION.md |
| 10.5.8 complexity mitigations | Inverted index, LSH banding, sparse ops, A_max cap, 10-min target for 5k accounts/7 days | ⚠️ | A_max cap done; LSH banding done (MinHash/`datasketch`); the O(n²) temporal/provenance/co-amplification loops are vectorized (numpy) but not further optimized with an inverted bin index — untested at 5,000-account scale |

## Confidence, Transparency, Suppression (10.6) — unchanged, still this service

| Rule | Status | Implementation |
|---|---|---|
| Score transparency (never show composite without breakdown) | ✅ | Every persisted `CoordinatedNetwork` carries SY/DU/CO/PR/AU alongside `coordination_score` — enforced at the table level, not just an API convention, since the backend reads this table directly |
| Confidence banding (High needs score AND breadth) | ✅ | `confidence.determine_confidence_band`, regression-tested against the spec's own example |
| Suppression: below N_min never surfaced | ✅ | `clustering.detect_communities` retention filter |
| Suppression: Low confidence hidden by default | 🔀 | Now a backend list-view concern (this service persists `confidence_band`; hiding Low by default is a query the backend makes) |
| Suppression: ≥60% allowlisted → suppressed entirely | ✅ | `confidence.is_allowlist_suppressed` |
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
| Minimum necessary retention | ⚠️ | `governance.purge_expired_evidence`, runs automatically inside every scheduled sweep; no longer scheduler-independent (folded into the sweep so exactly one F5 route stays exposed), and no longer skips reported networks (report status is invisible to this service now) — a deliberate behavior change, see COORDINATION.md |
| Standing disclaimer, verbatim, on every report and the detail page | 🔀 | Text preserved in COORDINATION.md for the backend to reproduce; this service no longer renders it anywhere |
| False-positive management: dismissal logged, reviewable in aggregate | 🔀 | Dismissal/review logging moved to the backend entirely |

## Data Model (10.10)

**9 of the 13 originally-built PRD tables remain** (`app/models/coordination.py`),
matching the backend doc's ownership table exactly: `detection_run`,
`coordinated_network`, `network_account`, `account`, `network_edge`,
`network_evidence_post`, `network_burst_bin`, `network_claim_link`,
`offtopic_cluster`. The other 4 (`network_review_log`, `coordination_allowlist`,
`network_report`, `export_audit_log`) plus the F4 `coordination_settings` singleton
were dropped from this service (and from the live DB) — that responsibility, and
whatever tables back it, is now the backend's. `coordinated_network.review_status`
was also dropped (vestigial once review moved away — nothing on this side could
write it anymore).

## Out of Scope / Explicitly Excluded (10.3, 10.9.1) — confirmed untouched

- Detection over Non-Existing/Synthetic claims — not built (correct, PRD excludes it).
- Cross-city/cross-tenant correlation — not built (correct).
- Automated reporting to platforms via API — not built (correct, and permanently excluded).
- Attribution / real-identity inference — not built (correct, permanently excluded).

---

## Summary

- **User stories (US43-64)**: all moved to the backend; not this service's scope to trace anymore.
- **Pipeline stages (10.5.1-10.5.8)**: all 8 stages implemented and unchanged by the rearchitecture, except trigger *decisions* moving to the backend; 3 data-availability gaps (w_amp, w_meta/PR, w_struct) remain honest signal-input limitations, not logic bugs.
- **Governance**: rules about the pipeline's own behavior remain fully enforced; rules about review/reporting/disclaimer moved to the backend along with those surfaces; retention purge changed behavior (age-based only, folded into the sweep).
- **Data model**: 9/9 tables from the backend doc's ownership list, verified present; the other 4 tables + F4 config table dropped from both the code and the live DB.
