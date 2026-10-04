"""Seniority resolution and partial-order comparison."""

import uuid

import pytest

from app.domain.normalization import normalize
from app.domain.resolution import ResolutionProvenance, ResolutionStatus
from app.domain.seniority import (
    EvidenceSource,
    SeniorityComparisonOutcome,
    SeniorityLevelRecord,
    SeniorityRepresentationRecord,
    SeniorityResolution,
    compare_levels,
    compare_seniority,
    resolve_seniority,
)
from app.models.enums import RepresentationKind, RepresentationOrigin, SeniorityTrack

IC_LADDER = ["Intern/Trainee", "Junior", "Mid", "Senior", "Staff", "Principal"]
MANAGEMENT_LADDER = ["Manager", "Senior Manager", "Director", "VP"]


def level(name: str, track: SeniorityTrack, rank: int) -> SeniorityLevelRecord:
    return SeniorityLevelRecord(
        level_id=uuid.uuid5(uuid.NAMESPACE_DNS, f"{track}:{name}"),
        canonical_name=name,
        track=track,
        rank_within_track=rank,
    )


def representation(
    display_text: str,
    lvl: SeniorityLevelRecord,
    *,
    kind: RepresentationKind = RepresentationKind.CANONICAL,
    origin: RepresentationOrigin = RepresentationOrigin.CURATION,
) -> SeniorityRepresentationRecord:
    return SeniorityRepresentationRecord(
        normalized_representation=normalize(display_text),
        display_text=display_text,
        kind=kind,
        origin=origin,
        level=lvl,
    )


class StubLookup:
    def __init__(self, records: list[SeniorityRepresentationRecord]) -> None:
        self._by_normalized = {r.normalized_representation: r for r in records}

    def find_by_normalized(self, normalized: str) -> SeniorityRepresentationRecord | None:
        return self._by_normalized.get(normalized)


def seeded_lookup() -> StubLookup:
    records: list[SeniorityRepresentationRecord] = []
    for rank, name in enumerate(IC_LADDER, start=1):
        records.append(representation(name, level(name, SeniorityTrack.IC, rank)))
    for rank, name in enumerate(MANAGEMENT_LADDER, start=1):
        records.append(representation(name, level(name, SeniorityTrack.MANAGEMENT, rank)))
    senior = level("Senior", SeniorityTrack.IC, 4)
    records.append(representation("Sr", senior, kind=RepresentationKind.ALIAS))
    return StubLookup(records)


def resolve(raw: str) -> SeniorityResolution:
    return resolve_seniority(raw, seeded_lookup(), EvidenceSource.TITLE)


# --- ordering within a track ----------------------------------------------


def test_ic_ladder_is_ordered() -> None:
    ranks = [resolve(name).resolved_rank_within_track for name in IC_LADDER]
    assert ranks == sorted(ranks)
    assert ranks == [1, 2, 3, 4, 5, 6]


def test_management_ladder_is_ordered() -> None:
    ranks = [resolve(name).resolved_rank_within_track for name in MANAGEMENT_LADDER]
    assert ranks == [1, 2, 3, 4]


def test_tracks_are_distinct() -> None:
    assert resolve("Senior").resolved_track is SeniorityTrack.IC
    assert resolve("Director").resolved_track is SeniorityTrack.MANAGEMENT


@pytest.mark.invariant
def test_equal_ranks_in_different_tracks_are_not_equal_levels() -> None:
    """Rank is meaningful only within a track.

    IC rank 4 (Senior) and Management rank 4 (VP) share a number and
    mean nothing to each other.
    """
    ic = resolve("Senior")
    mgmt = resolve("VP")
    assert ic.resolved_rank_within_track == mgmt.resolved_rank_within_track
    assert compare_seniority(ic, mgmt) is SeniorityComparisonOutcome.DIFFERENT_TRACK


# --- comparison -----------------------------------------------------------


def test_same_level_is_at_level() -> None:
    assert compare_seniority(resolve("Senior"), resolve("Senior")) is (
        SeniorityComparisonOutcome.AT_LEVEL
    )


def test_higher_subject_is_above() -> None:
    assert compare_seniority(resolve("Senior"), resolve("Staff")) is (
        SeniorityComparisonOutcome.ABOVE
    )


def test_lower_subject_is_below() -> None:
    assert compare_seniority(resolve("Senior"), resolve("Junior")) is (
        SeniorityComparisonOutcome.BELOW
    )


