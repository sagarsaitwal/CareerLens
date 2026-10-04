# CareerLens — Architecture (Proposed, High-Level)

> This document describes the **proposed** high-level architecture only.
> No code, containers, or infrastructure have been created yet. Decisions
> here are a direction, not a commitment — they will be revisited as
> implementation begins.

This document owns **system boundaries**: components, processing layers,
trust boundaries, version pinning, concurrency, caching, failure
architecture, and third-party capability limits. Data definitions are owned
by [DATABASE.md](DATABASE.md); matching behaviour by
[AI-MATCHING.md](AI-MATCHING.md).

## 1. Goals of this document

- Give a shared mental model of how the major pieces fit together.
- Define component boundaries and responsibilities.
- Avoid premature detail: no API contracts, schemas, or container specs
  here.

## 2. System Overview

```
                    ┌─────────────────┐
   browser ────────▶│      Nginx      │  reverse proxy +
                    │                 │  serves built static frontend
                    └────────┬────────┘
                             │ /api
                             ▼
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐
│  Frontend   │     │     Backend      │────▶│  PostgreSQL │
│ React+Vite  │     │    (FastAPI)     │◀────│  (source of │
│ static build│     │                  │     │   truth)    │
└─────────────┘     └──────────────────┘     └─────────────┘
                            │  ▲                     ▲
                            ▼  │                     │
                     ┌──────────────────┐            │
                     │      Redis       │            │
                     │ queue / cache /  │            │
                     │ locks (NOT state)│            │
                     └──────────────────┘            │
                            │  ▲                     │
                            ▼  │                     │
                     ┌──────────────────┐            │
                     │     Workers      │────────────┘
                     │ (discovery,      │
                     │  resolution,     │  APScheduler enqueues
                     │  matching)       │  periodic work into Redis
                     └──────────────────┘
                        │            │
                        ▼            ▼
              ┌──────────────┐  ┌──────────────┐
              │ External Job │  │ LLM Provider │
              │ Sources      │  │  (boundary)  │
              └──────────────┘  └──────────────┘
```

The frontend is a **static build** served by Nginx — there is no Node
runtime in production. This is a deliberate choice for the minimum
deployment target (see
[SYSTEM-REQUIREMENTS.md](SYSTEM-REQUIREMENTS.md)).

