"""Job foundation: sources, canonical postings, cross-source identity

Revision ID: 0004_job_foundation
Revises: 0003_profile_foundation
Create Date: 2026-10-04

Reviewed by hand: yes

Deliberately NO job_posting_version table. Unlike the profile side,
`job_posting` is a live, mutable, purgeable record (DATABASE.md Sections
4.1 and 4.5); historical interpretability comes later from
`MatchResultJobDescriptor` captured at match time.

Source identity is NOT on `job_posting`. One canonical job may be found
in several places, so `job_source_id`, `source_job_id` and `source_url`
live on `job_posting_source`.

`job_posting.first_seen_at` / `last_seen_at` are deliberately ABSENT as
columns: DATABASE.md Section 2.15 defines them as derived from the
associated `job_posting_source` rows. Materializing them here would
create values nothing maintains.

Invariants enforced by this migration (DATABASE.md Sections 2.15-2.15.2,
4.3, 4.4, 6):
  - 27: one canonical job per dedupe_key
                                  (uq_job_posting_dedupe_key)
  - 28: cross-source observations resolve to one canonical posting
                                  (uq_job_posting_source_identity,
                                   uq_job_posting_source_pair)
  - 29: every mention records the catalog version it resolved against
                                  (job_skill_mention.catalog_version_at_resolution
                                   NOT NULL; that one operation records ONE
                                   version is the ingestion service's job)
  - duplicate mentions collapse within a collection
                                  (uq_job_skill_mention_posting_type_representation)
  - a resolved mention carries identity; an unresolved or indeterminate
    one carries none
                                  (ck_*_resolved_requires_identity)
  - a URL is never called verified unless one exists
                                  (ck_job_posting_verified_requires_url)
  - controlled vocabularies for job source type, posting status,
    extraction status, employment type, work mode, mention type and the
    seniority reference

Also completes the foreign key that 0001 deferred,
`skill_promotion_evidence.job_posting_id -> job_posting.id`, with
ON DELETE RESTRICT: promotion evidence counts independent postings, so
a cascade would let a posting deletion silently lower an evidence count.

Tests live in tests/test_job_db.py.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_job_foundation"
down_revision: str | None = "0003_profile_foundation"
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
JOB_SOURCE_TYPES = "'job_board', 'career_page', 'api'"
WORK_MODES = "'remote', 'hybrid', 'onsite'"
EMPLOYMENT_TYPES = (
    "'full_time', 'part_time', 'contract', 'temporary', 'internship', 'unknown'"
)
POSTING_STATUSES = "'active', 'closed', 'unknown'"
EXTRACTION_STATUSES = "'complete', 'partial'"
VERIFICATION_STATUSES = "'verified', 'unverified', 'not_available'"
MENTION_TYPES = "'required', 'preferred', 'mentioned', 'responsibility'"


def _identity_coherence(
    status_column: str, *identity_columns: str, allow_unset: bool = False
) -> str:
    """CHECK text tying a 'resolved' status to actually carrying identity.

    Mirrors app.models.catalog.identity_coherence_check, spelled out here
    for the same reason 0003 does: a migration is a frozen artifact and
    must keep producing the schema it produced on the day it ran, even
    after the application helper changes.
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


