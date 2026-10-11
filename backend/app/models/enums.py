"""Catalog vocabularies.

Stored as VARCHAR with CHECK constraints rather than native PostgreSQL
enums: the values are part of the documented specification and change
only by deliberate decision, and CHECK constraints are far easier to
evolve in a migration than ALTER TYPE.
"""

from enum import StrEnum


class RepresentationKind(StrEnum):
    """How a textual form relates to its catalog entry."""

    CANONICAL = "canonical"
    ALIAS = "alias"


class RepresentationOrigin(StrEnum):
    """Where a representation came from.

    `promotion` and `user_confirmation` exist so that evidence-gated
    promotion and human confirmation remain distinguishable from curated
    seed data. A model never writes here directly.
    """

    CURATION = "curation"
    PROMOTION = "promotion"
    USER_CONFIRMATION = "user_confirmation"


class SkillRelationshipType(StrEnum):
    """Identity, family and adjacency are three distinct concepts.

    Neither value implies equivalence. `family` is directional: the
    source is a specialization of the target.
    """

    FAMILY = "family"
    ADJACENT = "adjacent"


class PromotionOutcome(StrEnum):
    PENDING = "pending"
    PROMOTED_AS_SKILL = "promoted_as_skill"
    PROMOTED_AS_ALIAS = "promoted_as_alias"
    REJECTED = "rejected"


class ConflictType(StrEnum):
    REPRESENTATION_COLLISION = "representation_collision"
    CONTRADICTORY_RESOLUTION_CANDIDATES = "contradictory_resolution_candidates"
    MERGE_PRECONDITION_FAILURE = "merge_precondition_failure"


class ConflictStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class SkillOrigin(StrEnum):
    """How a canonical Skill came into existence."""

    CURATION = "curation"
    PROMOTION = "promotion"


class ResolutionStatus(StrEnum):
    """Outcome of a resolution attempt.

    `INDETERMINATE` is distinct from `UNRESOLVED`: indeterminate means
    evidence exists but could not be pinned to one entry, whereas
    unresolved means no mapping was found at all. Conflating them would
    let an uncertainty masquerade as an absence.
    """

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    INDETERMINATE = "indeterminate"


class ResolutionProvenance(StrEnum):
    """How a resolution was established. Exactly six tiers, no seventh."""

    EXACT_MATCH = "exact_match"
    NORMALIZED = "normalized"
    ALIAS_RULE = "alias_rule"
    USER_CONFIRMED = "user_confirmed"
    AI_SEMANTIC = "ai_semantic"
    UNRESOLVED = "unresolved"


class EvidenceSource(StrEnum):
    """WHERE a seniority reference's text came from.

    Orthogonal to provenance, which says HOW it resolved. A structured
    field and a phrase scraped from a description can both resolve via
    `exact_match` while carrying very different authority, so the two
    must never be merged.
    """

    STRUCTURED_FIELD = "structured_field"
    TITLE = "title"
    REQUIREMENTS = "requirements"
    RESPONSIBILITIES = "responsibilities"
    DESCRIPTION = "description"


class Proficiency(StrEnum):
    """Capability level for a ProfileSkill.

    Ordinal: Strong > Working > Learning. Three levels is a deliberate
    ceiling, not an incomplete scale, and there is no numeric score.

    Absence of a ProfileSkill is a DISTINCT state from `LEARNING` — the
    two map to different gap types downstream, so they must never be
    collapsed.
    """

    STRONG = "Strong"
    WORKING = "Working"
    LEARNING = "Learning"


# Ordered weakest to strongest, for comparisons that need rank.
PROFICIENCY_ORDER: tuple[Proficiency, ...] = (
    Proficiency.LEARNING,
    Proficiency.WORKING,
    Proficiency.STRONG,
)


class SeniorityTrack(StrEnum):
    """Career tracks.

    There is deliberately no `UNKNOWN` member. Track-unknown is an
    *absence*, expressed by an unresolved seniority reference, never by a
    third stored track (DATABASE.md Section 2.6).
    """

    IC = "IC"
    MANAGEMENT = "Management"


class SeniorityRelationshipType(StrEnum):
    """Cross-track relationships.

    The table is intentionally seeded empty: populating it would assume a
    specific company's ladder, so cross-track comparison returns
    `different_track` instead of an invented magnitude.
    """

    CROSS_TRACK_BAND = "cross_track_band"


class JobSourceType(StrEnum):
    """What kind of origin a JobSource is (DATABASE.md Section 2.14)."""

    JOB_BOARD = "job_board"
    CAREER_PAGE = "career_page"
    API = "api"


class WorkMode(StrEnum):
    """Where the work happens.

    The same three values the profile side uses for
    `preferred_work_modes`. Stage 2 compares the two directly, so they
    are one vocabulary rather than two that happen to agree. There is
    deliberately no `unknown` member: a posting that does not state a
    work mode leaves the column NULL, which is an absence rather than a
    stated value.
    """

    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"


class EmploymentType(StrEnum):
    """Engagement form offered by a posting.

    `UNKNOWN` is a real member, not a placeholder, because DATABASE.md
    Section 2.15 requires an unrecognised source value to BECOME
    `unknown` rather than be guessed into the nearest member. Coercion
    would invent a fact the posting never stated.
    """

    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    TEMPORARY = "temporary"
    INTERNSHIP = "internship"
    UNKNOWN = "unknown"


class JobPostingStatus(StrEnum):
    """Lifecycle state of a posting.

    `UNKNOWN` is first-class: a source that stops listing a posting has
    not said it closed, and recording `CLOSED` on that basis would
    manufacture a fact.
    """

    ACTIVE = "active"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class ExtractionStatus(StrEnum):
    """How complete requirement extraction was for a posting.

    `PARTIAL` must never be read as "no further requirements exist"
    (DATABASE.md Section 2.15). It separates "the posting asks for
    nothing else" from "we did not finish looking".
    """

    COMPLETE = "complete"
    PARTIAL = "partial"


class DirectApplyVerificationStatus(StrEnum):
    """Whether a direct application URL was verified.

    A URL is never fabricated, so `NOT_AVAILABLE` and `UNVERIFIED` are
    both legitimate terminal states and neither may be presented as a
    usable link.
    """

    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    NOT_AVAILABLE = "not_available"


class JobMentionType(StrEnum):
    """Which collection a job-side skill mention belongs to.

    One table discriminated by this column, because Section 2.9 defines
    the mention as a shared field pattern rather than a table.

    `RESPONSIBILITY` is evidence only and carries no matching influence.
    Whether responsibilities may evidence a preference is deferred, so
    the category exists to retain the text without granting it weight
    that has not been approved.
    """

    REQUIRED = "required"
    PREFERRED = "preferred"
    MENTIONED = "mentioned"
    RESPONSIBILITY = "responsibility"
