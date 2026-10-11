"""Fixture-ingestion input contract and its validation.

Ingestion is fixture-driven (ARCHITECTURE.md Section 3.3): postings come
from local files through the same validated contract a fetcher would
produce. Fixture content is UNTRUSTED input — validated, never trusted.

Pure validation only: no database access, no persistence, no catalog
resolution and no model calls. A fixture is one source declaration plus
a list of postings:

    {"source": {"name", "base_url", "type"},
     "postings": [{...posting fields..., "mentions": [...]}]}

Each posting is validated independently and reported on its own; one
bad posting never aborts the batch. Policy (DATABASE.md Sections 2.15
and 4.4):

  * company and title must not normalize to empty; a supplied location
    must not either, while None means "not stated".
  * Text and URLs are kept exactly as supplied — never trimmed or
    truncated. A value that does not fit is rejected.
  * URLs must be absolute http(s) with a host and no whitespace.
  * Enum fields match mechanically (`normalize()` on both sides). An
    unrecognised employment_type or status becomes `unknown` and an
    unrecognised extraction_status becomes `partial`, the values that
    claim nothing. An unrecognised work_mode, source type or seniority
    evidence_source rejects, because no member can say "unrecognised".
  * direct_apply_verification_status is not an input: a fixture cannot
    claim a verified link. It is `unverified` when a URL is present and
    `not_available` otherwise.
  * A malformed mention is dropped on its own, with its reason, and
    forces extraction_status to `partial`: the extraction is then
    demonstrably incomplete. Duplicates within (mention_type,
    normalized_representation) collapse to the first occurrence.
  * Postings sharing a source_job_id within one fixture are ALL rejected.
  * Salaries are non-negative, fit a 32-bit INTEGER, and max >= min.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, Final
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from app.domain.normalization import is_valid_representation, normalize
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

# Column widths in app/models/job.py. Over-long values are rejected,
# never truncated.
MAX_URL_LENGTH: Final[int] = 2048
MAX_TEXT_LENGTH: Final[int] = 512
MAX_SOURCE_NAME_LENGTH: Final[int] = 255
MAX_MENTION_LENGTH: Final[int] = 255
# Salaries are PostgreSQL INTEGER (signed 32-bit). Checked here so an
# out-of-range value is a validation error, not a failed insert.
MAX_SALARY: Final[int] = 2**31 - 1


def _match[E: StrEnum](value: object, vocabulary: type[E]) -> E | None:
    """The member `value` names after mechanical normalization, if any."""
    if not isinstance(value, str):
        raise ValueError("must be a string")
    key = normalize(value)
    return next((member for member in vocabulary if normalize(member.value) == key), None)


def _required_member[E: StrEnum](value: object, vocabulary: type[E]) -> E:
    member = _match(value, vocabulary)
    if member is None:
        allowed = ", ".join(m.value for m in vocabulary)
        raise ValueError(f"unrecognised value {value!r}; expected one of: {allowed}")
    return member


def _check_url(value: str) -> str:
    if len(value) > MAX_URL_LENGTH:
        raise ValueError(f"longer than {MAX_URL_LENGTH} characters")
    if any(character.isspace() for character in value):
        raise ValueError("must not contain whitespace")
    try:
        parts = urlsplit(value)
    except ValueError as error:
        raise ValueError(f"not a valid URL: {error}") from None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("must be an absolute http or https URL with a host")
    return value


def _check_identity_text(value: str, max_length: int = MAX_TEXT_LENGTH) -> str:
    if not normalize(value):
        raise ValueError("must not be empty, whitespace-only or normalize to empty")
    if len(value) > max_length:
        raise ValueError(f"longer than {max_length} characters")
    return value


class _Contract(BaseModel):
    # strict: no silent coercion ("5" is not an int); forbid: a misspelt
    # or unapproved field (e.g. a claimed verification status) rejects.
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class SourceDeclaration(_Contract):
    """The one JobSource a fixture's postings were discovered on."""

    name: str
    base_url: str
    type: JobSourceType

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _check_identity_text(value, MAX_SOURCE_NAME_LENGTH)

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, value: str) -> str:
        return _check_url(value)

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, value: object) -> JobSourceType:
        return _required_member(value, JobSourceType)


class SeniorityInput(_Contract):
    """The posting's seniority wording and where it came from. Unresolved."""

    raw_text: str
    evidence_source: EvidenceSource

    @field_validator("raw_text")
    @classmethod
    def _raw_text(cls, value: str) -> str:
        if not is_valid_representation(value):
            raise ValueError("empty, whitespace-only or over-long seniority text")
        if len(value) > MAX_TEXT_LENGTH:
            raise ValueError(f"longer than {MAX_TEXT_LENGTH} characters")
        return value

    @field_validator("evidence_source", mode="before")
    @classmethod
    def _evidence_source(cls, value: object) -> EvidenceSource:
        return _required_member(value, EvidenceSource)


