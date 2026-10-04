"""Catalog foundation: effective-versioned skill and seniority catalogs

Revision ID: 0001_catalog_foundation
Revises:
Create Date: 2026-10-04

Reviewed by hand: yes

Catalog rows are EFFECTIVE-VERSIONED: a change closes the current row by
setting `valid_to_version` and inserts a successor. Nothing is mutated or
deleted in place. A row is live at catalog version V when

    valid_from_version <= V < COALESCE(valid_to_version, infinity)

This is what makes a historical match re-executable (AI-MATCHING.md
invariant 6): resolution scoped to the version a MatchResult recorded
sees exactly the rows that were live then.

Uniqueness therefore cannot be a plain UNIQUE index, which would forbid
a representation from having both a closed historical row and a live
successor. It is expressed as an EXCLUDE constraint over the validity
range: no two rows may share the key while their version ranges overlap.
That is the same invariant, correctly scoped.

Invariants enforced by this migration (DATABASE.md Section 6):
  - 6:  normalized_representation unique per representation namespace,
        per version  (ex_*_representation_normalized_version)
  - 7:  a representation binds to at most one catalog entry
        (same EXCLUDE plus the NOT NULL FK)
  - 8:  identity / family / adjacent remain distinct
        (ck_skill_relationship_relationship_type_valid)
  - 17: promotion evidence counts independent postings
        (uq_skill_promotion_evidence_representation_posting)
  - 19: append-only catalog_version (trigger below)
  - 22: seniority ranks comparable only within a track
        (ex_seniority_level_track_rank_version; no global rank column)
  - 24: the seniority catalog shares catalog_version — no fifth axis

Tests proving each constraint rejects a violating row live in
tests/test_catalog_db.py and tests/test_catalog_versioning.py.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_catalog_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VALIDITY_RANGE_CHECK = "valid_to_version IS NULL OR valid_to_version > valid_from_version"


def _validity_columns() -> list[sa.Column]:
    return [
        sa.Column("valid_from_version", sa.Integer(), nullable=False),
        sa.Column("valid_to_version", sa.Integer(), nullable=True),
    ]


def _validity_constraints(table: str) -> list[sa.schema.SchemaItem]:
    return [
        sa.ForeignKeyConstraint(
            ["valid_from_version"],
            ["catalog_version.version_number"],
            name=op.f(f"fk_{table}_valid_from_version_catalog_version"),
        ),
        sa.ForeignKeyConstraint(
            ["valid_to_version"],
            ["catalog_version.version_number"],
            name=op.f(f"fk_{table}_valid_to_version_catalog_version"),
        ),
        sa.CheckConstraint(VALIDITY_RANGE_CHECK, name=op.f(f"ck_{table}_validity_range_valid")),
    ]


def _add_version_exclude(
    table: str, key_column: str, constraint: str, where: str | None = None
) -> None:
    """Forbid two rows sharing `key_column` while their versions overlap."""
    predicate = f" WHERE ({where})" if where else ""
    op.execute(
        f"""
        ALTER TABLE {table}
        ADD CONSTRAINT {constraint}
        EXCLUDE USING gist (
            {key_column} WITH =,
            int4range(valid_from_version, valid_to_version) WITH &&
        ){predicate};
        """
    )


def upgrade() -> None:
    # Required so gist indexes can use equality on scalar types
    # (text, uuid) alongside the range overlap operator.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "catalog_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_version")),
        sa.UniqueConstraint("version_number", name=op.f("uq_catalog_version_version_number")),
        sa.UniqueConstraint("label", name=op.f("uq_catalog_version_label")),
        sa.CheckConstraint(
            "version_number > 0", name=op.f("ck_catalog_version_version_number_positive")
        ),
    )

    # A catalog version record is immutable once written. Catalog
    # ENTRIES evolve by closing and superseding rows; the version record
    # describing them must never be rewritten, or a historical
    # MatchResult loses the meaning of its recorded catalog_version.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION careerlens_forbid_mutation()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'append-only table %: % is not permitted',
                TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER catalog_version_append_only
        BEFORE UPDATE OR DELETE ON catalog_version
        FOR EACH ROW EXECUTE FUNCTION careerlens_forbid_mutation();
        """
    )

    op.create_table(
        "skill",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_from", sa.String(length=32), nullable=False),
        # Skill is an identity anchor and is not itself versioned:
        # whether a skill exists at a version is determined by whether
        # it has a live canonical representation at that version.
        sa.Column("created_in_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill")),
        sa.ForeignKeyConstraint(
            ["created_in_version"],
            ["catalog_version.version_number"],
            name=op.f("fk_skill_created_in_version_catalog_version"),
        ),
        sa.CheckConstraint(
            "created_from IN ('curation', 'promotion')",
            name=op.f("ck_skill_created_from_valid"),
        ),
    )

    op.create_table(
        "skill_representation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("normalized_representation", sa.String(length=255), nullable=False),
        sa.Column("display_text", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        *_validity_columns(),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_representation")),
        sa.ForeignKeyConstraint(
            ["skill_id"], ["skill.id"], ondelete="CASCADE",
            name=op.f("fk_skill_representation_skill_id_skill"),
        ),
        *_validity_constraints("skill_representation"),
        sa.CheckConstraint(
            "kind IN ('canonical', 'alias')", name=op.f("ck_skill_representation_kind_valid")
        ),
        sa.CheckConstraint(
            "origin IN ('curation', 'promotion', 'user_confirmation')",
            name=op.f("ck_skill_representation_origin_valid"),
        ),
        sa.CheckConstraint(
            "normalized_representation <> ''",
            name=op.f("ck_skill_representation_normalized_representation_not_empty"),
        ),
    )
    # THE identity invariant, scoped per version: canonical-name
    # uniqueness, alias uniqueness, alias-binds-to-one-skill, promotion
    # idempotency and collision rejection, all from one constraint.
    _add_version_exclude(
        "skill_representation",
        "normalized_representation",
        "ex_skill_representation_normalized_version",
    )
    _add_version_exclude(
        "skill_representation",
        "skill_id",
        "ex_skill_representation_one_canonical_per_skill",
        where="kind = 'canonical'",
    )
    op.create_index(
        "ix_skill_representation_normalized_validity",
        "skill_representation",
        ["normalized_representation", "valid_from_version", "valid_to_version"],
    )

    op.create_table(
        "skill_relationship",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("from_skill_id", sa.Uuid(), nullable=False),
        sa.Column("to_skill_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_type", sa.String(length=16), nullable=False),
        *_validity_columns(),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_relationship")),
        sa.ForeignKeyConstraint(
            ["from_skill_id"], ["skill.id"], ondelete="CASCADE",
            name=op.f("fk_skill_relationship_from_skill_id_skill"),
        ),
        sa.ForeignKeyConstraint(
            ["to_skill_id"], ["skill.id"], ondelete="CASCADE",
            name=op.f("fk_skill_relationship_to_skill_id_skill"),
        ),
        *_validity_constraints("skill_relationship"),
        sa.CheckConstraint(
            "from_skill_id <> to_skill_id",
            name=op.f("ck_skill_relationship_no_self_relationship"),
        ),
        sa.CheckConstraint(
            "relationship_type IN ('family', 'adjacent')",
            name=op.f("ck_skill_relationship_relationship_type_valid"),
        ),
    )
    op.create_index(
        "ix_skill_relationship_validity",
        "skill_relationship",
        ["from_skill_id", "valid_from_version", "valid_to_version"],
    )

    op.create_table(
        "skill_promotion_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("normalized_representation", sa.String(length=255), nullable=False),
        sa.Column("raw_text_observed", sa.String(length=255), nullable=False),
        # No FK yet: JobPosting arrives in Milestone 5, which adds it.
        sa.Column("job_posting_id", sa.Uuid(), nullable=False),
        sa.Column(
            "observed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("extraction_confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("context_signals", postgresql.JSONB(), nullable=True),
        sa.Column("user_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("outcome", sa.String(length=32), nullable=False, server_default="pending"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_promotion_evidence")),
        # Re-ingesting the same posting cannot inflate evidence.
        sa.UniqueConstraint(
            "normalized_representation", "job_posting_id",
            name="uq_skill_promotion_evidence_representation_posting",
        ),
        sa.CheckConstraint(
            "outcome IN ('pending', 'promoted_as_skill', 'promoted_as_alias', 'rejected')",
            name=op.f("ck_skill_promotion_evidence_outcome_valid"),
        ),
        sa.CheckConstraint(
            "extraction_confidence IS NULL "
            "OR (extraction_confidence >= 0 AND extraction_confidence <= 1)",
            name=op.f("ck_skill_promotion_evidence_extraction_confidence_range"),
        ),
    )

    op.create_table(
        "skill_conflict",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conflict_type", sa.String(length=48), nullable=False),
        sa.Column("normalized_representation", sa.String(length=255), nullable=False),
        sa.Column(
            "candidate_skill_ids",
            postgresql.ARRAY(sa.Uuid()),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column(
            "detected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        *_validity_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_conflict")),
        *_validity_constraints("skill_conflict"),
        sa.CheckConstraint(
            "conflict_type IN ('representation_collision', "
            "'contradictory_resolution_candidates', 'merge_precondition_failure')",
            name=op.f("ck_skill_conflict_conflict_type_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')", name=op.f("ck_skill_conflict_status_valid")
        ),
    )
    op.create_index(
        "ix_skill_conflict_representation_validity",
        "skill_conflict",
        ["normalized_representation", "valid_from_version", "valid_to_version"],
    )

    op.create_table(
        "seniority_level",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("track", sa.String(length=16), nullable=False),
        sa.Column("rank_within_track", sa.Integer(), nullable=False),
        *_validity_columns(),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seniority_level")),
        *_validity_constraints("seniority_level"),
        sa.CheckConstraint(
            "track IN ('IC', 'Management')", name=op.f("ck_seniority_level_track_valid")
        ),
        sa.CheckConstraint(
            "rank_within_track > 0", name=op.f("ck_seniority_level_rank_positive")
        ),
    )
    # Rank is unique within a track at any given version. There is no
    # global rank column, which is what keeps seniority a partial order.
    op.execute(
        """
        ALTER TABLE seniority_level
        ADD CONSTRAINT ex_seniority_level_track_rank_version
        EXCLUDE USING gist (
            track WITH =,
            rank_within_track WITH =,
            int4range(valid_from_version, valid_to_version) WITH &&
        );
        """
    )

    op.create_table(
        "seniority_representation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("seniority_level_id", sa.Uuid(), nullable=False),
        sa.Column("normalized_representation", sa.String(length=255), nullable=False),
        sa.Column("display_text", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        *_validity_columns(),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seniority_representation")),
        sa.ForeignKeyConstraint(
            ["seniority_level_id"], ["seniority_level.id"], ondelete="CASCADE",
            name=op.f("fk_seniority_representation_seniority_level_id_seniority_level"),
        ),
        *_validity_constraints("seniority_representation"),
        sa.CheckConstraint(
            "kind IN ('canonical', 'alias')",
            name=op.f("ck_seniority_representation_kind_valid"),
        ),
        sa.CheckConstraint(
            "origin IN ('curation', 'promotion', 'user_confirmation')",
            name=op.f("ck_seniority_representation_origin_valid"),
        ),
        sa.CheckConstraint(
            "normalized_representation <> ''",
            name=op.f("ck_seniority_representation_normalized_representation_not_empty"),
        ),
    )
    _add_version_exclude(
        "seniority_representation",
        "normalized_representation",
        "ex_seniority_representation_normalized_version",
    )
    _add_version_exclude(
        "seniority_representation",
        "seniority_level_id",
        "ex_seniority_representation_one_canonical_per_level",
        where="kind = 'canonical'",
    )
    op.create_index(
        "ix_seniority_representation_normalized_validity",
        "seniority_representation",
        ["normalized_representation", "valid_from_version", "valid_to_version"],
    )

    op.create_table(
        "seniority_relationship",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("from_level_id", sa.Uuid(), nullable=False),
        sa.Column("to_level_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_type", sa.String(length=32), nullable=False),
        *_validity_columns(),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seniority_relationship")),
        sa.ForeignKeyConstraint(
            ["from_level_id"], ["seniority_level.id"], ondelete="CASCADE",
            name=op.f("fk_seniority_relationship_from_level_id_seniority_level"),
        ),
        sa.ForeignKeyConstraint(
            ["to_level_id"], ["seniority_level.id"], ondelete="CASCADE",
            name=op.f("fk_seniority_relationship_to_level_id_seniority_level"),
        ),
        *_validity_constraints("seniority_relationship"),
        sa.CheckConstraint(
            "from_level_id <> to_level_id",
            name=op.f("ck_seniority_relationship_no_self_relationship"),
        ),
        sa.CheckConstraint(
            "relationship_type IN ('cross_track_band')",
            name=op.f("ck_seniority_relationship_relationship_type_valid"),
        ),
    )
    op.create_index(
        "ix_seniority_relationship_validity",
        "seniority_relationship",
        ["from_level_id", "valid_from_version", "valid_to_version"],
    )


def downgrade() -> None:
    op.drop_table("seniority_relationship")
    op.drop_table("seniority_representation")
    op.drop_table("seniority_level")
    op.drop_table("skill_conflict")
    op.drop_table("skill_promotion_evidence")
    op.drop_table("skill_relationship")
    op.drop_table("skill_representation")
    op.drop_table("skill")
    op.execute("DROP TRIGGER IF EXISTS catalog_version_append_only ON catalog_version")
    op.drop_table("catalog_version")
    op.execute("DROP FUNCTION IF EXISTS careerlens_forbid_mutation()")
