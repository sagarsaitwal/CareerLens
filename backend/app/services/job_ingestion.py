"""Persist a validated job fixture (DATABASE.md Sections 2.14-2.15.2, 4.3).

Input is a `FixtureResult` from app/schemas/job_ingestion.py, so every
posting reaching here has already passed the contract. This module adds
what the contract cannot know: what is already stored.

One operation:

  1. Refuse, writing nothing, when the fixture or its source is invalid.
  2. Under a Redis lock scoped to the source declaration, open ONE
     session and pin ONE catalog version (invariant 29).
  3. Resolve the source by exact (name, base_url, type): create when
     absent, use exactly one active match, refuse an inactive or
     ambiguous one. `last_crawled_at` is never written: a fixture run is
     not a crawl.
  4. Process each posting in its own savepoint, in dedupe_key order so
     concurrent operations wait on keys in the same order and cannot
     deadlock.
  5. Commit inside the lock.

Ownership: the source whose INSERT creates a canonical posting owns it,
and only the owner may change its content or replace its mentions.
Another source's differing content is reported, never stored.

Only the two expected uniqueness conflicts reject a single posting;
they arise only if the lock was bypassed. Any other database error
rolls back the whole operation and propagates.

Writes are Core statements, not ORM objects, so a rolled-back savepoint
leaves no half-added objects in the session. Nothing here calls a
model: resolution is the deterministic cascade only, so `ai_semantic`
cannot be produced.
"""

import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

import redis
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.locking import distributed_lock, lock_key
from app.domain.jobs import build_dedupe_key, build_fingerprint
from app.domain.resolution import resolve_skill
from app.domain.seniority import resolve_seniority
from app.models.enums import ResolutionProvenance
from app.models.job import JobPosting, JobPostingSource, JobSkillMention, JobSource
from app.repositories.catalog import CatalogSnapshot
from app.schemas.job_ingestion import (
    FixtureResult,
    MentionInput,
    PostingInput,
    PostingResult,
    SourceDeclaration,
)

# Comfortably above a large fixture. distributed_lock has no renewal, so
# an expiry mid-operation falls back to the database constraints.
INGESTION_LOCK_TTL_MS: Final[int] = 300_000

# The ONLY conflicts that reject a single posting. Both are on
# JobPostingSource and can fire only if the source lock was bypassed.
EXPECTED_CONFLICTS: Final[frozenset[str]] = frozenset(
    {"uq_job_posting_source_identity", "uq_job_posting_source_pair"}
)

_SENIORITY_COLUMNS: Final[tuple[str, ...]] = (
    "seniority_normalized",
    "seniority_level_id",
    "seniority_resolution_status",
    "seniority_resolution_provenance",
    "seniority_resolved_level_name",
    "seniority_resolved_track",
    "seniority_evidence_source",
    "seniority_catalog_version",
)
# Canonical fields outside the fingerprint; the same ownership applies.
_OWNED_COLUMNS: Final[tuple[str, ...]] = (
    "salary_min",
    "salary_max",
    "salary_currency",
    "posting_date",
    "status",
    "extraction_status",
)


