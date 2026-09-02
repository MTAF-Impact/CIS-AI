# Claim Scoring System — Reference

Implements PRD v1.3.1 Section 6 exactly. Pure-function math lives in
`app/services/scoring_engine.py` (zero I/O, fully unit-tested — see `tests/unit/test_scoring_engine.py`),
except Falseness (F), which needs a database round-trip and lives separately in
`app/services/falseness_service.py`.

**Applies to Existing/Generic claims only.** Non-Existing/Synthetic claims are never
scored — every score column stays `NULL` on those rows, by design (see `DATA_MODEL.md`).

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
- `w1=w2=w3=w4=0.25` (`ReachWeights`, currently equal-weighted placeholders — the PRD
  leaves per-component weighting unspecified; tune `DEFAULT_REACH_WEIGHTS` in
  `scoring_engine.py` if given real values).
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
V = 100 × sigmoid(V_zscore)                            # squashed to [0, 100]
```

- `Volume_t` / `Volume_t-Δ` — Supporting-post counts in the current vs. previous 24h
  window (`ROLLING_WINDOW_HOURS = 24.0`), per-claim.
- `ε = 1.0` — prevents division-by-zero for a brand-new claim.
- Baseline mean/std comes from `topic_volume_buckets` (hourly Supporting-volume history
  per topic) — bucket-over-bucket deltas, at least 3 buckets required or the baseline
  defaults to `(0, 0)`, which the sigmoid squash then neutrally maps to `50`
  ("no baseline yet" cold-start).
- Squashed via a numerically-stable logistic sigmoid (branches on the sign of `z` to
  avoid overflow on large negative inputs — `z=0 → 50`, unbounded `z` approaches `0` or
  `100`).

### F — Falseness Confidence

*How confidently the claim can be confirmed false, against verified official sources.*

```
F = SimilarityToKnownDebunk × 100     (or NULL if neither path below finds anything)
```

Two paths, tried in order (`app/services/falseness_service.py`):

1. `SimilarityToKnownDebunk` — top cosine-similarity match between the claim's own
   embedding and every `official_sources.embedding` (pgvector `cosine_distance`,
   `similarity = 1 - distance`), threshold `0.55` (`DEFAULT_MATCH_THRESHOLD`).
   `official_sources` is seeded from TurnBackHoax.id's public feed
   (`scripts/seed_debunk_corpus.py`, safe to re-run periodically) — see `DATA_MODEL.md`
   and `docs/SOURCES.md`.
2. If that misses, a live Google Fact Check Tools API lookup on the claim's own text
   (`app/services/fact_check_client.py`) — any matching `ClaimReview` with a
   false-reading `textualRating` scores a fixed `LIVE_FACT_CHECK_MATCH_SCORE` (75.0),
   since it's a real verified verdict rather than a modelled similarity. Silently
   skipped whenever `GOOGLE_API_KEY` is unset.
- If neither path finds anything, **F is `NULL`, never `0`.** `0` would wrongly assert
  "confirmed true"; `NULL` means "no signal either way," which is handled explicitly in
  the composite formula below.

### H — Harm Severity

*Estimated real-world damage if the claim is left unaddressed.*

```
H = 0.35·PublicSafety + 0.30·InstitutionalTrust + 0.20·Economic + 0.15·PolicyDisruption
```

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

Weight rationale: F and H carry the highest combined weight (0.60) — this is a
risk-triage tool, not a virality tracker. R+V (0.30 combined) capture urgency of spread.
EI (0.10) is weighted lowest.

**If F is `NULL`** (neither the corpus match nor the live Fact Check API fallback finds
anything — still common for a novel claim with no prior fact-check): F's 0.30 weight is
**dropped and the remaining weights renormalize** to sum to 1.0 —
```
ClaimScore = (0.15·R + 0.15·V + 0.30·H + 0.10·EI) / 0.70
```
— rather than treating missing F as `0`, which would wrongly assert "confirmed true".

Bounded `[0, 100]` by construction (weights sum to 1.0, every input is already `[0,100]`);
`scoring_engine.claim_score()` still clamps as a safety net.

---

## Net Pushback Ratio (NPR) discount

*Captures how much the public is already self-correcting the claim — discounts the
score accordingly, without ever erasing it.*

```
NPR = OpposingVolume / (SupportingVolume + OpposingVolume)     # rolling 24h window
DiscountFactor = 1 - (γ × NPR)                                  # γ = 0.5
FinalClaimScore = ClaimScore × DiscountFactor
```

- `γ = 0.5` (`GAMMA`) — even total pushback (`NPR = 1`) reduces the score by at most 50%.
- `DiscountFactor` bounded `[0.5, 1]`; since `ClaimScore ∈ [0,100]`, `FinalClaimScore` is
  guaranteed `[0,100]`.
- **Edge case — dormant:** if Supporting + Opposing volume is `0` in the window, `NPR` is
  **not computed** (`NULL`) and the claim is flagged `is_dormant: true` instead of being
  discounted. A dormant claim's `discount_factor` reads `1.0` (no discount) — dormancy
  must be *flagged*, never silently discounted.
- **Edge case — reliability threshold:** if total volume (Supporting+Opposing) is below
  `RELIABILITY_THRESHOLD = 25` posts (midpoint of the PRD's recommended 20–30 range),
  `DiscountFactor` defaults to `1.0` regardless of `NPR` — too little data to trust the
  pushback signal.

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
