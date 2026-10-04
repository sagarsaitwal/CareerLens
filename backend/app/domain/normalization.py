"""Mechanical normalization.

Pure functions, no I/O. Deterministic by construction: the same input
always yields the same output, which is what makes Stage 2
re-executable.

Normalization is mechanical only — case, whitespace and punctuation. It
collapses trivially-different spellings of the SAME token. It does NOT
decide synonymy: "k8s" and "kubernetes" normalize to different strings
and are related only by a catalogued alias.

These rules are governed by `rule_version` (app/core/versions.py). Any
change to them changes resolution outcomes and must bump it.
"""

import re
import unicodedata
from typing import Final

# Characters that commonly decorate technology names without changing
# identity: "Node.js" / "node js", "C++" is deliberately NOT stripped
# because the plus signs are load-bearing.
_SEPARATOR_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\s_\-/\\.,;:]+")
_COLLAPSE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")

MAX_REPRESENTATION_LENGTH: Final[int] = 255


def normalize(raw: str) -> str:
    """Return the normalized form of a raw representation.

    Steps, in order:
      1. Unicode NFKC, so visually identical characters compare equal.
      2. Casefold, which handles more than str.lower for non-ASCII.
      3. Replace separator punctuation with a single space.
      4. Collapse runs of whitespace and strip.

    An input that normalizes to an empty string is an extraction defect,
    not an unresolved skill. Callers reject it at ingestion rather than
    entering it into the catalog or the promotion-evidence pool.
    """
    folded = unicodedata.normalize("NFKC", raw).casefold()
    separated = _SEPARATOR_PATTERN.sub(" ", folded)
    return _COLLAPSE_PATTERN.sub(" ", separated).strip()


def is_valid_representation(raw: str) -> bool:
    """Whether a raw mention is well formed enough to enter the catalog.

    Empty, whitespace-only and over-long mentions are rejected at
    ingestion (DATABASE.md Section 4.4).
    """
    if not raw or not raw.strip():
        return False
    normalized = normalize(raw)
    return bool(normalized) and len(normalized) <= MAX_REPRESENTATION_LENGTH
