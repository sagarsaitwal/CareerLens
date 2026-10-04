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
