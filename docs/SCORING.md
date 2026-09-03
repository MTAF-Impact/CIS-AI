# Claim Scoring System — Reference

Implements PRD v1.3.1 Section 6 exactly. Pure-function math lives in
`app/services/scoring_engine.py` (zero I/O, fully unit-tested — see `tests/unit/test_scoring_engine.py`),
except Falseness (F), which needs a database round-trip and lives separately in
`app/services/falseness_service.py`.

**Applies to Existing/Generic claims only.** Non-Existing/Synthetic claims are never
scored — every score column stays `NULL` on those rows, by design (see `DATA_MODEL.md`).

## Dynamic parameters (`cis_settings`)

Every weight and threshold named in this doc is a **default**, not a hardcoded
constant — the real value is read from the backend-owned `cis_settings` table at
runtime, cached 30s, snapshotted once per scoring pass via
`app/services/config_service.RuntimeConfig` (see
`documentation/CIS/AI_DYNAMIC_PARAMETER.md` for the full parameter catalog and the
ownership/write-path rules — this service is `SELECT`-only). A missing row (or a
missing table, e.g. before the backend has deployed it) is not an error: every value
falls back to the constant documented here.

Two defaults were deliberately chosen to diverge from that doc's own suggested
values (decided with the user, 2026-09-03):
- **`scoring.velocity_epsilon` defaults to `1.0`, not `0.0001`.** `0.0001` removes
  today's deliberate low-volume damping — see V's formula below.
- **`scoring.velocity_interval_hours` and `scoring.npr_window_hours` both default to
  `24`**, matching today's single window, not the doc's suggested `6`/`36` split.
  Diverging from `24` is an intentional follow-up, not something that should happen
  implicitly just because this table now exists.

**Not yet wired into scoring**, even though `cis_settings` has rows for them: the
Reach normalization window (`scoring.reach_normalization_window_days`, AP-10 — needs
a real query-level time filter added to `clustering_service._reach_inputs`, not just
a config read) and the debunk-segment cap (`ai.debunk_segment_max_count`, AP-21).
Both are real behavior changes the source doc itself flags as deserving their own
before/after comparison, deferred on purpose rather than landed silently alongside
this pass.

## Design principle

Every formula must be explainable in plain terms to a non-technical policy reviewer, and
every score must be traceable to its inputs — no parameter should produce a number that
can't be audited. This is why `GET /claims/{id}` always returns every individual
component, never just the collapsed `final_claim_score`.

## Scoring scope: Supporting vs. Opposing content