class MentionInput(_Contract):
    """One job-side skill mention, as the source separated it. Unresolved."""

    mention_type: JobMentionType
    raw_text: str

    @field_validator("mention_type", mode="before")
    @classmethod
    def _mention_type(cls, value: object) -> JobMentionType:
        return _required_member(value, JobMentionType)

    @field_validator("raw_text")
    @classmethod
    def _raw_text(cls, value: str) -> str:
        # Extraction defects, not unresolved skills (DATABASE.md 4.4).
        if not is_valid_representation(value) or len(value) > MAX_MENTION_LENGTH:
            raise ValueError("empty, whitespace-only or over-long mention text")
        return value

    @property
    def normalized_representation(self) -> str:
        return normalize(self.raw_text)


class PostingInput(_Contract):
    """One posting as one source observed it. Mentions are validated apart."""

    # --- source observation (JobPostingSource) ---
    source_job_id: str
    source_url: str

    # --- the canonical job (JobPosting) ---
    title: str
    company: str
    location: str | None = None
    work_mode: WorkMode | None = None
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    seniority: SeniorityInput | None = None
    experience_requirements: str | None = None
    description_raw: str | None = None
    official_url: str | None = None
    direct_apply_url: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    posting_date: date | None = None
    status: JobPostingStatus = JobPostingStatus.UNKNOWN
    extraction_status: ExtractionStatus = ExtractionStatus.PARTIAL

    @field_validator("source_job_id")
    @classmethod
    def _source_job_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        if len(value) > MAX_TEXT_LENGTH:
            raise ValueError(f"longer than {MAX_TEXT_LENGTH} characters")
        return value

    @field_validator("source_url", "official_url", "direct_apply_url")
    @classmethod
    def _urls(cls, value: str | None) -> str | None:
        return None if value is None else _check_url(value)

    @field_validator("title", "company")
    @classmethod
    def _identity(cls, value: str) -> str:
        return _check_identity_text(value)

    @field_validator("location")
    @classmethod
    def _location(cls, value: str | None) -> str | None:
        return None if value is None else _check_identity_text(value)

    @field_validator("work_mode", mode="before")
    @classmethod
    def _work_mode(cls, value: object) -> WorkMode | None:
        return None if value is None else _required_member(value, WorkMode)

    @field_validator("employment_type", mode="before")
    @classmethod
    def _employment_type(cls, value: object) -> EmploymentType:
        if value is None:
            return EmploymentType.UNKNOWN
        return _match(value, EmploymentType) or EmploymentType.UNKNOWN

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, value: object) -> JobPostingStatus:
        if value is None:
            return JobPostingStatus.UNKNOWN
        return _match(value, JobPostingStatus) or JobPostingStatus.UNKNOWN

    @field_validator("extraction_status", mode="before")
    @classmethod
    def _extraction_status(cls, value: object) -> ExtractionStatus:
        if value is None:
            return ExtractionStatus.PARTIAL
        return _match(value, ExtractionStatus) or ExtractionStatus.PARTIAL

    @field_validator("salary_min", "salary_max")
    @classmethod
    def _salary_bounds(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 0:
            raise ValueError("must not be negative")
        if value > MAX_SALARY:
            raise ValueError(f"must not exceed {MAX_SALARY}")
        return value

    @field_validator("salary_currency")
    @classmethod
    def _currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) != 3 or not (value.isascii() and value.isalpha()):
            raise ValueError("must be exactly three alphabetic characters")
        return value.upper()

    @field_validator("posting_date", mode="before")
    @classmethod
    def _posting_date(cls, value: object) -> date | None:
        if value is None or isinstance(value, date):
            return value
        if not isinstance(value, str):
            raise ValueError("must be an ISO date string")
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ValueError("must be an ISO date string") from None

    @model_validator(mode="after")
    def _salary_order(self) -> "PostingInput":
        # Mirrors ck_job_posting_salary_range_ordered. The per-field bounds
        # above are stricter than the database, which allows a negative
        # salary_max when salary_min is absent.
        if (
            self.salary_min is not None
            and self.salary_max is not None
            and self.salary_max < self.salary_min
        ):
            raise ValueError("salary_max must not be below salary_min")
        return self

    @property
    def direct_apply_verification_status(self) -> DirectApplyVerificationStatus:
        """Never taken from input: verification is not something a fixture can claim."""
        if self.direct_apply_url is None:
            return DirectApplyVerificationStatus.NOT_AVAILABLE
        return DirectApplyVerificationStatus.UNVERIFIED


