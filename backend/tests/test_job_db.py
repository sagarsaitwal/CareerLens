"""Job schema constraints.

Run against a real PostgreSQL: these are database guarantees, not
application conventions, so they are tested against the actual DDL.

Helpers here CONSTRUCT rows without flushing. Tests add and flush
explicitly, so a rejection is raised inside the `pytest.raises` block it
belongs to rather than inside a helper — otherwise a test can pass for
the wrong reason.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.catalog import SkillPromotionEvidence
from app.models.enums import (
    DirectApplyVerificationStatus,
    EmploymentType,
    ExtractionStatus,
    JobMentionType,
    JobPostingStatus,
    JobSourceType,
    ResolutionProvenance,
    ResolutionStatus,
)
from app.models.job import JobPosting, JobPostingSource, JobSkillMention, JobSource

pytestmark = pytest.mark.integration

# The version 0002 seeds (asserted in test_catalog_db.py), so mention rows
# point at a catalog that really exists.
SEEDED_CATALOG_VERSION = 1
SOURCE_URL = "https://example.test/jobs/ext"


def source(**overrides: object) -> JobSource:
    defaults: dict[str, object] = {
        "name": f"Board {uuid.uuid4().hex[:8]}",
        "base_url": "https://example.test",
        "type": JobSourceType.JOB_BOARD.value,
    }
    defaults.update(overrides)
    return JobSource(**defaults)  # type: ignore[arg-type]


def posting(session: Session, **overrides: object) -> JobPosting:
    """An unsaved posting whose owner source is persisted, so the NOT NULL
    owner FK never masks the constraint a test is actually about."""
    defaults: dict[str, object] = {
        "owner_job_source_id": persisted_source(session).id,
        "title": "Senior Platform Engineer",
        "company": "Example Industries",
        "dedupe_key": uuid.uuid4().hex,
        "fingerprint": uuid.uuid4().hex,
    }
    defaults.update(overrides)
    return JobPosting(**defaults)  # type: ignore[arg-type]


def mention(job_posting_id: uuid.UUID, **overrides: object) -> JobSkillMention:
    defaults: dict[str, object] = {
        "job_posting_id": job_posting_id,
        "mention_type": JobMentionType.REQUIRED.value,
        "raw_text": "Docker",
        "normalized_representation": "docker",
        "resolution_status": ResolutionStatus.UNRESOLVED.value,
        "resolution_provenance": ResolutionProvenance.UNRESOLVED.value,
        "catalog_version_at_resolution": SEEDED_CATALOG_VERSION,
    }
    defaults.update(overrides)
    return JobSkillMention(**defaults)  # type: ignore[arg-type]


def persisted_posting(session: Session, **overrides: object) -> JobPosting:
    row = posting(session, **overrides)
    session.add(row)
    session.flush()
    return row


def persisted_source(session: Session, **overrides: object) -> JobSource:
    row = source(**overrides)
    session.add(row)
    session.flush()
    return row


# --- 1. JobSource ---------------------------------------------------------


def test_job_source_creation(db_session: Session) -> None:
    row = persisted_source(db_session)
    assert row.id is not None
    assert row.is_active is True
    assert row.last_crawled_at is None


def test_job_source_names_need_not_be_unique(db_session: Session) -> None:
    """DATABASE.md Section 2.14 approves no uniqueness on name."""
    first = persisted_source(db_session, name="Shared Board")
    second = persisted_source(db_session, name="Shared Board")
    assert first.id != second.id


def test_job_source_name_must_not_be_empty(db_session: Session) -> None:
    db_session.add(source(name=""))
    with pytest.raises(IntegrityError, match="name_not_empty"):
        db_session.flush()


def test_job_source_type_vocabulary_is_enforced(db_session: Session) -> None:
    db_session.add(source(type="rss_feed"))
    with pytest.raises(IntegrityError, match="type_valid"):
        db_session.flush()


@pytest.mark.parametrize("source_type", list(JobSourceType))
def test_every_documented_source_type_is_accepted(
    db_session: Session, source_type: JobSourceType
) -> None:
    row = persisted_source(db_session, type=source_type.value)
    assert row.type == source_type.value


# --- 2. JobPosting --------------------------------------------------------


def test_job_posting_creation_uses_documented_defaults(db_session: Session) -> None:
    row = persisted_posting(db_session)
    db_session.refresh(row)
    assert row.status == JobPostingStatus.UNKNOWN.value
    assert row.extraction_status == ExtractionStatus.PARTIAL.value
    assert row.employment_type == EmploymentType.UNKNOWN.value
    assert (
        row.direct_apply_verification_status
        == DirectApplyVerificationStatus.NOT_AVAILABLE.value
    )


def test_job_posting_carries_no_source_identity() -> None:
    """Source identity belongs to JobPostingSource, structurally."""
    columns = {column.name for column in JobPosting.__table__.columns}
    assert "job_source_id" not in columns
    assert "source_job_id" not in columns
    assert "source_url" not in columns


def test_experience_requirements_is_unparsed_text(db_session: Session) -> None:
    """The source's wording, with no derived year fields."""
    columns = {column.name for column in JobPosting.__table__.columns}
    assert "experience_requirements" in columns
    assert not {name for name in columns if "years" in name}

    row = persisted_posting(
        db_session, experience_requirements="6-9 years of platform experience"
    )
    assert row.experience_requirements == "6-9 years of platform experience"


