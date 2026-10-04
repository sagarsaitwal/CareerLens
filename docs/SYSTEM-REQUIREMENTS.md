# CareerLens — System Requirements

This document owns **infrastructure, deployment sizing, and the
implementation-level requirement index**. It deliberately **cross-references
rather than duplicates** the behavioural requirements, which are owned by:

| Document | Owns |
|---|---|
| [REQUIREMENTS.md](REQUIREMENTS.md) | Product/functional semantics |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Boundaries, layers, trust, concurrency, failure |
| [AI-MATCHING.md](AI-MATCHING.md) | Stage behaviour, determinism, degradation, invariants |
| [DATABASE.md](DATABASE.md) | Data model, constraints, immutability, retention |

Duplicating those here would guarantee drift.

## 1. Technology Stack

| Layer | Choice |
|---|---|
| Backend | Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic, Uvicorn |
| Database | PostgreSQL 16+ — **the source of truth** |
| Queue / cache / locks | Redis 7 — **never durable state** |
| Scheduling | APScheduler, enqueuing into Redis |
| Frontend | React, TypeScript, Vite, Tailwind CSS — **static build** |
| Reverse proxy | Nginx |
| Containers | Docker + Docker Compose |
| Backend testing | Pytest, Hypothesis, **real PostgreSQL** |
| Frontend testing | Vitest |
| E2E testing | Playwright |
| AI | Provider abstraction; no direct dependency on one vendor |

> **SQLite must not be used**, including for tests that validate database
> constraints. Constraint behaviour is part of the specification and only
> PostgreSQL enforces it as designed.

## 2. Development and Deployment Environment

> **CareerLens is WSL2-only. Windows is the host OS and nothing more.**

All development, building, testing, container execution and deployment
occur **inside WSL2**. The production-like environment is reproducible
entirely from WSL2.

### 2.1 Topology

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
                ├── postgres, redis, api, worker, nginx
```

### 2.2 Prohibited on Windows

| Do not | Instead |
|---|---|
| Install Python on Windows | Python lives in WSL2 and in the `api`/`worker` images |
| Install Node/npm on Windows | Node lives in WSL2 and in the frontend build stage |
| Install Docker Desktop | Docker **Engine** inside the WSL2 distribution |
| Install PostgreSQL, Redis or Nginx on Windows | They run as containers |
| Create Windows services for any component | Containers managed by Compose |
| Modify Windows `PATH`/environment for CareerLens | Configure inside WSL2 and `.env` |
| Require PowerShell/CMD to install or run anything | Every command runs in a WSL2 shell |
| Introduce any Windows-specific runtime dependency | Keep the runtime POSIX-only |

### 2.3 Environments

| | Development | Deployment target |
|---|---|---|
| OS | Fedora 44 **under WSL2** | Ubuntu Server 24.04 LTS |
| Containers | Docker Engine + Compose | Docker Engine + Compose |
| Runtime artifacts | Identical images | Identical images |

### 2.4 Working-copy location

The repository currently sits at `/mnt/d/Project/CareerLens`, which is the
Windows drive surfaced through WSL2's `drvfs`.

> **Recommendation:** keep the development working copy on the **Linux
> filesystem** (for example `~/CareerLens`) rather than under `/mnt/`.
> Cross-filesystem bind mounts into containers are substantially slower
> for the many-small-files I/O that dependency installs, test runs and
> Vite builds generate, and `drvfs` permission mapping interacts awkwardly
> with the non-root container user.
>
> This is a performance and ergonomics recommendation, not a correctness
> requirement — the stack is designed to need **no write access** to the
> bind-mounted source (bytecode writing is disabled and all tool caches
> are redirected to `/tmp`).

### 2.5 Setup and validation procedure

Run entirely inside WSL2:

```bash
# 0. Confirm you are in WSL2, not a Windows shell
grep -qi microsoft /proc/version && echo "WSL2 OK"

# 1. One-time: Docker Engine inside the distribution
#    (Fedora shown; use your distribution's package manager)
sudo dnf install -y docker docker-compose-plugin
sudo systemctl enable --now docker       # or: sudo service docker start
sudo usermod -aG docker "$USER"          # log out/in for group to apply

# 2. Clone and configure
git clone <repository> CareerLens
cd CareerLens
cp .env.example .env
#    edit .env and set POSTGRES_PASSWORD

# 3. Validate the toolchain
docker --version
docker compose version
make preflight          # checks WSL2, Docker, daemon, and .env

# 4. Build and start
make up

# 5. Verify
docker compose ps                        # all services healthy
curl http://localhost:8000/api/health    # API directly
curl http://localhost:8080/api/health    # API through Nginx
curl -I http://localhost:8080/           # static frontend

# 6. Test
make test

