"""Queue names and Redis roles.

Redis is transport, cache and locks only — never durable application
state (ARCHITECTURE.md Section 3.5). Anything that must survive a Redis
flush belongs in PostgreSQL.
"""

from enum import StrEnum
from typing import Final


class Queue(StrEnum):
    """Work queues, mirroring the processing layers."""

    # Fetch, dedupe, Stage 1 extraction.
    INGEST = "careerlens:q:ingest"
    # Resolution cascade; re-resolution after a catalog change.
    RESOLVE = "careerlens:q:resolve"
    # Stages 2-4 against a pinned ProfileVersion and catalog_version.
    MATCH = "careerlens:q:match"


# Semantic verdict cache. The key must carry enough version context that
# a catalog or model change cannot serve a stale verdict
# (AI-MATCHING.md Section 14).
SEMANTIC_CACHE_PREFIX: Final[str] = "careerlens:cache:semantic"

# Locks guarding operations that must converge under concurrency:
# catalog promotion and ProfileVersion minting.
LOCK_PREFIX: Final[str] = "careerlens:lock"


def semantic_cache_key(
    representation_a: str,
    representation_b: str,
    catalog_version: str,
    model_version: str,
) -> str:
    """Build a semantic verdict cache key.

    Representations are ordered so that (a, b) and (b, a) share one entry —
    semantic equivalence is symmetric, and asking twice wastes a model call.
    """
    first, second = sorted((representation_a, representation_b))
    return f"{SEMANTIC_CACHE_PREFIX}:{catalog_version}:{model_version}:{first}|{second}"