def test_dedupe_key_is_unique(db_session: Session) -> None:
    key = uuid.uuid4().hex
    persisted_posting(db_session, dedupe_key=key)
    db_session.add(posting(db_session, dedupe_key=key))
    with pytest.raises(IntegrityError, match="uq_job_posting_dedupe_key"):
        db_session.flush()


def test_fingerprint_is_not_unique(db_session: Session) -> None:
    """Two different jobs may coincidentally say the same thing."""
    shared = uuid.uuid4().hex
    persisted_posting(db_session, fingerprint=shared)
    second = persisted_posting(db_session, fingerprint=shared)
    assert second.id is not None


def test_posting_status_vocabulary_is_enforced(db_session: Session) -> None:
    db_session.add(posting(db_session, status="expired"))
    with pytest.raises(IntegrityError, match="status_valid"):
        db_session.flush()


def test_extraction_status_vocabulary_is_enforced(db_session: Session) -> None:
    db_session.add(posting(db_session, extraction_status="none"))
    with pytest.raises(IntegrityError, match="extraction_status_valid"):
        db_session.flush()


def test_work_mode_vocabulary_is_enforced(db_session: Session) -> None:
    db_session.add(posting(db_session, work_mode="hybrid-ish"))
    with pytest.raises(IntegrityError, match="work_mode_valid"):
        db_session.flush()


def test_work_mode_may_be_absent(db_session: Session) -> None:
    """WorkMode has no `unknown` member; absence is expressed by NULL."""
    row = persisted_posting(db_session, work_mode=None)
    assert row.work_mode is None


@pytest.mark.invariant
def test_verified_status_without_a_url_is_rejected(db_session: Session) -> None:
    """"Never fabricate a URL" as a database guarantee.

    Without this the rule is an application convention, and a bug could
    present a verified link that does not exist.
    """
    db_session.add(
        posting(
            db_session,
            direct_apply_url=None,
            direct_apply_verification_status=(
                DirectApplyVerificationStatus.VERIFIED.value
            ),
        )
    )
    with pytest.raises(IntegrityError, match="verified_requires_url"):
        db_session.flush()


def test_absent_apply_url_is_a_legitimate_state(db_session: Session) -> None:
    row = persisted_posting(
        db_session,
        direct_apply_url=None,
        direct_apply_verification_status=(
            DirectApplyVerificationStatus.NOT_AVAILABLE.value
        ),
    )
    assert row.direct_apply_url is None


def test_inverted_salary_range_is_rejected(db_session: Session) -> None:
    db_session.add(posting(db_session, salary_min=200, salary_max=100))
    with pytest.raises(IntegrityError, match="salary_range_ordered"):
        db_session.flush()


