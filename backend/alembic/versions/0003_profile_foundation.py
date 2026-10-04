"""Profile foundation: live editing surface and immutable versions

Revision ID: 0003_profile_foundation
Revises: 0002_seed_catalog
Create Date: 2026-10-04

Reviewed by hand: yes

Two layers with deliberately different mutability:

  user_profile / profile_skill / matching_criteria
      The live editing surface. Freely mutable.

  profile_version
      The immutable matching snapshot. UPDATE and DELETE are rejected
      by trigger, reusing careerlens_forbid_mutation() from 0001.

Invariants enforced by this migration (DATABASE.md Sections 2.10-2.13):
  - ProfileVersion immutable once created      (trigger)
  - monotonic versions per profile             (uq_profile_version_profile_number)
  - target seniority never defaults to current (two independent nullable
                                                column groups; no default)
  - proficiency limited to the documented three levels, with no numeric
    score                                      (ck_profile_skill_proficiency_valid)
  - weight is exact NUMERIC in [0,1]           (ck_matching_criteria_weight_range)
  - a resolved mention must carry identity, and an unresolved or
    indeterminate one must carry none           (ck_*_resolved_requires_identity
                                                 on profile_skill,
                                                 matching_criteria and both
                                                 user_profile seniority groups)
  - catalog version recorded for reproducibility
                                               (fk to catalog_version.version_number)

Tests live in tests/test_profile_db.py and tests/test_profile_snapshot.py.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_profile_foundation"
down_revision: str | None = "0002_seed_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESOLUTION_STATUSES = "'resolved', 'unresolved', 'indeterminate'"
RESOLUTION_PROVENANCES = (
    "'exact_match', 'normalized', 'alias_rule', "
    "'user_confirmed', 'ai_semantic', 'unresolved'"
)
EVIDENCE_SOURCES = (
    "'structured_field', 'title', 'requirements', 'responsibilities', 'description'"
)
TRACKS = "'IC', 'Management'"


def _identity_coherence(
    status_column: str, *identity_columns: str, allow_unset: bool = False
) -> str:
    """CHECK text tying a 'resolved' status to actually carrying identity.

    Mirrors app.models.catalog.identity_coherence_check. Spelled out here
    rather than imported, because a migration is a frozen artifact: it
    must keep producing the schema it produced on the day it ran, even
    after the application's helper changes.
    """
    present = " AND ".join(f"{column} IS NOT NULL" for column in identity_columns)
    absent = " AND ".join(f"{column} IS NULL" for column in identity_columns)
    coherent = (
        f"({status_column} = 'resolved' AND {present}) "
        f"OR ({status_column} <> 'resolved' AND {absent})"
    )
    if allow_unset:
        return f"({status_column} IS NULL AND {absent}) OR ({coherent})"
    return coherent


def _seniority_columns(prefix: str) -> list[sa.Column]:
    """One seniority reference, as a prefixed column group.

    A field pattern rather than a table, per DATABASE.md Section 2.9.
    """
    return [
        sa.Column(f"{prefix}_seniority_raw_text", sa.String(length=255), nullable=True),
        sa.Column(f"{prefix}_seniority_normalized", sa.String(length=255), nullable=True),
        sa.Column(f"{prefix}_seniority_level_id", sa.Uuid(), nullable=True),
        sa.Column(
            f"{prefix}_seniority_resolution_status", sa.String(length=16), nullable=True
        ),
        sa.Column(
            f"{prefix}_seniority_resolution_provenance", sa.String(length=32), nullable=True
        ),
        # Denormalized by value: historical meaning must survive catalog
        # rename and merge.
        sa.Column(
            f"{prefix}_seniority_resolved_level_name", sa.String(length=255), nullable=True
        ),
        sa.Column(f"{prefix}_seniority_resolved_track", sa.String(length=16), nullable=True),
        sa.Column(
            f"{prefix}_seniority_evidence_source", sa.String(length=32), nullable=True
        ),
        sa.Column(f"{prefix}_seniority_catalog_version", sa.Integer(), nullable=True),
    ]


def _seniority_constraints(prefix: str) -> list[sa.schema.SchemaItem]:
    return [
        sa.ForeignKeyConstraint(
            [f"{prefix}_seniority_level_id"],
            ["seniority_level.id"],
            name=op.f(f"fk_user_profile_{prefix}_seniority_level_id_seniority_level"),
        ),
        sa.CheckConstraint(
            f"{prefix}_seniority_resolution_status IS NULL "
            f"OR {prefix}_seniority_resolution_status IN ({RESOLUTION_STATUSES})",
            name=op.f(f"ck_user_profile_{prefix}_seniority_status_valid"),
        ),
        sa.CheckConstraint(
            f"{prefix}_seniority_resolution_provenance IS NULL "
            f"OR {prefix}_seniority_resolution_provenance IN ({RESOLUTION_PROVENANCES})",
            name=op.f(f"ck_user_profile_{prefix}_seniority_provenance_valid"),
        ),
        sa.CheckConstraint(
            f"{prefix}_seniority_resolved_track IS NULL "
            f"OR {prefix}_seniority_resolved_track IN ({TRACKS})",
            name=op.f(f"ck_user_profile_{prefix}_seniority_track_valid"),
        ),
        sa.CheckConstraint(
            f"{prefix}_seniority_evidence_source IS NULL "
            f"OR {prefix}_seniority_evidence_source IN ({EVIDENCE_SOURCES})",
            name=op.f(f"ck_user_profile_{prefix}_seniority_evidence_source_valid"),
        ),
        # A reference may be wholly unset, but it may not be
        # half-resolved: no resolved status without a pinned level, and
        # no pinned level under an unresolved or indeterminate status.
        sa.CheckConstraint(
            _identity_coherence(
                f"{prefix}_seniority_resolution_status",
                f"{prefix}_seniority_level_id",
                f"{prefix}_seniority_resolved_level_name",
                f"{prefix}_seniority_resolved_track",
                allow_unset=True,
            ),
            name=op.f(f"ck_user_profile_{prefix}_seniority_resolved_requires_identity"),
        ),
    ]


def _mention_columns() -> list[sa.Column]:
    """The skill mention pattern, shared by capability and intent."""
    return [
        sa.Column("raw_text", sa.String(length=255), nullable=False),
        sa.Column("normalized_representation", sa.String(length=255), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=True),
        sa.Column("resolution_status", sa.String(length=16), nullable=False),
        sa.Column("resolution_provenance", sa.String(length=32), nullable=False),
        sa.Column("resolved_canonical_name", sa.String(length=255), nullable=True),
        sa.Column("catalog_version_at_resolution", sa.Integer(), nullable=True),
    ]


def _mention_constraints(table: str) -> list[sa.schema.SchemaItem]:
    return [
        sa.ForeignKeyConstraint(
            ["skill_id"], ["skill.id"], name=op.f(f"fk_{table}_skill_id_skill")
        ),
        sa.CheckConstraint(
            f"resolution_status IN ({RESOLUTION_STATUSES})",
            name=op.f(f"ck_{table}_resolution_status_valid"),
        ),
        sa.CheckConstraint(
            f"resolution_provenance IN ({RESOLUTION_PROVENANCES})",
            name=op.f(f"ck_{table}_resolution_provenance_valid"),
        ),
        sa.CheckConstraint(
            "normalized_representation <> ''",
            name=op.f(f"ck_{table}_normalized_representation_not_empty"),
        ),
        # Shared by capability and intent: both carry their own skill
        # identity, so both need the same guarantee that a 'resolved'
        # status and a real identity never come apart.
        sa.CheckConstraint(
            _identity_coherence(
                "resolution_status", "skill_id", "resolved_canonical_name"
            ),
            name=op.f(f"ck_{table}_resolved_requires_identity"),
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "user_profile",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("experience_years", sa.Integer(), nullable=True),
        *_seniority_columns("current"),
        *_seniority_columns("target"),
        sa.Column("preferred_locations", postgresql.ARRAY(sa.String(length=255)), nullable=True),
        sa.Column("preferred_work_modes", postgresql.ARRAY(sa.String(length=16)), nullable=True),
        # Nullable until v1 is minted lazily. The FK is added after
        # profile_version exists, because the two reference each other.
        sa.Column("current_profile_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_profile")),
        *_seniority_constraints("current"),
        *_seniority_constraints("target"),
        sa.CheckConstraint(
            "experience_years IS NULL OR experience_years >= 0",
            name=op.f("ck_user_profile_experience_years_non_negative"),
        ),
    )

    op.create_table(
        "profile_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_profile_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("catalog_version_at_creation", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profile_version")),
        sa.ForeignKeyConstraint(
            ["user_profile_id"], ["user_profile.id"], ondelete="CASCADE",
            name=op.f("fk_profile_version_user_profile_id_user_profile"),
        ),
        # Pins the catalog this snapshot was resolved against, so a
        # historical re-execution never silently uses the latest.
        sa.ForeignKeyConstraint(
            ["catalog_version_at_creation"], ["catalog_version.version_number"],
            name=op.f("fk_profile_version_catalog_version_at_creation_catalog_version"),
        ),
        sa.UniqueConstraint(
            "user_profile_id", "version_number", name="uq_profile_version_profile_number"
        ),
        sa.CheckConstraint(
            "version_number > 0", name=op.f("ck_profile_version_version_number_positive")
        ),
    )
    op.create_index(
        "ix_profile_version_profile_number",
        "profile_version",
        ["user_profile_id", "version_number"],
    )

    op.create_foreign_key(
        "fk_user_profile_current_version",
        "user_profile",
        "profile_version",
        ["current_profile_version_id"],
        ["id"],
    )

    # A ProfileVersion is immutable once written. Only INSERT is
    # permitted, so new versions remain creatable while history cannot
    # be rewritten. The function is already defined by 0001.
    op.execute(
        """
        CREATE TRIGGER profile_version_immutable
        BEFORE UPDATE OR DELETE ON profile_version
        FOR EACH ROW EXECUTE FUNCTION careerlens_forbid_mutation();
        """
    )

    op.create_table(
        "profile_skill",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_profile_id", sa.Uuid(), nullable=False),
        *_mention_columns(),
        sa.Column("proficiency", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profile_skill")),
        sa.ForeignKeyConstraint(
            ["user_profile_id"], ["user_profile.id"], ondelete="CASCADE",
            name=op.f("fk_profile_skill_user_profile_id_user_profile"),
        ),
        *_mention_constraints("profile_skill"),
        sa.UniqueConstraint(
            "user_profile_id", "normalized_representation",
            name="uq_profile_skill_profile_representation",
        ),
        # Exactly the three documented levels, and no numeric score.
        sa.CheckConstraint(
            "proficiency IN ('Strong', 'Working', 'Learning')",
            name=op.f("ck_profile_skill_proficiency_valid"),
        ),
    )

    op.create_table(
        "matching_criteria",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_profile_id", sa.Uuid(), nullable=False),
        *_mention_columns(),
        sa.Column("weight", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("is_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_matching_criteria")),
        sa.ForeignKeyConstraint(
            ["user_profile_id"], ["user_profile.id"], ondelete="CASCADE",
            name=op.f("fk_matching_criteria_user_profile_id_user_profile"),
        ),
        *_mention_constraints("matching_criteria"),
        sa.UniqueConstraint(
            "user_profile_id", "normalized_representation",
            name="uq_matching_criteria_profile_representation",
        ),
        sa.CheckConstraint(
            "weight >= 0.0 AND weight <= 1.0",
            name=op.f("ck_matching_criteria_weight_range"),
        ),
    )


def downgrade() -> None:
    op.drop_table("matching_criteria")
    op.drop_table("profile_skill")
    op.execute("DROP TRIGGER IF EXISTS profile_version_immutable ON profile_version")
    op.drop_constraint("fk_user_profile_current_version", "user_profile", type_="foreignkey")
    op.drop_index("ix_profile_version_profile_number", table_name="profile_version")
    op.drop_table("profile_version")
    op.drop_table("user_profile")
