"""The fixture-ingestion contract validates untrusted input, never trusts it.

ARCHITECTURE.md Section 3.3 and DATABASE.md Sections 2.15 and 4.4. Pure
validation: no database, network or live source is involved.
"""

from datetime import date
from typing import Any

import pytest

from app.models.enums import (
    DirectApplyVerificationStatus,
    EmploymentType,
    EvidenceSource,
    ExtractionStatus,
    JobMentionType,
    JobPostingStatus,
    JobSourceType,
    WorkMode,
)
from app.schemas.job_ingestion import PostingResult, validate_fixture, validate_posting


def raw_posting(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "source_job_id": "ext-1",
        "source_url": "https://board.example.test/jobs/ext-1",
        "title": "Senior Platform Engineer",
        "company": "Example Industries",
    }
    values.update(overrides)
    return values


def mention(raw_text: str = "Docker", mention_type: str = "required") -> dict[str, str]:
    return {"mention_type": mention_type, "raw_text": raw_text}


def raw_fixture(*postings: dict[str, Any], **source: Any) -> dict[str, Any]:
    declared: dict[str, Any] = {
        "name": "Example Board",
        "base_url": "https://board.example.test",
        "type": "job_board",
    }
    declared.update(source)
    return {"source": declared, "postings": list(postings)}


def accepted(**overrides: Any) -> PostingResult:
    result = validate_posting(0, raw_posting(**overrides))
    assert result.accepted, result.errors
    return result


def rejected(**overrides: Any) -> PostingResult:
    result = validate_posting(0, raw_posting(**overrides))
    assert not result.accepted
    assert result.errors
    return result


def rejected_on(field: str, **overrides: Any) -> None:
    errors = rejected(**overrides).errors
    assert any(error.startswith(field) for error in errors), errors


# --- accepted input -------------------------------------------------------


def test_minimal_posting_is_accepted_with_documented_defaults() -> None:
    posting = accepted().posting
    assert posting is not None
    assert posting.location is None
    assert posting.work_mode is None
    assert posting.employment_type is EmploymentType.UNKNOWN
    assert posting.status is JobPostingStatus.UNKNOWN
    assert posting.extraction_status is ExtractionStatus.PARTIAL
    assert posting.direct_apply_verification_status is DirectApplyVerificationStatus.NOT_AVAILABLE


def test_full_posting_is_accepted_and_kept_as_supplied() -> None:
    posting = accepted(
        title="  Senior Platform Engineer ",
        location="Pune, India",
        work_mode="Hybrid",
        employment_type="Full-Time",
        seniority={"raw_text": "Senior", "evidence_source": "structured_field"},
        experience_requirements="5+ years",
        description_raw="Build the platform.",
        official_url="https://example.test/careers/ABC",
        direct_apply_url="https://example.test/apply/ABC",
        salary_min=100,
        salary_max=200,
        salary_currency="inr",
        posting_date="2026-10-01",
        status="Active",
        extraction_status="complete",
    ).posting
    assert posting is not None
    assert posting.title == "  Senior Platform Engineer "  # never trimmed
    assert posting.work_mode is WorkMode.HYBRID
    assert posting.employment_type is EmploymentType.FULL_TIME
    assert posting.seniority is not None
    assert posting.seniority.evidence_source is EvidenceSource.STRUCTURED_FIELD
    assert posting.official_url == "https://example.test/careers/ABC"
    assert posting.salary_currency == "INR"
    assert posting.posting_date == date(2026, 10, 1)
    assert posting.status is JobPostingStatus.ACTIVE
    assert posting.extraction_status is ExtractionStatus.COMPLETE
    assert posting.direct_apply_verification_status is DirectApplyVerificationStatus.UNVERIFIED


# --- identity fields ------------------------------------------------------


@pytest.mark.parametrize("field", ["company", "title"])
@pytest.mark.parametrize("value", ["", "   ", "-/_."])
def test_company_and_title_must_not_normalize_to_empty(field: str, value: str) -> None:
    rejected_on(field, **{field: value})