def test_open_ended_salary_is_permitted(db_session: Session) -> None:
    row = persisted_posting(db_session, salary_min=100, salary_max=None)
    assert row.salary_max is None


@pytest.mark.invariant
def test_posting_seniority_resolved_without_identity_is_rejected(
    db_session: Session,
) -> None:
    db_session.add(
        posting(
            db_session,
            seniority_raw_text="Senior",
            seniority_normalized="senior",
            seniority_resolution_status=ResolutionStatus.RESOLVED.value,
            seniority_resolution_provenance=ResolutionProvenance.EXACT_MATCH.value,
            seniority_evidence_source="structured_field",
        )
    )
    with pytest.raises(IntegrityError, match="seniority_resolved_requires_identity"):
        db_session.flush()


def test_posting_without_seniority_is_permitted(db_session: Session) -> None:
    """A posting evidencing no seniority is a legitimate state."""
    row = persisted_posting(db_session)
    assert row.seniority_resolution_status is None


# --- 3 & 4. JobPostingSource ----------------------------------------------


def test_source_observation_links_to_a_canonical_posting(
    db_session: Session,
) -> None:
    job = persisted_posting(db_session)
    board = persisted_source(db_session)
    db_session.add(
        JobPostingSource(
            job_posting_id=job.id,
            job_source_id=board.id,
            source_job_id="ext-1",
            source_url=SOURCE_URL,
        )
    )
    db_session.flush()
    db_session.refresh(job)
    assert len(job.sources) == 1
    assert job.sources[0].source_job_id == "ext-1"


def test_many_sources_may_point_at_one_posting(db_session: Session) -> None:
    """Cross-source dedup: one canonical job, several provenances."""
    job = persisted_posting(db_session)
    for index in range(3):
        board = persisted_source(db_session, name=f"Board {index} {uuid.uuid4().hex[:6]}")
        db_session.add(
            JobPostingSource(
                job_posting_id=job.id,
                job_source_id=board.id,
                source_job_id=f"ext-{index}",
                source_url=SOURCE_URL,
            )
        )
    db_session.flush()
    db_session.refresh(job)
    assert len(job.sources) == 3


@pytest.mark.invariant
def test_a_source_cannot_list_one_external_posting_twice(db_session: Session) -> None:
    """Re-crawling creates no duplicate (DATABASE.md Section 4.3)."""
    board = persisted_source(db_session)
    first = persisted_posting(db_session)
    second = persisted_posting(db_session)
    db_session.add(
        JobPostingSource(
            job_posting_id=first.id,
            job_source_id=board.id,
            source_job_id="ext-1",
            source_url=SOURCE_URL,
        )
    )
    db_session.flush()
    db_session.add(
        JobPostingSource(
            job_posting_id=second.id,
            job_source_id=board.id,
            source_job_id="ext-1",
            source_url=SOURCE_URL,
        )
    )
    with pytest.raises(IntegrityError, match="uq_job_posting_source_identity"):
        db_session.flush()


@pytest.mark.invariant
def test_one_source_lists_a_posting_once(db_session: Session) -> None:
    board = persisted_source(db_session)
    job = persisted_posting(db_session)
    db_session.add(
        JobPostingSource(
            job_posting_id=job.id,
            job_source_id=board.id,
            source_job_id="a",
            source_url=SOURCE_URL,
        )
    )
    db_session.flush()
    db_session.add(
        JobPostingSource(
            job_posting_id=job.id,
            job_source_id=board.id,
            source_job_id="b",
            source_url=SOURCE_URL,
        )
    )
    with pytest.raises(IntegrityError, match="uq_job_posting_source_pair"):
        db_session.flush()


def test_source_observation_requires_a_source_url(db_session: Session) -> None:
    """Where a posting was discovered is part of the observation (Section 2.15.1)."""
    board = persisted_source(db_session)
    job = persisted_posting(db_session)
    db_session.add(
        JobPostingSource(job_posting_id=job.id, job_source_id=board.id, source_job_id="ext")
    )
    with pytest.raises(IntegrityError, match="source_url"):
        db_session.flush()


