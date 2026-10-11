"""Canonical ownership of a job posting

Revision ID: 0005_job_posting_owner
Revises: 0004_job_foundation
Create Date: 2026-10-11

Reviewed by hand: yes

Adds `job_posting.owner_job_source_id`: the source that created the
canonical posting. Only that source may overwrite the posting's content
or replace its mentions (DATABASE.md Section 2.15). It is set in the
same statement that creates the posting and never changes during
ingestion, so ownership never depends on observation timestamps, which
a caller supplies and an older fixture re-run could backdate.

The column is NOT NULL with no backfill. Nothing before this revision
creates postings, so `job_posting` must be empty; the upgrade refuses
rather than invent an owner for existing rows.

RESTRICT, not CASCADE: a source must not disappear from under a posting
it owns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_job_posting_owner"
down_revision: str | None = "0004_job_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM job_posting) THEN
                RAISE EXCEPTION
                    'job_posting has rows; 0005 cannot assign their owners. '
                    'Ownership is never inferred from existing data.';
            END IF;
        END
        $$;
        """
    )
    op.add_column(
        "job_posting",
        sa.Column("owner_job_source_id", sa.Uuid(), nullable=False),
    )
    op.create_foreign_key(
        op.f("fk_job_posting_owner_job_source_id_job_source"),
        "job_posting",
        "job_source",
        ["owner_job_source_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_job_posting_owner_job_source_id_job_source"),
        "job_posting",
        type_="foreignkey",
    )
    op.drop_column("job_posting", "owner_job_source_id")
