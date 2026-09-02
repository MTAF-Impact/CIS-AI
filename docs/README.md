# CIS AI Service — Documentation

This is the deep-reference documentation set for the CIS AI Service. If you're
integrating against this service from the Go backend (or any other consumer), start
with **API Reference** and **Go Integration**.

| Doc | What's in it |
|---|---|
| [`API_REFERENCE.md`](./API_REFERENCE.md) | **Every** endpoint: method, path, auth, full request/response schema field-by-field, status codes, error cases, example JSON. Start here to consume this API. |
| [`GO_INTEGRATION.md`](./GO_INTEGRATION.md) | The full HTTP contract with the Go backend (8 backend→AI + 2 AI→backend flows as of PRD v1.5) — expanded from the backend repo's own `AI-INTEGRATION.md` with implementation-level detail on this side: exact code locations, retry/idempotency guarantees, error handling, config. |
| [`DATA_MODEL.md`](./DATA_MODEL.md) | Every table this service owns, column by column: types, nullability, defaults, FKs, what each field means and when it's populated. The ownership boundary with the Go backend's `cis_*` tables. |
| [`SCORING.md`](./SCORING.md) | The full Claim Scoring System reference — every formula, every weight, every edge case (dormancy, missing Falseness, reliability threshold), when scores are (re)computed. |
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | Tech stack, request lifecycle, the background-task pattern (read this before adding any async work), module map, key design decisions. |
| [`MODULES.md`](./MODULES.md) | Function-by-function walkthrough of every file in `app/services/` — what it does, what calls it, what it calls. |
| [`SETUP.md`](./SETUP.md) | Local dev, every environment variable, seeding/schema-reset scripts, testing, CI/CD, deployment target. |
| [`CRAWLER.md`](./CRAWLER.md) | The `crawler/` module that feeds real content into `/ingest/batch` (RSS, YouTube, Telegram) — env vars, local dry-run, manual setup steps. Ships inside the AI service's own deployment, triggered via `POST /admin/run-crawler`. |
| [`SOURCES.md`](./SOURCES.md) | The data-source registry: what's wired up (and where), what's deferred and why, feasibility/impact of every candidate source from the Data Pipeline & Source Spec v1.0 audit. |
| [`COORDINATION.md`](./COORDINATION.md) | F5 — Coordinated-Network Detector: the detection pipeline (Stages 0-6), the one run-trigger endpoint, the 9-table data model, config, and governance — scoped to what this service owns per the backend ownership split (list/detail/review/allowlist/reports/F4-config moved to the backend). |
| [`F5_TRACEABILITY.md`](./F5_TRACEABILITY.md) | PRD v1.4 Section 10 requirements mapped to current status — pipeline stages still traced item-by-item; US43-64 marked moved-to-backend now that ownership split. |

The top-level [`README.md`](../README.md) stays the quick-start entry point (install,
run, curl examples) — these docs are the exhaustive reference behind it.

## Where to start, by task

- **"I need to call this API from the Go backend"** → `API_REFERENCE.md` +
  `GO_INTEGRATION.md`.
- **"I need to read/join this service's tables directly"** → don't — see the ownership
  rule in `GO_INTEGRATION.md`. Read-only `SELECT` access is fine; anything else goes
  through the HTTP flows listed there.
- **"What does `final_claim_score` actually mean and how is it computed"** →
  `SCORING.md`.
- **"I'm adding a new endpoint that needs to do slow/async work"** →
  `ARCHITECTURE.md`'s background-task section first, or you will reintroduce a bug
  that's already shipped and been fixed once.
- **"I need to understand what a specific service function does"** → `MODULES.md`.
- **"I need to run this locally / add a test"** → `SETUP.md`.
