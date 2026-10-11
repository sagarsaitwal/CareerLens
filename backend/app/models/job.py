"""Job ingestion models (DATABASE.md Sections 2.14-2.15.2).

Canonical, source-of-truth job information, deduplicated across sources.
Not user-specific.

A deliberate asymmetry with the profile side: there is NO
JobPostingVersion. `JobPosting` is a live, mutable, purgeable record
(DATABASE.md Sections 4.1 and 4.5), and historical interpretability is
provided downstream by `MatchResultJobDescriptor` captured at match
time. Nothing here may acquire immutability semantics by accident.

Source identity lives on `JobPostingSource`, never here: one canonical
job may be discovered in several places, and that is what makes
"deduplicated across sources" representable rather than merely
asserted.

This module defines schema only. `dedupe_key` and `fingerprint` are
derived in app/domain/jobs.py, and ingestion lives in
app/services/job_ingestion.py.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.catalog import enum_check, identity_coherence_check, nullable_enum_check
from app.models.enums import (
    DirectApplyVerificationStatus,
    EmploymentType,
    EvidenceSource,
    ExtractionStatus,
    JobMentionType,
    JobPostingStatus,
    JobSourceType,
    ResolutionProvenance,
    ResolutionStatus,
    SeniorityTrack,
    WorkMode,
)


class JobSource(Base):
    """A permitted origin of postings (DATABASE.md Section 2.14)."""

    __tablename__ = "job_source"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    observations: Mapped[list["JobPostingSource"]] = relationship(
        back_populates="source"
    )

    __table_args__ = (
        CheckConstraint(enum_check("type", JobSourceType), name="type_valid"),
        CheckConstraint("name <> ''", name="name_not_empty"),
    )


class JobPosting(Base):
    """The canonical job, independent of where it was found."""

    __tablename__ = "job_posting"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # The source that created this posting: the only one allowed to
    # overwrite its content or replace its mentions. Set in the creating
    # INSERT and never changed by ingestion (DATABASE.md Section 2.15).
    owner_job_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_source.id", ondelete="RESTRICT"), nullable=False
    )

    # --- core identity --------------------------------------------------
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    company: Mapped[str] = mapped_column(String(512), nullable=False)

    # --- content ---------------------------------------------------------
    # UNTRUSTED external input: data, never instruction
    # (AI-MATCHING.md Section 10). Text because postings are long and no
    # documented length limit applies.
    description_raw: Mapped[str | None] = mapped_column(Text)

    # --- URLs -------------------------------------------------------------
    official_url: Mapped[str | None] = mapped_column(String(2048))
    # Nullable and never fabricated. The CHECK below is what makes that a
    # database guarantee rather than an application convention.
    direct_apply_url: Mapped[str | None] = mapped_column(String(2048))
    direct_apply_verification_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DirectApplyVerificationStatus.NOT_AVAILABLE.value,
        server_default=DirectApplyVerificationStatus.NOT_AVAILABLE.value,
    )

    # --- classification ----------------------------------------------------
    location: Mapped[str | None] = mapped_column(String(512))
    # NULL means the posting did not state one. WorkMode has no `unknown`
    # member, so absence is expressed by absence.
    work_mode: Mapped[str | None] = mapped_column(String(16))
    # NOT NULL because `unknown` IS a member: a posting that states
    # nothing, and a posting stating something unrecognised, both land on
    # `unknown` rather than being guessed.
    employment_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=EmploymentType.UNKNOWN.value,
        server_default=EmploymentType.UNKNOWN.value,
    )
    # The source's wording, stored verbatim. Deliberately NOT parsed into
    # a comparable range: AI-MATCHING.md requires an experience-range
    # check but specifies no structure for it, so inventing one here
    # would pre-empt a deferred decision.
    experience_requirements: Mapped[str | None] = mapped_column(Text)
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)
    salary_currency: Mapped[str | None] = mapped_column(String(3))

    # --- seniority reference (DATABASE.md Section 2.9) ----------------------
    # What the posting EVIDENCES or requests, never a guess about the
    # company's hierarchy. Level name AND track are carried by value so
    # the record survives catalog evolution.
    seniority_raw_text: Mapped[str | None] = mapped_column(String(512))
    seniority_normalized: Mapped[str | None] = mapped_column(String(255))
    seniority_level_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("seniority_level.id")
    )
    seniority_resolution_status: Mapped[str | None] = mapped_column(String(16))
    seniority_resolution_provenance: Mapped[str | None] = mapped_column(String(32))
    seniority_resolved_level_name: Mapped[str | None] = mapped_column(String(255))
    seniority_resolved_track: Mapped[str | None] = mapped_column(String(16))
    seniority_evidence_source: Mapped[str | None] = mapped_column(String(32))
    seniority_catalog_version: Mapped[int | None] = mapped_column(Integer)

    # --- lifecycle ----------------------------------------------------------
    posting_date: Mapped[date | None] = mapped_column(Date)
    last_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=JobPostingStatus.UNKNOWN.value,
        server_default=JobPostingStatus.UNKNOWN.value,
    )
    # WHICH job this is. Derivation lives in a later task; the column and
    # its uniqueness are the schema contract.
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # WHAT it currently says. Distinct from dedupe_key and deliberately
    # not unique: two different jobs may coincidentally say the same
    # thing, and a job keeps its identity across content changes.
    fingerprint: Mapped[str] = mapped_column(String(255), nullable=False)
    extraction_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ExtractionStatus.PARTIAL.value,
        server_default=ExtractionStatus.PARTIAL.value,
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

    sources: Mapped[list["JobPostingSource"]] = relationship(
        back_populates="posting", cascade="all, delete-orphan"
    )
    mentions: Mapped[list["JobSkillMention"]] = relationship(
        back_populates="posting", cascade="all, delete-orphan"
    )

    @property
    def first_seen_at(self) -> datetime | None:
        """Earliest observation across every source listing this job.

        DERIVED, not stored. These are observations OF a job, not
        properties of it — a job can be fresh on one board and stale on
        another — so materializing them here would create a value
        nothing maintains. None when no source has observed it yet.
        """
        return min((source.first_seen_at for source in self.sources), default=None)

    @property
    def last_seen_at(self) -> datetime | None:
        """Latest observation across every source listing this job."""
        return max((source.last_seen_at for source in self.sources), default=None)

    __table_args__ = (
        # One canonical job per dedupe_key. With the uniqueness on
        # JobPostingSource this is what DATABASE.md Section 4.3 means by
        # "re-ingesting creates no duplicates".
        UniqueConstraint("dedupe_key", name="uq_job_posting_dedupe_key"),
        CheckConstraint("title <> ''", name="title_not_empty"),
        CheckConstraint("company <> ''", name="company_not_empty"),
        CheckConstraint("dedupe_key <> ''", name="dedupe_key_not_empty"),
        CheckConstraint("fingerprint <> ''", name="fingerprint_not_empty"),
        CheckConstraint(enum_check("status", JobPostingStatus), name="status_valid"),
        CheckConstraint(
            enum_check("extraction_status", ExtractionStatus),
            name="extraction_status_valid",
        ),
        CheckConstraint(
            enum_check("employment_type", EmploymentType), name="employment_type_valid"
        ),
        CheckConstraint(
            enum_check(
                "direct_apply_verification_status", DirectApplyVerificationStatus
            ),
            name="direct_apply_verification_status_valid",
        ),
        CheckConstraint(
            nullable_enum_check("work_mode", WorkMode), name="work_mode_valid"
        ),
        # "Never fabricate an application URL" as a database guarantee:
        # nothing may claim verification without a URL to verify.
        CheckConstraint(
            "direct_apply_verification_status <> 'verified' "
            "OR direct_apply_url IS NOT NULL",
            name="verified_requires_url",
        ),
        CheckConstraint(
            "salary_min IS NULL OR salary_min >= 0", name="salary_min_non_negative"
        ),
        CheckConstraint(
            "salary_min IS NULL OR salary_max IS NULL OR salary_max >= salary_min",
            name="salary_range_ordered",
        ),
        CheckConstraint(
            nullable_enum_check("seniority_resolution_status", ResolutionStatus),
            name="seniority_status_valid",
        ),
        CheckConstraint(
            nullable_enum_check(
                "seniority_resolution_provenance", ResolutionProvenance
            ),
            name="seniority_provenance_valid",
        ),
        CheckConstraint(
            nullable_enum_check("seniority_resolved_track", SeniorityTrack),
            name="seniority_track_valid",
        ),
        CheckConstraint(
            nullable_enum_check("seniority_evidence_source", EvidenceSource),
            name="seniority_evidence_source_valid",
        ),
        # The same coherence rule the profile side carries: no resolved
        # status without a pinned level, and no pinned level under an
        # unresolved or indeterminate status. `allow_unset` because a
        # posting may evidence no seniority at all.
        CheckConstraint(
            identity_coherence_check(
                "seniority_resolution_status",
                "seniority_level_id",
                "seniority_resolved_level_name",
                "seniority_resolved_track",
                allow_unset=True,
            ),
            name="seniority_resolved_requires_identity",
        ),
    )


class JobPostingSource(Base):
    """One source's identity for, and observation of, a canonical job.

    Several of these may point at the same `JobPosting`: that is what
    cross-source deduplication means in this schema.
    """

    __tablename__ = "job_posting_source"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_posting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("job_posting.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_source.id"), nullable=False
    )
    source_job_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    posting: Mapped[JobPosting] = relationship(back_populates="sources")
    source: Mapped[JobSource] = relationship(back_populates="observations")

    __table_args__ = (
        # Re-crawling a source cannot create a second row for the same
        # external posting.
        UniqueConstraint(
            "job_source_id", "source_job_id", name="uq_job_posting_source_identity"
        ),
        # One source lists a given canonical job once.
        UniqueConstraint(
            "job_posting_id", "job_source_id", name="uq_job_posting_source_pair"
        ),
        CheckConstraint("source_job_id <> ''", name="source_job_id_not_empty"),
        # Enforces last_seen_at >= first_seen_at within one observation row.
        # It does not make first_seen_at immutable or stop it moving forward.
        CheckConstraint("last_seen_at >= first_seen_at", name="seen_order_valid"),
    )


class JobSkillMention(Base):
    """One extracted skill mention on a posting.

    All four documented collections share the mention field pattern
    (DATABASE.md Section 2.9), so they are one table discriminated by
    `mention_type`.

    POSITIVE-ONLY: there is no representation for an explicit negative
    statement. A mention's absence means "not mentioned", never "ruled
    out".
    """

    __tablename__ = "job_skill_mention"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_posting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("job_posting.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Keeps the four collections distinct. `responsibility` is evidence
    # only and must not become a matching signal through this schema.
    mention_type: Mapped[str] = mapped_column(String(16), nullable=False)

    raw_text: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_representation: Mapped[str] = mapped_column(String(255), nullable=False)
    skill_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skill.id")
    )
    resolution_status: Mapped[str] = mapped_column(String(16), nullable=False)
    resolution_provenance: Mapped[str] = mapped_column(String(32), nullable=False)
    # By value, so a historical record stays interpretable after the
    # catalog is renamed or merged.
    resolved_canonical_name: Mapped[str | None] = mapped_column(String(255))
    # NOT NULL whatever the status (invariant 29): an unresolved mention
    # still records the pinned catalog it failed to resolve against.
    catalog_version_at_resolution: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    posting: Mapped[JobPosting] = relationship(back_populates="mentions")

    __table_args__ = (
        # Duplicate mentions collapse within a collection: multiplicity
        # inside a document is not evidence. The scope includes
        # mention_type because a skill stated as required and also
        # appearing in prose is two claims, not one duplicated.
        UniqueConstraint(
            "job_posting_id",
            "mention_type",
            "normalized_representation",
            name="uq_job_skill_mention_posting_type_representation",
        ),
        CheckConstraint(
            enum_check("mention_type", JobMentionType), name="mention_type_valid"
        ),
        CheckConstraint(
            enum_check("resolution_status", ResolutionStatus),
            name="resolution_status_valid",
        ),
        CheckConstraint(
            enum_check("resolution_provenance", ResolutionProvenance),
            name="resolution_provenance_valid",
        ),
        CheckConstraint(
            "normalized_representation <> ''", name="normalized_representation_not_empty"
        ),
        CheckConstraint("raw_text <> ''", name="raw_text_not_empty"),
        # A resolved mention must carry identity; an unresolved or
        # indeterminate one must not pretend to have any.
        CheckConstraint(
            identity_coherence_check(
                "resolution_status", "skill_id", "resolved_canonical_name"
            ),
            name="resolved_requires_identity",
        ),
    )