def test_last_seen_cannot_precede_first_seen(db_session: Session) -> None:
    """A row cannot record its last observation before its first.

    This checks ordering within one row only. It does not prove that
    first_seen_at never moves forward on update; that is ingestion logic.
    """
    board = persisted_source(db_session)
    job = persisted_posting(db_session)
    now = datetime.now(UTC)
    db_session.add(
        JobPostingSource(
            job_posting_id=job.id,
            job_source_id=board.id,
            source_job_id="ext",
            source_url=SOURCE_URL,
            first_seen_at=now,
            last_seen_at=now - timedelta(days=1),
        )
    )
    with pytest.raises(IntegrityError, match="seen_order_valid"):
        db_session.flush()


def test_canonical_seen_timestamps_are_derived_not_stored(
    db_session: Session,
) -> None:
    """DATABASE.md defines them as an aggregate over source observations."""
    columns = {column.name for column in JobPosting.__table__.columns}
    assert "first_seen_at" not in columns
    assert "last_seen_at" not in columns

    job = persisted_posting(db_session)
    assert job.first_seen_at is None
    assert job.last_seen_at is None

    earliest = datetime.now(UTC) - timedelta(days=5)
    latest = datetime.now(UTC)
    for index, (first, last) in enumerate(
        ((earliest, earliest + timedelta(days=1)), (latest - timedelta(days=1), latest))
    ):
        board = persisted_source(db_session, name=f"B{index} {uuid.uuid4().hex[:6]}")
        db_session.add(
            JobPostingSource(
                job_posting_id=job.id,
                job_source_id=board.id,
                source_job_id=f"ext-{index}",
                source_url=SOURCE_URL,
                first_seen_at=first,
                last_seen_at=last,
            )
        )
    db_session.flush()
    db_session.refresh(job)

    assert job.first_seen_at == earliest
    assert job.last_seen_at == latest


# --- 5, 6, 7. JobSkillMention ---------------------------------------------


@pytest.mark.parametrize("mention_type", list(JobMentionType))
def test_every_mention_type_is_accepted(
    db_session: Session, mention_type: JobMentionType
) -> None:
    job = persisted_posting(db_session)
    db_session.add(mention(job.id, mention_type=mention_type.value))
    db_session.flush()
    stored = db_session.scalars(
        select(JobSkillMention).where(JobSkillMention.job_posting_id == job.id)
    ).one()
    assert stored.mention_type == mention_type.value


def test_unknown_mention_type_is_rejected(db_session: Session) -> None:
    job = persisted_posting(db_session)
    db_session.add(mention(job.id, mention_type="nice_to_have"))
    with pytest.raises(IntegrityError, match="mention_type_valid"):
        db_session.flush()


@pytest.mark.invariant
def test_duplicate_mention_in_one_collection_is_rejected(
    db_session: Session,
) -> None:
    """Multiplicity within a document is not evidence (Section 4.4)."""
    job = persisted_posting(db_session)
    db_session.add(mention(job.id))
    db_session.flush()
    db_session.add(mention(job.id))
    with pytest.raises(
        IntegrityError, match="uq_job_skill_mention_posting_type_representation"
    ):
        db_session.flush()


def test_same_representation_in_different_collections_stays_distinct(
    db_session: Session,
) -> None:
    """Required and mentioned are two claims, not one duplicated."""
    job = persisted_posting(db_session)
    for mention_type in (JobMentionType.REQUIRED, JobMentionType.MENTIONED):
        db_session.add(mention(job.id, mention_type=mention_type.value))
    db_session.flush()
    db_session.refresh(job)
    assert {row.mention_type for row in job.mentions} == {"required", "mentioned"}


def test_same_representation_across_postings_is_permitted(
    db_session: Session,
) -> None:
    first = persisted_posting(db_session)
    second = persisted_posting(db_session)
    db_session.add(mention(first.id))
    db_session.add(mention(second.id))
    db_session.flush()