class Outcome(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    ATTACHED = "attached"
    REJECTED = "rejected"


@dataclass(frozen=True)
class PostingOutcome:
    index: int
    outcome: Outcome
    job_posting_id: uuid.UUID | None = None
    reasons: tuple[str, ...] = ()
    # Report-only: a non-owner's differing content is never stored.
    content_differs: bool = False
    mentions_differ: bool = False
    # observed_at was earlier than the stored last_seen_at.
    stale_observation: bool = False


@dataclass(frozen=True)
class IngestionReport:
    """`refused` non-empty means the operation wrote nothing."""

    refused: tuple[str, ...]
    job_source_id: uuid.UUID | None
    catalog_version: int | None
    postings: tuple[PostingOutcome, ...]


def ingestion_lock_key(source: SourceDeclaration) -> str:
    """Keyed on the declaration: the JobSource row may not exist yet."""
    identity = json.dumps([source.name, source.base_url, source.type.value])
    return lock_key("job_ingestion", hashlib.sha256(identity.encode("utf-8")).hexdigest())


def ingest_fixture(
    session_factory: Callable[[], Session],
    fixture: FixtureResult,
    *,
    observed_at: datetime,
    redis_client: redis.Redis,
) -> IngestionReport:
    """Persist one validated fixture as one operation.

    Raises ValueError for a naive `observed_at`, LockAcquisitionTimeoutError
    when the source lock is held elsewhere, and any unexpected database
    error; in every case nothing is written.
    """
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    observed_at = observed_at.astimezone(UTC)

    if fixture.source is None or fixture.errors:
        return IngestionReport(fixture.errors or ("source: invalid",), None, None, ())

    with (
        distributed_lock(
            redis_client, ingestion_lock_key(fixture.source), ttl_ms=INGESTION_LOCK_TTL_MS
        ),
        session_factory() as session,
    ):
        catalog = CatalogSnapshot.current(session)
        version = catalog.version.version_number
        source_id, refusal = _resolve_source(session, fixture.source)
        if source_id is None:
            # Leaving the block without commit rolls back.
            return IngestionReport((refusal,), None, version, ())

        outcomes = _ingest_postings(session, source_id, fixture.postings, catalog, observed_at)
        session.commit()

    return IngestionReport((), source_id, version, outcomes)


def _resolve_source(session: Session, declared: SourceDeclaration) -> tuple[uuid.UUID | None, str]:
    rows = session.execute(
        select(JobSource.id, JobSource.is_active).where(
            JobSource.name == declared.name,
            JobSource.base_url == declared.base_url,
            JobSource.type == declared.type.value,
        )
    ).all()
    if len(rows) > 1:
        return None, f"source: ambiguous, {len(rows)} JobSource rows match exactly"
    if rows:
        if not rows[0].is_active:
            return None, "source: the matching JobSource is inactive"
        return rows[0].id, ""
    created = session.execute(
        insert(JobSource)
        .values(
            id=uuid.uuid4(),
            name=declared.name,
            base_url=declared.base_url,
            type=declared.type.value,
            is_active=True,
        )
        .returning(JobSource.id)
    ).scalar_one()
    return created, ""


def _ingest_postings(
    session: Session,
    source_id: uuid.UUID,
    results: tuple[PostingResult, ...],
    catalog: CatalogSnapshot,
    observed_at: datetime,
) -> tuple[PostingOutcome, ...]:
    outcomes: dict[int, PostingOutcome] = {}
    accepted: list[tuple[str, PostingResult]] = []
    for result in results:
        if result.posting is None:
            outcomes[result.index] = PostingOutcome(
                result.index, Outcome.REJECTED, reasons=result.errors
            )
            continue
        key = build_dedupe_key(
            result.posting.company, result.posting.title, result.posting.location
        )
        accepted.append((key, result))

    # One source supplying several ids for one canonical job: reject all.
    by_key: defaultdict[str, list[PostingResult]] = defaultdict(list)
    for key, result in accepted:
        by_key[key].append(result)
    for group in by_key.values():
        if len(group) > 1:
            indices = ", ".join(str(result.index) for result in group)
            for result in group:
                outcomes[result.index] = PostingOutcome(
                    result.index,
                    Outcome.REJECTED,
                    reasons=(
                        f"dedupe_key: postings {indices} describe the same job "
                        "under different source_job_ids",
                    ),
                )

    for key, result in sorted(accepted, key=lambda item: item[0]):
        if result.index in outcomes:
            continue
        try:
            with session.begin_nested():
                outcomes[result.index] = _ingest_one(
                    session, source_id, key, result, catalog, observed_at
                )
        except IntegrityError as error:
            constraint = _constraint_name(error)
            if constraint not in EXPECTED_CONFLICTS:
                raise
            outcomes[result.index] = PostingOutcome(
                result.index,
                Outcome.REJECTED,
                reasons=(f"{constraint}: conflicting concurrent write",),
            )
    return tuple(outcomes[index] for index in sorted(outcomes))


def _constraint_name(error: IntegrityError) -> str | None:
    diag = getattr(error.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None


def _find_observation(session: Session, source_id: uuid.UUID, source_job_id: str) -> Any | None:
    return session.execute(
        select(
            JobPostingSource.id,
            JobPostingSource.last_seen_at,
            JobPosting.id.label("posting_id"),
            JobPosting.dedupe_key,
            JobPosting.owner_job_source_id,
        )
        .join(JobPosting, JobPosting.id == JobPostingSource.job_posting_id)
        .where(
            JobPostingSource.job_source_id == source_id,
            JobPostingSource.source_job_id == source_job_id,
        )
    ).one_or_none()


def _ingest_one(
    session: Session,
    source_id: uuid.UUID,
    dedupe_key: str,
    result: PostingResult,
    catalog: CatalogSnapshot,
    observed_at: datetime,
) -> PostingOutcome:
    posting = result.posting
    assert posting is not None
    index = result.index
    fingerprint = _fingerprint(posting)

    known = _find_observation(session, source_id, posting.source_job_id)
    if known is not None:
        if known.dedupe_key != dedupe_key:
            return PostingOutcome(
                index,
                Outcome.REJECTED,
                known.posting_id,
                reasons=(
                    f"source_job_id: {posting.source_job_id!r} previously described a "
                    "different job; identity changes are rejected",
                ),
            )
        session.execute(
            update(JobPostingSource)
            .where(JobPostingSource.id == known.id)
            .values(
                # Monotonic; first_seen_at is never written after insert.
                last_seen_at=func.greatest(JobPostingSource.last_seen_at, observed_at),
                source_url=posting.source_url,
            )
        )
        stale = observed_at < known.last_seen_at
        if known.owner_job_source_id == source_id:
            changed = _apply_owner_content(session, known.posting_id, posting, fingerprint, catalog)
            changed |= _replace_mentions_if_changed(
                session, known.posting_id, result.mentions, catalog
            )
            outcome = Outcome.UPDATED if changed else Outcome.UNCHANGED
            return PostingOutcome(index, outcome, known.posting_id, stale_observation=stale)
        content, mentions = _differences(session, known.posting_id, fingerprint, result.mentions)
        return PostingOutcome(
            index,
            Outcome.UNCHANGED,
            known.posting_id,
            content_differs=content,
            mentions_differ=mentions,
            stale_observation=stale,
        )

    # Ownership is fixed by the INSERT that wins: a concurrent creator
    # makes this a no-op, and the re-read below then attaches.
    created = session.execute(
        pg_insert(JobPosting)
        .values(
            id=uuid.uuid4(),
            owner_job_source_id=source_id,
            dedupe_key=dedupe_key,
            fingerprint=fingerprint,
            **_content_values(posting, catalog),
            **_owned_values(posting),
            direct_apply_verification_status=posting.direct_apply_verification_status.value,
        )
        .on_conflict_do_nothing(constraint="uq_job_posting_dedupe_key")
        .returning(JobPosting.id)
    ).scalar_one_or_none()

    if created is not None:
        _insert_observation(session, created, source_id, posting, observed_at)
        _insert_mentions(session, created, result.mentions, catalog)
        return PostingOutcome(index, Outcome.CREATED, created)

    existing = session.execute(
        select(JobPosting.id).where(JobPosting.dedupe_key == dedupe_key)
    ).scalar_one()
    already_listed = session.execute(
        select(JobPostingSource.source_job_id).where(
            JobPostingSource.job_posting_id == existing,
            JobPostingSource.job_source_id == source_id,
        )
    ).scalar_one_or_none()
    if already_listed is not None:
        return PostingOutcome(
            index,
            Outcome.REJECTED,
            existing,
            reasons=(
                f"dedupe_key: this source already lists the job as {already_listed!r}; "
                "one source supplying several ids for one job is rejected",
            ),
        )
    _insert_observation(session, existing, source_id, posting, observed_at)
    content, mentions = _differences(session, existing, fingerprint, result.mentions)
    return PostingOutcome(
        index,
        Outcome.ATTACHED,
        existing,
        content_differs=content,
        mentions_differ=mentions,
    )


def _fingerprint(posting: PostingInput) -> str:
    return build_fingerprint(
        title=posting.title,
        company=posting.company,
        location=posting.location,
        work_mode=_value(posting.work_mode),
        employment_type=posting.employment_type.value,
        seniority=posting.seniority.raw_text if posting.seniority else None,
        experience_requirements=posting.experience_requirements,
        description_raw=posting.description_raw,
        official_url=posting.official_url,
        direct_apply_url=posting.direct_apply_url,
    )


def _value(member: StrEnum | None) -> str | None:
    # Plain strings for the driver; enum members are not passed through.
    return None if member is None else member.value


def _content_values(posting: PostingInput, catalog: CatalogSnapshot) -> dict[str, Any]:
    """Fingerprint fields plus the seniority they imply, pinned to `catalog`."""
    values: dict[str, Any] = {
        "title": posting.title,
        "company": posting.company,
        "location": posting.location,
        "work_mode": _value(posting.work_mode),
        "employment_type": posting.employment_type.value,
        "seniority_raw_text": posting.seniority.raw_text if posting.seniority else None,
        "experience_requirements": posting.experience_requirements,
        "description_raw": posting.description_raw,
        "official_url": posting.official_url,
        "direct_apply_url": posting.direct_apply_url,
    }
    values.update(_seniority_values(posting, catalog))
    return values


def _seniority_values(posting: PostingInput, catalog: CatalogSnapshot) -> dict[str, Any]:
    """Every seniority resolution column, resolved against `catalog`; all NULL if absent."""
    values: dict[str, Any] = dict.fromkeys(_SENIORITY_COLUMNS)
    if posting.seniority is None:
        return values
    resolution = resolve_seniority(
        posting.seniority.raw_text,
        catalog.seniority,
        posting.seniority.evidence_source,
    )
    _require_deterministic(resolution.provenance)
    values.update(
        seniority_normalized=resolution.normalized_representation,
        seniority_level_id=resolution.level_id,
        seniority_resolution_status=resolution.status.value,
        seniority_resolution_provenance=resolution.provenance.value,
        seniority_resolved_level_name=resolution.resolved_level_name,
        seniority_resolved_track=_value(resolution.resolved_track),
        seniority_evidence_source=resolution.evidence_source.value,
        seniority_catalog_version=catalog.version.version_number,
    )
    return values


def _owned_values(posting: PostingInput) -> dict[str, Any]:
    return {
        "salary_min": posting.salary_min,
        "salary_max": posting.salary_max,
        "salary_currency": posting.salary_currency,
        "posting_date": posting.posting_date,
        "status": posting.status.value,
        "extraction_status": posting.extraction_status.value,
    }


def _apply_owner_content(
    session: Session,
    posting_id: uuid.UUID,
    posting: PostingInput,
    fingerprint: str,
    catalog: CatalogSnapshot,
) -> bool:
    """Owner only. Writes nothing when nothing differs."""
    stored = session.execute(
        select(
            JobPosting.fingerprint,
            JobPosting.direct_apply_url,
            JobPosting.seniority_catalog_version,
            *(getattr(JobPosting, column) for column in _OWNED_COLUMNS),
        ).where(JobPosting.id == posting_id)
    ).one()

    changes: dict[str, Any] = {
        column: value
        for column, value in _owned_values(posting).items()
        if getattr(stored, column) != value
    }
    if stored.fingerprint != fingerprint:
        changes.update(_content_values(posting, catalog), fingerprint=fingerprint)
        # Re-derived only when the URL itself changes, so a future
        # `verified` status survives an unrelated content edit.
        if stored.direct_apply_url != posting.direct_apply_url:
            changes["direct_apply_verification_status"] = (
                posting.direct_apply_verification_status.value
            )
    elif (
        posting.seniority is not None
        and stored.seniority_catalog_version != catalog.version.version_number
    ):
        # Same wording, newer catalog: re-resolve so seniority and the
        # mentions stay pinned to one catalog version, as mentions are.
        changes.update(_seniority_values(posting, catalog))
    if not changes:
        return False
    session.execute(update(JobPosting).where(JobPosting.id == posting_id).values(**changes))
    return True


def _stored_mentions(session: Session, posting_id: uuid.UUID) -> list[Any]:
    return list(
        session.execute(
            select(
                JobSkillMention.mention_type,
                JobSkillMention.normalized_representation,
                JobSkillMention.catalog_version_at_resolution,
            ).where(JobSkillMention.job_posting_id == posting_id)
        ).all()
    )


def _mention_set(mentions: tuple[MentionInput, ...]) -> set[tuple[str, str]]:
    # raw_text deliberately excluded: a spelling-only change is not applied.
    return {(m.mention_type.value, m.normalized_representation) for m in mentions}


def _replace_mentions_if_changed(
    session: Session,
    posting_id: uuid.UUID,
    mentions: tuple[MentionInput, ...],
    catalog: CatalogSnapshot,
) -> bool:
    """Owner only: replace when the set or the pinned catalog version changed."""
    stored = _stored_mentions(session, posting_id)
    stored_set = {(row.mention_type, row.normalized_representation) for row in stored}
    version = catalog.version.version_number
    if stored_set == _mention_set(mentions) and all(
        row.catalog_version_at_resolution == version for row in stored
    ):
        return False
    session.execute(delete(JobSkillMention).where(JobSkillMention.job_posting_id == posting_id))
    _insert_mentions(session, posting_id, mentions, catalog)
    return True


def _insert_mentions(
    session: Session,
    posting_id: uuid.UUID,
    mentions: tuple[MentionInput, ...],
    catalog: CatalogSnapshot,
) -> None:
    rows = []
    for mention in mentions:
        resolution = resolve_skill(mention.raw_text, catalog.skills)
        _require_deterministic(resolution.provenance)
        rows.append(
            {
                "id": uuid.uuid4(),
                "job_posting_id": posting_id,
                "mention_type": mention.mention_type.value,
                "raw_text": mention.raw_text,
                "normalized_representation": resolution.normalized_representation,
                "skill_id": resolution.skill_id,
                "resolution_status": resolution.status.value,
                "resolution_provenance": resolution.provenance.value,
                "resolved_canonical_name": resolution.resolved_canonical_name,
                "catalog_version_at_resolution": catalog.version.version_number,
            }
        )
    if rows:
        session.execute(insert(JobSkillMention), rows)


def _differences(
    session: Session,
    posting_id: uuid.UUID,
    fingerprint: str,
    mentions: tuple[MentionInput, ...],
) -> tuple[bool, bool]:
    """Non-owner: what differs from the canonical record. Reported, never stored."""
    stored_fingerprint = session.execute(
        select(JobPosting.fingerprint).where(JobPosting.id == posting_id)
    ).scalar_one()
    stored_set = {
        (row.mention_type, row.normalized_representation)
        for row in _stored_mentions(session, posting_id)
    }
    return stored_fingerprint != fingerprint, stored_set != _mention_set(mentions)


def _insert_observation(
    session: Session,
    posting_id: uuid.UUID,
    source_id: uuid.UUID,
    posting: PostingInput,
    observed_at: datetime,
) -> None:
    session.execute(
        insert(JobPostingSource).values(
            id=uuid.uuid4(),
            job_posting_id=posting_id,
            job_source_id=source_id,
            source_job_id=posting.source_job_id,
            source_url=posting.source_url,
            first_seen_at=observed_at,
            last_seen_at=observed_at,
        )
    )


def _require_deterministic(provenance: ResolutionProvenance) -> None:
    # The cascade cannot produce it; this keeps that true if it ever could.
    if provenance is ResolutionProvenance.AI_SEMANTIC:
        raise RuntimeError("ai_semantic must be unreachable on the ingestion path")
