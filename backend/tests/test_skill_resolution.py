"""Deterministic skill resolution.

No model is involved at any point: `ai_semantic` must be unreachable
from this cascade.
"""

import uuid

import pytest

from app.domain.normalization import normalize
from app.domain.resolution import (
    DETERMINISTIC_PROVENANCE,
    RepresentationRecord,
    ResolutionProvenance,
    ResolutionStatus,
    resolve_skill,
)
from app.models.enums import RepresentationKind, RepresentationOrigin

KUBERNETES_ID = uuid.uuid4()


class StubLookup:
    """Dict-backed stand-in for the catalog."""

    def __init__(
        self,
        records: dict[str, RepresentationRecord] | None = None,
        conflicts: set[str] | None = None,
    ) -> None:
        self._records = records or {}
        self._conflicts = conflicts or set()

    def find_by_normalized(self, normalized: str) -> RepresentationRecord | None:
        return self._records.get(normalized)

    def has_open_conflict(self, normalized: str) -> bool:
        return normalized in self._conflicts


def record(
    display_text: str,
    *,
    kind: RepresentationKind = RepresentationKind.CANONICAL,
    origin: RepresentationOrigin = RepresentationOrigin.CURATION,
    canonical_name: str = "Kubernetes",
    entry_id: uuid.UUID = KUBERNETES_ID,
) -> RepresentationRecord:
    return RepresentationRecord(
        entry_id=entry_id,
        normalized_representation=normalize(display_text),
        display_text=display_text,
        kind=kind,
        origin=origin,
        canonical_name=canonical_name,
    )


def lookup_with(*records: RepresentationRecord, conflicts: set[str] | None = None) -> StubLookup:
    return StubLookup({r.normalized_representation: r for r in records}, conflicts)


# --- canonical resolution -------------------------------------------------


def test_canonical_exact_match() -> None:
    result = resolve_skill("Kubernetes", lookup_with(record("Kubernetes")))
    assert result.status is ResolutionStatus.RESOLVED
    assert result.provenance is ResolutionProvenance.EXACT_MATCH
    assert result.skill_id == KUBERNETES_ID
    assert result.resolved_canonical_name == "Kubernetes"


def test_canonical_match_requiring_normalization() -> None:
    """Case and whitespace differences resolve, tagged `normalized`."""
    result = resolve_skill("  KUBERNETES  ", lookup_with(record("Kubernetes")))
    assert result.status is ResolutionStatus.RESOLVED
    assert result.provenance is ResolutionProvenance.NORMALIZED


# --- alias resolution -----------------------------------------------------


def test_alias_resolves_to_its_canonical_name() -> None:
    """An alias reports the CANONICAL name, not its own text.

    This is what makes historical denormalization correct.
    """
    alias = record("K8s", kind=RepresentationKind.ALIAS, canonical_name="Kubernetes")
    result = resolve_skill("k8s", lookup_with(alias))

    assert result.status is ResolutionStatus.RESOLVED
    assert result.provenance is ResolutionProvenance.ALIAS_RULE
    assert result.resolved_canonical_name == "Kubernetes"
    assert result.skill_id == KUBERNETES_ID


def test_alias_and_canonical_resolve_to_the_same_skill() -> None:
    catalog = lookup_with(
        record("Kubernetes"),
        record("K8s", kind=RepresentationKind.ALIAS),
    )
    assert (
        resolve_skill("Kubernetes", catalog).skill_id
        == resolve_skill("K8s", catalog).skill_id
    )


def test_exactly_typed_alias_reports_as_an_alias_not_an_exact_match() -> None:
    """Resolves an overlap in the documented provenance tiers.

    `exact_match` reads "identical to a canonical name or alias" and
    `alias_rule` reads "matched an explicit catalogued alias", so typing
    "AWS" exactly satisfies both. The tie breaks toward the alias
    because the required explanation is "matched via catalogued alias" —
    calling it an exact match would hide the fact the user needs.
    """
    alias = record("AWS", kind=RepresentationKind.ALIAS, canonical_name="Amazon Web Services")
    result = resolve_skill("AWS", lookup_with(alias))

    assert result.raw_text == alias.display_text  # an exact textual hit
    assert result.provenance is ResolutionProvenance.ALIAS_RULE


def test_canonical_untransformed_hit_is_exact_match() -> None:
    result = resolve_skill("Kubernetes", lookup_with(record("Kubernetes")))
    assert result.provenance is ResolutionProvenance.EXACT_MATCH


def test_user_confirmed_outranks_other_tiers() -> None:
    confirmed = record(
        "K8S cluster",
        kind=RepresentationKind.ALIAS,
        origin=RepresentationOrigin.USER_CONFIRMATION,
    )
    result = resolve_skill("K8S cluster", lookup_with(confirmed))
    assert result.provenance is ResolutionProvenance.USER_CONFIRMED


# --- unresolved and indeterminate -----------------------------------------


def test_unknown_representation_is_unresolved_not_an_error() -> None:
    result = resolve_skill("SomeNewTechnologyX", lookup_with())
    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.provenance is ResolutionProvenance.UNRESOLVED
    assert result.reason == "not_in_catalog"
    assert result.skill_id is None


def test_malformed_mention_is_rejected_as_a_defect() -> None:
    result = resolve_skill("   ", lookup_with())
    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.reason == "malformed_mention"


def test_open_conflict_yields_indeterminate_not_a_guess() -> None:
    """Declining to resolve is correct; tie-breaking is prohibited."""
    catalog = lookup_with(record("ADF"), conflicts={normalize("ADF")})
    result = resolve_skill("ADF", catalog)

    assert result.status is ResolutionStatus.INDETERMINATE
    assert result.reason == "open_catalog_conflict"
    assert result.skill_id is None


def test_indeterminate_is_distinct_from_unresolved() -> None:
    """Uncertainty must never be flattened into absence."""
    assert ResolutionStatus.INDETERMINATE != ResolutionStatus.UNRESOLVED


# --- AI boundary ----------------------------------------------------------


@pytest.mark.invariant
def test_deterministic_cascade_never_emits_ai_semantic() -> None:
    """AI-MATCHING invariant 3: ai_semantic never enters Stage 2 silently."""
    catalog = lookup_with(
        record("Kubernetes"),
        record("K8s", kind=RepresentationKind.ALIAS),
    )
    for raw in ["Kubernetes", "k8s", "KUBERNETES", "unknown-thing", "", "   "]:
        assert resolve_skill(raw, catalog).provenance is not ResolutionProvenance.AI_SEMANTIC


@pytest.mark.invariant
def test_ai_semantic_is_not_a_deterministic_tier() -> None:
    assert ResolutionProvenance.AI_SEMANTIC not in DETERMINISTIC_PROVENANCE
    assert len(DETERMINISTIC_PROVENANCE) == 4


def test_resolutions_report_whether_they_are_deterministic() -> None:
    resolved = resolve_skill("Kubernetes", lookup_with(record("Kubernetes")))
    assert resolved.is_deterministic is True


@pytest.mark.invariant
def test_resolution_is_reproducible() -> None:
    """Stage 2 re-executability: same inputs give the same output."""
    catalog = lookup_with(record("Kubernetes"))
    assert resolve_skill("Kubernetes", catalog) == resolve_skill("Kubernetes", catalog)