@pytest.mark.invariant
def test_resolved_mention_without_identity_is_rejected(db_session: Session) -> None:
    job = persisted_posting(db_session)
    db_session.add(
        mention(
            job.id,
            skill_id=None,
            resolution_status=ResolutionStatus.RESOLVED.value,
            resolution_provenance=ResolutionProvenance.EXACT_MATCH.value,
            resolved_canonical_name=None,
        )
    )
    with pytest.raises(IntegrityError, match="resolved_requires_identity"):
        db_session.flush()


@pytest.mark.invariant
def test_indeterminate_mention_carrying_identity_is_rejected(
    db_session: Session,
) -> None:
    """Indeterminate means evidence exists but could not be pinned."""
    job = persisted_posting(db_session)
    known_skill = db_session.scalars(select(text("id FROM skill LIMIT 1"))).one()
    db_session.add(
        mention(
            job.id,
            skill_id=known_skill,
            resolution_status=ResolutionStatus.INDETERMINATE.value,
            resolution_provenance=ResolutionProvenance.UNRESOLVED.value,
            resolved_canonical_name="Docker",
        )
    )
    with pytest.raises(IntegrityError, match="resolved_requires_identity"):
        db_session.flush()


def test_unresolved_mention_without_identity_is_permitted(
    db_session: Session,
) -> None:
    """Unresolved is a normal operating state, never an error."""
    job = persisted_posting(db_session)
    db_session.add(
        mention(
            job.id,
            raw_text="Flibbertigibbet",
            normalized_representation="flibbertigibbet",
        )
    )
    db_session.flush()


def test_mention_requires_a_non_empty_representation(db_session: Session) -> None:
    job = persisted_posting(db_session)
    db_session.add(mention(job.id, normalized_representation=""))
    with pytest.raises(IntegrityError, match="normalized_representation_not_empty"):
        db_session.flush()


@pytest.mark.invariant
def test_mention_without_catalog_version_is_rejected(db_session: Session) -> None:
    """Invariant 29: every mention records the catalog it resolved against."""
    job = persisted_posting(db_session)
    db_session.add(mention(job.id, catalog_version_at_resolution=None))
    with pytest.raises(IntegrityError, match="catalog_version_at_resolution"):
        db_session.flush()


# --- 8. Employment type vocabulary ----------------------------------------


@pytest.mark.parametrize("employment_type", list(EmploymentType))
def test_every_documented_employment_type_is_accepted(
    db_session: Session, employment_type: EmploymentType
) -> None:
    row = persisted_posting(db_session, employment_type=employment_type.value)
    assert row.employment_type == employment_type.value


def test_employment_type_vocabulary_is_closed(db_session: Session) -> None:
    """An unrecognised source value must land on `unknown`, not a new member."""
    db_session.add(posting(db_session, employment_type="permanent"))
    with pytest.raises(IntegrityError, match="employment_type_valid"):
        db_session.flush()


def test_employment_type_vocabulary_matches_documentation() -> None:
    assert [member.value for member in EmploymentType] == [
        "full_time",
        "part_time",
        "contract",
        "temporary",
        "internship",
        "unknown",
    ]


# --- 9. SkillPromotionEvidence FK -----------------------------------------


@pytest.mark.invariant
def test_promotion_evidence_requires_a_real_posting(db_session: Session) -> None:
    """Evidence counts independent postings, so the reference must be real."""
    db_session.add(
        SkillPromotionEvidence(
            normalized_representation="somenewtechnologyz",
            raw_text_observed="SomeNewTechnologyZ",
            job_posting_id=uuid.uuid4(),
        )
    )
    with pytest.raises(IntegrityError, match="job_posting"):
        db_session.flush()


def test_promotion_evidence_accepts_a_real_posting(db_session: Session) -> None:
    job = persisted_posting(db_session)
    db_session.add(
        SkillPromotionEvidence(
            normalized_representation="somenewtechnologyz",
            raw_text_observed="SomeNewTechnologyZ",
            job_posting_id=job.id,
        )
    )
    db_session.flush()


