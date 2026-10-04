# CareerLens — Requirements

This document defines the functional requirements for CareerLens. It is the
source of truth for *what* the system must do. Technical design decisions
derived from these requirements live in [ARCHITECTURE.md](ARCHITECTURE.md),
[DATABASE.md](DATABASE.md), and [AI-MATCHING.md](AI-MATCHING.md).

## 1. Job Discovery

- Discover relevant job openings from permitted/public job sources and
  company career pages only. The system must respect each source's terms of
  service and `robots.txt`.
- Search by capabilities and skills, not only job titles — a user with
  "distributed systems in Go" skills should surface relevant roles even if
  the title is unconventional (e.g. "Member of Technical Staff").
- Detect new postings and detect updates to previously discovered postings
  (e.g. description changed, requirements changed, posting closed).
- Deduplicate jobs that appear across multiple sources or are re-posted.
- Capture, per job, the following fields:
  - Company
  - Title
  - Location
  - Work mode (remote / hybrid / onsite)
  - Description
  - Requirements (structured, split into mandatory vs. preferred where
    possible — see [AI-MATCHING.md](AI-MATCHING.md))
  - Experience level/range
  - Salary, when available
  - Posting date
  - Source URL (where it was discovered)
  - Verified direct application URL (see Section 3)

## 2. Job Matching

- Match discovered jobs against a configurable user profile (skills,
  experience, seniority, location/work-mode preferences, and other
  user-defined criteria).
- Evaluation dimensions include: technical skills, responsibilities,
  experience, seniority, location, work mode, and required vs. preferred
  skills.
- Matching must **not** rely primarily on job title matching — title is one
  weak signal among many, not the primary filter.
- Every match produces:
  - A match score, always reported together with its coverage. Both are
    undefined when the system could not assess the job's requirements at
    all — never reported as a zero score or a low category.
  - A human-readable explanation of why the job matched (or didn't).
  - A list of identified skill gaps.
  - A clear distinction between mandatory requirements the candidate
    satisfies/fails and preferred requirements the candidate satisfies/fails.

## 3. Direct Application

- Every recommended job should carry a verified direct application URL
  whenever one is available.
- The company's own application page is preferred over third-party
  aggregator application flows.
- The system must **never fabricate or guess** an application URL. If a
  verified direct URL cannot be found, the job is shown without one rather
  than with a guessed one.
- The system must **not** automatically apply to jobs under any
  circumstance.
- The user is always the one who reviews the job and manually submits the
  application; CareerLens's role ends at surfacing the verified link.

## 4. Application Tracking

### 4.1 Status pipeline

Applications move through the following tracked statuses:

1. Applied
2. Recruiter Contact
3. HR Screening
4. Technical Round 1
5. Technical Round 2
6. Managerial
7. Final HR
8. Offer
9. Rejected
10. Withdrawn
11. No Response
12. Position Closed

The status model should allow non-linear transitions (e.g. an application
can move directly from "Applied" to "Rejected", or skip rounds), since
real-world hiring processes vary by company.

### 4.2 Tracked details per application

- Company
- Position
- Job URL
- Direct apply URL
- Match score (captured at time of application)
- Applied date
- Recruiter / contact (name, contact info)
- Resume version used (see Section 5)
- CTC (current / offered / negotiated, as applicable)
- Notice period
- Interview dates
- Interview notes
- Rejection reason
- Follow-up date
- Personal notes

## 5. Resume Management

- Support multiple resume versions (e.g. "Backend-focused", "Full-stack",
  "Leadership-emphasis").
- Every application record must reference which resume version was used.
- This enables resume performance analytics (Section 6).

## 6. Dashboard and Analytics

The dashboard must surface:

- New job matches (recently discovered, unreviewed).
- Match categories (e.g. strong match / partial match / stretch).
- Application pipeline (counts/status across the full pipeline).
- Interview pipeline (applications currently in interview stages).
- Application-to-interview conversion rate.
- Resume performance (which resume version correlates with better
  conversion).
- Skill-gap analysis (aggregated across jobs, to guide upskilling).
- Company/application statistics (e.g. applications per company, response
  rates by company).
- Follow-up tracking (applications with an upcoming or overdue follow-up
  date).

## 7. Notifications

- Notifications are handled entirely inside the application (in-app
  notification center/feed).
- Email integration is explicitly **out of scope** and must not be
  implemented.

## 8. User Control & Safety Boundaries

The system must **never automatically**:

- Apply to jobs.
- Send recruiter messages.
- Send referral requests.
- Perform LinkedIn actions (connection requests, messages, endorsements,
  etc.).

The user is always the final decision-maker. CareerLens is a discovery,
matching, and tracking assistant — not an autonomous application agent.

## Non-Goals (explicitly out of scope for now)

- Automated job applications of any kind.
- Automated outreach to recruiters, referrers, or hiring managers.
- Automated LinkedIn or social platform actions.
- Email sending/receiving integration.
- Scraping sources that disallow automated access.
