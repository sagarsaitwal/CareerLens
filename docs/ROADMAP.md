# CareerLens — Roadmap

Logical implementation phases. Phases are sequential in dependency terms
(each builds on the last) but scope within a phase can be adjusted as
learnings emerge. No dates are committed here — this is an ordering of
work, not a schedule.

## Phase 0 — Project Scaffolding (this task)

- Repository structure.
- Documentation: requirements, architecture, database, AI-matching
  approach, roadmap.
- No code, no dependencies, no infrastructure.

## Phase 1 — Foundations

- Finalize initial database schema and create first migrations.
- Stand up backend skeleton (FastAPI project structure, health check,
  config management).
- Stand up frontend skeleton (Next.js + TypeScript project structure).
- Define `UserProfile` and `MatchingCriteria` data entry (manual
  configuration, no UI polish required yet).

## Phase 2 — Job Discovery (Minimal)

- Select and integrate a small number of initial permitted job
  sources/career pages.
- Implement discovery worker(s): fetch, normalize, deduplicate.
- Implement new/updated-posting detection.
- Persist `JobPosting` records.
- No matching yet — raw discovered jobs are visible/inspectable.

## Phase 3 — Matching Engine (v1)

- Implement requirement extraction (mandatory vs. preferred).
- Implement rule-based scoring against `MatchingCriteria`.
- Integrate LLM semantic analysis layer.
- Implement score fusion, explanation generation, and skill-gap output.
- Surface `MatchResult`s via backend API.

## Phase 4 — Direct Application Link Verification

- Implement logic to locate and verify a direct, company-owned application
  URL per posting where possible.
- Enforce "never fabricate a URL" at the data layer (nullable, verified
  flag).
- Surface verified links (or their absence) clearly in the UI.

## Phase 5 — Application Tracking

- Implement `Application`, `ApplicationStatusEvent`, `InterviewEvent`,
  `Note` entities and APIs.
- Implement the full status pipeline (Applied → ... → Offer/Rejected/
  Withdrawn/No Response/Position Closed).
- Build tracker UI (board/table view).

## Phase 6 — Resume Management

- Implement `Resume` entity and versioning.
- Link resume version to each `Application`.
- Basic resume management UI (upload/label/activate version).

## Phase 7 — Dashboard & Analytics

- New matches view, match categories.
- Application and interview pipeline views.
- Application-to-interview conversion metrics.
- Resume performance analytics.
- Skill-gap aggregation view.
- Company/application statistics.
- Follow-up tracking view.

## Phase 8 — In-App Notifications

- Notification data model and generation triggers (new match, follow-up
  due, etc.).
- In-app notification center UI.
- Explicitly no email integration.

## Phase 9 — Hardening & Operability

- Scheduler (APScheduler) tuning for discovery/matching run cadence.
- Containerization with Podman.
- Logging, monitoring, and error handling review.
- Revisit architecture decisions deferred in earlier phases.

## Ongoing / Cross-Phase

- Continuous enforcement of user-control boundaries: no auto-apply, no
  auto-messaging, no LinkedIn automation, at every layer (worker, backend,
  frontend).
- Documentation kept in sync with implementation decisions as they're made.
