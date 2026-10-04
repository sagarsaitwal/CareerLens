"""Deterministic seniority resolution and comparison.

Seniority is a PARTIAL ORDER: ranks compare only within a track, and
cross-track comparison yields `different_track` rather than an invented
magnitude (DATABASE.md Section 3.6).

Pure functions, no I/O, no model calls.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from app.domain.normalization import is_valid_representation, normalize
from app.domain.resolution import ResolutionProvenance, ResolutionStatus
from app.models.enums import (
    EvidenceSource,
    RepresentationKind,
    RepresentationOrigin,
    SeniorityTrack,
)

# Re-exported for the same reason as the resolution vocabulary: the
# definition lives in app.models.enums so database CHECK constraints can
# use it without importing the domain.
__all__ = [
    "AMBIGUOUS_SENIORITY_TERMS",
    "NON_SENIORITY_ROLE_TERMS",
    "EvidenceSource",
    "SeniorityComparisonOutcome",
    "SeniorityLevelRecord",
    "SeniorityLookup",
    "SeniorityRepresentationRecord",
    "SeniorityResolution",
    "compare_levels",
    "compare_seniority",
    "resolve_seniority",
]


class SeniorityComparisonOutcome(StrEnum):
    AT_LEVEL = "at_level"
    BELOW = "below"
    ABOVE = "above"
    DIFFERENT_TRACK = "different_track"
    UNKNOWN = "unknown"
    INDETERMINATE = "indeterminate"


# Titles that are seniority-ADJACENT but whose meaning varies by company.
# They must never be resolved by substring matching or by guessing:
# "Lead" is not Senior, "Architect" is not Principal, "Head" is not VP.
# They are INDETERMINATE — evidence exists, but it cannot be pinned.
#
# Deliberately held in code rather than in the catalog, because
# DATABASE.md Section 2.7 states these are absent from the catalog by
# design. Governed by `rule_version`.
AMBIGUOUS_SENIORITY_TERMS: Final[frozenset[str]] = frozenset(
    {"lead", "architect", "associate", "head"}
)

# Role nouns that carry NO seniority signal at all. Distinct from the
# ambiguous set: there is nothing to pin, so the answer is UNKNOWN
# rather than indeterminate. Absence of a modifier must never default
# to Mid.
NON_SENIORITY_ROLE_TERMS: Final[frozenset[str]] = frozenset(
    {"engineer", "software engineer", "developer", "programmer"}
)


@dataclass(frozen=True, slots=True)
class SeniorityLevelRecord:
    """A catalog seniority level, as the resolver needs to see it."""

    level_id: uuid.UUID
    canonical_name: str
    track: SeniorityTrack
    rank_within_track: int


@dataclass(frozen=True, slots=True)
class SeniorityRepresentationRecord:
    normalized_representation: str
    display_text: str
    kind: RepresentationKind
    origin: RepresentationOrigin
    level: SeniorityLevelRecord


@dataclass(frozen=True, slots=True)
class SeniorityResolution:
    """The result of resolving one seniority reference.

    Level name AND track are captured by value, so a historical record
    survives catalog evolution.
    """

    raw_text: str
    normalized_representation: str
    status: ResolutionStatus
    provenance: ResolutionProvenance
    evidence_source: EvidenceSource
    level_id: uuid.UUID | None = None
    resolved_level_name: str | None = None
    resolved_track: SeniorityTrack | None = None
    # Carried by value so comparison needs no catalog round-trip and a
    # historical record stays interpretable after the catalog changes.
    resolved_rank_within_track: int | None = None
    reason: str | None = None


class SeniorityLookup(Protocol):
    def find_by_normalized(
        self, normalized: str
    ) -> SeniorityRepresentationRecord | None: ...


def _provenance_for(
    record: SeniorityRepresentationRecord, raw_text: str
) -> ResolutionProvenance:
    """Same precedence as skill resolution; see app.domain.resolution."""
    if record.origin is RepresentationOrigin.USER_CONFIRMATION:
        return ResolutionProvenance.USER_CONFIRMED
    if record.kind is RepresentationKind.ALIAS:
        return ResolutionProvenance.ALIAS_RULE
    if raw_text == record.display_text:
        return ResolutionProvenance.EXACT_MATCH
    return ResolutionProvenance.NORMALIZED


def resolve_seniority(
    raw_text: str,
    lookup: SeniorityLookup,
    evidence_source: EvidenceSource,
) -> SeniorityResolution:
    """Resolve a seniority reference deterministically.

    Matching is whole-string against catalogued modifier rules. There is
    no substring scan: a title is only recognised if its text, or a
    catalogued alias of it, is in the catalog.
    """
    if not is_valid_representation(raw_text):
        return SeniorityResolution(
            raw_text=raw_text,
            normalized_representation="",
            status=ResolutionStatus.UNRESOLVED,
            provenance=ResolutionProvenance.UNRESOLVED,
            evidence_source=evidence_source,
            reason="malformed_reference",
        )

    normalized = normalize(raw_text)

    # Checked BEFORE the catalog so an ambiguous term can never be
    # resolved even if some future seed mistakenly catalogued it.
    if normalized in AMBIGUOUS_SENIORITY_TERMS:
        return SeniorityResolution(
            raw_text=raw_text,
            normalized_representation=normalized,
            status=ResolutionStatus.INDETERMINATE,
            provenance=ResolutionProvenance.UNRESOLVED,
            evidence_source=evidence_source,
            reason="ambiguous_title",
        )

    if normalized in NON_SENIORITY_ROLE_TERMS:
        return SeniorityResolution(
            raw_text=raw_text,
            normalized_representation=normalized,
            status=ResolutionStatus.UNRESOLVED,
            provenance=ResolutionProvenance.UNRESOLVED,
            evidence_source=evidence_source,
            reason="no_seniority_signal",
        )

    record = lookup.find_by_normalized(normalized)
    if record is None:
        return SeniorityResolution(
            raw_text=raw_text,
            normalized_representation=normalized,
            status=ResolutionStatus.UNRESOLVED,
            provenance=ResolutionProvenance.UNRESOLVED,
            evidence_source=evidence_source,
            reason="not_in_catalog",
        )

    return SeniorityResolution(
        raw_text=raw_text,
        normalized_representation=normalized,
        status=ResolutionStatus.RESOLVED,
        provenance=_provenance_for(record, raw_text),
        evidence_source=evidence_source,
        level_id=record.level.level_id,
        resolved_level_name=record.level.canonical_name,
        resolved_track=record.level.track,
        resolved_rank_within_track=record.level.rank_within_track,
    )


def compare_seniority(
    reference: SeniorityResolution,
    subject: SeniorityResolution,
) -> SeniorityComparisonOutcome:
    """Compare a subject level against a reference level.

    `reference` is the profile side (current or target), `subject` is the
    job side. The outcome describes where the JOB sits relative to the
    profile.

    No numeric distance is produced: target-distance symmetry and any
    weighting remain deferred, and this function must not pre-empt them.
    """
    if (
        reference.status is ResolutionStatus.INDETERMINATE
        or subject.status is ResolutionStatus.INDETERMINATE
    ):
        return SeniorityComparisonOutcome.INDETERMINATE

    if (
        reference.status is not ResolutionStatus.RESOLVED
        or subject.status is not ResolutionStatus.RESOLVED
    ):
        return SeniorityComparisonOutcome.UNKNOWN

    if reference.resolved_track is not subject.resolved_track:
        # No magnitude claim is made across tracks. Cross-track
        # relationships are seeded empty by decision.
        return SeniorityComparisonOutcome.DIFFERENT_TRACK

    reference_rank = reference.resolved_rank_within_track
    subject_rank = subject.resolved_rank_within_track
    if reference_rank is None or subject_rank is None:
        return SeniorityComparisonOutcome.UNKNOWN

    return _compare_ranks(reference_rank, subject_rank)


def _compare_ranks(reference_rank: int, subject_rank: int) -> SeniorityComparisonOutcome:
    """Rank comparison, valid only because both sides share a track."""
    if subject_rank == reference_rank:
        return SeniorityComparisonOutcome.AT_LEVEL
    if subject_rank < reference_rank:
        return SeniorityComparisonOutcome.BELOW
    return SeniorityComparisonOutcome.ABOVE


def compare_levels(
    reference: SeniorityLevelRecord,
    subject: SeniorityLevelRecord,
) -> SeniorityComparisonOutcome:
    """Compare two resolved catalog levels directly.

    Ranks are comparable ONLY within a track, which is why the track
    check comes first and short-circuits.
    """
    if reference.track is not subject.track:
        return SeniorityComparisonOutcome.DIFFERENT_TRACK
    return _compare_ranks(reference.rank_within_track, subject.rank_within_track)