Every claim cluster contains two sides: **Supporting** content (posts spreading the
claim) and **Opposing** content (posts organically disputing/correcting it — assessed
per-post via `stance` classification, see `DATA_MODEL.md`'s `content_items.stance`).

- **R, V, F, H, EI are calculated exclusively on Supporting-side content.** These five
  answer one question — *how dangerous is the false claim itself, right now* — and are
  never blended with Opposing-side data.
- Opposing-side content enters the pipeline in exactly one place: the Net Pushback Ratio
  (NPR), below.
- `EI_opposing` is the same EI formula applied to Opposing content, for **display only**
  — it never feeds `claim_score`, `npr`, `discount_factor`, or `final_claim_score`.
  Reaction data alone can't disambiguate what an Opposing-side reaction is actually
  agreeing with (the debunker, or defending the original claim) — folding it into
  scoring would break auditability.

---

## The 5 primary parameters

All five are normalized to strictly `[0, 100]` before being weighted.

### R — Reach & Spread

*How far and wide the claim has traveled.*

```
R_raw = w1·log(1+Impressions) + w2·log(1+UniqueAuthors)
      + w3·log(1+ContentCount) + w4·(DistinctPlatforms / TotalMonitoredPlatforms)

R = min-max normalize R_raw to [0, 100], scoped per-topic
```

- `Impressions` — `SUM(content_items.impressions)` across the claim's Supporting posts.
- `UniqueAuthors` — `COUNT(DISTINCT content_items.author_id)`, Supporting posts.
- `ContentCount` — `COUNT(*)`, Supporting posts.
- `DistinctPlatforms` — `COUNT(DISTINCT content_items.source)`, Supporting posts;
  `TotalMonitoredPlatforms = 5` (`len(ContentSource)`).
- `w1=w2=w3=w4=0.25` by default (`scoring.reach_weight_*` in `cis_settings`,
  `ReachWeights` in code — equal-weighted placeholders; the PRD leaves per-component
  weighting unspecified, these are **not** sum-constrained since min-max
  normalization afterwards makes the absolute scale irrelevant).
- `log(1+x)` — prevents one viral outlier from dominating.
- **Normalization is scoped per-topic**, not globally: every Existing claim's raw R
  within the same topic is min-max scaled together (`normalize_minmax_per_topic`). A
  single-claim topic (or all-equal raw values) maps to `50` (neutral midpoint) rather
  than dividing by zero. Re-run whenever a claim's Reach inputs change
  (`clustering_service.renormalize_topic_reach`, called after every clustering pass and
  every ingest that touches a topic).

### V — Velocity

*How fast the claim is currently growing, independent of its total size.*

```
V_raw = (Volume_t - Volume_t-Δ) / (Volume_t-Δ + ε)
V_zscore = (V_raw - baseline_mean) / baseline_std      # topic's historical baseline
V = min-max map clamp(V_zscore, z_min, z_max) onto [0, 100]
```

- `Volume_t` / `Volume_t-Δ` — Supporting-post counts in the current vs. previous
  window (`scoring.velocity_interval_hours`, defaults to `24`), per-claim.
- `ε` (`scoring.velocity_epsilon`, defaults to `1.0`) — deliberate low-volume damping,
  not a bare division-by-zero guard: going from 0→5 posts reads as `5/1`, not `5/ε≈0`.
- Baseline mean/std comes from `topic_volume_buckets` (hourly Supporting-volume history
  per topic) — bucket-over-bucket deltas, at least 3 buckets required or the baseline
  defaults to `(0, 0)`, which then maps to `V_zscore = 0` ("no baseline yet"
  cold-start).
- `z_min`/`z_max` (`scoring.velocity_zscore_min`/`_max`, default `-3`/`3`) — `V_zscore`
  is **clamped** to this range, then linearly mapped onto `[0, 100]` (PRD §6.2.2's
  min-max form, chosen over a sigmoid squash: an admin-configured `z_min`/`z_max`
  should mean exactly what it says — `z_min` maps to `0`, `z_max` maps to `100` — not
  an asymptote the score approaches but never reaches). With the symmetric default
  range, `z=0 → 50`; a `z_zscore` outside `[z_min, z_max]` saturates at `0`/`100`
  rather than continuing to grow.

### F — Falseness Confidence

*How confidently the claim can be confirmed false, against verified official sources.*

```
F = SimilarityToKnownDebunk × 100     (or NULL if neither path below finds anything)
```

Two paths, tried in order (`app/services/falseness_service.py`):

1. `SimilarityToKnownDebunk` — top cosine-similarity match between the claim's own
   embedding and every `official_sources.embedding` (pgvector `cosine_distance`,
   `similarity = 1 - distance`), threshold `0.55` by default
   (`scoring.falseness_match_threshold`). `official_sources` is seeded from
   TurnBackHoax.id's public feed (`scripts/seed_debunk_corpus.py`, safe to re-run
   periodically) — see `DATA_MODEL.md` and `docs/SOURCES.md`.
2. If that misses, a live Google Fact Check Tools API lookup on the claim's own text
   (`app/services/fact_check_client.py`) — any matching `ClaimReview` with a
   false-reading `textualRating` scores a fixed `75.0` by default
   (`scoring.falseness_live_match_score`), since it's a real verified verdict rather
   than a modelled similarity. Silently skipped whenever `GOOGLE_API_KEY` is unset.
- If neither path finds anything, **F is `NULL`, never `0`.** `0` would wrongly assert
  "confirmed true"; `NULL` means "no signal either way," which is handled explicitly in
  the composite formula below.

### H — Harm Severity

*Estimated real-world damage if the claim is left unaddressed.*

```
H = 0.35·PublicSafety + 0.30·InstitutionalTrust + 0.20·Economic + 0.15·PolicyDisruption
```

Weights above are the defaults (`scoring.harm_weight_*`). `PolicyDisruption` carries a
hard ceiling of `0.25`, clamped on read regardless of what the table holds — PRD
§6.2.4's bias guardrail against scoring policy criticism itself as harm (the backend
also enforces this on write; this is a second layer, not a replacement).

Each sub-score is AI-classified (`LLMClient.classify_harm`) against a detailed 5-band
rubric (0–20 / 21–40 / 41–60 / 61–80 / 81–100, each with concrete example scenarios —
see the full text in `app/services/llm_client.py::HARM_CLASSIFICATION_SYSTEM_PROMPT`),
then human-confirmable/overridable via `PATCH /claims/{id}/harm/confirm`
(`Claim.harm_human_confirmed`).