@dataclass(frozen=True)
class DroppedMention:
    index: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PostingResult:
    """The outcome for one posting: accepted (posting set) or rejected (errors set)."""

    index: int
    posting: PostingInput | None
    mentions: tuple[MentionInput, ...] = ()
    dropped_mentions: tuple[DroppedMention, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.posting is not None


@dataclass(frozen=True)
class FixtureResult:
    source: SourceDeclaration | None
    errors: tuple[str, ...]
    postings: tuple[PostingResult, ...]


def _reasons(error: ValidationError) -> tuple[str, ...]:
    return tuple(
        f"{'.'.join(str(part) for part in item['loc']) or 'input'}: {item['msg']}"
        for item in error.errors()
    )


def _validate_mentions(
    raw_mentions: Sequence[object],
) -> tuple[tuple[MentionInput, ...], tuple[DroppedMention, ...]]:
    kept: dict[tuple[JobMentionType, str], MentionInput] = {}
    dropped: list[DroppedMention] = []
    for index, raw in enumerate(raw_mentions):
        try:
            mention = MentionInput.model_validate(raw)
        except ValidationError as error:
            dropped.append(DroppedMention(index, _reasons(error)))
            continue
        # Multiplicity within a document is not evidence (DATABASE.md 4.4).
        kept.setdefault((mention.mention_type, mention.normalized_representation), mention)
    return tuple(kept.values()), tuple(dropped)


def validate_posting(index: int, raw: object) -> PostingResult:
    """Validate one posting; never raises for bad input."""
    if not isinstance(raw, Mapping):
        return PostingResult(index, None, errors=("input: posting must be an object",))
    fields = dict(raw)
    raw_mentions = fields.pop("mentions", [])
    if not isinstance(raw_mentions, list):
        return PostingResult(index, None, errors=("mentions: must be a list",))
    try:
        posting = PostingInput.model_validate(fields)
    except ValidationError as error:
        return PostingResult(index, None, errors=_reasons(error))

    mentions, dropped = _validate_mentions(raw_mentions)
    if dropped:
        posting = posting.model_copy(update={"extraction_status": ExtractionStatus.PARTIAL})
    return PostingResult(index, posting, mentions, dropped)


def validate_fixture(payload: object) -> FixtureResult:
    """Validate a whole fixture; every posting gets its own result."""
    if not isinstance(payload, Mapping):
        return FixtureResult(None, ("input: fixture must be an object",), ())
    errors: list[str] = []
    # repr() first: keys of mixed types do not sort against each other.
    unexpected = sorted(repr(key) for key in set(payload) - {"source", "postings"})
    if unexpected:
        errors.append(f"input: unexpected fields {', '.join(unexpected)}")

    source: SourceDeclaration | None = None
    try:
        source = SourceDeclaration.model_validate(payload.get("source"))
    except ValidationError as error:
        errors.extend(f"source.{reason}" for reason in _reasons(error))

    raw_postings: Any = payload.get("postings")
    if not isinstance(raw_postings, list):
        errors.append("postings: must be a list")
        return FixtureResult(source, tuple(errors), ())
    results = [validate_posting(index, raw) for index, raw in enumerate(raw_postings)]
    return FixtureResult(
        source, tuple(errors), _reject_duplicate_source_job_ids(raw_postings, results)
    )


def _reject_duplicate_source_job_ids(
    raw_postings: list[object], results: list[PostingResult]
) -> tuple[PostingResult, ...]:
    """Reject EVERY posting sharing a source_job_id; none is kept arbitrarily.

    One source lists one external posting once (UNIQUE (job_source_id,
    source_job_id)), so two postings claiming one id within a fixture are
    contradictory input. Ids are compared exactly as supplied, as the
    database compares them, and a posting already rejected for other
    reasons still counts.
    """
    indices: dict[str, list[int]] = {}
    for index, raw in enumerate(raw_postings):
        source_job_id = raw.get("source_job_id") if isinstance(raw, Mapping) else None
        if isinstance(source_job_id, str):
            indices.setdefault(source_job_id, []).append(index)

    rejected = list(results)
    for source_job_id, involved in indices.items():
        if len(involved) < 2:
            continue
        error = (
            f"source_job_id: {source_job_id!r} is duplicated within this fixture "
            f"(postings {', '.join(map(str, involved))})"
        )
        for index in involved:
            rejected[index] = PostingResult(index, None, errors=(*results[index].errors, error))
    return tuple(rejected)