Two boundaries carry security weight and are detailed in
[Section 6](#6-trust-and-security-boundaries): external job sources
(untrusted input) and the LLM provider (data egress).

## 3. Components

### 3.1 Frontend (`frontend/`)

- **Stack:** React + TypeScript + Vite + Tailwind CSS. Built to static
  assets and served by Nginx; **no server-side rendering and no Node
  runtime in production**.
- **Responsibilities:** dashboard and analytics, job match review,
  application tracker, resume version management, in-app notifications,
  profile and matching-criteria configuration.
- **Explicitly not responsible for:** initiating applications, sending
  messages, or any automated outbound action. "Open application page"
  opens a link; it never submits anything.
- Must present `match_score` **together with** fit coverage, and must
  render the mandatory disclaimers for indeterminate and not-evidenced
  outcomes
  ([AI-MATCHING.md Section 9](AI-MATCHING.md#9-explanation-requirements)).

### 3.2 Backend (`backend/`)

- **Stack:** Python + FastAPI.
- **Responsibilities:** REST API, application tracking, resume management,
  dashboard aggregation, orchestration of discovery/resolution/matching
  runs, catalog administration surfaces.
- Enforces the safety boundaries in
  [REQUIREMENTS.md](REQUIREMENTS.md#8-user-control--safety-boundaries) **at
  the API layer** — no endpoint exists that auto-applies or auto-messages.

### 3.3 Workers (`workers/`)

- **Scheduler:** APScheduler, running in-process. It **enqueues work into
  Redis** rather than executing it; the worker process consumes the queue.
  The two are complementary — APScheduler decides *when*, Redis carries
  *what*.
- **Discovery workers:** fetch permitted public sources and career pages,
  normalize postings, detect new/updated/duplicate postings, resolve a
  verified direct application URL.
  - **Browser automation:** Playwright, only where permitted, and only to
    *read* publicly available posting data.
  - **No live external source is integrated yet.** Which sources to
    integrate first remains deferred
    ([Section 8](#8-explicitly-deferred)) and any fetcher is bound by each
    source's terms of service and `robots.txt`, so ingestion is currently
    **fixture-driven**: postings are loaded from local files through the
    same validated contract a fetcher would produce. Fixture content is
    still untrusted input and is validated, not trusted. Swapping in a
    real fetcher changes where the bytes come from and nothing about how
    they are deduplicated or resolved.
- **Resolution workers:** run the skill resolution cascade at ingestion,
  process re-resolution after catalog changes, and accumulate promotion
  evidence.
- **Matching workers:** run Stages 2–4 against a pinned `ProfileVersion`
  and a pinned catalog version.

Workers are separated from the backend API process so long-running
scraping and matching cannot block request/response cycles.

### 3.4 Skill Catalog

Shared reference data (`Skill`, `SkillAlias`, `SkillRelationship`,
promotion evidence, conflicts — see
[DATABASE.md](DATABASE.md#21-skill)), neither user-specific nor
job-specific.

- Read by resolution workers at ingestion and by matching workers via a
  **pinned version**.
- Written only through **evidence-gated promotion** or explicit curation —
  never directly by a model.
- Whether it is a separate service or a schema within the main database is
  deferred; the *boundary* is what matters.

### 3.5 Redis

**Transport and cache only — never durable application state.** Losing
Redis must cost throughput, not correctness.

Three roles:

| Role | Use |
|---|---|
| Queue transport | `ingest`, `resolve`, `match` work items enqueued by APScheduler or the API |
| Cache | **Semantic verdict cache**, keyed on representation pair + `catalog_version` + `model_version` (see [Section 4.1](#41-caching)) |
| Distributed locks | Guarding catalog promotion and `ProfileVersion` minting against concurrent duplicates |

Anything that must survive a Redis flush belongs in PostgreSQL.

### 3.6 Nginx

Reverse proxy and static file server:

- Serves the built React/Vite assets directly.
- Proxies `/api` to the FastAPI service.
- Terminates the single public entry point, so the API and worker are not
  exposed directly.

### 3.7 Database (`database/`)

PostgreSQL 16+. **The source of truth for all durable state.** Database
constraints — not only application validation — enforce the invariants in
[DATABASE.md Section 6](DATABASE.md#6-database-invariants). See
[DATABASE.md](DATABASE.md).

### 3.8 Scripts (`scripts/`)

Reserved for future maintenance scripts. Empty for now.

## 4. Processing Layers

```
Ingestion          fetch, dedupe, Stage 1 extraction
     |
Resolution         tiered cascade -> persisted resolution + provenance
     |             (runs here, NOT at match time)
     |
Matching           Stage 2 (deterministic) -> Stage 3 (selective) -> Stage 4
     |
Presentation       score + coverage + category + explanation
```

The architectural significance of resolving at **ingestion** rather than
match time:

- Matching reads persisted identity, so **Stage 2 performs no model
  calls**.
- The N×M explosion of comparing every profile skill against every job
  skill with a model is avoided structurally.
- A catalog change triggers **re-resolution of live data only** — never a
  rewrite of history.

### 4.1 Caching

- **Semantic comparison verdicts are cached**, keyed by representation
  pair + `catalog_version` + `model_version`. A verdict is asked once, not
  once per posting.
- Extraction output is cached per `dedupe_key`.
- *Recommendation:* batch Stage 3 per job rather than per skill pair.
- Cache lifetimes and sizes are deferred.

## 5. Cross-Cutting Concerns

### 5.1 AI Matching

A hybrid of deterministic rule-based scoring and LLM semantic analysis,
with a resolution layer between extraction and scoring. Detailed in
[AI-MATCHING.md](AI-MATCHING.md).

### 5.2 Notifications

In-app only. The backend writes notification records; the frontend reads
them. No email/SMS integration.

### 5.3 Observability

Three metric classes — user-facing, system-health, and
catalog-improvement — defined in
[AI-MATCHING.md Section 14](AI-MATCHING.md#14-observability-and-cost).
Unresolved outcomes are instrumented **separately from errors**.

## 6. Trust and Security Boundaries

### 6.1 Third-party capability boundary

The "never automatically apply, message, or act on LinkedIn" rule is an
**architectural invariant enforced at the API and service boundary**, not
a UI convention:

- No worker holds credentials for third-party job platforms or LinkedIn.
- No component can submit forms, authenticate as the user, or perform any
  write action on an external site.
- Playwright is scoped to reading public posting data.
- No code path exists for auto-apply, recruiter messaging, or referral
  requests.

### 6.2 Untrusted input boundary

`JobPosting.description_raw` and all external job text are **untrusted
data from the open internet**. They are treated as data, never as
instructions, and model output derived from them is accepted only through
a structurally validated contract — see
[AI-MATCHING.md Section 10](AI-MATCHING.md#10-ai-safety-and-bounded-behaviour).

### 6.3 LLM Trust Boundary

> **Only `ProfileVersion` content may cross the LLM boundary.**

Because the matching engine's sole profile input is an immutable
`ProfileVersion`
([DATABASE.md Section 3.2](DATABASE.md#32-historical-integrity-of-match-results)),
and that snapshot contains only matching-relevant inputs, the following
**cannot** reach a model by construction:

- Resume files and resume content
- CTC (current, offered, negotiated)
- Recruiter contacts
- Interview notes and dates
- Application history and status
- Personal notes
- Any other non-matching profile information

An access-control rule that exists for correctness also delivers data
minimization. This boundary must be enforced explicitly, not assumed.

### 6.4 Secrets and audit

- Secrets live in environment/configuration **only** — never in the
  repository, logs, audit records, snapshots, or error messages.
- Audit records are **append-only**.
- User confirmations are attributable and timestamped.

## 7. Consistency, Concurrency and Failure Behaviour

### 7.1 Version pinning

- A **match run pins one catalog version at start** and uses it
  throughout. Mid-run drift would make results within a single run
  mutually incomparable.
- An **ingestion operation likewise pins one catalog version at start**
  and resolves every mention in that operation against it, persisting
  that version per mention
  ([DATABASE.md Section 4.3](DATABASE.md#43-idempotency-requirements)).
- A match is computed against one pinned `ProfileVersion`.
- All four version axes are recorded on the resulting `MatchResult`
  ([AI-MATCHING.md Section 11](AI-MATCHING.md#11-reproducibility--two-distinct-meanings)).

### 7.2 Concurrency

- Concurrent promotion of the same representation **converges on one
  canonical entry** via normalized-representation uniqueness.
- Concurrent alias creation is **serialized**; the losing attempt fails
  loudly as a conflict rather than silently overwriting.
- `ProfileVersion` creation is **atomic** — a snapshot never captures a
  half-applied profile edit or a partially-applied re-resolution.
- Concurrent re-resolution and matching are safe, because matching reads a
  pinned catalog version and persisted resolutions.
- Concurrent triggers must not mint duplicate `ProfileVersion`s for the
  same outcome.

### 7.3 Failure architecture

> **Failure must never manufacture a negative conclusion.**

The behaviour table is owned by
[AI-MATCHING.md Section 12](AI-MATCHING.md#12-failure-and-degradation). The
architectural consequence:

- **LLM outage is a degradation path** — Stage 3 is skipped, coverage
  drops, the system keeps serving honest partial results.
- **Catalog outage is a blocking condition** — matching must not proceed,
  because wrong identity resolution is worse than no answer.

This asymmetry means the catalog is on the **critical path** for matching
while the LLM provider is not. Availability expectations for the two
differ accordingly.

Degraded results record a cause and are eligible for re-match.

## 8. Explicitly Deferred

- API endpoint contracts.
- **Authentication/authorization mechanism** — now also carrying the
  requirement that user confirmations be attributable.
- Deployment topology.
- Specific job sources to integrate first.
- Whether the skill catalog is a separate service or a schema within the
  main database.
- Catalog-version pinning granularity beyond "per match run".
- Numeric targets: latency budgets, cache lifetimes, batch sizes, Stage 3
  invocation ceilings, LLM cost limits.
- Degraded-result re-match prioritization relative to ordinary staleness.
- Observability thresholds distinguishing normal catalog maturation from
  broken promotion.

These will be addressed in later phases — see [ROADMAP.md](ROADMAP.md).

## 9. Development and Deployment Environment

> **CareerLens is a WSL2-only project. Windows is the host OS and
> nothing more.**
>
> No CareerLens component — Python, Node, Docker, PostgreSQL, Redis or
> Nginx — is installed on Windows. All development, building, testing,
> container execution and deployment happen **inside WSL2**.

### 9.1 Environment topology

```
Windows 11                      host OS only — no CareerLens tooling
└── WSL2
    └── Linux distribution (Fedora 44)
        ├── Git
        ├── Docker Engine + Docker CLI + Compose plugin
        ├── Python tooling
        ├── Node/npm tooling (development and build only)
        └── CareerLens
            └── Docker Compose
                ├── postgres   container
                ├── redis      container
                ├── api        container (FastAPI)
                ├── worker     container
                └── nginx      container
```

**Prohibited on Windows:** installing Python, Node/npm, Docker Desktop,
PostgreSQL, Redis or Nginx; creating Windows services for any component;
modifying the Windows `PATH` or environment for CareerLens; requiring
PowerShell or CMD to install or run anything; any Windows-specific
runtime dependency.

Every command in this repository — `make`, `docker compose`, `pytest`,
`npm` — is expected to run from a WSL2 shell. Where a setup instruction
would require installation on Windows, the WSL2 equivalent is used
instead.

### 9.2 Development environment

- **OS:** Fedora 44 under WSL2.
- **Containerization:** Docker Engine inside the distribution (not
  Docker Desktop).
- **Working copy:** see the note on filesystem location in
  [SYSTEM-REQUIREMENTS.md](SYSTEM-REQUIREMENTS.md) — a working copy on
  the Linux filesystem performs substantially better than one under
  `/mnt/`.

### 9.3 Deployment target

- **OS:** Ubuntu Server 24.04 LTS.
- **Containerization:** Docker + Docker Compose.
- **Sizing:** see [SYSTEM-REQUIREMENTS.md](SYSTEM-REQUIREMENTS.md).

Because the entire runtime is containerized and exercised inside WSL2,
the development stack and the Ubuntu deployment stack are the same
artifacts. **The production-like environment is reproducible entirely
from WSL2.**

The container runtime is **Docker**, replacing the earlier Podman
direction, so development and the deployment target share one toolchain.