def upgrade() -> None:
    op.create_table(
        "job_source",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_crawled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_source")),
        sa.CheckConstraint(
            f"type IN ({JOB_SOURCE_TYPES})", name=op.f("ck_job_source_type_valid")
        ),
        sa.CheckConstraint("name <> ''", name=op.f("ck_job_source_name_not_empty")),
    )

    op.create_table(
        "job_posting",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("company", sa.String(length=512), nullable=False),
        # Untrusted external input (AI-MATCHING.md Section 10).
        sa.Column("description_raw", sa.Text(), nullable=True),
        sa.Column("official_url", sa.String(length=2048), nullable=True),
        sa.Column("direct_apply_url", sa.String(length=2048), nullable=True),
        sa.Column(
            "direct_apply_verification_status",
            sa.String(length=32),
            nullable=False,
            server_default="not_available",
        ),
        sa.Column("location", sa.String(length=512), nullable=True),
        sa.Column("work_mode", sa.String(length=16), nullable=True),
        sa.Column(
            "employment_type", sa.String(length=16), nullable=False, server_default="unknown"
        ),
        # The source's wording, stored verbatim and never parsed here.
        sa.Column("experience_requirements", sa.Text(), nullable=True),
        sa.Column("salary_min", sa.Integer(), nullable=True),
        sa.Column("salary_max", sa.Integer(), nullable=True),
        sa.Column("salary_currency", sa.String(length=3), nullable=True),
        # Seniority reference (DATABASE.md Section 2.9).
        sa.Column("seniority_raw_text", sa.String(length=512), nullable=True),
        sa.Column("seniority_normalized", sa.String(length=255), nullable=True),
        sa.Column("seniority_level_id", sa.Uuid(), nullable=True),
        sa.Column("seniority_resolution_status", sa.String(length=16), nullable=True),
        sa.Column("seniority_resolution_provenance", sa.String(length=32), nullable=True),
        sa.Column("seniority_resolved_level_name", sa.String(length=255), nullable=True),
        sa.Column("seniority_resolved_track", sa.String(length=16), nullable=True),
        sa.Column("seniority_evidence_source", sa.String(length=32), nullable=True),
        sa.Column("seniority_catalog_version", sa.Integer(), nullable=True),
        sa.Column("posting_date", sa.Date(), nullable=True),
        sa.Column("last_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("fingerprint", sa.String(length=255), nullable=False),
        sa.Column(
            "extraction_status", sa.String(length=16), nullable=False, server_default="partial"
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_posting")),
        sa.ForeignKeyConstraint(
            ["seniority_level_id"], ["seniority_level.id"],
            name=op.f("fk_job_posting_seniority_level_id_seniority_level"),
        ),
        sa.UniqueConstraint("dedupe_key", name="uq_job_posting_dedupe_key"),
        sa.CheckConstraint("title <> ''", name=op.f("ck_job_posting_title_not_empty")),
        sa.CheckConstraint("company <> ''", name=op.f("ck_job_posting_company_not_empty")),
        sa.CheckConstraint(
            "dedupe_key <> ''", name=op.f("ck_job_posting_dedupe_key_not_empty")
        ),
        sa.CheckConstraint(
            "fingerprint <> ''", name=op.f("ck_job_posting_fingerprint_not_empty")
        ),
        sa.CheckConstraint(
            f"status IN ({POSTING_STATUSES})", name=op.f("ck_job_posting_status_valid")
        ),
        sa.CheckConstraint(
            f"extraction_status IN ({EXTRACTION_STATUSES})",
            name=op.f("ck_job_posting_extraction_status_valid"),
        ),
        sa.CheckConstraint(
            f"employment_type IN ({EMPLOYMENT_TYPES})",
            name=op.f("ck_job_posting_employment_type_valid"),
        ),
        sa.CheckConstraint(
            f"direct_apply_verification_status IN ({VERIFICATION_STATUSES})",
            name=op.f("ck_job_posting_direct_apply_verification_status_valid"),
        ),
        sa.CheckConstraint(
            f"work_mode IS NULL OR work_mode IN ({WORK_MODES})",
            name=op.f("ck_job_posting_work_mode_valid"),
        ),
        # "Never fabricate an application URL" as a database guarantee.
        sa.CheckConstraint(
            "direct_apply_verification_status <> 'verified' "
            "OR direct_apply_url IS NOT NULL",
            name=op.f("ck_job_posting_verified_requires_url"),
        ),
        sa.CheckConstraint(
            "salary_min IS NULL OR salary_min >= 0",
            name=op.f("ck_job_posting_salary_min_non_negative"),
        ),
        sa.CheckConstraint(
            "salary_min IS NULL OR salary_max IS NULL OR salary_max >= salary_min",
            name=op.f("ck_job_posting_salary_range_ordered"),
        ),
        sa.CheckConstraint(
            f"seniority_resolution_status IS NULL "
            f"OR seniority_resolution_status IN ({RESOLUTION_STATUSES})",
            name=op.f("ck_job_posting_seniority_status_valid"),
        ),
        sa.CheckConstraint(
            f"seniority_resolution_provenance IS NULL "
            f"OR seniority_resolution_provenance IN ({RESOLUTION_PROVENANCES})",
            name=op.f("ck_job_posting_seniority_provenance_valid"),
        ),
        sa.CheckConstraint(
            f"seniority_resolved_track IS NULL "
            f"OR seniority_resolved_track IN ({TRACKS})",
            name=op.f("ck_job_posting_seniority_track_valid"),
        ),
        sa.CheckConstraint(
            f"seniority_evidence_source IS NULL "
            f"OR seniority_evidence_source IN ({EVIDENCE_SOURCES})",
            name=op.f("ck_job_posting_seniority_evidence_source_valid"),
        ),
        sa.CheckConstraint(
            _identity_coherence(
                "seniority_resolution_status",
                "seniority_level_id",
                "seniority_resolved_level_name",
                "seniority_resolved_track",
                allow_unset=True,
            ),
            name=op.f("ck_job_posting_seniority_resolved_requires_identity"),
        ),
    )

    op.create_table(
        "job_posting_source",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_posting_id", sa.Uuid(), nullable=False),
        sa.Column("job_source_id", sa.Uuid(), nullable=False),
        sa.Column("source_job_id", sa.String(length=512), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_posting_source")),
        sa.ForeignKeyConstraint(
            ["job_posting_id"], ["job_posting.id"], ondelete="CASCADE",
            name=op.f("fk_job_posting_source_job_posting_id_job_posting"),
        ),
        sa.ForeignKeyConstraint(
            ["job_source_id"], ["job_source.id"],
            name=op.f("fk_job_posting_source_job_source_id_job_source"),
        ),
        # Re-crawling a source cannot duplicate an external posting.
        sa.UniqueConstraint(
            "job_source_id", "source_job_id", name="uq_job_posting_source_identity"
        ),
        # One source lists a given canonical job once.
        sa.UniqueConstraint(
            "job_posting_id", "job_source_id", name="uq_job_posting_source_pair"
        ),
        sa.CheckConstraint(
            "source_job_id <> ''",
            name=op.f("ck_job_posting_source_source_job_id_not_empty"),
        ),
        sa.CheckConstraint(
            "last_seen_at >= first_seen_at",
            name=op.f("ck_job_posting_source_seen_order_valid"),
        ),
    )

    op.create_table(
        "job_skill_mention",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_posting_id", sa.Uuid(), nullable=False),
        sa.Column("mention_type", sa.String(length=16), nullable=False),
        sa.Column("raw_text", sa.String(length=255), nullable=False),
        sa.Column("normalized_representation", sa.String(length=255), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=True),
        sa.Column("resolution_status", sa.String(length=16), nullable=False),
        sa.Column("resolution_provenance", sa.String(length=32), nullable=False),
        sa.Column("resolved_canonical_name", sa.String(length=255), nullable=True),
        sa.Column("catalog_version_at_resolution", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_skill_mention")),
        sa.ForeignKeyConstraint(
            ["job_posting_id"], ["job_posting.id"], ondelete="CASCADE",
            name=op.f("fk_job_skill_mention_job_posting_id_job_posting"),
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"], ["skill.id"], name=op.f("fk_job_skill_mention_skill_id_skill")
        ),
        # Multiplicity within a document is not evidence
        # (DATABASE.md Section 4.4).
        sa.UniqueConstraint(
            "job_posting_id", "mention_type", "normalized_representation",
            name="uq_job_skill_mention_posting_type_representation",
        ),
        sa.CheckConstraint(
            f"mention_type IN ({MENTION_TYPES})",
            name=op.f("ck_job_skill_mention_mention_type_valid"),
        ),
        sa.CheckConstraint(
            f"resolution_status IN ({RESOLUTION_STATUSES})",
            name=op.f("ck_job_skill_mention_resolution_status_valid"),
        ),
        sa.CheckConstraint(
            f"resolution_provenance IN ({RESOLUTION_PROVENANCES})",
            name=op.f("ck_job_skill_mention_resolution_provenance_valid"),
        ),
        sa.CheckConstraint(
            "normalized_representation <> ''",
            name=op.f("ck_job_skill_mention_normalized_representation_not_empty"),
        ),
        sa.CheckConstraint(
            "raw_text <> ''", name=op.f("ck_job_skill_mention_raw_text_not_empty")
        ),
        sa.CheckConstraint(
            _identity_coherence(
                "resolution_status", "skill_id", "resolved_canonical_name"
            ),
            name=op.f("ck_job_skill_mention_resolved_requires_identity"),
        ),
    )

    # The FK 0001 deferred until JobPosting existed. RESTRICT rather than
    # CASCADE: promotion evidence counts INDEPENDENT postings, so a
    # cascade would let a posting deletion silently lower an evidence
    # count and quietly un-promote a skill. The purge policy for
    # JobPosting is deferred, and blocking the delete is what keeps that
    # decision explicit.
    op.create_foreign_key(
        "fk_skill_promotion_evidence_job_posting_id_job_posting",
        "skill_promotion_evidence",
        "job_posting",
        ["job_posting_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_skill_promotion_evidence_job_posting_id_job_posting",
        "skill_promotion_evidence",
        type_="foreignkey",
    )
    op.drop_table("job_skill_mention")
    op.drop_table("job_posting_source")
    op.drop_table("job_posting")
    op.drop_table("job_source")
