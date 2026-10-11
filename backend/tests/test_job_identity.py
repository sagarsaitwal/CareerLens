"""dedupe_key and fingerprint are distinct, deterministic and versioned.

DATABASE.md Section 2.15 and invariant 27. Pure functions: no database,
network or job source is involved.
"""

import inspect
from collections.abc import Callable
from typing import Any

import pytest

from app.core.versions import RULE_VERSION
from app.domain.jobs import build_dedupe_key, build_fingerprint

DEDUPE_FIELDS = {"company", "title", "location"}
FINGERPRINT_FIELDS = {
    "title",
    "company",
    "location",
    "work_mode",
    "employment_type",
    "seniority",
    "experience_requirements",
    "description_raw",
    "official_url",
    "direct_apply_url",
}
NULLABLE_FINGERPRINT_FIELDS = FINGERPRINT_FIELDS - {"title", "company", "employment_type"}


def content(**overrides: str | None) -> dict[str, Any]:
    values: dict[str, Any] = {
        "title": "Senior Platform Engineer",
        "company": "Example Industries",
        "location": "Pune, India",
        "work_mode": "hybrid",
        "employment_type": "full_time",
        "seniority": "Senior",
        "experience_requirements": "5+ years of backend experience",
        "description_raw": "Build and run the platform.",
        "official_url": "https://example.test/careers/platform",
        "direct_apply_url": "https://example.test/apply/platform",
    }
    values.update(overrides)
    return values


def dedupe(**overrides: str | None) -> str:
    values = content(**overrides)
    return build_dedupe_key(values["company"], values["title"], values["location"])


def fingerprint(**overrides: str | None) -> str:
    return build_fingerprint(**content(**overrides))


def parameters(function: Callable[..., str]) -> set[str]:
    return set(inspect.signature(function).parameters) - {"rule_version"}


# --- inputs are exactly the documented fields -----------------------------


def test_dedupe_key_inputs_are_exactly_company_title_location() -> None:
    assert parameters(build_dedupe_key) == DEDUPE_FIELDS


def test_fingerprint_inputs_are_exactly_the_documented_fields() -> None:
    assert parameters(build_fingerprint) == FINGERPRINT_FIELDS


@pytest.mark.parametrize(
    "excluded", ["first_seen_at", "last_seen_at", "source_job_id", "source_url"]
)
def test_observations_and_source_identity_cannot_reach_either_key(excluded: str) -> None:
    with pytest.raises(TypeError):
        build_fingerprint(**content(), **{excluded: "x"})
    with pytest.raises(TypeError):
        build_dedupe_key("Example Industries", "Engineer", "Pune", **{excluded: "x"})


# --- determinism ----------------------------------------------------------


def test_identical_inputs_give_identical_keys() -> None:
    assert dedupe() == dedupe()
    assert fingerprint() == fingerprint()


def test_keys_fit_their_columns() -> None:
    for key in (dedupe(), fingerprint()):
        assert 0 < len(key) <= 255


def test_dedupe_key_and_fingerprint_never_coincide() -> None:
    """Two distinct keys, never one field (invariant 27)."""
    assert dedupe() != fingerprint()


# --- dedupe_key -----------------------------------------------------------


def test_dedupe_key_ignores_case_and_whitespace_variants() -> None:
    assert dedupe() == dedupe(
        company="  EXAMPLE   Industries ",
        title="senior-platform  engineer",
        location="pune,   INDIA",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("company", "Other Industries"), ("title", "Staff Platform Engineer"), ("location", "Mumbai")],
)
def test_dedupe_key_changes_with_identity_fields(field: str, value: str) -> None:
    assert dedupe(**{field: value}) != dedupe()


@pytest.mark.parametrize("field", sorted(FINGERPRINT_FIELDS - DEDUPE_FIELDS))
def test_dedupe_key_ignores_content_fields(field: str) -> None:
    """A reworded or re-classified repost of the same role is one job.

    Salary is not a fingerprint field either; that it cannot reach the
    dedupe_key is covered by the exact-inputs test above.
    """
    assert dedupe(**{field: "changed content"}) == dedupe()


def test_dedupe_key_distinguishes_absent_from_empty_location() -> None:
    assert dedupe(location=None) != dedupe(location="")


def test_dedupe_key_keeps_a_blank_location_distinct_from_none() -> None:
    """Never silently converted to None; rejecting it is the ingestion boundary's job."""
    assert dedupe(location="   ") != dedupe(location=None)


@pytest.mark.parametrize("field", ["company", "title"])
@pytest.mark.parametrize("value", ["", "   ", "\t\n", "-/_.", " - . "])
def test_dedupe_key_rejects_identity_fields_that_normalize_to_empty(field: str, value: str) -> None:
    """A blank company or title would merge every such posting into one job."""
    with pytest.raises(ValueError, match=field):
        dedupe(**{field: value})


# --- fingerprint ----------------------------------------------------------


@pytest.mark.parametrize("field", sorted(FINGERPRINT_FIELDS))
def test_fingerprint_changes_with_every_documented_field(field: str) -> None:
    assert fingerprint(**{field: "a different value"}) != fingerprint()


def test_fingerprint_ignores_normalization_equivalent_content() -> None:
    assert fingerprint() == fingerprint(
        description_raw="  BUILD and run   the platform ",
        experience_requirements="5+ Years of Backend Experience",
    )


@pytest.mark.parametrize("field", sorted(NULLABLE_FINGERPRINT_FIELDS))
def test_fingerprint_distinguishes_absent_from_empty(field: str) -> None:
    assert fingerprint(**{field: None}) != fingerprint(**{field: ""})


# --- URLs are hashed exactly as supplied ------------------------------------


@pytest.mark.parametrize("field", ["official_url", "direct_apply_url"])
@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("https://example.test/jobs/ABC123", "https://example.test/jobs/abc123"),
        ("https://example.test/jobs/a-b", "https://example.test/jobs/a_b"),
        ("https://example.test/jobs/a.b", "https://example.test/jobs/a/b"),
        ("https://example.test/jobs?id=1", "https://example.test/jobs?ID=1"),
        ("https://example.test/jobs/1", "https://example.test/jobs/1/"),
    ],
)
def test_fingerprint_does_not_normalize_urls(field: str, first: str, second: str) -> None:
    """Differences mechanical normalization would erase still change the fingerprint."""
    assert fingerprint(**{field: first}) != fingerprint(**{field: second})


@pytest.mark.parametrize("field", ["official_url", "direct_apply_url"])
def test_identical_urls_give_identical_fingerprints(field: str) -> None:
    url = "https://example.test/jobs/ABC123?ref=Board"
    assert fingerprint(**{field: url}) == fingerprint(**{field: url})


# --- versioning -----------------------------------------------------------


def test_default_rule_version_is_the_current_one() -> None:
    values = content()
    current = build_dedupe_key(
        values["company"], values["title"], values["location"], rule_version=RULE_VERSION
    )
    assert current == dedupe()
    assert build_fingerprint(**values, rule_version=RULE_VERSION) == fingerprint()


def test_a_different_rule_version_gives_different_keys() -> None:
    """An old key stops matching rather than silently colliding."""
    values = content()
    other = RULE_VERSION + "-next"
    other_key = build_dedupe_key(
        values["company"], values["title"], values["location"], rule_version=other
    )
    assert other_key != dedupe()
    assert build_fingerprint(**values, rule_version=other) != fingerprint()
