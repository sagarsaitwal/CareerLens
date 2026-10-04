"""Concurrent ProfileVersion minting.

These commit for real, outside the rolled-back `db_session` fixture:
the race being tested is between transactions, so it cannot be observed
inside one.

Both halves of the documented guarantee are covered — the Redis lock
that makes concurrent callers converge on one outcome, and the
PostgreSQL uniqueness constraint that is the backstop when the lock is
bypassed.
"""

import threading
import uuid

import pytest
import redis
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.versions import PROFILE_SNAPSHOT_SCHEMA_VERSION
from app.models.profile import ProfileVersion, UserProfile
from app.repositories.catalog import CatalogSnapshot
from app.services.profile import mint_version_with_lock, profile_version_lock_key

pytestmark = pytest.mark.integration

THREADS = 8


@pytest.fixture
def session_factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=migrated_engine, expire_on_commit=False)


@pytest.fixture
def committed_profile(
    session_factory: sessionmaker[Session],
) -> tuple[uuid.UUID, int]:
    """A profile that really exists in the database.

    Deliberately not torn down. ProfileVersion is append-only, so a
    cleanup DELETE would be rejected by the same trigger that protects
    the invariant — and cascading from the profile would hit it too.
    Each run gets a fresh label and id, every assertion below is scoped
    to that id, and `migrated_engine` rebuilds the schema per session,
    so the rows left behind cannot reach another test.
    """
    with session_factory() as session:
        profile = UserProfile(
            label=f"Concurrency {uuid.uuid4().hex[:8]}",
            experience_years=9,
            preferred_locations=["Pune"],
        )
        session.add(profile)
        session.commit()
        return profile.id, CatalogSnapshot.current(session).version.version_number


def _run_concurrently(target, count: int) -> tuple[list, list[BaseException]]:
    results: list = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(count)
    guard = threading.Lock()

    def worker() -> None:
        try:
            # Release every thread at once, so they genuinely contend
            # rather than running one after another.
            barrier.wait(timeout=10)
            outcome = target()
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted on
            with guard:
                errors.append(exc)
            return
        with guard:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results, errors


@pytest.mark.invariant
def test_concurrent_minting_creates_exactly_one_version(
    session_factory: sessionmaker[Session],
    redis_client: redis.Redis,
    committed_profile: tuple[uuid.UUID, int],
) -> None:
    """The documented convergence guarantee.

    Several callers mint the same unchanged profile at once. Exactly one
    version may exist afterwards, and the losers must return None rather
    than raise: nothing had changed by the time they looked, which is
    the correct answer, not a failure.
    """
    profile_id, catalog_version = committed_profile

    results, errors = _run_concurrently(
        lambda: mint_version_with_lock(
            session_factory, profile_id, catalog_version, redis_client
        ),
        THREADS,
    )

    assert errors == []
    minted = [version for version in results if version is not None]
    assert len(minted) == 1, f"expected one mint, got {len(minted)}"
    assert len(results) == THREADS

    with session_factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(ProfileVersion)
            .where(ProfileVersion.user_profile_id == profile_id)
        )
    assert count == 1


@pytest.mark.invariant
def test_concurrent_minting_produces_contiguous_version_numbers(
    session_factory: sessionmaker[Session],
    redis_client: redis.Redis,
    committed_profile: tuple[uuid.UUID, int],
) -> None:
    """Version numbers stay monotonic with no gaps and no duplicates.

    Each round changes the profile, so each round must mint. Run under
    contention, a lost update would show up here as a skipped number or
    a missing version.
    """
    profile_id, catalog_version = committed_profile
    rounds = 4

    for index in range(rounds):
        with session_factory() as session:
            profile = session.get(UserProfile, profile_id)
            assert profile is not None
            profile.experience_years = 10 + index
            session.commit()

        results, errors = _run_concurrently(
            lambda: mint_version_with_lock(
                session_factory, profile_id, catalog_version, redis_client
            ),
            THREADS,
        )
        assert errors == []
        assert len([v for v in results if v is not None]) == 1

    with session_factory() as session:
        numbers = list(
            session.scalars(
                select(ProfileVersion.version_number)
                .where(ProfileVersion.user_profile_id == profile_id)
                .order_by(ProfileVersion.version_number)
            )
        )
    assert numbers == list(range(1, rounds + 1))


@pytest.mark.invariant
def test_duplicate_version_number_is_rejected_by_the_database(
    session_factory: sessionmaker[Session],
    committed_profile: tuple[uuid.UUID, int],
) -> None:
    """The backstop for a bypassed lock.

    Redis is transport and locks only, never durable state, so the lock
    can be lost — a crashed holder whose TTL expired mid-operation. What
    must not happen is a duplicate version number surviving. PostgreSQL
    turns that lost race into a rejected write.
    """
    profile_id, catalog_version = committed_profile

    with session_factory() as session:
        for _ in range(2):
            session.add(
                ProfileVersion(
                    user_profile_id=profile_id,
                    version_number=1,
                    schema_version=PROFILE_SNAPSHOT_SCHEMA_VERSION,
                    catalog_version_at_creation=catalog_version,
                    snapshot={"profile_id": str(profile_id)},
                )
            )
        with pytest.raises(IntegrityError, match="uq_profile_version_profile_number"):
            session.commit()
        session.rollback()


def test_lock_key_is_scoped_per_profile() -> None:
    """Two profiles minting at once must not serialize against each other."""
    first = profile_version_lock_key(uuid.uuid4())
    second = profile_version_lock_key(uuid.uuid4())
    assert first != second
    assert "profile_version_mint" in first
