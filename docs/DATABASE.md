# CareerLens — Database (Planned Entities & Relationships)

> Entities, fields and constraints are **planned design**. Migrations are
> implemented with Alembic; see
> [SYSTEM-REQUIREMENTS.md](SYSTEM-REQUIREMENTS.md) for infrastructure.

**Stack:** PostgreSQL 16+ (the source of truth), SQLAlchemy 2.x, Alembic.

This document owns the **data model**: entities, fields, provenance and
version fields, uniqueness constraints, immutability, historical
denormalization, retention, and database-level invariants. Matching
*behaviour* is owned by [AI-MATCHING.md](AI-MATCHING.md); component and
trust boundaries by [ARCHITECTURE.md](ARCHITECTURE.md).

> **Database-level enforcement is required.** Where PostgreSQL can enforce
> an invariant — uniqueness, immutability, append-only behaviour,
> referential integrity, promotion idempotency — it must, rather than
> relying on application validation alone.

## 1. Entity Overview

```
Skill 1───* SkillRepresentation    [kind = canonical | alias]
Skill 1───* SkillRelationship      [directional: from_skill -> to_skill]
SkillPromotionEvidence  *───0..1 Skill   [evidence predates the Skill]
SkillConflict *───0..1 Skill

SeniorityLevel 1───* SeniorityRepresentation
SeniorityLevel 1───* SeniorityRelationship   [cross-track; initially empty]

UserProfile 1───* ProfileSkill
UserProfile 1───* MatchingCriteria
UserProfile 1───* ProfileVersion
UserProfile 0..1───1 ProfileVersion         [current pointer; null until v1]
UserProfile 1───* Resume
UserProfile 1───* MatchResult
UserProfile 1───* UserJobDecisionEvent
UserProfile 1───* Application

ProfileVersion 1───* MatchResult

JobSource 1───* JobPosting
JobPosting 1───* MatchResult
JobPosting 1───* UserJobDecisionEvent
JobPosting 1───* Application

MatchResult 1───1 MatchResultJobDescriptor   [survives JobPosting deletion]
MatchResult 1───* SkillGap
MatchResult 1───* IndeterminateComparison
MatchResult 1───* PreferenceAlignment
MatchResult 1───* SeniorityComparison
MatchResult 1───* Application

Resume 1───* Application
Application 1───* ApplicationStatusEvent
Application 1───* InterviewEvent
Application 1───* Note

Notification *───1 (Application | JobPosting)   [polymorphic reference]
```