# 7. Inspect
docker compose logs -f api
docker compose logs -f worker
```

`make preflight` fails with an actionable message if the shell is not
WSL2, Docker is absent, the daemon is unreachable, or `.env` is missing.

## 3. Deployment Sizing

### 3.1 Minimum viable target

| Resource | Minimum |
|---|---|
| vCPU | 2 |
| RAM | 4 GB |
| Disk | 40+ GB SSD |

> **4 GB is the minimum viable target, not the performance target.**

### 3.2 Recommended

| Resource | Recommended |
|---|---|
| vCPU | 4 |
| RAM | 8 GB |
| Disk | 80+ GB SSD |

### 3.3 Service budget on the minimum target

| Service | Approximate RSS |
|---|---|
| PostgreSQL (conservatively configured) | 0.8–1.0 GB |
| Redis (with `maxmemory` cap) | 0.15–0.25 GB |
| FastAPI (conservative worker count) | 0.3–0.4 GB |
| Worker (single process) | 0.3–0.4 GB |
| Nginx + static assets | ~0.05 GB |
| **Total** | **~1.6–2.1 GB** |

Required configuration on the minimum target:

- PostgreSQL tuned conservatively (small `shared_buffers`, bounded
  `max_connections`).
- Redis with an explicit `maxmemory` and eviction policy.
- **One** worker process.
- Conservative Uvicorn worker count.
- **Browser automation must not run multiple Chromium instances
  concurrently.** Playwright is the single largest memory risk
  (0.3–0.5 GB per instance) and must run strictly sequentially here.

### 3.4 Externalization

PostgreSQL, Redis, the API and the worker must be separable onto distinct
hosts later **without changing the domain model**. This is achieved by
reaching every backing service through environment-configured URLs only —
never hardcoded hosts, and never shared in-process state.

## 4. Functional Requirements Index

Implementation-level capabilities, each pointing at its owning document:

| Capability | Specified in |
|---|---|
| Profile creation and versioning | DATABASE.md 2.10, 2.13 |
| Job ingestion and deduplication | DATABASE.md 2.15; REQUIREMENTS.md 1 |
| Skill extraction (Stage 1) | AI-MATCHING.md 2 |
| Skill resolution | AI-MATCHING.md 3 |
| Seniority resolution | AI-MATCHING.md 3.1 |
| Deterministic matching (Stage 2) | AI-MATCHING.md 2, 5 |
| Selective semantic matching (Stage 3) | AI-MATCHING.md 2 |
| Preference matching | AI-MATCHING.md 7 |
| Deterministic scoring (Stage 4) | AI-MATCHING.md 2, 6 |
| Explanation generation | AI-MATCHING.md 9 |
| Catalog promotion | AI-MATCHING.md 8; DATABASE.md 2.4 |
| Conflict handling | DATABASE.md 2.5, 4.2 |
| Re-matching and staleness | DATABASE.md 3.2 |

## 5. Non-Functional Requirements Index

| NFR | Specified in |
|---|---|
| Determinism | AI-MATCHING.md 2, 11 |
| Reproducibility distinction (re-executable vs. evidence-preserved) | AI-MATCHING.md 11 |
| Idempotency | DATABASE.md 4.3 |
| Auditability | DATABASE.md 2.9, 2.16; AI-MATCHING.md 4 |
| Historical immutability | DATABASE.md 4.1 |
| Security | ARCHITECTURE.md 6 |
| Privacy / egress boundary | ARCHITECTURE.md 6.3 |
| Prompt-injection resistance | AI-MATCHING.md 10; ARCHITECTURE.md 6.2 |
| Graceful degradation | AI-MATCHING.md 12; ARCHITECTURE.md 7.3 |
| Concurrency safety | ARCHITECTURE.md 7.2; DATABASE.md 4.3 |
| Data integrity | DATABASE.md 4, 6 |
| Observability | AI-MATCHING.md 14 |
| Scalability | Section 3.4 above |
| Maintainability | Section 6 below |
| Testability | Section 7 below |

## 6. Maintainability Requirements

- **The domain layer is pure.** Resolution, Stage 2 and Stage 4 are
  functions of their inputs with no I/O. The re-executability guarantee in
  AI-MATCHING.md Section 11 is only verifiable if this holds.
- **`rule_version` is bumped deliberately** on any change to domain
  behaviour; it is never auto-derived.
- **One owner per concept.** A concept documented in one place is
  cross-referenced, not restated, elsewhere.
- **Database constraints and application semantics stay aligned.** Where
  both express an invariant, both must change together.

## 7. Testability Requirements

**The documented hard invariants are the test specification** — DATABASE.md
Section 6 and AI-MATCHING.md Section 13. Every hard invariant must have a
named test.

| Layer | Tool | Scope |
|---|---|---|
| Domain unit | Pytest | Pure functions: resolution tiers, comparison, coverage, seniority partial order |
| Property-based | Pytest + Hypothesis | Stage 2 re-executability; Stage 4 determinism |
| Database integration | Pytest + **real PostgreSQL** | Constraints actually reject violations |
| Contract | Pytest | Model responses with extra/hostile fields rejected |
| Injection | Pytest | Adversarial postings cannot alter score, category, gaps or catalog |
| Degradation | Pytest | LLM down ⇒ indeterminate; catalog down ⇒ matching refuses |
| Egress | Pytest | Forbidden profile data cannot reach a provider |
| Frontend unit | Vitest | Semantic labels map 1:1; disclaimers always rendered |
| E2E | Playwright | Match review, profile edit, catalog confirmation |

## 8. Operational Requirements

- **Secrets** live only in environment/configuration. Never in the
  repository, logs, audit records, snapshots or error messages.
- **Audit records** are append-only.
- **Metrics** are grouped into the three classes defined in
  AI-MATCHING.md Section 14, with `unresolved` outcomes metered
  separately from errors.
- **Backups** of PostgreSQL are required because it is the source of
  truth; Redis requires none, as it holds no durable state.

## 9. Explicitly Deferred

- Numeric performance targets: latency budgets, throughput, cache
  lifetimes, batch sizes, Stage 3 invocation ceilings, LLM cost limits.
- Backup schedule, retention and restore procedure.
- TLS termination and certificate management.
- Monitoring/alerting stack and thresholds.
- Horizontal scaling topology.
- Authentication and authorization mechanism
  ([ARCHITECTURE.md Section 8](ARCHITECTURE.md#8-explicitly-deferred)).
