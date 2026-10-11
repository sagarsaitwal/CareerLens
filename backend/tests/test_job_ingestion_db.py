"""Fixture ingestion against real PostgreSQL and Redis.

Most tests run inside the rolled-back `db_session`: the service gets a
session factory bound to the SAME connection with
join_transaction_mode="create_savepoint", so its own savepoints and
commit nest inside the test's transaction and vanish afterwards.

The concurrency tests commit for real, because the races they cover are
between transactions. Their rows use a unique company so nothing they
leave behind can collide with another test's dedupe_key.
"""

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import pytest
import redis
from sqlalchemy import Engine, delete, func, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.locking import LockAcquisitionTimeoutError
from app.domain.jobs import build_dedupe_key
from app.domain.resolution import SkillResolution
from app.models.enums import (
    DirectApplyVerificationStatus,
    JobSourceType,
    ResolutionProvenance,
    ResolutionStatus,
)
from app.models.job import JobPosting, JobPostingSource, JobSkillMention, JobSource
from app.schemas.job_ingestion import validate_fixture
from app.services import job_ingestion
from app.services.catalog import add_skill, open_catalog_version
from app.services.job_ingestion import (
    IngestionReport,
    Outcome,
    ingest_fixture,
    ingestion_lock_key,
)

pytestmark = pytest.mark.integration

T1 = datetime(2026, 10, 1, 9, tzinfo=UTC)
T2 = T1 + timedelta(days=1)
T3 = T1 + timedelta(days=2)


@pytest.fixture
def factory(db_session: Session) -> Callable[[], Session]:
    connection = db_session.connection()
    return lambda: Session(bind=connection, join_transaction_mode="create_savepoint")


@pytest.fixture
def company() -> str:
    return f"Example Industries {uuid.uuid4().hex[:8]}"


def declaration(name: str | None = None) -> dict[str, Any]:
    return {
        "name": name or f"Board {uuid.uuid4().hex[:8]}",
        "base_url": "https://board.example.test",
        "type": "job_board",
    }


def raw(company: str, source_job_id: str = "ext-1", **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "source_job_id": source_job_id,
        "source_url": f"https://board.example.test/jobs/{source_job_id}",
        "title": "Senior Platform Engineer",
        "company": company,
        "location": "Pune, India",
        "description_raw": "Run the platform.",
        "mentions": [{"mention_type": "required", "raw_text": "Docker"}],
    }
    values.update(overrides)
    return values


def ingest(
    factory: Callable[[], Session],
    redis_client: redis.Redis,
    source: dict[str, Any],
    *postings: dict[str, Any],
    observed_at: datetime = T1,
) -> IngestionReport:
    fixture = validate_fixture({"source": source, "postings": list(postings)})
    return ingest_fixture(factory, fixture, observed_at=observed_at, redis_client=redis_client)


def postings_for(session: Session, company: str) -> list[Any]:
    return list(session.execute(select(JobPosting).where(JobPosting.company == company)).scalars())


def observations(session: Session, posting_id: uuid.UUID) -> list[Any]:
    return list(
        session.execute(
            select(JobPostingSource).where(JobPostingSource.job_posting_id == posting_id)
        ).scalars()
    )


def mentions(session: Session, posting_id: uuid.UUID) -> list[Any]:
    return list(
        session.execute(
            select(JobSkillMention)
            .where(JobSkillMention.job_posting_id == posting_id)
            .order_by(JobSkillMention.normalized_representation)
        ).scalars()
    )


def fresh(session: Session, row: Any) -> Any:
    session.refresh(row)
    return row


# --- creation ---------------------------------------------------------------