@pytest.mark.invariant
def test_deleting_a_posting_with_evidence_is_blocked(db_session: Session) -> None:
    """RESTRICT, not CASCADE.

    Promotion evidence is historical audit evidence. Cascading would let
    a posting deletion silently lower an evidence count and quietly
    un-promote a skill, so the delete is refused instead.
    """
    job = persisted_posting(db_session)
    db_session.add(
        SkillPromotionEvidence(
            normalized_representation="somenewtechnologyz",
            raw_text_observed="SomeNewTechnologyZ",
            job_posting_id=job.id,
        )
    )
    db_session.flush()

    with pytest.raises(IntegrityError, match="fk_skill_promotion_evidence"):
        db_session.execute(
            text("DELETE FROM job_posting WHERE id = :id"), {"id": job.id}
        )


def test_posting_delete_cascades_to_its_own_children(db_session: Session) -> None:
    """Sources and mentions belong to the posting; evidence does not."""
    job = persisted_posting(db_session)
    board = persisted_source(db_session)
    db_session.add(
        JobPostingSource(
            job_posting_id=job.id,
            job_source_id=board.id,
            source_job_id="ext",
            source_url=SOURCE_URL,
        )
    )
    db_session.add(mention(job.id))
    db_session.flush()

    db_session.execute(text("DELETE FROM job_posting WHERE id = :id"), {"id": job.id})
    db_session.flush()

    assert db_session.scalars(select(JobPostingSource)).all() == []
    assert db_session.scalars(select(JobSkillMention)).all() == []


# --- 10. Migration shape ---------------------------------------------------


def test_migration_created_every_job_table(migrated_engine: object) -> None:
    inspector = inspect(migrated_engine)  # type: ignore[arg-type]
    tables = set(inspector.get_table_names())
    assert {
        "job_source",
        "job_posting",
        "job_posting_source",
        "job_skill_mention",
    } <= tables


def test_promotion_evidence_fk_uses_restrict(migrated_engine: object) -> None:
    inspector = inspect(migrated_engine)  # type: ignore[arg-type]
    keys = [
        key
        for key in inspector.get_foreign_keys("skill_promotion_evidence")
        if key["referred_table"] == "job_posting"
    ]
    assert len(keys) == 1
    assert keys[0]["options"].get("ondelete") == "RESTRICT"


# --- 11. Canonical owner (0005) --------------------------------------------


def test_posting_requires_an_owner_source(db_session: Session) -> None:
    db_session.add(posting(db_session, owner_job_source_id=None))
    with pytest.raises(IntegrityError, match="owner_job_source_id"):
        db_session.flush()


def test_owner_must_be_an_existing_source(db_session: Session) -> None:
    db_session.add(posting(db_session, owner_job_source_id=uuid.uuid4()))
    with pytest.raises(IntegrityError, match="fk_job_posting_owner_job_source_id_job_source"):
        db_session.flush()


@pytest.mark.invariant
def test_owning_source_cannot_be_deleted(db_session: Session) -> None:
    """RESTRICT: a source must not disappear from under a posting it owns."""
    job = persisted_posting(db_session)
    with pytest.raises(IntegrityError, match="fk_job_posting_owner_job_source_id_job_source"):
        db_session.execute(
            text("DELETE FROM job_source WHERE id = :id"), {"id": job.owner_job_source_id}
        )


def test_owner_fk_uses_restrict(migrated_engine: object) -> None:
    inspector = inspect(migrated_engine)  # type: ignore[arg-type]
    columns = {column["name"]: column for column in inspector.get_columns("job_posting")}
    assert columns["owner_job_source_id"]["nullable"] is False
    keys = [
        key
        for key in inspector.get_foreign_keys("job_posting")
        if key["constrained_columns"] == ["owner_job_source_id"]
    ]
    assert len(keys) == 1
    assert keys[0]["referred_table"] == "job_source"
    assert keys[0]["options"].get("ondelete") == "RESTRICT"
