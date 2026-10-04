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
