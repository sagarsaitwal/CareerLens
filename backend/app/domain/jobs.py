"""Deterministic job identity: `dedupe_key` and `fingerprint`.

Pure functions, no I/O. See DATABASE.md Section 2.15, "Identity and
content state", and invariant 27.

    dedupe_key   WHICH job is this?          company, title, location
    fingerprint  WHAT does it currently say? the ten documented content fields

Derivation, identical for both keys:
  1. Each text input passes through `normalize()`, the same mechanical
     rules that govern skill identity. The two URL fields of the
     fingerprint are the one exception: they are hashed exactly as
     supplied, because URL paths and identifiers are case- and
     punctuation-sensitive. `None` stays `None` (JSON null), so an absent
     value never equals an empty one.
  2. The fields, the key kind and the rule version form one JSON object,
     serialized with sorted keys, no insignificant whitespace, and UTF-8
     encoding.
  3. The SHA-256 hex digest of those bytes is the key: 64 characters,
     inside the 255-character columns.

Versioning: `rule_version` (default `RULE_VERSION`) is hashed into both
keys, so a key computed under old rules never compares equal to one
computed under new rules. No separate version axis is introduced. The
key kind is hashed too, so the two keys can never collide with each
other.

Observation timestamps and source identity are not parameters, so they
cannot reach either key.
"""

import hashlib
import json

from app.core.versions import RULE_VERSION
from app.domain.normalization import normalize


def _normalized(value: str | None) -> str | None:
    return None if value is None else normalize(value)


def _digest(kind: str, rule_version: str, fields: dict[str, str | None]) -> str:
    payload = {"kind": kind, "rule_version": rule_version, "fields": fields}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_dedupe_key(
    company: str, title: str, location: str | None, *, rule_version: str = RULE_VERSION
) -> str:
    """Identity of a job, from exactly company, title and location.

    Everything else is excluded by decision (DATABASE.md Section 2.15),
    so a reworded repost of the same role keeps its identity.

    Raises ValueError when company or title normalizes to empty: such a
    value cannot identify a job, and keying it would merge every such
    posting into one. Location may be None or empty and the two stay
    distinct; rejecting a blank location is the ingestion boundary's job.
    """
    normalized_company = normalize(company)
    normalized_title = normalize(title)
    if not normalized_company:
        raise ValueError("company normalizes to empty and cannot identify a job")
    if not normalized_title:
        raise ValueError("title normalizes to empty and cannot identify a job")
    return _digest(
        "dedupe_key",
        rule_version,
        {
            "company": normalized_company,
            "title": normalized_title,
            "location": _normalized(location),
        },
    )


def build_fingerprint(
    *,
    title: str,
    company: str,
    location: str | None,
    work_mode: str | None,
    employment_type: str,
    seniority: str | None,
    experience_requirements: str | None,
    description_raw: str | None,
    official_url: str | None,
    direct_apply_url: str | None,
    rule_version: str = RULE_VERSION,
) -> str:
    """Current matching-relevant content of a job.

    `seniority` is the posting's seniority wording (`seniority_raw_text`),
    normalized here exactly as `seniority_normalized` is. Catalog
    resolution results are deliberately excluded, so a catalog change
    alone never reports a content edit.

    `official_url` and `direct_apply_url` are hashed exactly as supplied,
    so any change to a stored URL changes the fingerprint. URL
    canonicalization is deferred (DATABASE.md Section 2.15).
    """
    return _digest(
        "fingerprint",
        rule_version,
        {
            "title": normalize(title),
            "company": normalize(company),
            "location": _normalized(location),
            "work_mode": _normalized(work_mode),
            "employment_type": normalize(employment_type),
            "seniority": _normalized(seniority),
            "experience_requirements": _normalized(experience_requirements),
            "description_raw": _normalized(description_raw),
            "official_url": official_url,
            "direct_apply_url": direct_apply_url,
        },
    )
