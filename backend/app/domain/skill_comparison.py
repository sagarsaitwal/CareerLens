"""Stage 2 skill-pair comparison (AI-MATCHING.md Section 5).

Pure and deterministic: no model calls, no I/O beyond the supplied
relationship lookup, and no credit, score or category. It answers only
what Stage 2 can ESTABLISH about one profile skill and one job skill;
what that is worth is Stage 4, whose formula is deferred.

    profile side   job side                    outcome / relationship
    resolved       resolved, same skill        MATCH / identity (or alias)
    resolved       resolved, different skill   NON_IDENTITY / family, adjacent or none
    resolved       unresolved                  INDETERMINATE
    unresolved     resolved                    INDETERMINATE
    unresolved     unresolved, same text       MATCH / normalized_text
    unresolved     unresolved, different text  INDETERMINATE

Identity, family and adjacency never collapse (invariant 13): a family
or adjacent pair is a definite NON-identity that merely carries the
catalogued relationship. Family keeps its direction (DATABASE.md 2.3:
the source is a specialization of the target); whether and how much
each direction earns is deferred, so this module does not decide it.

Only deterministic provenance is evidence here (Section 4, invariant 3):
a side resolved by `ai_semantic`, or left `indeterminate` by an open
catalog conflict, makes the pair INDETERMINATE, never a match or a gap.

Both sides must have been resolved against the same catalog version as
the lookup; reconciling versions is the caller's job.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.domain.resolution import DETERMINISTIC_PROVENANCE
from app.models.enums import ResolutionProvenance, ResolutionStatus, SkillRelationshipType


class PairOutcome(StrEnum):
    MATCH = "match"
    NON_IDENTITY = "non_identity"
    INDETERMINATE = "indeterminate"


class PairRelationship(StrEnum):
    IDENTITY = "identity"
    ALIAS = "alias"
    NORMALIZED_TEXT = "normalized_text"
    FAMILY = "family"
    # Same domain, not substitutable: never a match and no Stage 2 credit.
    ADJACENT = "adjacent"
    NONE = "none"


class FamilyDirection(StrEnum):
    # The profile's skill is a specialization of the job's (EKS -> Kubernetes).
    PROFILE_SPECIALIZES_JOB = "profile_specializes_job"
    # The job's skill is a specialization of the profile's.
    JOB_SPECIALIZES_PROFILE = "job_specializes_profile"


class ComparableSkill(Protocol):
    """A resolved-or-not mention: ProfileVersion entries and job mentions both fit."""

    @property
    def raw_text(self) -> str: ...
    @property
    def normalized_representation(self) -> str: ...
    @property
    def status(self) -> ResolutionStatus: ...
    @property
    def provenance(self) -> ResolutionProvenance: ...
    @property
    def skill_id(self) -> uuid.UUID | None: ...
    @property
    def resolved_canonical_name(self) -> str | None: ...


class RelationshipLookup(Protocol):
    """Catalog relationships at the pinned version (SkillRelationshipRepository)."""

    def has_relationship(
        self,
        from_skill_id: uuid.UUID,
        to_skill_id: uuid.UUID,
        relationship_type: SkillRelationshipType,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class SideEvidence:
    """One side as compared, by value."""

    raw_text: str
    normalized_representation: str
    status: ResolutionStatus
    provenance: ResolutionProvenance
    canonical_name: str | None


@dataclass(frozen=True, slots=True)
class SkillPairComparison:
    outcome: PairOutcome
    relationship: PairRelationship
    profile: SideEvidence
    job: SideEvidence
    family_direction: FamilyDirection | None = None
    # Why the pair is indeterminate; None otherwise.
    reason: str | None = None
    establishing_stage: int = 2


def _evidence(side: ComparableSkill) -> SideEvidence:
    return SideEvidence(
        raw_text=side.raw_text,
        normalized_representation=side.normalized_representation,
        status=side.status,
        provenance=side.provenance,
        canonical_name=side.resolved_canonical_name,
    )


# Per-side causes that make a pair indeterminate, most fundamental first.
# The pair reports the highest-ranked cause across BOTH sides, so the
# reason never depends on which side is profile and which is job. An
# incoherent record cannot be trusted at all, provenance included; a
# model judgement is never evidence; an open conflict is the mildest.
INCOHERENT_RESOLUTION = "incoherent_resolution"
NON_DETERMINISTIC_PROVENANCE = "non_deterministic_provenance"
SIDE_INDETERMINATE = "side_indeterminate"
SIDE_CAUSE_PRECEDENCE: tuple[str, ...] = (
    INCOHERENT_RESOLUTION,
    NON_DETERMINISTIC_PROVENANCE,
    SIDE_INDETERMINATE,
)


def _side_cause(side: ComparableSkill) -> str | None:
    # Identity without "resolved" asserts a resolution the pipeline never made.
    if side.status is not ResolutionStatus.RESOLVED and side.skill_id is not None:
        return INCOHERENT_RESOLUTION
    if side.status is ResolutionStatus.INDETERMINATE:
        return SIDE_INDETERMINATE
    if side.status is ResolutionStatus.RESOLVED:
        if side.skill_id is None:
            return INCOHERENT_RESOLUTION
        if side.provenance not in DETERMINISTIC_PROVENANCE:
            return NON_DETERMINISTIC_PROVENANCE
    return None


def compare_skill_pair(
    profile: ComparableSkill, job: ComparableSkill, relationships: RelationshipLookup
) -> SkillPairComparison:
    """What Stage 2 can establish about one profile skill and one job skill."""
    profile_side, job_side = _evidence(profile), _evidence(job)

    def result(
        outcome: PairOutcome,
        relationship: PairRelationship = PairRelationship.NONE,
        *,
        direction: FamilyDirection | None = None,
        reason: str | None = None,
    ) -> SkillPairComparison:
        return SkillPairComparison(outcome, relationship, profile_side, job_side, direction, reason)

    causes = {_side_cause(profile), _side_cause(job)}
    for cause in SIDE_CAUSE_PRECEDENCE:
        if cause in causes:
            return result(PairOutcome.INDETERMINATE, reason=cause)

    profile_resolved = profile.status is ResolutionStatus.RESOLVED
    job_resolved = job.status is ResolutionStatus.RESOLVED

    if not profile_resolved and not job_resolved:
        if profile.normalized_representation == job.normalized_representation:
            return result(PairOutcome.MATCH, PairRelationship.NORMALIZED_TEXT)
        return result(PairOutcome.INDETERMINATE, reason="unresolved_different_text")

    if profile_resolved != job_resolved:
        return result(PairOutcome.INDETERMINATE, reason="one_side_unresolved")

    profile_id, job_id = profile.skill_id, job.skill_id
    assert profile_id is not None and job_id is not None  # else INCOHERENT_RESOLUTION

    if profile_id == job_id:
        via_alias = ResolutionProvenance.ALIAS_RULE in (profile.provenance, job.provenance)
        return result(
            PairOutcome.MATCH, PairRelationship.ALIAS if via_alias else PairRelationship.IDENTITY
        )

    family = SkillRelationshipType.FAMILY
    adjacent = SkillRelationshipType.ADJACENT
    found: list[tuple[PairRelationship, FamilyDirection | None]] = []
    if relationships.has_relationship(profile_id, job_id, family):
        found.append((PairRelationship.FAMILY, FamilyDirection.PROFILE_SPECIALIZES_JOB))
    if relationships.has_relationship(job_id, profile_id, family):
        found.append((PairRelationship.FAMILY, FamilyDirection.JOB_SPECIALIZES_PROFILE))
    # Adjacency is symmetric but may be catalogued in either direction.
    if relationships.has_relationship(
        profile_id, job_id, adjacent
    ) or relationships.has_relationship(job_id, profile_id, adjacent):
        found.append((PairRelationship.ADJACENT, None))

    if len(found) > 1:
        # A catalog defect; arbitrary tie-breaking is prohibited (DATABASE.md 4.4).
        return result(PairOutcome.INDETERMINATE, reason="contradictory_relationships")
    if found:
        relationship, direction = found[0]
        return result(PairOutcome.NON_IDENTITY, relationship, direction=direction)
    return result(PairOutcome.NON_IDENTITY)
