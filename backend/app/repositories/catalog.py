"""Version-scoped catalog lookups.

Every lookup here is pinned to ONE catalog version. There is no
unscoped query and no default version, because the failure this guards
against is silent: an unscoped lookup returns plausible rows from
today's catalog while claiming to reproduce a historical result.

A row is live at catalog version V when

    valid_from_version <= V < COALESCE(valid_to_version, infinity)

Callers obtain lookups through `CatalogSnapshot`, which binds every
catalog concern — skill representations, conflicts, relationships,
seniority levels and representations — to a single version at once, so
a caller cannot accidentally mix versions across them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.orm import Session

from app.domain.resolution import RepresentationRecord
from app.domain.seniority import SeniorityLevelRecord, SeniorityRepresentationRecord
from app.models.catalog import (
    CatalogVersion,
    CatalogVersioned,
    SkillConflict,
    SkillRelationship,
    SkillRepresentation,
)
from app.models.enums import (
    ConflictStatus,
    RepresentationKind,
    RepresentationOrigin,
    SeniorityTrack,
    SkillRelationshipType,
)
from app.models.seniority import (
    SeniorityLevel,
    SeniorityRelationship,
    SeniorityRepresentation,
)


class UnknownCatalogVersionError(LookupError):
    """Raised when a requested catalog version does not exist.

    Deliberately an error rather than a fallback: silently resolving
    against the latest catalog would fabricate a historical result.
    """


class EmptyCatalogError(LookupError):
    """Raised when no catalog version exists at all."""


def live_at[M: CatalogVersioned](model: type[M], version: int) -> ColumnElement[bool]:
    """Predicate selecting rows live at `version`."""
    return and_(
        model.valid_from_version <= version,
        or_(model.valid_to_version.is_(None), model.valid_to_version > version),
    )


@dataclass(frozen=True, slots=True)
class CatalogVersionRef:
    """An identified catalog version, safe to carry around."""

    id: uuid.UUID
    version_number: int
    label: str


class SkillRepresentationRepository:
    """Implements the domain's RepresentationLookup protocol, version-scoped."""

    def __init__(self, session: Session, version: int) -> None:
        self._session = session
        self._version = version

    @property
    def catalog_version_number(self) -> int:
        return self._version

    def find_by_normalized(self, normalized: str) -> RepresentationRecord | None:
        representation = self._session.scalars(
            select(SkillRepresentation).where(
                SkillRepresentation.normalized_representation == normalized,
                live_at(SkillRepresentation, self._version),
            )
        ).first()
        if representation is None:
            return None

        return RepresentationRecord(
            entry_id=representation.skill_id,
            normalized_representation=representation.normalized_representation,
            display_text=representation.display_text,
            kind=RepresentationKind(representation.kind),
            origin=RepresentationOrigin(representation.origin),
            canonical_name=self._canonical_name_for(representation),
        )

    def has_open_conflict(self, normalized: str) -> bool:
        """Whether a conflict was open for this representation AT this version.

        Conflict rows are themselves versioned, so a conflict opened
        later cannot retroactively make an earlier resolution
        indeterminate, and one that was open then still blocks on
        re-execution.
        """
        return (
            self._session.scalars(
                select(SkillConflict.id).where(
                    SkillConflict.normalized_representation == normalized,
                    SkillConflict.status == ConflictStatus.OPEN.value,
                    live_at(SkillConflict, self._version),
                )
            ).first()
            is not None
        )

    def _canonical_name_for(self, representation: SkillRepresentation) -> str:
        """The canonical display name for this representation's skill.

        An alias must report the canonical name, not its own text, so
        historical records denormalize the right value.
        """
        if representation.kind == RepresentationKind.CANONICAL.value:
            return representation.display_text

        canonical = self._session.scalars(
            select(SkillRepresentation).where(
                SkillRepresentation.skill_id == representation.skill_id,
                SkillRepresentation.kind == RepresentationKind.CANONICAL.value,
                live_at(SkillRepresentation, self._version),
            )
        ).first()
        if canonical is None:
            raise LookupError(
                f"skill {representation.skill_id} has no canonical representation "
                f"live at catalog version {self._version}"
            )
        return canonical.display_text


class SkillRelationshipRepository:
    """Relationship state as it stood at one catalog version."""

    def __init__(self, session: Session, version: int) -> None:
        self._session = session
        self._version = version

    def relationships_from(
        self, skill_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, SkillRelationshipType]]:
        rows = self._session.execute(
            select(SkillRelationship.to_skill_id, SkillRelationship.relationship_type).where(
                SkillRelationship.from_skill_id == skill_id,
                live_at(SkillRelationship, self._version),
            )
        ).all()
        return [(row[0], SkillRelationshipType(row[1])) for row in rows]

    def has_relationship(
        self,
        from_skill_id: uuid.UUID,
        to_skill_id: uuid.UUID,
        relationship_type: SkillRelationshipType,
    ) -> bool:
        return (
            self._session.scalars(
                select(SkillRelationship.id).where(
                    SkillRelationship.from_skill_id == from_skill_id,
                    SkillRelationship.to_skill_id == to_skill_id,
                    SkillRelationship.relationship_type == relationship_type.value,
                    live_at(SkillRelationship, self._version),
                )
            ).first()
            is not None
        )


