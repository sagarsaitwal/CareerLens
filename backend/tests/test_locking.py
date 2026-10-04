"""Distributed lock primitive.

Run against a real Redis. A fake would confirm only that the code calls
the commands it calls; what needs proving is that SET NX PX actually
excludes a second holder and that the release script refuses to delete
someone else's lock.
"""

import threading
import time
import uuid

import pytest
import redis

from app.core.locking import (
    LockAcquisitionTimeoutError,
    distributed_lock,
    lock_key,
)
from app.worker.queues import LOCK_PREFIX


@pytest.fixture
def key() -> str:
    """A key unique to one test, so concurrent runs never collide."""
    return lock_key("test", uuid.uuid4().hex)


# --- key namespacing ------------------------------------------------------


def test_keys_are_namespaced() -> None:
    """Locks share Redis with queues and caches, so they stay prefixed."""
    assert lock_key("profile_version_mint", "abc").startswith(f"{LOCK_PREFIX}:")


# --- acquire and release --------------------------------------------------


def test_lock_is_held_then_released(redis_client: redis.Redis, key: str) -> None:
    with distributed_lock(redis_client, key):
        assert redis_client.get(key) is not None
    assert redis_client.get(key) is None


def test_lock_is_released_after_an_exception(
    redis_client: redis.Redis, key: str
) -> None:
    """A failing critical section must not leave the lock wedged.

    Without this the only recovery would be waiting out the TTL, which
    is sized for crashes, not for ordinary errors.
    """
    with pytest.raises(RuntimeError), distributed_lock(redis_client, key):
        raise RuntimeError("boom")
    assert redis_client.get(key) is None


def test_lock_has_a_ttl(redis_client: redis.Redis, key: str) -> None:
    """A crashed holder must not block the key forever."""
    with distributed_lock(redis_client, key, ttl_ms=5_000):
        assert 0 < redis_client.pttl(key) <= 5_000


# --- mutual exclusion -----------------------------------------------------


def test_second_holder_is_excluded_while_the_first_holds(
    redis_client: redis.Redis, key: str
) -> None:
    with (
        distributed_lock(redis_client, key),
        pytest.raises(LockAcquisitionTimeoutError),
        distributed_lock(redis_client, key, wait_timeout_seconds=0.2),
    ):
        pytest.fail("acquired a lock that was already held")


def test_waiting_caller_acquires_once_the_holder_releases(
    redis_client: redis.Redis, key: str
) -> None:
    order: list[str] = []
    released = threading.Event()

    def holder() -> None:
        with distributed_lock(redis_client, key):
            order.append("holder-in")
            time.sleep(0.2)
            order.append("holder-out")
        released.set()

    def waiter() -> None:
        with distributed_lock(redis_client, key, wait_timeout_seconds=5.0):
            order.append("waiter-in")

    first = threading.Thread(target=holder)
    second = threading.Thread(target=waiter)
    first.start()
    time.sleep(0.05)
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert released.is_set()
    # The waiter must not enter until the holder has left. Overlap here
    # would mean the lock is decorative.
    assert order == ["holder-in", "holder-out", "waiter-in"]


def test_timeout_raises_rather_than_proceeding_unlocked(
    redis_client: redis.Redis, key: str
) -> None:
    """Failing to acquire must never degrade into running unprotected."""
    entered = False
    redis_client.set(key, "someone-else", px=2_000)
    try:
        with (
            pytest.raises(LockAcquisitionTimeoutError),
            distributed_lock(redis_client, key, wait_timeout_seconds=0.2),
        ):
            entered = True
        assert not entered
    finally:
        redis_client.delete(key)


# --- ownership on release -------------------------------------------------


@pytest.mark.invariant
def test_release_does_not_delete_another_holders_lock(
    redis_client: redis.Redis, key: str
) -> None:
    """The case the ownership check exists for.

    If a holder's TTL expires mid-operation and someone else acquires
    the key, the original holder's late release must be a no-op. A plain
    DEL would hand a second caller's lock away and turn the safety
    mechanism into the exact race it prevents.
    """
    with distributed_lock(redis_client, key, ttl_ms=100):
        time.sleep(0.25)
        # Stand in for the next acquirer.
        redis_client.set(key, "a-different-holder", px=5_000)

    try:
        assert redis_client.get(key) == b"a-different-holder"
    finally:
        redis_client.delete(key)