@pytest.mark.parametrize("field", ["company", "title"])
def test_company_and_title_are_required(field: str) -> None:
    values = raw_posting()
    del values[field]
    result = validate_posting(0, values)
    assert not result.accepted
    assert any(error.startswith(field) for error in result.errors)


@pytest.mark.parametrize("value", ["", "   ", "\t", " , "])
def test_blank_location_string_is_rejected(value: str) -> None:
    rejected_on("location", location=value)


def test_missing_location_is_none_and_allowed() -> None:
    posting = accepted(location=None).posting
    assert posting is not None and posting.location is None


def test_over_long_text_is_rejected_not_truncated() -> None:
    rejected_on("title", title="x" * 513)


# --- source identity ------------------------------------------------------


@pytest.mark.parametrize("field", ["source_job_id", "source_url"])
def test_source_identity_is_required(field: str) -> None:
    values = raw_posting()
    del values[field]
    result = validate_posting(0, values)
    assert any(error.startswith(field) for error in result.errors)


@pytest.mark.parametrize("value", ["", "   "])
def test_source_job_id_must_not_be_blank(value: str) -> None:
    rejected_on("source_job_id", source_job_id=value)


# --- URLs -----------------------------------------------------------------


@pytest.mark.parametrize("field", ["source_url", "official_url", "direct_apply_url"])
@pytest.mark.parametrize(
    "value",
    [
        "",
        "example.test/jobs/1",
        "/jobs/1",
        "ftp://example.test/jobs/1",
        "javascript:alert(1)",
        "https://",
        " https://example.test/jobs/1",
        "https://example.test/jobs/1 ",
        "https://example.test/jobs/a b",
        "https://example.test/" + "x" * 2048,
    ],
)
def test_invalid_urls_are_rejected(field: str, value: str) -> None:
    rejected_on(field, **{field: value})


def test_urls_are_kept_exactly_as_supplied() -> None:
    url = "HTTPS://Example.test/Jobs/ABC_1/?Ref=Board"
    posting = accepted(source_url=url, official_url=url).posting
    assert posting is not None
    assert posting.source_url == url
    assert posting.official_url == url


def test_a_fixture_cannot_claim_a_verified_link() -> None:
    rejected_on(
        "direct_apply_verification_status",
        direct_apply_url="https://example.test/apply/1",
        direct_apply_verification_status="verified",
    )


# --- enum mappings --------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("full_time", EmploymentType.FULL_TIME),
        ("Full-time", EmploymentType.FULL_TIME),
        ("FULL TIME", EmploymentType.FULL_TIME),
        ("internship", EmploymentType.INTERNSHIP),
        ("permanent", EmploymentType.UNKNOWN),
        ("", EmploymentType.UNKNOWN),
        (None, EmploymentType.UNKNOWN),
    ],
)
def test_employment_type_maps_mechanically_and_unrecognised_becomes_unknown(
    value: str | None, expected: EmploymentType
) -> None:
    posting = accepted(employment_type=value).posting
    assert posting is not None and posting.employment_type is expected


@pytest.mark.parametrize(("value", "expected"), [("REMOTE", WorkMode.REMOTE), (None, None)])
def test_work_mode_maps_mechanically(value: str | None, expected: WorkMode | None) -> None:
    posting = accepted(work_mode=value).posting
    assert posting is not None and posting.work_mode is expected


@pytest.mark.parametrize("value", ["remote-first", "", "anywhere"])
def test_unrecognised_work_mode_rejects_the_posting(value: str) -> None:
    """Never silently None: absence and an unrecognised statement differ."""
    rejected_on("work_mode", work_mode=value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("Closed", JobPostingStatus.CLOSED), ("expired", JobPostingStatus.UNKNOWN)],
)
def test_unrecognised_status_becomes_unknown_never_closed(
    value: str, expected: JobPostingStatus
) -> None:
    posting = accepted(status=value).posting
    assert posting is not None and posting.status is expected


def test_unrecognised_extraction_status_becomes_partial() -> None:
    posting = accepted(extraction_status="exhaustive").posting
    assert posting is not None and posting.extraction_status is ExtractionStatus.PARTIAL


