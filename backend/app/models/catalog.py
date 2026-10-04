"""Skill catalog models.

Shared reference data: neither user-specific nor job-specific
(DATABASE.md Sections 2.1-2.5).
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import (
    ConflictStatus,
    ConflictType,
    PromotionOutcome,
    RepresentationKind,
    RepresentationOrigin,
    SkillOrigin,
    SkillRelationshipType,
)


def enum_check(column: str, enum: type[StrEnum]) -> str:
    """Render a CHECK expression restricting a column to an enum's values."""
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"


# Shared CHECK for every effective-versioned table.
VALIDITY_RANGE_CHECK = "valid_to_version IS NULL OR valid_to_version > valid_from_version"


class CatalogVersioned:
    """Mixin for catalog rows that are effective-versioned.

    Catalog entries are never mutated or deleted in place. A change
    CLOSES the current row by setting `valid_to_version` and INSERTS a
    successor. A row is live at catalog version V when:

        valid_from_version <= V < COALESCE(valid_to_version, infinity)

    This is what makes a historical match re-executable: resolution
    scoped to the version a MatchResult recorded sees exactly the rows
    that were live then, not today's catalog.

    Uniqueness is enforced per version range by EXCLUDE constraints
    defined in the migration — a plain UNIQUE index cannot express
    "unique among rows live at any given version".
    """

    valid_from_version: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("catalog_version.version_number"),
        nullable=False,
    )
    valid_to_version: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("catalog_version.version_number"),
        nullable=True,
    )


class CatalogVersion(Base):
    """An append-only record of catalog state transitions.

    `catalog_version` is one of the four version axes recorded on every
    MatchResult, so a historical result can state which catalog it was
    computed against.

    Rows here are **immutable once written** (trigger-enforced). That is
    distinct from the catalog entities themselves, which DATABASE.md
    Section 4.1 defines as mutable reference data: it is the *version
    record* that must never be rewritten, not the entries it describes.
    """

    __tablename__ = "catalog_version"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    label: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("version_number > 0", name="version_number_positive"),
    )


class Skill(Base):
    """A canonical skill concept.

    The canonical *name* is not stored here: it lives as the `canonical`
    row in SkillRepresentation, so one unique index governs names and
    aliases together.
    """

    __tablename__ = "skill"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    created_from: Mapped[str] = mapped_column(String(32), nullable=False)
    # Skill is an identity anchor and is not itself versioned: whether a
    # skill exists at a version is determined by whether it has a live
    # canonical representation at that version.
    created_in_version: Mapped[int] = mapped_column(
        Integer, ForeignKey("catalog_version.version_number"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    representations: Mapped[list["SkillRepresentation"]] = relationship(
        back_populates="skill", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(enum_check("created_from", SkillOrigin), name="created_from_valid"),
    )


class SkillRepresentation(CatalogVersioned, Base):
    """Every textual form identifying a Skill — canonical name and aliases.

    Held in ONE table so uniqueness governs canonical names and aliases
    together: that single namespace delivers canonical-name uniqueness,
    alias uniqueness, alias-binds-to-one-skill, promotion idempotency
    and collision rejection simultaneously.

    Because rows are effective-versioned, uniqueness is enforced by an
    EXCLUDE constraint over the validity range rather than a plain
    UNIQUE index — the same invariant, scoped per version. See the
    migration for the DDL.
    """

    __tablename__ = "skill_representation"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skill.id", ondelete="CASCADE"), nullable=False
    )
    normalized_representation: Mapped[str] = mapped_column(String(255), nullable=False)
    display_text: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    skill: Mapped[Skill] = relationship(back_populates="representations")

    __table_args__ = (
        CheckConstraint(enum_check("kind", RepresentationKind), name="kind_valid"),
        CheckConstraint(enum_check("origin", RepresentationOrigin), name="origin_valid"),
        CheckConstraint(
            "normalized_representation <> ''", name="normalized_representation_not_empty"
        ),
        CheckConstraint(VALIDITY_RANGE_CHECK, name="validity_range_valid"),
    )


class SkillRelationship(CatalogVersioned, Base):
    """A typed, directional relationship between canonical skills.

    Never an alias and never equivalence. `family` direction is
    load-bearing: holding EKS evidences Kubernetes substantially, the
    reverse only weakly.

    Effective-versioned, because relationship state affects matching and
    so must be reconstructible at the version a historical result used.
    """

    __tablename__ = "skill_relationship"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    from_skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skill.id", ondelete="CASCADE"), nullable=False
    )
    to_skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skill.id", ondelete="CASCADE"), nullable=False
    )
    relationship_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("from_skill_id <> to_skill_id", name="no_self_relationship"),
        CheckConstraint(
            enum_check("relationship_type", SkillRelationshipType),
            name="relationship_type_valid",
        ),
        CheckConstraint(VALIDITY_RANGE_CHECK, name="validity_range_valid"),
    )


class SkillPromotionEvidence(Base):
    """Evidence that an unresolved representation should enter the catalog.

    Keyed on the representation because evidence accrues *before* any
    Skill exists.
    """

    __tablename__ = "skill_promotion_evidence"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    normalized_representation: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_text_observed: Mapped[str] = mapped_column(String(255), nullable=False)
    # No foreign key yet: JobPosting arrives in Milestone 5, which adds
    # the constraint. The column and the uniqueness rule depending on it
    # are needed now.
    job_posting_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    extraction_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    context_signals: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    user_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    outcome: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PromotionOutcome.PENDING.value
    )

    __table_args__ = (
        # Evidence counts INDEPENDENT postings: re-ingesting the same
        # posting cannot inflate it.
        UniqueConstraint(
            "normalized_representation",
            "job_posting_id",
            name="uq_skill_promotion_evidence_representation_posting",
        ),
        CheckConstraint(enum_check("outcome", PromotionOutcome), name="outcome_valid"),
        CheckConstraint(
            "extraction_confidence IS NULL "
            "OR (extraction_confidence >= 0 AND extraction_confidence <= 1)",
            name="extraction_confidence_range",
        ),
    )


class SkillConflict(CatalogVersioned, Base):
    """A recorded, unresolved catalog contradiction.

    Conflicts fail loudly. While one is open for a representation, that
    representation stays unresolved — declining to guess is correct.

    Effective-versioned, because conflict state changes resolution
    outcomes. A conflict opened in c2 must not make a c1 resolution
    indeterminate retroactively, and a conflict that was open in c1 must
    still block resolution when c1 is re-executed. Closing a conflict
    CLOSES the row at the version that resolved it rather than flipping
    `status` in place.
    """

    __tablename__ = "skill_conflict"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conflict_type: Mapped[str] = mapped_column(String(48), nullable=False)
    normalized_representation: Mapped[str] = mapped_column(String(255), nullable=False)
    candidate_skill_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list
    )
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ConflictStatus.OPEN.value
    )
    resolution_note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(enum_check("conflict_type", ConflictType), name="conflict_type_valid"),
        CheckConstraint(enum_check("status", ConflictStatus), name="status_valid"),
        CheckConstraint(VALIDITY_RANGE_CHECK, name="validity_range_valid"),
        Index(
            "ix_skill_conflict_representation_validity",
            "normalized_representation",
            "valid_from_version",
            "valid_to_version",
        ),
    )
