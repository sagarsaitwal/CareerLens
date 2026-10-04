# CareerLens

CareerLens is an AI-powered **Career Opportunity Discovery & Application
Intelligence Platform**. It discovers relevant job openings, matches them
against a configurable candidate profile using a hybrid rule-based + LLM
approach, and helps track applications end-to-end — while keeping the
human firmly in control of every outbound action.

## Purpose

Job searching today is fragmented: discovery, relevance assessment, and
application tracking all happen in different places (job boards, email,
spreadsheets). CareerLens consolidates this into one system that:

- Surfaces relevant postings based on actual skills/capabilities, not just
  keyword-matched titles.
- Explains *why* a job matches and what skill gaps exist.
- Always points to a verified, direct application URL when available —
  never a guessed one.
- Tracks every application through a detailed status pipeline, from first
  contact through offer or rejection.
- Never takes an action on the user's behalf. CareerLens recommends and
  tracks; the user decides and acts.

## Current Status

**Phase 0 — Project scaffolding and documentation.** The repository
structure and planning documents exist; no application code, dependencies,
or infrastructure have been created yet. See [ROADMAP.md](docs/ROADMAP.md)
for what comes next.

## Planned Architecture

| Layer | Choice |
|---|---|
| Backend | Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic |
| Frontend | React + TypeScript + Vite + Tailwind CSS (static build) |
| Database | PostgreSQL 16+ (source of truth) |
| Queue / cache / locks | Redis 7 (never durable state) |
| Reverse proxy | Nginx (serves static frontend, proxies `/api`) |
| Scheduler | APScheduler (enqueues into Redis) |
| AI Matching | Deterministic rules + selective LLM, behind a provider abstraction |
| Browser automation | Playwright (read-only, permitted sources only) |
| Containers | Docker + Docker Compose |
| Deployment target | Ubuntu Server 24.04 LTS |

See [ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full high-level design
and [SYSTEM-REQUIREMENTS.md](docs/SYSTEM-REQUIREMENTS.md) for deployment
sizing.

## Repository Structure

```
CareerLens/
├── README.md
├── .gitignore
├── docs/
│   ├── REQUIREMENTS.md     — functional requirements
│   ├── ARCHITECTURE.md     — proposed high-level architecture
│   ├── DATABASE.md         — planned entities & relationships
│   ├── AI-MATCHING.md      — matching approach and stage behaviour
│   ├── SYSTEM-REQUIREMENTS.md — infrastructure, sizing, NFR index
│   └── ROADMAP.md          — implementation phases
├── backend/                — FastAPI backend + Alembic migrations
├── frontend/                — React + Vite frontend
├── workers/                — discovery, resolution & matching workers
├── database/                — database assets
├── nginx/                   — reverse proxy configuration
└── scripts/                — maintenance/dev scripts
```

## Development and Deployment Environment

> **CareerLens is WSL2-only. Windows is the host OS and nothing more.**
>
> Nothing is installed on Windows — not Python, Node, Docker Desktop,
> PostgreSQL, Redis or Nginx. All development, building, testing,
> container execution and deployment happen **inside WSL2**.

```
Windows 11              host OS only
└── WSL2
    └── Fedora 44
        ├── Git, Docker Engine, Python tooling, Node/npm
        └── CareerLens → Docker Compose
                          ├── postgres, redis
                          ├── api, worker
                          └── nginx
```

Every command below runs in a **WSL2 shell**. PowerShell and CMD are
never used.

```bash
cp .env.example .env     # then set POSTGRES_PASSWORD
make preflight           # verifies WSL2, Docker, daemon, .env
make up                  # http://localhost:8080
make test
docker compose ps
```

**Deployment target:** Ubuntu Server 24.04 LTS. Minimum viable host is
2 vCPU / 4 GB RAM / 40 GB SSD.

Full setup, validation and sizing:
[SYSTEM-REQUIREMENTS.md](docs/SYSTEM-REQUIREMENTS.md).

## Core Principles

1. **No autonomous action.** CareerLens never applies to jobs, messages
   recruiters, sends referral requests, or performs LinkedIn actions
   automatically. The user is always the final decision-maker.
2. **No fabricated data.** Application URLs and job details are only ever
   shown when verified from a real source — never guessed.
3. **Explainable matching.** Every match comes with a rationale, a
   skill-gap breakdown, and an explicit statement of what the system could
   *not* assess — never a bare number. Uncertainty is reported, never
   silently counted against the candidate.

## Documentation

- [Requirements](docs/REQUIREMENTS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Database Design](docs/DATABASE.md)
- [AI Matching Approach](docs/AI-MATCHING.md)
- [Roadmap](docs/ROADMAP.md)