`ProfileSkill`, `MatchingCriteria` and `JobPosting`'s extracted skills
carry a [skill mention](#29-mention-and-reference-patterns);
`UserProfile` and `JobPosting` seniority fields carry a
[seniority reference](#29-mention-and-reference-patterns).

See [Section 3.3](#33-data-separation-canonical-vs-user-specific-data) for
the canonical/user-specific split, [Section 3.4](#34-capability-vs-intent-fit-vs-preference)
for capability vs. intent, [Section 3.5](#35-open-world-vs-closed-world-evidence)
for the two axes' epistemics, and [Section 3.6](#36-seniority-as-a-partial-order)
for seniority ordering.

## 2. Entities

### 2.1 `Skill`

The **canonical skill catalog** — one entry per distinct skill concept.
Shared reference data: neither user-specific nor job-specific.

- id
- created_at / updated_at
- created_from (promotion / manual curation)

The canonical *name* is not stored here; it lives as the `canonical` row
in [`SkillRepresentation`](#22-skillrepresentation), so that one uniqueness
constraint governs names and aliases together.

The catalog is **mutable reference data** — entries may be renamed or
merged. Historical records are protected by value-denormalization, not by
freezing the catalog (see [Section 4.1](#41-immutability-and-historical-denormalization)).

### 2.2 `SkillRepresentation`

Every textual form by which a `Skill` can be identified — the canonical
name **and** every alias — held in **one table so that a single unique
index enforces the whole identity invariant**.

- id
- skill_id (FK → Skill)
- normalized_representation — **globally UNIQUE across the table**
- display_text
- kind (`canonical` / `alias`)
- origin (curation / promotion / user_confirmation)
- created_at

> One `UNIQUE` index on `normalized_representation` simultaneously
> delivers: canonical-name uniqueness, alias uniqueness, alias-binds-to-one-skill,
> promotion idempotency, and collision rejection. Splitting canonical names
> and aliases into two tables would make this invariant unenforceable in
> PostgreSQL.

> Exactly one `canonical` row per `Skill` (enforced by a partial unique
> index on `skill_id WHERE kind = 'canonical'`).

> A proposed representation that collides is **rejected** and recorded as
> a [`SkillConflict`](#25-skillconflict), never silently rebound.

### 2.3 `SkillRelationship`

A typed, **directional** relationship between canonical skills. Never an
alias, never equivalence.

- id
- from_skill_id (FK → Skill)
- to_skill_id (FK → Skill, `CHECK from_skill_id <> to_skill_id`)
- relationship_type (`family` / `adjacent`)
- created_at

| Type | Example | Direction | Meaning |
|---|---|---|---|
| `family` | EKS → Kubernetes | **Directional** | Source is a specialization of the target. Supports *partial, directional* credit |
| `adjacent` | Terraform ↔ Pulumi | Symmetric | Same domain, **not substitutable** |

> Identity, `family` and `adjacent` are three distinct concepts and must
> never collapse into equivalence. Family direction is load-bearing:
> holding EKS evidences Kubernetes substantially; the reverse only weakly.

### 2.4 `SkillPromotionEvidence`

Evidence that an unresolved representation should become a `Skill` or a
new alias. Evidence exists *before* any `Skill` does, so it is keyed on the
representation.

- id
- normalized_representation
- raw_text_observed
- job_posting_id (FK → JobPosting)
- observed_at
- extraction_confidence
- context_signals
- user_confirmed
- outcome (pending / promoted_as_skill / promoted_as_alias / rejected)

> **`UNIQUE (normalized_representation, job_posting_id)`.** This single
> constraint enforces "evidence counts independent postings" — re-ingesting
> the same posting cannot inflate evidence. See
> [Section 4.3](#43-idempotency-requirements).

> Gating rules are owned by
> [AI-MATCHING.md](AI-MATCHING.md#8-catalog-promotion-and-the-determinism-ratchet).

### 2.5 `SkillConflict`

A recorded, unresolved catalog contradiction. Conflicts **fail loudly**.

- id
- conflict_type (representation collision / contradictory resolution
  candidates / merge precondition failure)
- normalized_representation
- candidate_skill_ids
- detected_at
- status (open / resolved)
- resolution_note

> While a conflict is open for a representation, that representation
> **remains unresolved**. Declining to guess is correct behaviour.

### 2.6 `SeniorityLevel`

The **canonical seniority catalog**. Levels are ordered **within a track**
only — there is no global rank (see
[Section 3.6](#36-seniority-as-a-partial-order)).

- id
- track (`IC` / `Management`)
- rank_within_track (ordinal, meaningful **only** within the same track)
- created_at

> `UNIQUE (track, rank_within_track)`.

> **"track-unknown" is not a stored track.** It represents absence or
> uncertainty and is expressed by an unresolved
> [seniority reference](#29-mention-and-reference-patterns), never by a
> third enum value.

**Initial IC ladder:** Intern/Trainee < Junior < Mid < Senior < Staff <
Principal

**Initial Management ladder:** Manager < Senior Manager < Director < VP

Shipped as a versioned data migration, governed by `catalog_version`.

### 2.7 `SeniorityRepresentation`

The same structural pattern as
[`SkillRepresentation`](#22-skillrepresentation), for seniority.

- id
- seniority_level_id (FK → SeniorityLevel)
- normalized_representation — **globally UNIQUE across the table**
- display_text
- kind (`canonical` / `alias`)
- origin
- created_at

> **Deliberately absent from the catalog**, because no documented rule
> establishes their meaning: `Engineer`, `Software Engineer`, `Developer`,
> `Lead`, `Architect`, `Associate`, `Head`. These resolve to
> **unknown or indeterminate**, never to a guessed level.
>
> In particular: `Lead` must not become Senior, `Architect` must not
> become Principal, `Head` must not become VP, and absence of a seniority
> modifier must not default to Mid.

### 2.8 `SeniorityRelationship`

Optional cross-track relationships.

- id
- from_level_id / to_level_id (FK → SeniorityLevel)
- relationship_type (`cross_track_band`)
- created_at

> **Initially empty, by decision.** Populating it would assume a specific
> company's ladder. With no rows, cross-track comparison returns
> `different_track`, which is honest. Rows may be added later without
> schema change.

### 2.9 Mention and Reference Patterns

Shared **field patterns**, not standalone tables.

**Skill mention** — used by [`ProfileSkill`](#211-profileskill),
[`MatchingCriteria`](#212-matchingcriteria), and `JobPosting`'s extracted
skill collections:

- raw_text (as entered or extracted)
- normalized_representation
- skill_id (FK → Skill — **absent when unresolved**)
- resolution_status (`resolved` / `unresolved`)
- resolution_provenance (below)
- resolved_canonical_name — **by value, at time of resolution**
- catalog_version_at_resolution

**Seniority reference** — used by `UserProfile.current_seniority`,
`UserProfile.target_seniority`, and `JobPosting.seniority`:

- raw_text
- normalized_representation
- seniority_level_id (FK → SeniorityLevel — absent when unresolved)
- resolution_status (`resolved` / `unresolved`)
- resolution_provenance
- resolved_level_name **and** resolved_track — **both by value**
- **evidence_source** (`structured_field` / `title` / `requirements` /
  `responsibilities` / `description`)
- catalog_version_at_resolution

**Resolution provenance — exactly six tiers, no seventh:**

| Provenance | Meaning | Class |
|---|---|---|
| `exact_match` | Identical to a canonical name or alias | **Deterministic** |
| `normalized` | Matched after mechanical normalization only | **Deterministic** |
| `alias_rule` | Matched an explicit catalogued alias | **Deterministic** |
| `user_confirmed` | Human affirmed; stored as a rule | **Deterministic**, highest precedence |
| `ai_semantic` | A model judged equivalence | **Semantic** |
| `unresolved` | No mapping established | Neither — legitimate |

> Only the four deterministic tiers are consumed by Stage 2.
> `ai_semantic` becomes Stage 2 evidence only by passing through catalog
> promotion.

> **`resolution_provenance` and `evidence_source` are different concepts
> and must never be merged.** Provenance says *how it resolved*;
> evidence_source says *where the text came from*. A structured seniority
> field and a phrase scraped from a description can both resolve via
> `exact_match` while carrying very different authority.

> `unresolved` is a **normal operating state**, never an error, and must be
> metered separately from failures.

### 2.10 `UserProfile`

The live editing surface. Capability lives in
[`ProfileSkill`](#211-profileskill), intent in
[`MatchingCriteria`](#212-matchingcriteria).

- id
- name / label
- experience_years
- current_seniority — a [seniority reference](#29-mention-and-reference-patterns);
  a **current-fit** signal alongside `experience_years`
- target_seniority — **optional**; a [seniority reference](#29-mention-and-reference-patterns)
  that **includes track**. A **preference** signal that influences ranking,
  never a hard filter, and **never silently defaulting to
  `current_seniority`**. Unset means no target expressed
- preferred_locations
- preferred_work_modes (remote / hybrid / onsite)
- current_profile_version_id (FK → ProfileVersion, **nullable** — null
  until v1 is minted lazily, see [Section 2.13](#213-profileversion))
- created_at / updated_at

> `UserProfile`, `ProfileSkill` and `MatchingCriteria` are the **live
> editing surface**. Matching never reads them directly; it reads an
> immutable [`ProfileVersion`](#213-profileversion).

### 2.11 `ProfileSkill`

The **capability inventory** — *"What can I do?"*

- id
- user_profile_id (FK → UserProfile)
- [skill mention](#29-mention-and-reference-patterns)
- proficiency (`Strong` / `Working` / `Learning`)
- created_at / updated_at

**Ordinal levels:** `Strong > Working > Learning`.

- **Absence of a `ProfileSkill` is distinct from `Learning`**, mapping to
  the distinct `gap_type` values on [`SkillGap`](#217-skillgap)
  (`missing` vs. `below-threshold-experience`).
- **No additional proficiency levels** — three is a deliberate ceiling.

A `ProfileSkill` may exist with **no** `MatchingCriteria`.

> **Example.** `ProfileSkill(COBOL, Working)` with no criterion: the user
> knows COBOL and it is credited when a posting mentions it, but they are
> not targeting COBOL roles.

### 2.12 `MatchingCriteria`

**Search intent** — *"What should matching prioritize?"*

- id
- user_profile_id (FK → UserProfile)
- [skill mention](#29-mention-and-reference-patterns) — carries its own
  skill identity; **does not reference `ProfileSkill`**
- weight — **`NUMERIC`, `CHECK (weight >= 0.0 AND weight <= 1.0)`**
- is_required (required vs. preferred **intent** from the candidate's
  side: *a role must involve this for me to be interested*)
- created_at / updated_at

> **Weight is a continuous normalized scale stored as `NUMERIC`**, not
> floating point, so persisted values are exact. Business weight values
> are **configuration/data**, never hard-coded into the domain engine.

**Intent may exist without capability.** The link to capability is
*derived* by shared skill identity when both resolve, or by normalized
representation when they do not.

| `ProfileSkill` | `MatchingCriteria` | Meaning |
|---|---|---|
| yes | no | Known, not targeted (COBOL) |
| yes | yes | Known and emphasized |
| **no** | **yes** | **Targeted, not yet held — aspirational** |
| no | no | Not represented |

> Requiring a backing capability would force a false `Learning` claim for
> aspirational skills, converting a truthful `missing` gap into a
> misleading `below-threshold-experience` one.

### 2.13 `ProfileVersion`

The **immutable matching snapshot**. The matching input of record.

- id
- user_profile_id (FK → UserProfile)
- version_number (monotonic per profile; `UNIQUE (user_profile_id, version_number)`)
- schema_version
- catalog_version_at_creation
- created_at
- **snapshot** — a single **`JSONB`** document

**Snapshot format: `JSONB`.** The snapshot is a complete, self-contained
document carrying everything needed to reproduce the deterministic
calculation:

- profile identity/reference
- resolved skills — a union keyed by skill identity, each entry carrying
  raw_text, normalized_representation, resolved_canonical_name **by
  value**, skill_id (provenance pointer only), resolution_status,
  resolution_provenance, proficiency (absent for intent-only), weight and
  intent (absent for capability-only). Identity is the key where one
  exists, so a capability and an intent naming the same skill by
  different text ("Kubernetes" and "K8s") form one entry, while two
  mentions resolving to **different** skills never merge. Normalized
  text remains a fallback key only for mentions that have no identity
  yet, since resolution can reach capability and intent at different
  times.

  When **several mentions of the same kind** collapse into one entry —
  possible because the live tables are unique on
  `(profile, normalized_representation)` and not on identity — the
  surviving value is chosen by rule, never by row order:

  - **proficiency**: the **strongest** capability wins
    (Strong > Working > Learning). A duplicate must not be able to
    understate what the user can do.
  - **weight and is_required**: **required dominates optional**, then
    **higher weight dominates lower**. A duplicate must not be able to
    weaken a requirement the user expressed.
  - **raw_text, normalized_representation, provenance**: taken from an
    elected representative — **resolved** mentions first, then the row
    that supplied the winning proficiency (or, with no capability, the
    winning weight), then the lexicographically smallest text. The
    schema carries one of each per entry, so a representative is
    required; the identity itself is shared across the group, so
    nothing load-bearing is lost.

    The middle rule is **co-origination**: `raw_text` is evidence of how
    the user expressed the skill, so the entry's text and its surviving
    proficiency must describe one real row. Reporting "K8s" beside a
    Strong that came from the "Kubernetes" row would attribute a claim
    to a row that never made it. Co-origination can hold for only one
    axis, since capability and intent are different tables; the
    capability is preferred as the more concrete evidence claim. It also
    yields to rule one — an entry must never report `unresolved` while
    carrying the identity it is keyed on, which is the contradiction
    `resolved_requires_identity` forbids at row level.

  Every rule is a total order with a deterministic tie-break, so the
  snapshot is byte-identical regardless of the order rows are read in
- experience
- current_seniority and target_seniority — full seniority references,
  with level name **and track by value**
- preferred_locations and preferred_work_modes, in the order the user
  entered them. Stage 2 compares location and work mode, so these are
  matching inputs: left out, a historical match could not be
  re-executed without silently adopting today's preferences
- the **frozen seniority ladder** entries referenced by the snapshot
- the vocabularies in force (ordered proficiency levels)

> **The JSONB snapshot does not replace normalized tables.** `ProfileSkill`
> and `MatchingCriteria` remain the source of current application state;
> `ProfileVersion` is the immutable historical matching snapshot.

> **Never in the snapshot:** resumes, CTC, recruiter contacts, interview
> notes, application history, or unrelated personal information. The
> snapshot defines the maximum extent of data that may cross the LLM
> boundary ([ARCHITECTURE.md](ARCHITECTURE.md#63-llm-trust-boundary)).

> **Indexing:** add JSONB indexes only where a real access pattern
> justifies one. Do not pre-emptively index the snapshot.

**Immutability**

> Immutable once created. Never updated, corrected, or back-filled. A
> change creates a *new* version. Enforced by a database trigger rejecting
> `UPDATE` and `DELETE`.

**Minting rule — outcome-change based**

A new version is created **only when matching-relevant state changes**.
Matching-relevant state is exactly:

- `ProfileSkill` identity and proficiency (including add/remove)
- `current_seniority`, `target_seniority`
- `MatchingCriteria` (identity, weight, intent; including add/remove)
- matching-relevant experience information
- any user-confirmed matching resolution
- a change in resolution status affecting comparability
- matching-relevant responsibilities/narrative **once that feature is
  approved** (deferred, see [Section 7](#7-explicitly-deferred))

Unrelated profile metadata — a display label, cosmetic fields — must
**not** mint a version.

> **Minting is keyed on outcome, not process execution.** Re-running
> resolution that changes nothing mints nothing. A version may therefore
> be minted by system activity (catalog promotion resolving a skill), not
> only by user edits.

> **Concurrency:** minting is idempotent. Concurrent triggers representing
> the same resulting matching state must converge on **one** version, via
> a distributed lock plus the `UNIQUE (user_profile_id, version_number)`
> constraint. Multiple workers observing the same outcome never produce
> multiple versions.

**Bootstrap — lazy**

> **v1 is created lazily**, when the first matching-relevant state exists
> or the first match requires a snapshot. Creating an empty profile row
> does **not** mint a version; a snapshot of nothing has no value.
> `UserProfile.current_profile_version_id` is therefore nullable until v1
> exists. Concurrent first-match requests must not create duplicate
> bootstrap versions.

### 2.14 `JobSource`

- id
- name
- base_url
- type (public job board / company career page / API)
- is_active
- last_crawled_at

### 2.15 `JobPosting`

Canonical, source-of-truth job information, deduplicated across sources.
Not user-specific.

**Core identity**
- id, job_source_id (FK → JobSource), source_job_id, title, company

**Content**
- description_raw — **untrusted external input** (see
  [AI-MATCHING.md](AI-MATCHING.md#10-ai-safety-and-bounded-behaviour))

**URLs**
- source_url, official_url
- direct_apply_url (nullable — never fabricated)
- direct_apply_verification_status (verified / unverified / not_available)

**Classification**
- location, work_mode, employment_type
- seniority — a [seniority reference](#29-mention-and-reference-patterns)
  representing **what the posting evidences or requests**, never a guess
  about the company's hierarchy
- experience_requirements
- salary_min / salary_max / salary_currency (nullable)

**Lifecycle**
- posting_date, first_seen_at, last_seen_at, last_updated_at
- status (active / closed / unknown)
- dedupe_key / fingerprint
- extraction_status (complete / partial) — a partial extraction must
  **never** be read as "no further requirements exist"

**Extracted requirements** (Stage 1; each a
[skill mention](#29-mention-and-reference-patterns)):
- required_skills, preferred_skills, mentioned_skills, responsibilities

> **All skill collections are positive-only.** There is no representation
> for an explicit negative statement; negative-signal extraction is
> deferred.

> Resolution happens **at ingestion**, not match time. Catalog changes may
> trigger re-resolution of these live mentions; that never alters an
> existing `MatchResult`.

### 2.16 `MatchResult`

Output of the pipeline for a `(ProfileVersion, JobPosting)` pair.
**Immutable once written**, enforced by trigger.

**Identity and version axes**
- id, job_posting_id (FK, **nullable** — the posting may later be purged),
  user_profile_id, computed_at

All **four version axes**, all `NOT NULL`:

| Axis | Covers |
|---|---|
| profile_version_id (FK → ProfileVersion) | Profile inputs |
| rule_version | Matching algorithm **and normalization rules** |
| model_version | Stage 1 / Stage 3 model identity |
| catalog_version | Skills, aliases, relationships, **seniority catalog** |

> Normalization is deterministic code (`rule_version`); catalog data is
> `catalog_version`. **No fifth axis** — the seniority catalog shares
> `catalog_version`.

**Fit axis**
- match_score — **nullable**; quality of fit across requirements that
  could be evaluated. Indeterminate requirements are excluded from
  numerator **and** denominator
- match_category — **nullable**; derived from score **and** fit coverage
- fit_coverage — split **mandatory / preferred**
- evaluated_requirement_count / total_requirement_count

> `CHECK (evaluated_requirement_count > 0 OR (match_score IS NULL AND
> match_category IS NULL))` — **zero fit coverage yields an undefined
> score and an undefined category**, never zero and never a weak category.

- required_skill_matches, preferred_skill_matches
- partial_matches — *established* partial satisfaction only, never
  uncertainty
- skill_gaps → [`SkillGap`](#217-skillgap)
- indeterminate_comparisons → [below](#2162-indeterminatecomparison)
- location_match / work_mode_match / experience_match

**Preference axis**
- preference_alignments → [below](#2163-preferencealignment)
- preference_coverage — the state breakdown across criteria

> **Fit coverage and preference coverage are never merged.**

**Explanation and degradation**
- explanation
- degradation_cause (nullable) — why the result was computed with reduced
  capability; degraded results are eligible for re-match

**Per-comparison evidence.** Every entry in every collection carries: both
sides' raw_text; canonical names **by value**; relationship_class
(identity / alias / normalized-text / family / semantic);
resolution_provenance per side; establishing_stage (2 or 3);
stage3_certainty where applicable.

#### 2.16.1 `MatchResultJobDescriptor`

A **minimal immutable job descriptor**, captured at match time so a
historical result stays interpretable even if the `JobPosting` is later
purged.

- id, match_result_id (FK → MatchResult, one-to-one)
- job_posting_reference (the original id, retained even once purged)
- job_title
- company_name
- job_side_raw_skill_text (the raw mentions actually compared)
- job_side_canonical_names (**by value**)
- seniority_evidence (raw text, resolved level name and track by value,
  provenance, evidence_source)

> **Deliberately minimal.** This is *not* a copy of the `JobPosting`. It
> carries exactly what the required explanations need and nothing more, so
> that `JobPosting` can be purged under a future retention policy without
> making history uninterpretable.

#### 2.16.2 `IndeterminateComparison`

A requirement whose identity could **not** be established.

- id, match_result_id (FK)
- job_side raw_text and canonical name if any
- closest_profile_candidate (if any)
- is_mandatory
- reason

> **Never a `SkillGap`.** It asserts nothing about capability, contributes
> nothing to `match_score`, and must be reported as not counted against
> the user.

#### 2.16.3 `PreferenceAlignment`

One record per `MatchingCriteria` evaluated.

- id, match_result_id (FK)
- criterion skill identity (raw text, canonical name by value)
- state:

| State | Meaning |
|---|---|
| `aligned` | Positive evidence, identity established |
| `partially_aligned` | Family or semantic evidence |
| `indeterminate` | A candidate mention exists, identity not established |
| `not_evidenced` | **No candidate mention at all** |
| `negatively_indicated` | Explicit contradictory statement (extraction deferred) |

- job_side evidence, relationship_class, provenance, establishing_stage,
  stage3_certainty

> `indeterminate` ≠ `not_evidenced`. Indeterminate is fixable by catalog
> growth; not-evidenced is an irreducible property of the posting.
> `not_evidenced` means **only** that the posting gave no evidence — never
> unsupported, unavailable, rejected, incompatible, or negative.

> Preference alignment **never implies capability**, and absence is never
> negative evidence.

#### 2.16.4 `SeniorityComparison`

**Two separate records per match** — fit and preference are never merged
into one seniority value.

- id, match_result_id (FK)
- comparison_axis (`fit` / `preference`)
  - `fit` compares `JobPosting.seniority` against `current_seniority`
  - `preference` compares `JobPosting.seniority` against `target_seniority`
- outcome:

| Outcome | Condition |
|---|---|
| `at_level` | Same canonical level, same track |
| `below` | Same track, job ranks lower |
| `above` | Same track, job ranks higher |
| `different_track` | Tracks differ. **No magnitude claim** |
| `unknown` | One side has no seniority evidence |
| `indeterminate` | Evidence exists but did not resolve |

- profile_side and job_side level names and tracks **by value**
- provenance and evidence_source per side
- establishing_stage

> `UNIQUE (match_result_id, comparison_axis)` — at most one of each.

> A `preference` comparison is absent entirely when `target_seniority` is
> unset. Unset is **not** `not_evidenced` and **not** a default to current.

### 2.17 `SkillGap`

**Established** capability gaps only.

- id, match_result_id (FK)
- skill_name
- gap_type (`missing` / `below-threshold-experience`)
- is_mandatory

> Recorded **only** when identity was established and capability is
> genuinely absent or below threshold. Identity uncertainty belongs in
> `IndeterminateComparison`; preference outcomes never produce `SkillGap`;
> seniority uncertainty never produces `SkillGap`.

### 2.18 `UserJobDecisionEvent`

**Immutable decision history.** There is no mutable "current decision"
row; the current decision is **derived from the latest event**.

- id
- job_posting_id (FK), user_profile_id (FK)
- match_result_id (FK — the result visible at decision time)
- previous_decision (nullable for the first event)
- new_decision
- decided_at
- actor (attributable user/actor)
- reason_code (nullable — controlled enum, below)
- reason_text (nullable — optional free text)
- context (relevant decision context)

**Supported decisions:** `NEW`, `SAVED`, `INTERESTED`, `APPLY`, `SKIPPED`

**`reason_code` — controlled enum**, limited to documented product
behaviour:

- `company_desirable`
- `salary_attractive`
- `location_attractive`
- `remote_opportunity`
- `career_growth`
- `learning_missing_skill`
- `description_unusually_suitable`
- `want_to_stretch`
- `other`

> **Both are supported: a controlled code for analytics, optional free
> text for context.** Free text must never *replace* the code — if a
> reason is given, `reason_code` is required and `reason_text` is
> supplementary. The enum stays small; no large taxonomy is invented.

> Append-only, enforced by trigger. Historical decisions are never
> overwritten.

### 2.19 `Resume`

- id, user_profile_id (FK), label, file_reference, is_active, created_at

> Resume content is **never a matching input** and must never cross the
> LLM boundary.

### 2.20 `Application`

- id, job_posting_id (FK), match_result_id (FK), user_profile_id (FK),
  resume_id (FK)
- applied_date, application_url
- status (see [REQUIREMENTS.md](REQUIREMENTS.md#41-status-pipeline))
- recruiter_contact_name, recruiter_contact_info
- ctc_current, ctc_offered, notice_period
- rejection_reason, follow_up_date
- created_at / updated_at

> None of these is a matching input. CTC, recruiter contacts, notice
> period and notes must never cross the LLM boundary.

### 2.21 `ApplicationStatusEvent`

Append-only status transitions.

- id, application_id (FK), from_status (nullable), to_status, occurred_at,
  note

### 2.22 `InterviewEvent`

- id, application_id (FK), round_label, scheduled_at, notes

### 2.23 `Note`

- id, application_id (FK), body, created_at

### 2.24 `Notification`

In-app only.

- id, type, reference_type, reference_id, message, is_read, created_at

## 3. Design Principles

### 3.1 Match Score Informs, Never Controls, the Decision

> **Match score informs the decision; it never controls the decision. The
> user can apply to any discovered job regardless of score.**

`MatchResult` and `UserJobDecisionEvent` are separate so this holds
structurally: nothing prevents an `APPLY` decision against a job with a
low — or undefined — `match_score`.

Reason codes are listed in [Section 2.18](#218-userjobdecisionevent) and
are optional; a decision is valid without one.

### 3.2 Historical Integrity of Match Results

Historical `MatchResult` data must not be overwritten when the profile,
skills, **or the catalog** change.

> **Architectural invariant:** The matching engine must receive a specific
> immutable `ProfileVersion` as its profile input and must not read
> matching-relevant values directly from the current/live `UserProfile`
> while calculating a `MatchResult`.

**Example — the Terraform case:** at **v7** Terraform is `Working`; a job
scores **72%** referencing v7. The user raises it to `Strong`, minting
**v8**. Re-matching produces a **new** result, possibly scoring
differently. The original 72% stays linked to v7, unchanged.

**Staleness.** `UserProfile.current_profile_version_id` points at the
latest version; any `MatchResult` with a different `profile_version_id` is
a re-match candidate.

### 3.3 Data Separation (Canonical vs. User-Specific Data)

```
Job Source → JobPosting → MatchResult → UserJobDecisionEvent → Application
```

- **`JobPosting`** is **canonical job information**, independent of any user.
- **`MatchResult`, `UserJobDecisionEvent`, `Application`** are
  **user-specific**.
- **The skill and seniority catalogs** are a third category: **shared
  reference data**.

### 3.4 Capability vs. Intent, Fit vs. Preference

| Concept | Answers | Role |
|---|---|---|
| [`ProfileSkill`](#211-profileskill) | *What can I do?* | **Capability** |
| [`MatchingCriteria`](#212-matchingcriteria) | *What should matching prioritize?* | **Search intent** |
| `current_seniority` | *What level am I at?* | **Current-fit signal** |
| `target_seniority` | *What level do I want?* | **Preference signal** |

**Capability is not intent.** **Fit is not preference.** **Preferences
rank; they never filter.**

**Stretch has a structural definition:** strong preference alignment
combined with confirmed capability gaps on the same skills.

### 3.5 Open-World vs. Closed-World Evidence

| | **Fit axis** | **Preference axis** |
|---|---|---|
| Evidence source | `ProfileSkill` inventory | The `JobPosting` |
| Assumption | **Closed world** | **Open world** |
| Absence means | **Meaningful** — implies `missing` | **Not meaningful** |

*"The job does not mention Kubernetes"* is a claim about a **document**.
*"The job does not use Kubernetes"* is a claim about a **workplace**. The
first never entails the second.

Consequences: preference absence yields `not_evidenced`, never a negative
and never a `SkillGap`; preference alignment **accumulates positive
evidence** rather than completing a ratio; preference coverage is **not a
completeness measure** and **does not gate `match_category`**; fit
coverage **does** gate it.

### 3.6 Seniority as a Partial Order

Seniority is **ordered within a track and unordered across tracks**. This
is a deliberate refusal of false precision.

- `rank_within_track` is comparable **only** between levels sharing a
  track.
- Cross-track comparison yields `different_track` — never a magnitude,
  never a numeric distance.
- A Principal considering a Director role is a **track change**, not a
  promotion of `+1`.
- `SeniorityRelationship` starts empty, so no cross-track claim is made
  until one is explicitly curated.
- Seniority is derived from **catalogued rules, never substring
  matching**: "Senior Cloud Engineer" differs from "Cloud Engineer"
  because `Senior` is a catalogued modifier, not through string similarity.
- **Absence of a seniority modifier yields unknown**, never a default.

`target_seniority` **includes track**, because "I want to move into
management" is a track change that a level-only value cannot express.

## 4. Data Integrity, Immutability and Retention

### 4.1 Immutability and historical denormalization

| Record | Mutability |
|---|---|
| `ProfileVersion` | **Immutable** (trigger-enforced) |
| `MatchResult` + all evidence + job descriptor | **Immutable** (trigger-enforced) |
| `UserJobDecisionEvent` | **Append-only** |
| `ApplicationStatusEvent` | Append-only |
| Audit records | Append-only |
| Catalogs | **Mutable reference data** |

**Corrections create new records**, never in-place mutation.

**Value-denormalization is mandatory.** Historical records store canonical
names — and seniority level names **and tracks** — **by value**. A
`skill_id` or `seniority_level_id` in a historical record is a provenance
pointer that may dangle after a merge and must never drive interpretation.

**Catalog evolution affects the future only.** Rename, merge or alias
changes may trigger **re-resolution of live data**; they never alter an
existing `MatchResult` or `ProfileVersion`.

Catalog history is retained for interpretation, and a merge records a
**tombstone mapping** from old identity to new.

### 4.2 Uniqueness and identity constraints

- **`normalized_representation` is globally unique** within
  `SkillRepresentation`, and within `SeniorityRepresentation`.
- Exactly one `canonical` representation per catalog entry.
- A colliding representation is **rejected** and recorded as a conflict.
- **Ambiguous representations remain unresolved.**
- **Merge requires proven identity equivalence** — not mere relatedness.
- **Rename never changes identity.**
- **A single model output never creates canonical identity.**

### 4.3 Idempotency requirements

- Same inputs + same `rule_version` + same `catalog_version` produce
  **identical resolution and no new state**.
- **`ProfileVersion` minting is outcome-keyed**; a no-op sweep mints
  nothing, and concurrent triggers converge on one version.
- Re-ingesting a `JobPosting` creates no duplicates (`source_job_id`,
  `dedupe_key`).
- **Promotion is idempotent** — a second attempt converges on the existing
  entry via representation uniqueness.
- **Promotion evidence counts independent postings**, enforced by
  `UNIQUE (normalized_representation, job_posting_id)`.

### 4.4 Data quality at ingestion

- Empty, whitespace-only and malformed mentions are **rejected at
  ingestion** — extraction defects, not unresolved skills, and never
  entered into the promotion-evidence pool.
- Duplicate mentions within one posting **collapse to one**. Multiplicity
  within a document is not evidence.
- Contradictory resolution candidates leave the mention **unresolved** and
  raise a conflict. Arbitrary tie-breaking is prohibited.
- Stale catalog references in historical records are tolerated, because
  names are stored by value.
- **Unresolved is legitimate**, never an operational failure.

### 4.5 Retention and lifecycle

| Data | Lifecycle |
|---|---|
| `ProfileVersion` | **Immutable, retained indefinitely** |
| `MatchResult` + evidence + job descriptor | **Immutable, retained indefinitely** |
| Catalog history (incl. tombstones) | **Retained** for interpretation |
| Audit records | **Append-only, retained** |
| `JobPosting` | **Purgeable** under a future policy — `MatchResultJobDescriptor` preserves interpretability |
| `description_raw` | *Recommendation:* expirable for closed postings post-extraction |
| Rejected promotion evidence | *Recommendation:* expirable |
| Operational logs | *Recommendation:* expirable |

> Purge policies themselves are **not finalized** — see
> [Section 7](#7-explicitly-deferred).

## 5. Relationship Notes

- A `ProfileSkill` may exist without a `MatchingCriteria`, and vice versa.
- A `JobPosting` can have many `MatchResult`s over time.
- A `ProfileVersion` can back many `MatchResult`s; each result references
  exactly one version, permanently.
- `SkillPromotionEvidence` predates any `Skill`, so it keys on normalized
  representation.
- Seniority levels are comparable only within a track.
- `UserJobDecisionEvent` is append-only; the current decision is derived,
  not stored.
- `MatchResultJobDescriptor` is one-to-one with `MatchResult` and outlives
  the `JobPosting`.

## 6. Database Invariants

Matching-behaviour invariants live in
[AI-MATCHING.md Section 13](AI-MATCHING.md#13-matching-invariants).

1. `ProfileVersion` is never mutated.
2. `MatchResult` and its evidence are never mutated.
3. Historical records never change because the catalog changed.
4. Historical records store canonical names (and seniority tracks) **by
   value**; an id is never the basis of interpretation.
5. All **four** version axes are recorded and `NOT NULL`.
6. `normalized_representation` is globally unique per representation
   namespace.
7. A representation binds to at most one catalog entry; collisions are
   rejected.
8. Identity, `family` and `adjacent` remain distinct.
9. Canonical identity is never created from a single unverified model
   output.
10. Uncertainty is never recorded as a `SkillGap`.
11. A `SkillGap` requires that identity was established.
12. Preference outcomes never produce `SkillGap` entries.
13. Preference absence is `not_evidenced`, never a negative.
14. Fit coverage and preference coverage are never merged.
15. Zero fit coverage yields an undefined `match_score` **and**
    `match_category`.
16. `unresolved` is legitimate, never an error.
17. Promotion evidence counts independent postings only.
18. User confirmation is attributable, timestamped and auditable.
19. Audit and decision records are append-only.
20. `ProfileVersion` minting is outcome-keyed, idempotent and convergent
    under concurrency.
21. `MatchingCriteria.weight` is `NUMERIC` within `[0.0, 1.0]`.
22. Seniority ranks are comparable **only within a track**; cross-track
    comparison never yields a magnitude.
23. `target_seniority` carries a track and never defaults to
    `current_seniority`.
24. The seniority catalog shares `catalog_version` — **no fifth axis**.
25. Seniority uncertainty never produces a `SkillGap`.
26. A `MatchResult` remains interpretable after its `JobPosting` is
    purged.

## 7. Explicitly Deferred

- Exact column types beyond those decided here, and index tuning.
- Full-text/semantic search indexing strategy for job descriptions.
- Soft-delete vs. hard-delete conventions.
- Multi-user support.
- **Stage 3 profile-side narrative/responsibilities input** — interfaces
  must accommodate it later without changing `ProfileVersion`
  immutability, the LLM egress boundary, Stage 2 determinism, Stage 4
  scoring, the version axes, or `MatchResult` integrity.
- Batching of system-triggered `ProfileVersion` minting.
- `schema_version` migration strategy for older snapshot shapes.
- Whether target-seniority distance is treated **symmetrically**.
- Whether cross-track `SeniorityRelationship` rows are ever populated.
- Exact title-to-level rules beyond the initial ladders; whether
  `Architect`, `Lead`, `Head`, `Associate` ever receive canonical levels.
- Whether `Distinguished`/`Fellow` levels exist above `Principal`.
- Whether `target_seniority` may be a range.
- How a `JobPosting` expresses a **required proficiency level**.
- Whether per-skill experience is modelled separately.
- **Negative-signal extraction** and its `JobPosting` representation.
- **Merge reversibility** and the conflict-adjudication workflow.
- Whether `SkillPromotionEvidence` is immutable or compacted.
- Promotion evidence **thresholds** (numeric).
- Purge policy and retention windows for `JobPosting`, `description_raw`,
  rejected evidence and logs.
- Whether an aggregate confidence or `preference_score` is materialized.
- Scoring weights, coverage thresholds and the thresholds gating
  `match_category`.
