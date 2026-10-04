"""Catalog mutation.

Catalog entries are never updated or deleted in place. Every change
happens inside a NEW catalog version and either:

  - inserts a row live from that version, or
  - CLOSES an existing row by setting `valid_to_version` to it.

Closing is the only form of removal. That is what keeps earlier
versions reconstructible, and therefore what makes a historical match
re-executable.

A model never calls into this module: catalog mutation is deterministic
code, and promotion is evidence-gated (Milestone 9).
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.normalization import is_valid_representation, normalize
from app.models.catalog import (
    CatalogVersion,
    Skill,
    SkillConflict,
    SkillRelationship,
    SkillRepresentation,
)
from app.models.enums import (
    ConflictStatus,
    ConflictType,
    RepresentationKind,
    RepresentationOrigin,
    SkillOrigin,
    SkillRelationshipType,
)
from app.repositories.catalog import CatalogVersionRef, live_at


class InvalidRepresentationError(ValueError):
    """Raised for an extraction defect rather than an unresolved skill."""


def open_catalog_version(
    session: Session, label: str, description: str | None = None
) -> CatalogVersionRef:
    """Mint the next catalog version.

    Version numbers are monotonic, so they order correctly as range
    bounds.
    """
    highest = session.scalar(select(func.max(CatalogVersion.version_number))) or 0
    version = CatalogVersion(
        version_number=highest + 1, label=label, description=description
    )
    session.add(version)
    session.flush()
    return CatalogVersionRef(
        id=version.id, version_number=version.version_number, label=version.label
    )


def add_skill(
    session: Session,
    canonical_display_text: str,
    version: CatalogVersionRef,
    *,
    origin: RepresentationOrigin = RepresentationOrigin.CURATION,
    created_from: SkillOrigin = SkillOrigin.CURATION,
) -> Skill:
    """Create a skill and its canonical representation, live from `version`."""
    _require_valid(canonical_display_text)
    skill = Skill(
        created_from=created_from.value, created_in_version=version.version_number
    )
    session.add(skill)
    session.flush()
    add_representation(
        session,
        skill_id=skill.id,
        display_text=canonical_display_text,
        version=version,
        kind=RepresentationKind.CANONICAL,
        origin=origin,
    )
    return skill


def add_representation(
    session: Session,
    *,
    skill_id: uuid.UUID,
    display_text: str,
    version: CatalogVersionRef,
    kind: RepresentationKind = RepresentationKind.ALIAS,
    origin: RepresentationOrigin = RepresentationOrigin.CURATION,
) -> SkillRepresentation:
    """Add a representation live from `version`.

    A collision with another live representation is rejected by the
    database EXCLUDE constraint rather than silently rebound.
    """
    _require_valid(display_text)
    representation = SkillRepresentation(
        skill_id=skill_id,
        normalized_representation=normalize(display_text),
        display_text=display_text,
        kind=kind.value,
        origin=origin.value,
        valid_from_version=version.version_number,
    )
    session.add(representation)
    session.flush()
    return representation


def retire_representation(
    session: Session, *, normalized: str, version: CatalogVersionRef
) -> bool:
    """Close a representation so it is no longer live from `version` on.

    The row is retained: earlier versions must still resolve through it.
    """
    representation = session.scalars(
        select(SkillRepresentation).where(
            SkillRepresentation.normalized_representation == normalized,
            live_at(SkillRepresentation, version.version_number),
        )
    ).first()
    if representation is None:
        return False
    representation.valid_to_version = version.version_number
    session.flush()
    return True


def add_relationship(
    session: Session,
    *,
    from_skill_id: uuid.UUID,
    to_skill_id: uuid.UUID,
    relationship_type: SkillRelationshipType,
    version: CatalogVersionRef,
) -> SkillRelationship:
    relationship = SkillRelationship(
        from_skill_id=from_skill_id,
        to_skill_id=to_skill_id,
        relationship_type=relationship_type.value,
        valid_from_version=version.version_number,
    )
    session.add(relationship)
    session.flush()
    return relationship


def retire_relationship(
    session: Session, *, relationship: SkillRelationship, version: CatalogVersionRef
) -> None:
    relationship.valid_to_version = version.version_number
    session.flush()


def open_conflict(
    session: Session,
    *,
    normalized: str,
    conflict_type: ConflictType,
    candidate_skill_ids: list[uuid.UUID],
    version: CatalogVersionRef,
) -> SkillConflict:
    """Record a contradiction, live from `version`.

    While live, the representation stays unresolved at that version.
    """
    conflict = SkillConflict(
        conflict_type=conflict_type.value,
        normalized_representation=normalized,
        candidate_skill_ids=candidate_skill_ids,
        status=ConflictStatus.OPEN.value,
        valid_from_version=version.version_number,
    )
    session.add(conflict)
    session.flush()
    return conflict


def close_conflict(
    session: Session, *, conflict: SkillConflict, version: CatalogVersionRef
) -> None:
    """Close a conflict from `version` on.

    The open row is CLOSED rather than flipped to `resolved`, so
    re-executing an earlier version still sees it as open.
    """
    conflict.valid_to_version = version.version_number
    session.flush()


def _require_valid(display_text: str) -> None:
    if not is_valid_representation(display_text):
        raise InvalidRepresentationError(
            f"{display_text!r} is an extraction defect, not a catalog entry"
        )