@pytest.mark.invariant
def test_cross_track_never_claims_a_magnitude() -> None:
    """Principal -> Director is a track change, not +1."""
    outcome = compare_seniority(resolve("Principal"), resolve("Director"))
    assert outcome is SeniorityComparisonOutcome.DIFFERENT_TRACK
    assert outcome not in {
        SeniorityComparisonOutcome.ABOVE,
        SeniorityComparisonOutcome.BELOW,
    }


def test_compare_levels_matches_resolution_comparison() -> None:
    senior = level("Senior", SeniorityTrack.IC, 4)
    staff = level("Staff", SeniorityTrack.IC, 5)
    assert compare_levels(senior, staff) is SeniorityComparisonOutcome.ABOVE
    assert compare_levels(staff, senior) is SeniorityComparisonOutcome.BELOW
    assert compare_levels(senior, senior) is SeniorityComparisonOutcome.AT_LEVEL


# --- ambiguous titles -----------------------------------------------------


@pytest.mark.invariant
@pytest.mark.parametrize("title", ["Lead", "Architect", "Associate", "Head"])
def test_ambiguous_titles_are_indeterminate(title: str) -> None:
    """Lead is not Senior, Architect is not Principal, Head is not VP."""
    result = resolve(title)
    assert result.status is ResolutionStatus.INDETERMINATE
    assert result.reason == "ambiguous_title"
    assert result.resolved_level_name is None
    assert result.resolved_track is None


def test_ambiguous_title_in_comparison_is_indeterminate() -> None:
    assert compare_seniority(resolve("Senior"), resolve("Lead")) is (
        SeniorityComparisonOutcome.INDETERMINATE
    )


@pytest.mark.invariant
@pytest.mark.parametrize("title", ["Engineer", "Software Engineer", "Developer"])
def test_role_nouns_carry_no_seniority_signal(title: str) -> None:
    """Absence of a modifier is unknown, never a default of Mid."""
    result = resolve(title)
    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.reason == "no_seniority_signal"
    assert result.resolved_level_name is None


@pytest.mark.invariant
def test_no_substring_matching() -> None:
    """"Senior Cloud Engineer" must not resolve via the word "Senior".

    Matching is whole-string against catalogued rules; a substring scan
    would silently invent seniority for arbitrary titles.
    """
    result = resolve("Senior Cloud Engineer")
    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.resolved_level_name is None


@pytest.mark.invariant
def test_lead_devops_engineer_does_not_become_senior() -> None:
    result = resolve("Lead DevOps Engineer")
    assert result.resolved_level_name is None
    assert result.status is not ResolutionStatus.RESOLVED


def test_unknown_title_is_unresolved() -> None:
    result = resolve("Chief Vibes Officer")
    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.reason == "not_in_catalog"


# --- modifiers, provenance and evidence source ----------------------------


def test_catalogued_modifier_alias_resolves() -> None:
    result = resolve("Sr")
    assert result.status is ResolutionStatus.RESOLVED
    assert result.resolved_level_name == "Senior"
    assert result.provenance is ResolutionProvenance.ALIAS_RULE


def test_modifier_alias_is_case_insensitive() -> None:
    assert resolve("sr").resolved_level_name == "Senior"


@pytest.mark.invariant
def test_evidence_source_is_separate_from_provenance() -> None:
    """They answer different questions and must never be merged.

    Same text, same provenance, different authority.
    """
    from_field = resolve_seniority(
        "Senior", seeded_lookup(), EvidenceSource.STRUCTURED_FIELD
    )
    from_description = resolve_seniority(
        "Senior", seeded_lookup(), EvidenceSource.DESCRIPTION
    )

    assert from_field.provenance is from_description.provenance
    assert from_field.evidence_source is not from_description.evidence_source


def test_unresolved_sides_compare_as_unknown() -> None:
    assert compare_seniority(resolve("Senior"), resolve("Engineer")) is (
        SeniorityComparisonOutcome.UNKNOWN
    )


@pytest.mark.invariant
def test_seniority_resolution_never_emits_ai_semantic() -> None:
    for raw in ["Senior", "Lead", "Engineer", "Sr", "nonsense", ""]:
        result = resolve(raw)
        assert result.provenance is not ResolutionProvenance.AI_SEMANTIC
