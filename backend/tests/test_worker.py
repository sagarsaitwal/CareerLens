"""Worker queue-loop tests.

Regression cover for the bug where an empty queue terminated the worker:
redis-py derives the socket read deadline from the blocking command's own
timeout, so BLPOP's normal empty return surfaced as
redis.exceptions.TimeoutError and propagated out of the loop.
"""

import os

import pytest
import redis

from app.worker import main as worker
from app.worker.queues import Queue


class FakeRedis:
    """Minimal stand-in that scripts a sequence of blpop outcomes."""

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    def blpop(self, keys: list[str], timeout: int) -> object:
        self.calls += 1
        if not self._outcomes:
            return None
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def stop_after(n: int):
    """should_stop predicate that halts the loop after n checks."""
    state = {"n": 0}

    def _should_stop() -> bool:
        state["n"] += 1
        return state["n"] > n

    return _should_stop


# --- client configuration -------------------------------------------------


def test_socket_timeout_strictly_exceeds_block_timeout() -> None:
    """The core of the bug: equal or shorter deadlines make BLPOP raise.

    Measured against redis-py 8.1.0 — None and equal both raise at ~5.0s,
    strictly greater returns None at ~5.1s.
    """
    assert worker.SOCKET_TIMEOUT_SECONDS > worker.BLOCK_TIMEOUT_SECONDS


def test_client_sets_explicit_timeouts() -> None:
    """Defaults are not relied on; the deadline is configured explicitly."""
    client = worker.build_redis_client("redis://localhost:6379/0")
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == worker.SOCKET_TIMEOUT_SECONDS
    assert kwargs["socket_connect_timeout"] == worker.SOCKET_CONNECT_TIMEOUT_SECONDS


# --- poll_once ------------------------------------------------------------


def test_empty_queue_returns_none() -> None:
    assert worker.poll_once(FakeRedis([None]), ["q"]) is None


def test_socket_timeout_is_treated_as_an_empty_poll() -> None:
    """A timeout must not escape: this is what killed the worker."""
    client = FakeRedis([redis.exceptions.TimeoutError("Timeout reading from socket")])
    assert worker.poll_once(client, ["q"]) is None


def test_item_returns_its_queue_name() -> None:
    client = FakeRedis([(b"careerlens:q:ingest", b"payload")])
    assert worker.poll_once(client, ["q"]) == "careerlens:q:ingest"


def test_unexpected_redis_errors_propagate() -> None:
    """A genuine fault must not be mistaken for an idle queue."""
    client = FakeRedis([redis.exceptions.ResponseError("WRONGTYPE")])
    with pytest.raises(redis.exceptions.ResponseError):
        worker.poll_once(client, ["q"])


# --- consume loop ---------------------------------------------------------


def test_loop_survives_repeated_empty_polls() -> None:
    """The regression: three empty polls must not end the loop."""
    client = FakeRedis([None, None, None])
    worker.consume(client, ["q"], should_stop=stop_after(3), sleep=lambda _: None)
    assert client.calls == 3


def test_loop_survives_repeated_socket_timeouts() -> None:
    timeout = redis.exceptions.TimeoutError("Timeout reading from socket")
    client = FakeRedis([timeout, timeout, timeout])
    worker.consume(client, ["q"], should_stop=stop_after(3), sleep=lambda _: None)
    assert client.calls == 3


def test_loop_backs_off_on_connection_error_and_recovers() -> None:
    """Redis is transport, not state: an outage must not kill the worker."""
    slept: list[float] = []
    client = FakeRedis(
        [
            redis.exceptions.ConnectionError("connection refused"),
            redis.exceptions.ConnectionError("connection refused"),
            (b"careerlens:q:match", b"payload"),
        ]
    )
    worker.consume(client, ["q"], should_stop=stop_after(3), sleep=slept.append)

    assert client.calls == 3
    assert slept == [worker.BACKOFF_INITIAL_SECONDS, worker.BACKOFF_INITIAL_SECONDS * 2]


def test_backoff_is_capped() -> None:
    slept: list[float] = []
    errors = [redis.exceptions.ConnectionError("down")] * 12
    worker.consume(FakeRedis(errors), ["q"], should_stop=stop_after(12), sleep=slept.append)
    assert max(slept) == worker.BACKOFF_MAX_SECONDS


def test_loop_propagates_unexpected_errors() -> None:
    client = FakeRedis([redis.exceptions.ResponseError("WRONGTYPE")])
    with pytest.raises(redis.exceptions.ResponseError):
        worker.consume(client, ["q"], should_stop=stop_after(5), sleep=lambda _: None)


def test_loop_stops_when_asked() -> None:
    client = FakeRedis([None] * 10)
    worker.consume(client, ["q"], should_stop=lambda: True, sleep=lambda _: None)
    assert client.calls == 0


# --- real Redis -----------------------------------------------------------


@pytest.mark.integration
def test_blpop_against_real_redis_returns_none_when_empty() -> None:
    """End-to-end proof against the real server, not a fake.

    This is the test that would have caught the original bug: the fake
    cannot reproduce redis-py's socket-deadline behaviour.
    """
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("REDIS_URL not set")

    client = worker.build_redis_client(url)
    try:
        client.ping()
    except redis.exceptions.RedisError as exc:  # pragma: no cover
        pytest.skip(f"Redis unavailable: {exc}")

    try:
        assert worker.poll_once(client, [Queue.INGEST.value]) is None
    finally:
        client.close()
