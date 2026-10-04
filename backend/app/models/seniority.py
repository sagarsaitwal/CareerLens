"""Seniority catalog models.

Seniority is a **partial order**: levels are ordered within a track and
unordered across tracks (DATABASE.md Sections 2.6-2.8, 3.6).

Every table here is effective-versioned for the same reason as the skill
catalog: level membership, rank, representations and cross-track
relationships all affect resolution, so each must be reconstructible at
the catalog version a historical match recorded.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.catalog import VALIDITY_RANGE_CHECK, CatalogVersioned, enum_check
from app.models.enums import (
    RepresentationKind,
    RepresentationOrigin,
    SeniorityRelationshipType,
    SeniorityTrack,
)


class SeniorityLevel(CatalogVersioned, Base):
    """A canonical seniority level within a track.

    `rank_within_track` is meaningful ONLY between levels sharing a
    track. There is deliberately no global rank: a Principal considering
    a Director role is a track change, not a promotion of +1.

    Versioned because a ladder can be revised. Rank uniqueness within a
    track is therefore enforced per version range by an EXCLUDE
    constraint rather than a plain UNIQUE — see the migration.
    """

    __tablename__ = "seniority_level"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    track: Mapped[str] = mapped_column(String(16), nullable=False)
    rank_within_track: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    representations: Mapped[list["SeniorityRepresentation"]] = relationship(
        back_populates="level", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(enum_check("track", SeniorityTrack), name="track_valid"),
        CheckConstraint("rank_within_track > 0", name="rank_positive"),
        CheckConstraint(VALIDITY_RANGE_CHECK, name="validity_range_valid"),
    )


class SeniorityRepresentation(CatalogVersioned, Base):
    """Textual forms identifying a seniority level.

    Same structural pattern as SkillRepresentation, including the single
    uniqueness namespace, enforced per version range.

    Deliberately ABSENT from this catalog, because no documented rule
    establishes their meaning: Engineer, Software Engineer, Developer,
    Lead, Architect, Associate, Head. Those resolve to unknown or
    indeterminate, never to a guessed level.
    """

    __tablename__ = "seniority_representation"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    seniority_level_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("seniority_level.id", ondelete="CASCADE"), nullable=False
    )
    normalized_representation: Mapped[str] = mapped_column(String(255), nullable=False)
    display_text: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    level: Mapped[SeniorityLevel] = relationship(back_populates="representations")

    __table_args__ = (
        CheckConstraint(enum_check("kind", RepresentationKind), name="kind_valid"),
        CheckConstraint(enum_check("origin", RepresentationOrigin), name="origin_valid"),
        CheckConstraint(
            "normalized_representation <> ''", name="normalized_representation_not_empty"
        ),
        CheckConstraint(VALIDITY_RANGE_CHECK, name="validity_range_valid"),
        Index(
            "ix_seniority_representation_normalized_validity",
            "normalized_representation",
            "valid_from_version",
            "valid_to_version",
        ),
    )


class SeniorityRelationship(CatalogVersioned, Base):
    """Optional cross-track relationships.

    Seeded EMPTY by decision: populating it would assume a specific
    company's ladder. With no rows live at a version, cross-track
    comparison at that version returns `different_track`, which is
    honest. Rows can be added later without a schema change.
    """

    __tablename__ = "seniority_relationship"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    from_level_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("seniority_level.id", ondelete="CASCADE"), nullable=False
    )
    to_level_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("seniority_level.id", ondelete="CASCADE"), nullable=False
    )
    relationship_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("from_level_id <> to_level_id", name="no_self_relationship"),
        CheckConstraint(
            enum_check("relationship_type", SeniorityRelationshipType),
            name="relationship_type_valid",
        ),
        CheckConstraint(VALIDITY_RANGE_CHECK, name="validity_range_valid"),
    )