def test_non_string_enum_values_are_rejected() -> None:
    rejected_on("employment_type", employment_type=1)


def test_seniority_requires_a_recognised_evidence_source() -> None:
    rejected_on("seniority", seniority={"raw_text": "Senior", "evidence_source": "gut feeling"})
    rejected_on("seniority", seniority={"raw_text": "Senior"})
    rejected_on("seniority", seniority={"raw_text": "  ", "evidence_source": "title"})


# --- salary, currency, dates ----------------------------------------------


@pytest.mark.parametrize("value", ["usd", "Eur", "INR"])
def test_currency_is_three_letters_uppercased(value: str) -> None:
    posting = accepted(salary_currency=value).posting
    assert posting is not None and posting.salary_currency == value.upper()


@pytest.mark.parametrize("value", ["", "US", "USDX", "U5D", "€ab", " us"])
def test_invalid_currency_is_rejected(value: str) -> None:
    rejected_on("salary_currency", salary_currency=value)


def test_salary_mirrors_the_database_constraints() -> None:
    rejected_on("salary_min", salary_min=-1)
    rejected(salary_min=200, salary_max=100)
    rejected_on("salary_min", salary_min="100")  # strict: no coercion
    assert accepted(salary_min=100, salary_max=100).posting is not None
    assert accepted(salary_max=50).posting is not None


@pytest.mark.parametrize("overrides", [{"salary_max": -1}, {"salary_min": 0, "salary_max": -1}])
def test_negative_salary_max_is_rejected_on_its_own(overrides: dict[str, int]) -> None:
    rejected_on("salary_max", **overrides)


@pytest.mark.parametrize("field", ["salary_min", "salary_max"])
def test_salary_must_fit_a_32_bit_integer(field: str) -> None:
    assert accepted(**{field: 2**31 - 1}).posting is not None
    rejected_on(field, **{field: 2**31})
    rejected_on(field, **{field: 3_000_000_000})


@pytest.mark.parametrize("value", ["01/10/2026", "yesterday", 20261001])
def test_posting_date_must_be_iso(value: object) -> None:
    rejected_on("posting_date", posting_date=value)


def test_unknown_fields_are_rejected() -> None:
    rejected_on("titel", titel="Engineer")


# --- mentions -------------------------------------------------------------


def test_valid_mentions_are_kept_with_their_normalized_form() -> None:
    result = accepted(mentions=[mention("Node.js"), mention("Go", "preferred")])
    assert [(m.mention_type, m.normalized_representation) for m in result.mentions] == [
        (JobMentionType.REQUIRED, "node js"),
        (JobMentionType.PREFERRED, "go"),
    ]
    assert result.dropped_mentions == ()


@pytest.mark.parametrize(
    "bad",
    [
        mention(""),
        mention("   "),
        mention("x" * 256),
        mention("Docker", "nice to have"),
        {"raw_text": "Docker"},
        {"mention_type": "required", "raw_text": "Docker", "weight": 1},
        {"mention_type": "required", "raw_text": 7},
        "Docker",
    ],
)
def test_malformed_mention_is_dropped_alone_and_forces_partial(bad: object) -> None:
    result = accepted(extraction_status="complete", mentions=[mention("Go"), bad])
    assert [m.raw_text for m in result.mentions] == ["Go"]
    assert len(result.dropped_mentions) == 1
    dropped = result.dropped_mentions[0]
    assert dropped.index == 1
    assert dropped.reasons
    assert result.posting is not None
    assert result.posting.extraction_status is ExtractionStatus.PARTIAL


def test_duplicate_mentions_collapse_to_the_first_occurrence() -> None:
    result = accepted(
        extraction_status="complete",
        mentions=[mention("Node.js"), mention("node js"), mention("NODE-JS")],
    )
    assert [m.raw_text for m in result.mentions] == ["Node.js"]
    assert result.dropped_mentions == ()
    # Collapsing is not a defect, so the extraction stays as claimed.
    assert result.posting is not None
    assert result.posting.extraction_status is ExtractionStatus.COMPLETE


