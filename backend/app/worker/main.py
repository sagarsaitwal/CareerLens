"""Worker entry point.

APScheduler decides *when* work runs and enqueues it into Redis; this
process consumes the queues. The two are complementary, not alternatives
(ARCHITECTURE.md Section 3.3).

Milestone 1 establishes the process, its scheduler and its queue loop.
Handlers arrive with the milestones that own them.
"""

import logging
import signal
import sys
import time
from collections.abc import Callable
from types import FrameType
from typing import Final

import redis
from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.worker.queues import Queue

log = logging.getLogger(__name__)

# How long BLPOP blocks server-side before returning empty. Short enough
# that a shutdown signal is acted on promptly.
BLOCK_TIMEOUT_SECONDS: Final[int] = 5

# The client socket read deadline MUST be strictly greater than
# BLOCK_TIMEOUT_SECONDS.
#
# redis-py derives the socket read deadline from the blocking command's
# own timeout when no explicit socket_timeout is configured. The socket
# then expires at the same instant the server sends its empty reply, and
# the client loses that race essentially every time — turning a normal
# empty poll into redis.exceptions.TimeoutError. Measured against
# redis-py 8.1.0: default and equal timeouts both raise at ~5.0s, while
# a strictly greater one returns None at ~5.1s.
#
# The margin only has to cover the reply's round-trip after the server
# stops blocking. That is sub-millisecond for a container-local Redis, so
# five seconds is ample headroom for scheduling jitter while still
# surfacing a genuinely wedged socket within ten seconds.
SOCKET_TIMEOUT_MARGIN_SECONDS: Final[int] = 5
SOCKET_TIMEOUT_SECONDS: Final[int] = BLOCK_TIMEOUT_SECONDS + SOCKET_TIMEOUT_MARGIN_SECONDS

# Connecting is not a blocking operation, so it gets a tighter deadline.
SOCKET_CONNECT_TIMEOUT_SECONDS: Final[int] = 5

# Long-lived worker connections can be dropped silently by intermediate
# NAT. redis-py validates an idle connection before reuse at this
# interval; it does not interrupt an in-flight blocking read.
HEALTH_CHECK_INTERVAL_SECONDS: Final[int] = 30

# Redis is transport, not durable state (ARCHITECTURE.md Section 3.5):
# losing it costs throughput, not correctness. The worker therefore waits
# for it to come back rather than exiting, backing off so it does not
# hammer a down server, and capped so recovery is still prompt.
BACKOFF_INITIAL_SECONDS: Final[float] = 1.0
BACKOFF_MAX_SECONDS: Final[float] = 30.0

_shutdown = False


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    global _shutdown
    log.info("received signal %s, shutting down after current item", signum)
    _shutdown = True


def build_redis_client(url: str) -> redis.Redis:
    """Redis client whose socket deadline outlives the BLPOP block window."""
    return redis.Redis.from_url(
        url,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=SOCKET_CONNECT_TIMEOUT_SECONDS,
        health_check_interval=HEALTH_CHECK_INTERVAL_SECONDS,
    )


def build_scheduler(client: redis.Redis) -> BackgroundScheduler:
    """Periodic triggers. Each job only enqueues; none does the work."""
    scheduler = BackgroundScheduler()
    # Jobs are registered by the milestones that own them (discovery
    # sweeps in Milestone 5, re-match sweeps in Milestone 10).
    return scheduler


def poll_once(client: redis.Redis, queues: list[str]) -> str | None:
    """Block for one item. Return its queue name, or None if empty.

    An empty queue is normal control flow, not an error.

    A socket timeout is also treated as an empty poll. Because the socket
    deadline is strictly longer than the block window, a timeout means no
    reply arrived at all — so the server did not pop anything, and
    nothing is lost by retrying. Errors other than a timeout are left to
    propagate: a genuine fault must not be mistaken for an idle queue.
    """
    try:
        item = client.blpop(queues, timeout=BLOCK_TIMEOUT_SECONDS)
    except redis.exceptions.TimeoutError:
        return None
    if item is None:
        return None
    queue_name: bytes | str = item[0]
    return queue_name.decode() if isinstance(queue_name, bytes) else queue_name


def consume(
    client: redis.Redis,
    queues: list[str],
    *,
    should_stop: Callable[[], bool],
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Consume queues until told to stop.

    Transient Redis unavailability is survivable and does not terminate
    the worker. Anything else propagates.
    """
    backoff = BACKOFF_INITIAL_SECONDS

    while not should_stop():
        try:
            queue_name = poll_once(client, queues)
        except redis.exceptions.ConnectionError as exc:
            log.warning("redis unavailable (%s); retrying in %.0fs", exc, backoff)
            sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
            continue

        # Reset only after a clean poll, so a flapping connection still
        # backs off rather than retrying tightly.
        backoff = BACKOFF_INITIAL_SECONDS

        if queue_name is None:
            continue

        log.info("dequeued from %s", queue_name)
        # Dispatch lands with the milestone that introduces handlers.
        # The payload is deliberately not read yet: discarding it here
        # would lose work once handlers exist, so queues stay empty
        # until Milestone 5 enqueues anything.


def _interruptible_sleep(seconds: float) -> None:
    """Sleep in slices so a shutdown signal is not delayed by a backoff."""
    deadline = time.monotonic() + seconds
    while not _shutdown and time.monotonic() < deadline:
        time.sleep(min(0.5, deadline - time.monotonic()))


def run() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    client = build_redis_client(settings.redis_url.get_secret_value())
    scheduler = build_scheduler(client)
    scheduler.start()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    queues = [q.value for q in Queue]
    log.info("worker started; consuming %s", ", ".join(queues))

    try:
        consume(
            client,
            queues,
            should_stop=lambda: _shutdown,
            sleep=_interruptible_sleep,
        )
    finally:
        scheduler.shutdown(wait=False)
        client.close()

    log.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(run())