@pytest.mark.invariant
def test_first_ingest_creates_owned_posting_observation_and_resolved_mentions(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    report = ingest(
        factory,
        redis_client,
        declaration(),
        raw(
            company,
            seniority={"raw_text": "Senior", "evidence_source": "structured_field"},
            mentions=[
                {"mention_type": "required", "raw_text": "Docker"},
                {"mention_type": "preferred", "raw_text": "K8s"},
                {"mention_type": "mentioned", "raw_text": "Quantum Basket Weaving"},
            ],
        ),
        raw(company, "ext-2", title="Staff Engineer"),
    )
    assert report.refused == ()
    assert [p.outcome for p in report.postings] == [Outcome.CREATED, Outcome.CREATED]

    jobs = postings_for(db_session, company)
    assert len(jobs) == 2
    assert {job.owner_job_source_id for job in jobs} == {report.job_source_id}

    senior = next(job for job in jobs if job.title == "Senior Platform Engineer")
    assert senior.seniority_resolution_status == ResolutionStatus.RESOLVED.value
    assert senior.seniority_catalog_version == report.catalog_version
    assert senior.direct_apply_verification_status == (
        DirectApplyVerificationStatus.NOT_AVAILABLE.value
    )

    [observation] = observations(db_session, senior.id)
    assert observation.first_seen_at == observation.last_seen_at == T1

    rows = mentions(db_session, senior.id) + mentions(db_session, jobs[0].id)
    # Invariant 29: one pinned catalog version for the whole operation.
    assert {row.catalog_version_at_resolution for row in rows} == {report.catalog_version}
    assert ResolutionProvenance.AI_SEMANTIC.value not in {r.resolution_provenance for r in rows}
    by_text = {row.raw_text: row for row in mentions(db_session, senior.id)}
    assert by_text["Docker"].resolution_status == ResolutionStatus.RESOLVED.value
    assert by_text["K8s"].resolution_provenance == ResolutionProvenance.ALIAS_RULE.value
    assert by_text["Quantum Basket Weaving"].resolution_status == (
        ResolutionStatus.UNRESOLVED.value
    )


def test_contract_rejections_are_reported_and_the_rest_is_written(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    report = ingest(
        factory, redis_client, declaration(), raw(company), raw(company, "ext-2", title=" ")
    )
    assert [p.outcome for p in report.postings] == [Outcome.CREATED, Outcome.REJECTED]
    assert any(reason.startswith("title") for reason in report.postings[1].reasons)
    assert len(postings_for(db_session, company)) == 1


# --- re-observation and timestamps --------------------------------------------


@pytest.mark.invariant
def test_identical_reingest_only_advances_last_seen(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    source = declaration()
    ingest(factory, redis_client, source, raw(company), observed_at=T1)
    [job] = postings_for(db_session, company)
    before = {c.name: getattr(job, c.name) for c in JobPosting.__table__.columns}
    mention_ids = {row.id for row in mentions(db_session, job.id)}

    report = ingest(factory, redis_client, source, raw(company), observed_at=T2)

    assert report.postings[0].outcome is Outcome.UNCHANGED
    assert not report.postings[0].stale_observation
    job = fresh(db_session, job)
    assert {c.name: getattr(job, c.name) for c in JobPosting.__table__.columns} == before
    assert {row.id for row in mentions(db_session, job.id)} == mention_ids
    [observation] = observations(db_session, job.id)
    assert (observation.first_seen_at, observation.last_seen_at) == (T1, T2)


@pytest.mark.invariant
def test_older_observation_is_flagged_and_moves_no_timestamp(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    source = declaration()
    ingest(factory, redis_client, source, raw(company), observed_at=T2)
    report = ingest(factory, redis_client, source, raw(company), observed_at=T1)

    assert report.postings[0].stale_observation
    [job] = postings_for(db_session, company)
    [observation] = observations(db_session, job.id)
    # first_seen_at is never rewritten, not even backwards.
    assert (observation.first_seen_at, observation.last_seen_at) == (T2, T2)


def test_reobservation_updates_the_source_url(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    source = declaration()
    ingest(factory, redis_client, source, raw(company))
    moved = "https://board.example.test/listing/ext-1"
    ingest(factory, redis_client, source, raw(company, source_url=moved), observed_at=T2)
    [job] = postings_for(db_session, company)
    [observation] = observations(db_session, job.id)
    assert observation.source_url == moved


# --- owner updates ----------------------------------------------------------------


def test_owner_content_change_updates_posting_and_replaces_mentions(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    source = declaration()
    ingest(factory, redis_client, source, raw(company))
    [job] = postings_for(db_session, company)
    old_fingerprint = job.fingerprint

    report = ingest(
        factory,
        redis_client,
        source,
        raw(
            company,
            description_raw="Run AND grow the platform.",
            mentions=[{"mention_type": "required", "raw_text": "Terraform"}],
        ),
        observed_at=T2,
    )

    assert report.postings[0].outcome is Outcome.UPDATED
    job = fresh(db_session, job)
    assert job.description_raw == "Run AND grow the platform."
    assert job.fingerprint != old_fingerprint
    assert [row.raw_text for row in mentions(db_session, job.id)] == ["Terraform"]


def test_owner_change_outside_the_fingerprint_is_applied(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    source = declaration()
    ingest(factory, redis_client, source, raw(company))
    [job] = postings_for(db_session, company)
    fingerprint = job.fingerprint

    report = ingest(
        factory,
        redis_client,
        source,
        raw(company, salary_min=100, salary_max=200, salary_currency="inr", status="closed"),
        observed_at=T2,
    )

    assert report.postings[0].outcome is Outcome.UPDATED
    job = fresh(db_session, job)
    assert (job.salary_min, job.salary_max, job.salary_currency) == (100, 200, "INR")
    assert job.status == "closed"
    assert job.fingerprint == fingerprint


def test_raw_text_only_mention_change_is_not_applied(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    source = declaration()
    ingest(factory, redis_client, source, raw(company))
    report = ingest(
        factory,
        redis_client,
        source,
        raw(company, mentions=[{"mention_type": "required", "raw_text": "DOCKER"}]),
        observed_at=T2,
    )
    assert report.postings[0].outcome is Outcome.UNCHANGED
    [job] = postings_for(db_session, company)
    assert [row.raw_text for row in mentions(db_session, job.id)] == ["Docker"]


def test_newer_catalog_version_re_resolves_owner_mentions(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    source = declaration()
    posting = raw(company, mentions=[{"mention_type": "required", "raw_text": "Ansible"}])
    first = ingest(factory, redis_client, source, posting)
    [job] = postings_for(db_session, company)
    [before] = mentions(db_session, job.id)
    assert before.resolution_status == ResolutionStatus.UNRESOLVED.value

    version = open_catalog_version(db_session, f"t{uuid.uuid4().hex[:8]}")
    add_skill(db_session, "Ansible", version)

    second = ingest(factory, redis_client, source, posting, observed_at=T2)

    assert second.catalog_version == version.version_number > (first.catalog_version or 0)
    assert second.postings[0].outcome is Outcome.UPDATED
    [after] = mentions(db_session, job.id)
    assert after.resolution_status == ResolutionStatus.RESOLVED.value
    assert after.catalog_version_at_resolution == version.version_number


def test_newer_catalog_version_re_resolves_owner_seniority(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    """Identical owner input, newer catalog: seniority follows the pin like mentions do."""
    source = declaration()
    # No mentions, so only seniority can make the re-ingest an update.
    posting = raw(
        company,
        seniority={"raw_text": "Senior", "evidence_source": "structured_field"},
        mentions=[],
    )
    first = ingest(factory, redis_client, source, posting)
    [job] = postings_for(db_session, company)
    assert job.seniority_catalog_version == first.catalog_version
    fingerprint = job.fingerprint

    same = ingest(factory, redis_client, source, posting, observed_at=T2)
    assert same.postings[0].outcome is Outcome.UNCHANGED

    version = open_catalog_version(db_session, f"t{uuid.uuid4().hex[:8]}")
    newer = ingest(factory, redis_client, source, posting, observed_at=T3)

    assert newer.catalog_version == version.version_number
    assert newer.postings[0].outcome is Outcome.UPDATED
    job = fresh(db_session, job)
    assert job.seniority_catalog_version == version.version_number
    assert job.seniority_resolution_status == ResolutionStatus.RESOLVED.value
    assert job.seniority_resolved_level_name == "Senior"
    assert job.fingerprint == fingerprint


def test_direct_apply_status_is_re_derived_only_when_the_url_changes(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    source = declaration()
    url = "https://example.test/apply/1"
    ingest(factory, redis_client, source, raw(company, direct_apply_url=url))
    [job] = postings_for(db_session, company)
    assert job.direct_apply_verification_status == "unverified"

    # Simulate a later verification step (Phase 4).
    db_session.execute(
        update(JobPosting)
        .where(JobPosting.id == job.id)
        .values(direct_apply_verification_status="verified")
    )
    ingest(
        factory,
        redis_client,
        source,
        raw(company, direct_apply_url=url, description_raw="Edited."),
        observed_at=T2,
    )
    assert fresh(db_session, job).direct_apply_verification_status == "verified"

    ingest(
        factory,
        redis_client,
        source,
        raw(company, direct_apply_url="https://example.test/apply/2"),
        observed_at=T3,
    )
    assert fresh(db_session, job).direct_apply_verification_status == "unverified"


# --- ownership across sources -------------------------------------------------------


@pytest.mark.invariant
def test_non_owner_is_attached_and_its_differences_are_only_reported(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    owner = ingest(factory, redis_client, declaration(), raw(company))
    other_source = declaration()
    other = raw(
        company,
        "b-77",
        description_raw="A different description.",
        mentions=[{"mention_type": "required", "raw_text": "Linux"}],
    )

    attached = ingest(factory, redis_client, other_source, other, observed_at=T2)
    again = ingest(factory, redis_client, other_source, other, observed_at=T3)

    assert attached.postings[0].outcome is Outcome.ATTACHED
    assert attached.postings[0].content_differs and attached.postings[0].mentions_differ
    assert again.postings[0].outcome is Outcome.UNCHANGED
    assert again.postings[0].content_differs and again.postings[0].mentions_differ

    [job] = postings_for(db_session, company)  # invariant 28: one canonical job
    assert job.owner_job_source_id == owner.job_source_id
    assert job.description_raw == "Run the platform."
    assert [row.raw_text for row in mentions(db_session, job.id)] == ["Docker"]
    assert len(observations(db_session, job.id)) == 2


def test_older_fixture_from_another_source_cannot_take_ownership(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    owner = ingest(factory, redis_client, declaration(), raw(company), observed_at=T2)
    late = ingest(factory, redis_client, declaration(), raw(company, "b-1"), observed_at=T1)
    assert late.postings[0].outcome is Outcome.ATTACHED
    [job] = postings_for(db_session, company)
    assert job.owner_job_source_id == owner.job_source_id


# --- rejections -----------------------------------------------------------------------


def test_identity_change_for_a_known_source_job_id_is_rejected(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    source = declaration()
    ingest(factory, redis_client, source, raw(company))
    report = ingest(
        factory, redis_client, source, raw(company, title="Chief Platform Officer"), observed_at=T2
    )
    assert report.postings[0].outcome is Outcome.REJECTED
    assert "different job" in report.postings[0].reasons[0]
    [job] = postings_for(db_session, company)
    assert job.title == "Senior Platform Engineer"


def test_two_ids_for_one_job_in_one_fixture_are_all_rejected(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    report = ingest(
        factory,
        redis_client,
        declaration(),
        raw(company, "ext-1"),
        raw(company, "ext-2"),
        raw(company, "ext-3", title="Staff Engineer"),
    )
    assert [p.outcome for p in report.postings] == [
        Outcome.REJECTED,
        Outcome.REJECTED,
        Outcome.CREATED,
    ]
    assert [job.title for job in postings_for(db_session, company)] == ["Staff Engineer"]


def test_a_second_id_for_an_already_stored_job_is_rejected(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    source = declaration()
    ingest(factory, redis_client, source, raw(company, "ext-1"))
    report = ingest(factory, redis_client, source, raw(company, "ext-2"), observed_at=T2)
    assert report.postings[0].outcome is Outcome.REJECTED
    assert "already lists the job as 'ext-1'" in report.postings[0].reasons[0]
    [job] = postings_for(db_session, company)
    assert len(observations(db_session, job.id)) == 1


# --- source resolution ------------------------------------------------------------------


def test_source_is_created_once_and_reused(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    source = declaration()
    first = ingest(factory, redis_client, source, raw(company))
    second = ingest(factory, redis_client, source, raw(company), observed_at=T2)
    assert first.job_source_id == second.job_source_id
    rows = (
        db_session.execute(select(JobSource).where(JobSource.name == source["name"]))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].is_active and rows[0].last_crawled_at is None


def test_a_different_base_url_is_a_different_source(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    source = declaration()
    first = ingest(factory, redis_client, source, raw(company))
    elsewhere = {**source, "base_url": "https://mirror.example.test"}
    second = ingest(factory, redis_client, elsewhere, raw(company, "m-1"), observed_at=T2)
    assert first.job_source_id != second.job_source_id
    assert second.postings[0].outcome is Outcome.ATTACHED


def _add_source(session: Session, declared: dict[str, Any], *, active: bool = True) -> None:
    session.add(
        JobSource(
            name=declared["name"],
            base_url=declared["base_url"],
            type=JobSourceType(declared["type"]).value,
            is_active=active,
        )
    )
    session.flush()


@pytest.mark.parametrize("case", ["ambiguous", "inactive"])
def test_ambiguous_or_inactive_source_refuses_everything(
    db_session: Session,
    factory: Callable[[], Session],
    redis_client: redis.Redis,
    company: str,
    case: str,
) -> None:
    source = declaration()
    _add_source(db_session, source, active=case != "inactive")
    if case == "ambiguous":
        _add_source(db_session, source)

    report = ingest(factory, redis_client, source, raw(company))

    assert report.refused and report.postings == ()
    assert case in report.refused[0]
    assert postings_for(db_session, company) == []


# --- refusals and rollback ----------------------------------------------------------------


@pytest.mark.parametrize(
    "payload_change",
    [{"extra": 1}, {"source": {"name": " ", "base_url": "x", "type": "job_board"}}],
)
def test_invalid_fixture_or_source_writes_nothing(
    db_session: Session,
    factory: Callable[[], Session],
    redis_client: redis.Redis,
    company: str,
    payload_change: dict[str, Any],
) -> None:
    payload = {"source": declaration(), "postings": [raw(company)], **payload_change}
    report = ingest_fixture(
        factory, validate_fixture(payload), observed_at=T1, redis_client=redis_client
    )
    assert report.refused
    assert postings_for(db_session, company) == []


def test_naive_observed_at_is_rejected_before_any_write(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    fixture = validate_fixture({"source": declaration(), "postings": [raw(company)]})
    with pytest.raises(ValueError, match="timezone-aware"):
        ingest_fixture(
            factory, fixture, observed_at=datetime(2026, 10, 1), redis_client=redis_client
        )
    assert postings_for(db_session, company) == []


def test_expected_conflict_rejects_only_its_posting(
    db_session: Session,
    factory: Callable[[], Session],
    redis_client: redis.Redis,
    company: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backstop for a bypassed lock: uq_job_posting_source_identity."""
    source = declaration()
    ingest(factory, redis_client, source, raw(company, "ext-1"))
    # Blind the lookup, as a concurrent writer the lock missed would be.
    monkeypatch.setattr(job_ingestion, "_find_observation", lambda *args: None)

    report = ingest(
        factory,
        redis_client,
        source,
        raw(company, "ext-1", title="Renamed Role"),
        raw(company, "ext-9", title="Brand New Role"),
        observed_at=T2,
    )

    outcomes = {p.index: p for p in report.postings}
    assert outcomes[0].outcome is Outcome.REJECTED
    assert outcomes[0].reasons[0].startswith("uq_job_posting_source_identity")
    assert outcomes[1].outcome is Outcome.CREATED
    titles = sorted(job.title for job in postings_for(db_session, company))
    # The rejected posting's own INSERT was rolled back with its savepoint.
    assert titles == ["Brand New Role", "Senior Platform Engineer"]


def test_unexpected_integrity_error_rolls_back_the_whole_operation(
    db_session: Session,
    factory: Callable[[], Session],
    redis_client: redis.Redis,
    company: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def incoherent(raw_text: str, lookup: object) -> SkillResolution:
        # "resolved" with no identity violates ck_..._resolved_requires_identity.
        return SkillResolution(
            raw_text=raw_text,
            normalized_representation=raw_text.casefold(),
            status=ResolutionStatus.RESOLVED,
            provenance=ResolutionProvenance.EXACT_MATCH,
        )

    monkeypatch.setattr(job_ingestion, "resolve_skill", incoherent)
    source = declaration()
    with pytest.raises(IntegrityError, match="resolved_requires_identity"):
        ingest(
            factory,
            redis_client,
            source,
            raw(company, "ext-1", mentions=[]),
            raw(company, "ext-2", title="Other"),
        )
    assert postings_for(db_session, company) == []
    assert (
        db_session.scalar(
            select(func.count()).select_from(JobSource).where(JobSource.name == source["name"])
        )
        == 0
    )


def test_operational_error_rolls_back_the_whole_operation(
    db_session: Session,
    factory: Callable[[], Session],
    redis_client: redis.Redis,
    company: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = job_ingestion._insert_observation
    calls = {"n": 0}

    def flaky(*args: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OperationalError("INSERT", {}, Exception("connection lost"))
        real(*args)

    monkeypatch.setattr(job_ingestion, "_insert_observation", flaky)
    with pytest.raises(OperationalError):
        ingest(
            factory,
            redis_client,
            declaration(),
            raw(company, "ext-1"),
            raw(company, "ext-2", title="Other"),
        )
    assert postings_for(db_session, company) == []


def test_lock_timeout_writes_nothing(
    db_session: Session, factory: Callable[[], Session], redis_client: redis.Redis, company: str
) -> None:
    source = declaration()
    fixture = validate_fixture({"source": source, "postings": [raw(company)]})
    assert fixture.source is not None
    key = ingestion_lock_key(fixture.source)
    redis_client.set(key, "held-elsewhere", px=30_000)
    try:
        with pytest.raises(LockAcquisitionTimeoutError):
            ingest_fixture(factory, fixture, observed_at=T1, redis_client=redis_client)
    finally:
        redis_client.delete(key)
    assert postings_for(db_session, company) == []


def test_lock_is_scoped_to_the_source_declaration() -> None:
    fixture = validate_fixture({"source": declaration("A"), "postings": []})
    other = validate_fixture({"source": declaration("B"), "postings": []})
    assert fixture.source is not None and other.source is not None
    assert ingestion_lock_key(fixture.source) != ingestion_lock_key(other.source)
    assert "job_ingestion" in ingestion_lock_key(fixture.source)


# --- concurrency (real commits) ----------------------------------------------------------
#
# Each test forces the interleaving it is about instead of hoping threads
# overlap: run sequentially, the old barrier-based versions passed too.
# Waits are on conditions (a PostgreSQL lock wait, a Redis lock attempt,
# an Event), each bounded, never a fixed sleep.

WAIT_TIMEOUT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.01


class Worker:
    """Runs one ingestion in a thread and hands back its report or error."""

    def __init__(self, target: Callable[[], IngestionReport]) -> None:
        self.result: IngestionReport | None = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, args=(target,), daemon=True)

    def _run(self, target: Callable[[], IngestionReport]) -> None:
        try:
            self.result = target()
        except BaseException as exc:  # noqa: BLE001 - re-raised in finish()
            self.error = exc

    def start(self) -> "Worker":
        self.thread.start()
        return self

    def finish(self) -> IngestionReport:
        self.thread.join(timeout=WAIT_TIMEOUT_SECONDS)
        assert not self.thread.is_alive(), "worker did not finish within its timeout"
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def wait_until(condition: Callable[[], bool], worker: Worker, what: str) -> None:
    """Poll `condition`, failing fast if the worker finishes first."""
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while not condition():
        assert worker.thread.is_alive(), f"worker finished before {what}"
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        # Doubles as the poll interval and returns at once if the worker exits.
        worker.thread.join(timeout=POLL_INTERVAL_SECONDS)


@pytest.fixture
def committing_factory(migrated_engine: Engine, company: str) -> Iterator[sessionmaker[Session]]:
    """Committing sessions, with the committed rows removed afterwards.

    Other tests count rows table-wide, so nothing committed here may
    outlive the test. Deleting the postings cascades to their
    observations and mentions; the sources, owners included, go last.
    """
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False)
    yield factory
    with factory() as session:
        posting_ids = select(JobPosting.id).where(JobPosting.company == company)
        source_ids = set(
            session.scalars(
                select(JobPostingSource.job_source_id).where(
                    JobPostingSource.job_posting_id.in_(posting_ids)
                )
            )
        )
        source_ids |= set(
            session.scalars(
                select(JobPosting.owner_job_source_id).where(JobPosting.company == company)
            )
        )
        session.execute(delete(JobPosting).where(JobPosting.company == company))
        session.execute(delete(JobSource).where(JobSource.id.in_(source_ids)))
        session.commit()


def _blocked_by(engine: Engine, holder_pid: int) -> bool:
    """Whether some backend is waiting on a lock held by `holder_pid`.

    A fresh connection per call: statistics views are snapshotted per
    transaction, and pg_blocking_pids() is evaluated live.
    """
    with engine.connect() as connection:
        return bool(
            connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks "
                    "WHERE NOT granted AND :holder = ANY(pg_blocking_pids(pid)))"
                ),
                {"holder": holder_pid},
            ).scalar_one()
        )


@pytest.mark.invariant
@pytest.mark.parametrize("holder_ends", ["commit", "rollback"])
def test_creation_race_waits_on_the_uncommitted_insert_and_follows_its_outcome(
    migrated_engine: Engine,
    committing_factory: sessionmaker[Session],
    redis_client: redis.Redis,
    company: str,
    holder_ends: str,
) -> None:
    """Another source holds an uncommitted posting with the same dedupe_key.

    The ingestion's INSERT ... ON CONFLICT must WAIT on it. If the holder
    commits, the ingestion attaches and the holder's source owns the job;
    if it rolls back, the ingestion creates the job and owns it.
    """
    competing = raw(company, "b-1")
    holder = committing_factory()
    worker: Worker | None = None
    try:
        owner_a = JobSource(
            name=f"Holder {uuid.uuid4().hex[:8]}",
            base_url="https://a.test",
            type=JobSourceType.JOB_BOARD.value,
        )
        holder.add(owner_a)
        holder.flush()
        holder.add(
            JobPosting(
                owner_job_source_id=owner_a.id,
                title=competing["title"],
                company=company,
                location=competing["location"],
                dedupe_key=build_dedupe_key(company, competing["title"], competing["location"]),
                fingerprint="held",
            )
        )
        holder.flush()
        holder_pid = holder.execute(text("SELECT pg_backend_pid()")).scalar_one()

        source_b = declaration()
        worker = Worker(
            partial(ingest, committing_factory, redis_client, source_b, competing)
        ).start()
        wait_until(
            lambda: _blocked_by(migrated_engine, holder_pid),
            worker,
            "the ingestion to wait on the held insert",
        )

        if holder_ends == "commit":
            holder.commit()
        else:
            holder.rollback()
        report = worker.finish()
    finally:
        holder.rollback()  # no-op once committed; unblocks the worker on failure
        holder.close()
        if worker is not None:
            worker.thread.join(timeout=WAIT_TIMEOUT_SECONDS)
            assert not worker.thread.is_alive(), "worker did not finish within its timeout"

    with committing_factory() as session:
        [job] = postings_for(session, company)
        [observation] = observations(session, job.id)
        assert observation.job_source_id == report.job_source_id
        if holder_ends == "commit":
            assert report.postings[0].outcome is Outcome.ATTACHED
            assert job.owner_job_source_id == owner_a.id
        else:
            assert report.postings[0].outcome is Outcome.CREATED
            assert job.owner_job_source_id == report.job_source_id


@pytest.mark.invariant
def test_same_source_operations_serialize_on_the_ingestion_lock(
    committing_factory: sessionmaker[Session],
    redis_client: redis.Redis,
    company: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second operation must try, and fail, to take the lock while the
    first holds it, and may only start its work after the first commits."""
    source = declaration()
    fixture = validate_fixture({"source": source, "postings": []})
    assert fixture.source is not None
    key = ingestion_lock_key(fixture.source)

    order: list[str] = []
    order_guard = threading.Lock()
    first_inside = threading.Event()
    release_first = threading.Event()
    contended = threading.Event()
    attempts: list[bool] = []

    real_set = redis_client.set

    def counting_set(name: Any, value: Any, *args: Any, **kwargs: Any) -> Any:
        acquired = real_set(name, value, *args, **kwargs)
        if name == key and kwargs.get("nx"):
            attempts.append(bool(acquired))
            if len(attempts) >= 2:
                contended.set()
        return acquired

    real_ingest_postings = job_ingestion._ingest_postings

    def held_ingest_postings(*args: Any) -> Any:
        with order_guard:
            label = "first" if "first:enter" not in order else "second"
            order.append(f"{label}:enter")
        if label == "first":
            first_inside.set()
            assert release_first.wait(timeout=WAIT_TIMEOUT_SECONDS), "never released"
        result = real_ingest_postings(*args)
        with order_guard:
            order.append(f"{label}:exit")
        return result

    monkeypatch.setattr(redis_client, "set", counting_set)
    monkeypatch.setattr(job_ingestion, "_ingest_postings", held_ingest_postings)

    def run() -> IngestionReport:
        return ingest(committing_factory, redis_client, source, raw(company))

    first: Worker | None = None
    second: Worker | None = None
    try:
        first = Worker(run).start()
        assert first_inside.wait(timeout=WAIT_TIMEOUT_SECONDS), "first never took the lock"
        second = Worker(run).start()
        assert contended.wait(timeout=WAIT_TIMEOUT_SECONDS), "second never tried the lock"
        assert attempts[:2] == [True, False]  # first acquired; second was refused
        assert order == ["first:enter"]
        release_first.set()
        first_report = first.finish()
        second_report = second.finish()
    finally:
        release_first.set()  # never leave the first worker parked
        for worker in (first, second):
            if worker is not None:
                worker.thread.join(timeout=WAIT_TIMEOUT_SECONDS)
                assert not worker.thread.is_alive(), "worker did not finish within its timeout"

    assert order == ["first:enter", "first:exit", "second:enter", "second:exit"]
    assert first_report.postings[0].outcome is Outcome.CREATED
    assert second_report.postings[0].outcome is Outcome.UNCHANGED
    assert first_report.job_source_id == second_report.job_source_id
    with committing_factory() as session:
        [job] = postings_for(session, company)
        assert job.owner_job_source_id == first_report.job_source_id
        assert len(observations(session, job.id)) == 1
        assert (
            session.scalar(
                select(func.count()).select_from(JobSource).where(JobSource.name == source["name"])
            )
            == 1
        )
