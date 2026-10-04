"""The four version axes recorded on every MatchResult.

See AI-MATCHING.md Section 11 and DATABASE.md Section 2.16.

There are exactly four axes and no fifth. The seniority catalog shares
CATALOG_VERSION with the skill catalog.

    profile_version_id  profile inputs          (per-row, not here)
    rule_version        algorithm + normalization
    model_version       Stage 1 / Stage 3 model identity
    catalog_version     skills, aliases, relationships, seniority
"""

from typing import Final

# Bumped DELIBERATELY whenever deterministic behaviour in app/domain/
# changes: resolution rules, normalization, Stage 2 comparison, or
# Stage 4 arithmetic. Never auto-derived — a stale value silently breaks
# the Stage 2 re-executability guarantee.
RULE_VERSION: Final[str] = "r1"

# Identifies the model used by Stage 1 extraction and Stage 3 semantic
# analysis. "none" while no provider is configured; Stage 3 results are
# evidence-preserved rather than re-executable regardless of this value.
MODEL_VERSION_NONE: Final[str] = "none"

# Advances with catalog data migrations. Read from the database at
# runtime once the catalog exists (Milestone 2); this constant is the
# bootstrap value for an empty catalog.
CATALOG_VERSION_EMPTY: Final[str] = "c0"


def describe() -> dict[str, str]:
    """Version axes known without a database connection."""
    return {
        "rule_version": RULE_VERSION,
        "model_version": MODEL_VERSION_NONE,
        "catalog_version": CATALOG_VERSION_EMPTY,
    }