def test_same_skill_under_two_mention_types_is_two_claims() -> None:
    result = accepted(mentions=[mention("Docker"), mention("Docker", "preferred")])
    assert len(result.mentions) == 2


def test_mentions_must_be_a_list() -> None:
    assert not validate_posting(0, raw_posting(mentions="Docker")).accepted


# --- fixtures and per-posting results -------------------------------------


def test_each_posting_gets_its_own_result_and_one_bad_posting_does_not_abort() -> None:
    result = validate_fixture(
        raw_fixture(
            raw_posting(),
            raw_posting(source_job_id="ext-2", company="  "),
            raw_posting(source_job_id="ext-3", mentions=[mention("")]),
        )
    )
    assert result.errors == ()
    assert result.source is not None
    assert result.source.type is JobSourceType.JOB_BOARD
    assert [p.index for p in result.postings] == [0, 1, 2]
    assert [p.accepted for p in result.postings] == [True, False, True]
    assert any(error.startswith("company") for error in result.postings[1].errors)
    assert len(result.postings[2].dropped_mentions) == 1


def test_a_rejected_posting_reports_every_reason() -> None:
    errors = rejected(title="", source_url="nope", work_mode="sometimes").errors
    assert {error.split(":")[0] for error in errors} >= {"title", "source_url", "work_mode"}


@pytest.mark.parametrize(
    ("source", "field"),
    [
        ({"name": "   "}, "source.name"),
        ({"base_url": "board.example.test"}, "source.base_url"),
        ({"type": "rss_feed"}, "source.type"),
    ],
)
def test_invalid_source_declaration_is_reported(source: dict[str, Any], field: str) -> None:
    result = validate_fixture(raw_fixture(raw_posting(), **source))
    assert result.source is None
    assert any(error.startswith(field) for error in result.errors)


@pytest.mark.parametrize(
    "payload",
    [[], "fixture", {"source": None, "postings": "x"}, {"postings": [], "extra": 1}],
)
def test_malformed_fixture_shapes_are_reported_not_raised(payload: object) -> None:
    assert validate_fixture(payload).errors


def test_unexpected_keys_of_mixed_types_are_reported_not_raised() -> None:
    payload: dict[Any, Any] = raw_fixture(raw_posting())
    payload.update({1: "x", "extra": "y", (2, 3): "z"})
    result = validate_fixture(payload)
    unexpected = [error for error in result.errors if "unexpected fields" in error]
    assert unexpected == ["input: unexpected fields 'extra', (2, 3), 1"]
    # The rest of the fixture is still validated.
    assert result.source is not None
    assert [p.accepted for p in result.postings] == [True]


# --- duplicate source_job_id within one fixture ---------------------------


def test_every_posting_sharing_a_source_job_id_is_rejected() -> None:
    result = validate_fixture(
        raw_fixture(
            raw_posting(source_job_id="ext-1"),
            raw_posting(source_job_id="ext-2"),
            raw_posting(source_job_id="ext-1", title="Staff Engineer"),
        )
    )
    assert [p.accepted for p in result.postings] == [False, True, False]
    for index in (0, 2):
        errors = result.postings[index].errors
        assert errors == (
            "source_job_id: 'ext-1' is duplicated within this fixture (postings 0, 2)",
        )
        assert result.postings[index].mentions == ()


def test_duplicate_rejection_keeps_other_reasons() -> None:
    result = validate_fixture(
        raw_fixture(
            raw_posting(source_job_id="ext-1"), raw_posting(source_job_id="ext-1", company="")
        )
    )
    second = result.postings[1].errors
    assert any(error.startswith("company") for error in second)
    assert any("duplicated" in error for error in second)
    assert not result.postings[0].accepted


def test_source_job_ids_are_compared_exactly_as_supplied() -> None:
    """The database compares them exactly, so "EXT-1" and "ext-1" are two postings."""
    result = validate_fixture(
        raw_fixture(raw_posting(source_job_id="ext-1"), raw_posting(source_job_id="EXT-1"))
    )
    assert [p.accepted for p in result.postings] == [True, True]