class SeniorityRepresentationRepository:
    """Implements the domain's SeniorityLookup protocol, version-scoped."""

    def __init__(self, session: Session, version: int) -> None:
        self._session = session
        self._version = version

    def find_by_normalized(self, normalized: str) -> SeniorityRepresentationRecord | None:
        representation = self._session.scalars(
            select(SeniorityRepresentation).where(
                SeniorityRepresentation.normalized_representation == normalized,
                live_at(SeniorityRepresentation, self._version),
            )
        ).first()
        if representation is None:
            return None

        # The level itself is versioned too: a representation live at V
        # whose level is not is a data-integrity failure, not a miss.
        level = self._session.scalars(
            select(SeniorityLevel).where(
                SeniorityLevel.id == representation.seniority_level_id,
                live_at(SeniorityLevel, self._version),
            )
        ).first()
        if level is None:
            raise LookupError(
                f"seniority level {representation.seniority_level_id} is not live "
                f"at catalog version {self._version}"
            )

        return SeniorityRepresentationRecord(
            normalized_representation=representation.normalized_representation,
            display_text=representation.display_text,
            kind=RepresentationKind(representation.kind),
            origin=RepresentationOrigin(representation.origin),
            level=SeniorityLevelRecord(
                level_id=level.id,
                canonical_name=self._canonical_name_for(representation),
                track=SeniorityTrack(level.track),
                rank_within_track=level.rank_within_track,
            ),
        )

    def _canonical_name_for(self, representation: SeniorityRepresentation) -> str:
        if representation.kind == RepresentationKind.CANONICAL.value:
            return representation.display_text

        canonical = self._session.scalars(
            select(SeniorityRepresentation).where(
                SeniorityRepresentation.seniority_level_id
                == representation.seniority_level_id,
                SeniorityRepresentation.kind == RepresentationKind.CANONICAL.value,
                live_at(SeniorityRepresentation, self._version),
            )
        ).first()
        if canonical is None:
            raise LookupError(
                f"seniority level {representation.seniority_level_id} has no canonical "
                f"representation live at catalog version {self._version}"
            )
        return canonical.display_text


class SeniorityRelationshipRepository:
    """Cross-track relationship state at one catalog version.

    Seeded empty by decision, so with no live rows cross-track
    comparison returns `different_track`.
    """

    def __init__(self, session: Session, version: int) -> None:
        self._session = session
        self._version = version

    def cross_track_bands(self) -> list[tuple[uuid.UUID, uuid.UUID]]:
        rows = self._session.execute(
            select(SeniorityRelationship.from_level_id, SeniorityRelationship.to_level_id).where(
                live_at(SeniorityRelationship, self._version)
            )
        ).all()
        return [(row[0], row[1]) for row in rows]


class CatalogSnapshot:
    """Every catalog lookup, pinned to one version.

    Construct through `current()` or `at()` rather than directly, so a
    version is always chosen deliberately.
    """

    def __init__(self, session: Session, version: CatalogVersionRef) -> None:
        self.version = version
        self.skills = SkillRepresentationRepository(session, version.version_number)
        self.skill_relationships = SkillRelationshipRepository(
            session, version.version_number
        )
        self.seniority = SeniorityRepresentationRepository(session, version.version_number)
        self.seniority_relationships = SeniorityRelationshipRepository(
            session, version.version_number
        )

    @classmethod
    def current(cls, session: Session) -> CatalogSnapshot:
        """Pin the newest catalog version. Use when starting a NEW match run."""
        row = session.scalars(
            select(CatalogVersion).order_by(CatalogVersion.version_number.desc()).limit(1)
        ).first()
        if row is None:
            raise EmptyCatalogError("no catalog version exists; run the seed migration")
        return cls(session, _ref(row))

    @classmethod
    def at(cls, session: Session, version_number: int) -> CatalogSnapshot:
        """Pin a specific version. Use when RE-EXECUTING a historical match.

        Raises rather than falling back, so a historical re-run can
        never silently use today's catalog.
        """
        row = session.scalars(
            select(CatalogVersion).where(CatalogVersion.version_number == version_number)
        ).first()
        if row is None:
            raise UnknownCatalogVersionError(
                f"catalog version {version_number} does not exist; "
                "refusing to fall back to the current catalog"
            )
        return cls(session, _ref(row))


def _ref(row: CatalogVersion) -> CatalogVersionRef:
    return CatalogVersionRef(
        id=row.id, version_number=row.version_number, label=row.label
    )
