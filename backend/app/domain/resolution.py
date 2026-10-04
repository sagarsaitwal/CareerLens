"""Deterministic skill resolution.

Pure functions over a supplied lookup, so the cascade is testable
without a database and re-executable from recorded versions.

This module implements ONLY the four deterministic provenance tiers.
`ai_semantic` is Stage 3 and is deliberately unreachable from here: a
model judgement becomes Stage 2 evidence only by passing through
catalog promotion (AI-MATCHING.md Section 4).
"""

import uuid
from dataclasses import dataclass
from typing import Protocol

from app.domain.normalization import is_valid_representation, normalize
from app.models.enums import (
    RepresentationKind,
    RepresentationOrigin,
    ResolutionProvenance,
    ResolutionStatus,
)

# Re-exported so callers can keep importing the resolution vocabulary
# from the domain module that uses it. The definitions live in
# app.models.enums because the database CHECK constraints need them too,
# and that module depends on nothing else in the application.
__all__ = [
    "DETERMINISTIC_PROVENANCE",
    "RepresentationLookup",
    "RepresentationRecord",
    "ResolutionProvenance",
    "ResolutionStatus",
    "SkillResolution",
    "resolve_skill",
]

DETERMINISTIC_PROVENANCE: frozenset[ResolutionProvenance] = frozenset(
    {
        ResolutionProvenance.EXACT_MATCH,
        ResolutionProvenance.NORMALIZED,
        ResolutionProvenance.ALIAS_RULE,
        ResolutionProvenance.USER_CONFIRMED,
    }
)


@dataclass(frozen=True, slots=True)
class RepresentationRecord:
    """A catalog representation, as the resolver needs to see it."""

    entry_id: uuid.UUID
    normalized_representation: str
    display_text: str
    kind: RepresentationKind
    origin: RepresentationOrigin
    canonical_name: str


@dataclass(frozen=True, slots=True)
class SkillResolution:
    """The result of resolving one raw mention.

    `resolved_canonical_name` is captured BY VALUE so a historical record
    stays interpretable after the catalog is renamed or merged.
    """

    raw_text: str
    normalized_representation: str
    status: ResolutionStatus
    provenance: ResolutionProvenance
    skill_id: uuid.UUID | None = None
    resolved_canonical_name: str | None = None
    reason: str | None = None

    @property
    def is_deterministic(self) -> bool:
        return self.provenance in DETERMINISTIC_PROVENANCE


class RepresentationLookup(Protocol):
    """Catalog access the resolver depends on.

    A Protocol keeps the domain pure: tests pass a dict-backed stub, the
    application passes a repository.
    """

    def find_by_normalized(self, normalized: str) -> RepresentationRecord | None: ...

    def has_open_conflict(self, normalized: str) -> bool: ...


def _provenance_for(record: RepresentationRecord, raw_text: str) -> ResolutionProvenance:
    """Classify how the match was established.

    Precedence, highest first:
      1. user_confirmed  - a human affirmed this mapping
      2. alias_rule      - matched a catalogued alias, transformed or not
      3. exact_match     - matched the canonical name untransformed
      4. normalized      - matched the canonical name after normalization

    The documented tiers overlap, because `exact_match` reads "identical
    to a canonical name or alias" while `alias_rule` reads "matched an
    explicit catalogued alias". An exactly-typed alias satisfies both.
    The tie is broken toward the alias, because the required explanation
    is "K8s matched Kubernetes via catalogued alias" — reporting that as
    an exact match would hide the fact the user most needs to see.
    """
    if record.origin is RepresentationOrigin.USER_CONFIRMATION:
        return ResolutionProvenance.USER_CONFIRMED
    if record.kind is RepresentationKind.ALIAS:
        return ResolutionProvenance.ALIAS_RULE
    if raw_text == record.display_text:
        return ResolutionProvenance.EXACT_MATCH
    return ResolutionProvenance.NORMALIZED


def resolve_skill(raw_text: str, lookup: RepresentationLookup) -> SkillResolution:
    """Resolve one raw skill mention deterministically.

    Never calls a model. Returns UNRESOLVED rather than guessing, which
    is a legitimate operating state and not an error.
    """
    if not is_valid_representation(raw_text):
        return SkillResolution(
            raw_text=raw_text,
            normalized_representation="",
            status=ResolutionStatus.UNRESOLVED,
            provenance=ResolutionProvenance.UNRESOLVED,
            reason="malformed_mention",
        )

    normalized = normalize(raw_text)

    # An open conflict means the catalog itself is contradictory for this
    # representation. Declining to resolve is correct; picking a
    # candidate would be an arbitrary tie-break.
    if lookup.has_open_conflict(normalized):
        return SkillResolution(
            raw_text=raw_text,
            normalized_representation=normalized,
            status=ResolutionStatus.INDETERMINATE,
            provenance=ResolutionProvenance.UNRESOLVED,
            reason="open_catalog_conflict",
        )

    record = lookup.find_by_normalized(normalized)
    if record is None:
        return SkillResolution(
            raw_text=raw_text,
            normalized_representation=normalized,
            status=ResolutionStatus.UNRESOLVED,
            provenance=ResolutionProvenance.UNRESOLVED,
            reason="not_in_catalog",
        )

    return SkillResolution(
        raw_text=raw_text,
        normalized_representation=normalized,
        status=ResolutionStatus.RESOLVED,
        provenance=_provenance_for(record, raw_text),
        skill_id=record.entry_id,
        resolved_canonical_name=record.canonical_name,
    )