The classification prompt is optionally grounded in live BMKG weather-hazard context
(`app/services/hazard_context_service.fetch_bmkg_context`, threaded through as
`classify_harm`'s `hazard_context` param) — a claimed hazard that contradicts an
active forecast supports a higher `public_safety` score than the same claim text with
no grounding available. Silently omitted from the prompt whenever unreachable/no
`BMKG_ADM4_CODES` configured, never padded with a placeholder. See `docs/SOURCES.md`.

`PolicyDisruption` is deliberately the lowest-weighted sub-score, and scored
*conservatively and narrowly* — only the claim's concrete effect on policy execution
(inciting active boycotts/interference), **never mere criticism or disagreement** with a
policy. A government risk-triage tool must not treat ordinary policy criticism as "harm".

### EI — Emotional/Moral Intensity

*How angry or provoked the public reaction is.*

```
EI = (0.5 × OutrageWordDensity + 0.5 × NegativeReactionRatio) × 100
```

- `OutrageWordDensity` / `NegativeReactionRatio` — 0–1 ratios, computed from
  Supporting-side `content_items.outrage_score` (LLM-assessed at ingestion) and
  `negative_reaction_count`/`positive_reaction_count` respectively.
- `EI_opposing` — identical formula, Opposing-side content, **display only** (see above).

---

## Composite Claim Score

```
ClaimScore = 0.15·R + 0.15·V + 0.30·F + 0.30·H + 0.10·EI
```

Weights above are the defaults (`scoring.weight_*`, sum-constrained to `1.00` on the
backend's write path). Weight rationale: F and H carry the highest combined weight
(0.60) — this is a risk-triage tool, not a virality tracker. R+V (0.30 combined)
capture urgency of spread. EI (0.10) is weighted lowest.

**If F is `NULL`** (neither the corpus match nor the live Fact Check API fallback finds
anything — still common for a novel claim with no prior fact-check): F's weight is
**dropped and the remaining weights renormalize** over `1 - weight_falseness` —
```
ClaimScore = (0.15·R + 0.15·V + 0.30·H + 0.10·EI) / 0.70
```
— rather than treating missing F as `0`, which would wrongly assert "confirmed true".
This renormalization follows whatever `weight_falseness` actually is at read time, not
a hardcoded `0.70`.

Bounded `[0, 100]` by construction (weights sum to 1.0, every input is already `[0,100]`);
`scoring_engine.claim_score()` still clamps as a safety net.

---

## Net Pushback Ratio (NPR) discount

*Captures how much the public is already self-correcting the claim — discounts the
score accordingly, without ever erasing it.*

```
NPR = OpposingVolume / (SupportingVolume + OpposingVolume)     # rolling window
DiscountFactor = 1 - (γ × NPR)                                  # γ defaults to 0.5
FinalClaimScore = ClaimScore × DiscountFactor
```

- Window defaults to `24h` (`scoring.npr_window_hours`) — same default as V's interval
  today, though they're two independent settings now (diverging them, per the doc's
  suggested `36h` for NPR, is an intentional follow-up).
- `γ` (`scoring.discount_gamma`, defaults to `0.5`) — even total pushback (`NPR = 1`)
  reduces the score by at most `1 - γ`.
- `DiscountFactor` bounded `[1-γ, 1]`; since `ClaimScore ∈ [0,100]`, `FinalClaimScore` is
  guaranteed `[0,100]`.
- **Edge case — dormant:** if Supporting + Opposing volume is `0` in the window, `NPR` is
  **not computed** (`NULL`) and the claim is flagged `is_dormant: true` instead of being
  discounted. A dormant claim's `discount_factor` reads `1.0` (no discount) — dormancy
  must be *flagged*, never silently discounted. This rule is **not configurable** — it's
  a correctness rule about missing data, not a tuning knob.
- **Edge case — reliability threshold:** if total volume (Supporting+Opposing) is below
  `scoring.npr_reliability_minimum_posts` (defaults to `25`, midpoint of the PRD's
  recommended 20–30 range), `DiscountFactor` defaults to `1.0` regardless of `NPR` — too
  little data to trust the pushback signal.

---

## When scores are (re)computed

| Trigger | What runs |
|---|---|
| Clustering (`cluster_unclustered_content`, auto-triggered after ingest or `POST /claims/cluster-now`) | Full pipeline for every newly-touched claim: R/V/F/H/EI computed fresh, topic Reach renormalized across every claim in the touched topic (a sibling's Reach change makes its own cached score stale even with zero new content), NPR/discount/final recomputed. |
| `POST /claims/rescore` | NPR/Velocity/discount/final only, for **every** existing claim, independent of clustering — necessary because NPR can drift purely from wall-clock time (old Opposing posts aging out of the window) even with zero new activity. |
| `PATCH /claims/{id}/harm/confirm` | Recomputes `harm_score` from the (possibly human-overridden) sub-scores, then re-runs the full `rescore_claim` pass for that one claim. |

Every rescore appends a new `claim_score_snapshots` row (`final_claim_score` +
timestamp) — this is the only history of how a claim's score moved, and is what backs
the F3 trend chart (`GET /alerts/chart`).

`GET /claims/{id}` **always reads the cached columns** — it never recomputes live.
