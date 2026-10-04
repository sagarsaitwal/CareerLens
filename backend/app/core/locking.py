"""Redis-backed distributed locking.

A single Redis instance backs this deployment (SYSTEM-REQUIREMENTS.md),
so this is a lock against that one instance, not a multi-node Redlock —
adequate for what ARCHITECTURE.md's Redis role table documents this for:
"guarding catalog promotion and ProfileVersion minting against
concurrent duplicates".

Redis itself is transport/cache/locks only, never durable state
(ARCHITECTURE.md Section 3.5): if this lock is ever bypassed — a crashed
holder whose TTL expired mid-operation, say — PostgreSQL's own
uniqueness constraints are the backstop that turns a lost race into a
rejected duplicate rather than corrupted data. The lock's job is to make
concurrent callers converge on one outcome instead of each separately
discovering that rejection.
"""

import contextlib
import time
import uuid
from collections.abc import Iterator
from typing import Final

import redis

from app.worker.queues import LOCK_PREFIX

DEFAULT_TTL_MS: Final[int] = 10_000
DEFAULT_WAIT_TIMEOUT_SECONDS: Final[float] = 5.0
DEFAULT_RETRY_INTERVAL_SECONDS: Final[float] = 0.05

# Deletes the key only if it still holds OUR token. Without this check, a
# lock whose TTL expired and was reacquired by someone else would be torn
# down by the original holder's late release — turning a safety mechanism
# into the exact race it exists to prevent.
_RELEASE_IF_OWNER: Final[str] = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


class LockAcquisitionTimeoutError(TimeoutError):
    """Raised when a lock could not be acquired within the wait window."""


def lock_key(*parts: str) -> str:
    """Build a namespaced lock key, e.g. lock_key("profile_version_mint", id)."""
    return ":".join((LOCK_PREFIX, *parts))


def build_lock_client(url: str) -> redis.Redis:
    """A Redis client sized for lock operations.

    Short timeouts are correct here, unlike the worker's BLPOP-tuned
    client: acquiring a lock is a single non-blocking round trip, so a
    slow reply means something is actually wrong rather than an empty
    queue being normal.
    """
    return redis.Redis.from_url(url, socket_timeout=2.0, socket_connect_timeout=2.0)


@contextlib.contextmanager
def distributed_lock(
    client: redis.Redis,
    key: str,
    *,
    ttl_ms: int = DEFAULT_TTL_MS,
    wait_timeout_seconds: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    retry_interval_seconds: float = DEFAULT_RETRY_INTERVAL_SECONDS,
) -> Iterator[None]:
    """Hold an exclusive lock on `key` for the duration of the block.

    Polls until acquired or `wait_timeout_seconds` elapses, in which case
    `LockAcquisitionTimeoutError` is raised rather than proceeding
    unprotected — a caller that cannot get the lock must not silently
    fall back to running without one.

    `ttl_ms` bounds how long a crashed holder can block everyone else; it
    must comfortably exceed the slowest realistic critical section, since
    a lock that expires mid-operation is exactly the case the ownership
    check on release exists to make safe rather than catastrophic.
    """
    token = uuid.uuid4().hex
    deadline = time.monotonic() + wait_timeout_seconds
    acquired = False
    while time.monotonic() < deadline:
        if client.set(key, token, nx=True, px=ttl_ms):
            acquired = True
            break
        time.sleep(retry_interval_seconds)

    if not acquired:
        raise LockAcquisitionTimeoutError(
            f"could not acquire lock {key!r} within {wait_timeout_seconds}s"
        )

    try:
        yield
    finally:
        client.eval(_RELEASE_IF_OWNER, 1, key, token)
