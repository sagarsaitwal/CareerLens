"""Profile models.

Two layers, deliberately separate (DATABASE.md Sections 2.10-2.13):

  LIVE EDITING SURFACE  UserProfile, ProfileSkill, MatchingCriteria.
                        Mutable. The source of current application
                        state. Matching never reads these directly.

  MATCHING INPUT        ProfileVersion. An immutable JSONB snapshot of
                        matching-relevant state, created when that state
                        changes. This is what a MatchResult records and
                        what a historical match re-executes against.

The snapshot does not replace the normalized tables, and the normalized
tables are not versioned. Historical meaning lives in the snapshot, by
value, so it survives both profile edits and catalog evolution.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.catalog import enum_check, identity_coherence_check, nullable_enum_check

# Imported from models.enums rather than from the domain modules that
# use them: models must not depend on domain, or the two layers form an
# import cycle through this package's __init__.
from app.models.enums import (
    EvidenceSource,
    Proficiency,
    ResolutionProvenance,
    ResolutionStatus,
    SeniorityTrack,
)


class UserProfile(Base):
    """The live editing surface. Freely mutable.

    Seniority is stored as two independent references. `target` is
    optional and must never be defaulted from `current`: an unset target
    means no preference was expressed, which is a different claim from
    "wants to stay at the same level".
    """

    __tablename__ = "user_profile"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    experience_years: Mapped[int | None] = mapped_column(Integer)

    # --- current seniority: a FIT signal -------------------------------
    current_seniority_raw_text: Mapped[str | None] = mapped_column(String(255))
    current_seniority_normalized: Mapped[str | None] = mapped_column(String(255))
    current_seniority_level_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("seniority_level.id")
    )
    current_seniority_resolution_status: Mapped[str | None] = mapped_column(String(16))
    current_seniority_resolution_provenance: Mapped[str | None] = mapped_column(String(32))
    # Denormalized BY VALUE so historical meaning survives catalog change.
    current_seniority_resolved_level_name: Mapped[str | None] = mapped_column(String(255))
    current_seniority_resolved_track: Mapped[str | None] = mapped_column(String(16))
    current_seniority_evidence_source: Mapped[str | None] = mapped_column(String(32))
    current_seniority_catalog_version: Mapped[int | None] = mapped_column(Integer)

    # --- target seniority: a PREFERENCE signal, optional ---------------
    target_seniority_raw_text: Mapped[str | None] = mapped_column(String(255))
    target_seniority_normalized: Mapped[str | None] = mapped_column(String(255))
    target_seniority_level_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("seniority_level.id")
    )
    target_seniority_resolution_status: Mapped[str | None] = mapped_column(String(16))
    target_seniority_resolution_provenance: Mapped[str | None] = mapped_column(String(32))
    target_seniority_resolved_level_name: Mapped[str | None] = mapped_column(String(255))
    target_seniority_resolved_track: Mapped[str | None] = mapped_column(String(16))
    target_seniority_evidence_source: Mapped[str | None] = mapped_column(String(32))
    target_seniority_catalog_version: Mapped[int | None] = mapped_column(Integer)

    preferred_locations: Mapped[list[str] | None] = mapped_column(ARRAY(String(255)))
    preferred_work_modes: Mapped[list[str] | None] = mapped_column(ARRAY(String(16)))

    # Nullable until v1 is minted lazily: a snapshot of nothing has no
    # value, so creating a profile row does not create a version.
    current_profile_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("profile_version.id", use_alter=True, name="fk_user_profile_current_version"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    skills: Mapped[list["ProfileSkill"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    criteria: Mapped[list["MatchingCriteria"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "experience_years IS NULL OR experience_years >= 0",
            name="experience_years_non_negative",
        ),
        CheckConstraint(
            nullable_enum_check("current_seniority_resolution_status", ResolutionStatus),
            name="current_seniority_status_valid",
        ),
        CheckConstraint(
            nullable_enum_check(
                "current_seniority_resolution_provenance", ResolutionProvenance
            ),
            name="current_seniority_provenance_valid",
        ),
        CheckConstraint(
            nullable_enum_check("current_seniority_resolved_track", SeniorityTrack),
            name="current_seniority_track_valid",
        ),
        CheckConstraint(
            nullable_enum_check("current_seniority_evidence_source", EvidenceSource),
            name="current_seniority_evidence_source_valid",
        ),
        CheckConstraint(
            nullable_enum_check("target_seniority_resolution_status", ResolutionStatus),
            name="target_seniority_status_valid",
        ),
        CheckConstraint(
            nullable_enum_check(
                "target_seniority_resolution_provenance", ResolutionProvenance
            ),
            name="target_seniority_provenance_valid",
        ),
        CheckConstraint(
            nullable_enum_check("target_seniority_resolved_track", SeniorityTrack),
            name="target_seniority_track_valid",
        ),
        CheckConstraint(
            nullable_enum_check("target_seniority_evidence_source", EvidenceSource),
            name="target_seniority_evidence_source_valid",
        ),
        # Seniority carries the same resolved/identity coherence as a
        # skill mention. `allow_unset` because an unexpressed reference
        # leaves the whole group NULL — and an unset target in
        # particular is a legitimate state, not a missing resolution.
        CheckConstraint(
            identity_coherence_check(
                "current_seniority_resolution_status",
                "current_seniority_level_id",
                "current_seniority_resolved_level_name",
                "current_seniority_resolved_track",
                allow_unset=True,
            ),
            name="current_seniority_resolved_requires_identity",
        ),
        CheckConstraint(
            identity_coherence_check(
                "target_seniority_resolution_status",
                "target_seniority_level_id",
                "target_seniority_resolved_level_name",
                "target_seniority_resolved_track",
                allow_unset=True,
            ),
            name="target_seniority_resolved_requires_identity",
        ),
    )


class ProfileSkill(Base):
    """Capability inventory — "What can I do?"

    Belongs to the live UserProfile, NOT to a ProfileVersion: this table
    is current application state and is freely editable. Historical
    capability lives denormalized inside each ProfileVersion snapshot.

    Capability is not intent. A ProfileSkill may exist with no
    MatchingCriteria (known but not targeted), and a MatchingCriteria
    may exist with no ProfileSkill (targeted but not yet held).
    """

    __tablename__ = "profile_skill"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_profile.id", ondelete="CASCADE"), nullable=False
    )

    # --- skill mention pattern -----------------------------------------
    raw_text: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_representation: Mapped[str] = mapped_column(String(255), nullable=False)
    skill_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skill.id")
    )
    resolution_status: Mapped[str] = mapped_column(String(16), nullable=False)
    resolution_provenance: Mapped[str] = mapped_column(String(32), nullable=False)
    resolved_canonical_name: Mapped[str | None] = mapped_column(String(255))
    catalog_version_at_resolution: Mapped[int | None] = mapped_column(Integer)

    proficiency: Mapped[str] = mapped_column(String(16), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    profile: Mapped[UserProfile] = relationship(back_populates="skills")

    __table_args__ = (
        # One capability entry per skill representation per profile.
        UniqueConstraint(
            "user_profile_id",
            "normalized_representation",
            name="uq_profile_skill_profile_representation",
        ),
        CheckConstraint(enum_check("proficiency", Proficiency), name="proficiency_valid"),
        CheckConstraint(
            enum_check("resolution_status", ResolutionStatus), name="resolution_status_valid"
        ),
        CheckConstraint(
            enum_check("resolution_provenance", ResolutionProvenance),
            name="resolution_provenance_valid",
        ),
        CheckConstraint(
            "normalized_representation <> ''", name="normalized_representation_not_empty"
        ),
        # A resolved mention must carry the canonical name by value;
        # an unresolved or indeterminate one must not pretend to have an
        # identity, not even a partial one.
        CheckConstraint(
            identity_coherence_check(
                "resolution_status", "skill_id", "resolved_canonical_name"
            ),
            name="resolved_requires_identity",
        ),
    )


class MatchingCriteria(Base):
    """Search intent — "What should matching prioritize?"

    Carries its own skill identity and deliberately does NOT reference
    ProfileSkill: intent may exist without capability.
    """

    __tablename__ = "matching_criteria"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_profile.id", ondelete="CASCADE"), nullable=False
    )

    raw_text: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_representation: Mapped[str] = mapped_column(String(255), nullable=False)
    skill_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skill.id")
    )
    resolution_status: Mapped[str] = mapped_column(String(16), nullable=False)
    resolution_provenance: Mapped[str] = mapped_column(String(32), nullable=False)
    resolved_canonical_name: Mapped[str | None] = mapped_column(String(255))
    catalog_version_at_resolution: Mapped[int | None] = mapped_column(Integer)

    # Continuous normalized scale, stored exactly. Business values are
    # configuration, never hard-coded into the domain engine.
    weight: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    is_required: Mapped[bool] = mapped_column(nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    profile: Mapped[UserProfile] = relationship(back_populates="criteria")

    __table_args__ = (
        UniqueConstraint(
            "user_profile_id",
            "normalized_representation",
            name="uq_matching_criteria_profile_representation",
        ),
        CheckConstraint("weight >= 0.0 AND weight <= 1.0", name="weight_range"),
        CheckConstraint(
            enum_check("resolution_status", ResolutionStatus), name="resolution_status_valid"
        ),
        CheckConstraint(
            enum_check("resolution_provenance", ResolutionProvenance),
            name="resolution_provenance_valid",
        ),
        CheckConstraint(
            "normalized_representation <> ''", name="normalized_representation_not_empty"
        ),
        # Intent carries its own identity, so it needs the same coherence
        # guarantee capability has. A criterion claiming to be resolved
        # without a skill_id would silently become an unresolvable
        # requirement that matching could never satisfy.
        CheckConstraint(
            identity_coherence_check(
                "resolution_status", "skill_id", "resolved_canonical_name"
            ),
            name="resolved_requires_identity",
        ),
    )


class ProfileVersion(Base):
    """The immutable matching snapshot.

    Immutable once created — never updated, corrected or back-filled,
    enforced by a database trigger rejecting UPDATE and DELETE. A change
    to matching-relevant state creates a NEW version.

    The snapshot is self-contained: every value a deterministic match
    needs is denormalized into it, so a historical version stays
    interpretable after both the live profile and the catalog move on.
    """

    __tablename__ = "profile_version"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_profile.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Structure version of the snapshot document itself, so an older
    # shape stays readable under today's code.
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    # The catalog this profile's skills were resolved against. A
    # historical re-execution pins THIS, never the latest catalog.
    catalog_version_at_creation: Mapped[int] = mapped_column(
        Integer, ForeignKey("catalog_version.version_number"), nullable=False
    )
    snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "user_profile_id", "version_number", name="uq_profile_version_profile_number"
        ),
        CheckConstraint("version_number > 0", name="version_number_positive"),
        Index("ix_profile_version_profile_number", "user_profile_id", "version_number"),
    )
