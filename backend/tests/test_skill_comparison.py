"""Stage 2 skill-pair comparison (AI-MATCHING.md Section 5).

Pure: a stub relationship lookup stands in for the catalog, so no
database, network or model is involved.
"""

import uuid

import pytest

from app.domain.resolution import SkillResolution
from app.domain.skill_comparison import (
    FamilyDirection,
    PairOutcome,
    PairRelationship,
    compare_skill_pair,
)
from app.models.enums import ResolutionProvenance, ResolutionStatus, SkillRelationshipType

EKS, KUBERNETES, DOCKER, PROMETHEUS, GRAFANA, LINUX = (uuid.uuid4() for _ in range(6))
NAMES = {
    EKS: "Amazon EKS",
    KUBERNETES: "Kubernetes",
    DOCKER: "Docker",
    PROMETHEUS: "Prometheus",
    GRAFANA: "Grafana",
    LINUX: "Linux",
}


class StubRelationships:
    """The seeded shape: EKS -> Kubernetes family; adjacency stored one way."""

    def __init__(self, rows: set[tuple[uuid.UUID, uuid.UUID, SkillRelationshipType]]) -> None:
        self.rows = rows

    def has_relationship(
        self,
        from_skill_id: uuid.UUID,
        to_skill_id: uuid.UUID,
        relationship_type: SkillRelationshipType,
    ) -> bool:
        return (from_skill_id, to_skill_id, relationship_type) in self.rows


CATALOG = StubRelationships(
    {
        (EKS, KUBERNETES, SkillRelationshipType.FAMILY),
        (PROMETHEUS, GRAFANA, SkillRelationshipType.ADJACENT),
        (DOCKER, KUBERNETES, SkillRelationshipType.ADJACENT),
    }
)


def resolved(
    skill_id: uuid.UUID,
    raw_text: str | None = None,
    provenance: ResolutionProvenance = ResolutionProvenance.EXACT_MATCH,
) -> SkillResolution:
    text = raw_text or NAMES[skill_id]
    return SkillResolution(
        raw_text=text,
        normalized_representation=text.casefold(),
        status=ResolutionStatus.RESOLVED,
        provenance=provenance,
        skill_id=skill_id,
        resolved_canonical_name=NAMES[skill_id],
    )


def unresolved(raw_text: str, normalized: str | None = None) -> SkillResolution:
    return SkillResolution(
        raw_text=raw_text,
        normalized_representation=normalized or raw_text.casefold(),
        status=ResolutionStatus.UNRESOLVED,
        provenance=ResolutionProvenance.UNRESOLVED,
    )


def indeterminate(raw_text: str) -> SkillResolution:
    return SkillResolution(
        raw_text=raw_text,
        normalized_representation=raw_text.casefold(),
        status=ResolutionStatus.INDETERMINATE,
        provenance=ResolutionProvenance.UNRESOLVED,
    )


# --- the six rows of Section 5 ------------------------------------------------


def test_same_resolved_skill_is_an_identity_match() -> None:
    result = compare_skill_pair(resolved(DOCKER), resolved(DOCKER), CATALOG)
    assert (result.outcome, result.relationship) == (PairOutcome.MATCH, PairRelationship.IDENTITY)
    assert result.reason is None
    assert result.establishing_stage == 2


@pytest.mark.parametrize("side", ["profile", "job"])
def test_identity_reached_through_an_alias_is_classed_alias(side: str) -> None:
    alias = resolved(KUBERNETES, "K8s", ResolutionProvenance.ALIAS_RULE)
    canonical = resolved(KUBERNETES)
    pair = (alias, canonical) if side == "profile" else (canonical, alias)
    result = compare_skill_pair(*pair, CATALOG)
    assert (result.outcome, result.relationship) == (PairOutcome.MATCH, PairRelationship.ALIAS)


def test_different_unrelated_resolved_skills_are_a_definite_non_identity() -> None:
    result = compare_skill_pair(resolved(LINUX), resolved(DOCKER), CATALOG)
    assert (result.outcome, result.relationship) == (
        PairOutcome.NON_IDENTITY,
        PairRelationship.NONE,
    )
    assert result.family_direction is None and result.reason is None


@pytest.mark.parametrize(
    ("profile", "job"),
    [(resolved(DOCKER), unresolved("Podman")), (unresolved("Podman"), resolved(DOCKER))],
)
def test_resolved_against_unresolved_is_indeterminate(
    profile: SkillResolution, job: SkillResolution
) -> None:
    result = compare_skill_pair(profile, job, CATALOG)
    assert result.outcome is PairOutcome.INDETERMINATE
    assert result.reason == "one_side_unresolved"


def test_unresolved_pair_with_identical_normalized_text_matches() -> None:
    """A brand-new technology is matchable the day it appears."""
    result = compare_skill_pair(
        unresolved("Quantum.Weave", "quantum weave"),
        unresolved("quantum-weave", "quantum weave"),
        CATALOG,
    )
    assert (result.outcome, result.relationship) == (
        PairOutcome.MATCH,
        PairRelationship.NORMALIZED_TEXT,
    )


def test_unresolved_pair_with_different_text_is_indeterminate() -> None:
    result = compare_skill_pair(unresolved("Podman"), unresolved("Buildah"), CATALOG)
    assert result.outcome is PairOutcome.INDETERMINATE
    assert result.reason == "unresolved_different_text"


# --- family, adjacency and invariant 13 ----------------------------------------


@pytest.mark.parametrize(
    ("profile", "job", "direction"),
    [
        (EKS, KUBERNETES, FamilyDirection.PROFILE_SPECIALIZES_JOB),
        (KUBERNETES, EKS, FamilyDirection.JOB_SPECIALIZES_PROFILE),
    ],
)
def test_family_is_a_non_identity_that_keeps_its_direction(
    profile: uuid.UUID, job: uuid.UUID, direction: FamilyDirection
) -> None:
    result = compare_skill_pair(resolved(profile), resolved(job), CATALOG)
    assert (result.outcome, result.relationship) == (
        PairOutcome.NON_IDENTITY,
        PairRelationship.FAMILY,
    )
    assert result.family_direction is direction


@pytest.mark.parametrize(
    ("profile", "job"),
    [(PROMETHEUS, GRAFANA), (GRAFANA, PROMETHEUS), (DOCKER, KUBERNETES), (KUBERNETES, DOCKER)],
)
def test_adjacency_is_symmetric_and_never_a_match(profile: uuid.UUID, job: uuid.UUID) -> None:
    result = compare_skill_pair(resolved(profile), resolved(job), CATALOG)
    assert (result.outcome, result.relationship) == (
        PairOutcome.NON_IDENTITY,
        PairRelationship.ADJACENT,
    )
    assert result.family_direction is None


@pytest.mark.parametrize(
    "extra",
    [
        (KUBERNETES, EKS, SkillRelationshipType.FAMILY),
        (EKS, KUBERNETES, SkillRelationshipType.ADJACENT),
    ],
)
def test_contradictory_catalog_relationships_are_not_tie_broken(
    extra: tuple[uuid.UUID, uuid.UUID, SkillRelationshipType],
) -> None:
    catalog = StubRelationships({*CATALOG.rows, extra})
    result = compare_skill_pair(resolved(EKS), resolved(KUBERNETES), catalog)
    assert result.outcome is PairOutcome.INDETERMINATE
    assert result.reason == "contradictory_relationships"


def test_relationships_are_not_consulted_for_identity() -> None:
    class Exploding:
        def has_relationship(self, *args: object) -> bool:
            raise AssertionError("identity must not depend on relationships")

    result = compare_skill_pair(resolved(DOCKER), resolved(DOCKER), Exploding())
    assert result.outcome is PairOutcome.MATCH


# --- uncertainty and ai_semantic ------------------------------------------------


@pytest.mark.parametrize("side", ["profile", "job"])
def test_ai_semantic_is_never_deterministic_evidence(side: str) -> None:
    """Invariant 3: even an ai_semantic claim of the SAME skill is not a match."""
    semantic = resolved(DOCKER, "dockr", ResolutionProvenance.AI_SEMANTIC)
    pair = (semantic, resolved(DOCKER)) if side == "profile" else (resolved(DOCKER), semantic)
    result = compare_skill_pair(*pair, CATALOG)
    assert result.outcome is PairOutcome.INDETERMINATE
    assert result.reason == "non_deterministic_provenance"


@pytest.mark.parametrize("side", ["profile", "job"])
def test_indeterminate_side_is_never_matched_even_on_identical_text(side: str) -> None:
    """An open catalog conflict must not masquerade as an unresolved text match."""
    pair = (
        (indeterminate("Lead"), unresolved("Lead"))
        if side == "profile"
        else (unresolved("Lead"), indeterminate("Lead"))
    )
    result = compare_skill_pair(*pair, CATALOG)
    assert result.outcome is PairOutcome.INDETERMINATE
    assert result.reason == "side_indeterminate"


P = ResolutionProvenance


@pytest.mark.parametrize(
    ("profile_tier", "job_tier", "relationship"),
    [
        (P.EXACT_MATCH, P.EXACT_MATCH, PairRelationship.IDENTITY),
        (P.NORMALIZED, P.EXACT_MATCH, PairRelationship.IDENTITY),
        (P.EXACT_MATCH, P.NORMALIZED, PairRelationship.IDENTITY),
        (P.USER_CONFIRMED, P.EXACT_MATCH, PairRelationship.IDENTITY),
        (P.EXACT_MATCH, P.USER_CONFIRMED, PairRelationship.IDENTITY),
        (P.USER_CONFIRMED, P.NORMALIZED, PairRelationship.IDENTITY),
        (P.ALIAS_RULE, P.EXACT_MATCH, PairRelationship.ALIAS),
        (P.EXACT_MATCH, P.ALIAS_RULE, PairRelationship.ALIAS),
        (P.ALIAS_RULE, P.ALIAS_RULE, PairRelationship.ALIAS),
        (P.ALIAS_RULE, P.USER_CONFIRMED, PairRelationship.ALIAS),
    ],
)
def test_each_deterministic_tier_gives_its_relationship_class(
    profile_tier: ResolutionProvenance,
    job_tier: ResolutionProvenance,
    relationship: PairRelationship,
) -> None:
    result = compare_skill_pair(
        resolved(KUBERNETES, "K8s", profile_tier), resolved(KUBERNETES, "kube", job_tier), CATALOG
    )
    assert (result.outcome, result.relationship) == (PairOutcome.MATCH, relationship)
    assert (result.profile.provenance, result.job.provenance) == (profile_tier, job_tier)


@pytest.mark.parametrize("side", ["profile", "job"])
def test_resolved_without_a_skill_id_is_incoherent_not_semantic(side: str) -> None:
    broken = SkillResolution(
        raw_text="Docker",
        normalized_representation="docker",
        status=ResolutionStatus.RESOLVED,
        provenance=ResolutionProvenance.EXACT_MATCH,
    )
    pair = (broken, resolved(DOCKER)) if side == "profile" else (resolved(DOCKER), broken)
    result = compare_skill_pair(*pair, CATALOG)
    assert result.outcome is PairOutcome.INDETERMINATE
    assert result.reason == "incoherent_resolution"


def _unresolved_with_identity(
    status: ResolutionStatus = ResolutionStatus.UNRESOLVED,
) -> SkillResolution:
    return SkillResolution(
        raw_text="Docker",
        normalized_representation="docker",
        status=status,
        provenance=ResolutionProvenance.UNRESOLVED,
        skill_id=DOCKER,
    )


@pytest.mark.parametrize(
    "partner",
    [resolved(DOCKER), unresolved("docker"), unresolved("Podman")],
    ids=["resolved-same-skill", "unresolved-same-text", "unresolved-other-text"],
)
@pytest.mark.parametrize("side", ["profile", "job"])
def test_unresolved_carrying_a_skill_id_is_incoherent(side: str, partner: SkillResolution) -> None:
    """The reverse of a resolved side without identity: never a match on either."""
    broken = _unresolved_with_identity()
    pair = (broken, partner) if side == "profile" else (partner, broken)
    result = compare_skill_pair(*pair, CATALOG)
    assert result.outcome is PairOutcome.INDETERMINATE
    assert result.reason == "incoherent_resolution"


def test_indeterminate_carrying_a_skill_id_is_incoherent() -> None:
    broken = _unresolved_with_identity(ResolutionStatus.INDETERMINATE)
    for pair in ((broken, resolved(DOCKER)), (resolved(DOCKER), broken)):
        assert compare_skill_pair(*pair, CATALOG).reason == "incoherent_resolution"


@pytest.mark.parametrize("profile_first", [True, False])
@pytest.mark.parametrize(
    ("unresolved_text", "resolved_skill"), [("docker", DOCKER), ("Kubernetes", KUBERNETES)]
)
def test_resolved_against_unresolved_with_the_same_text_is_still_indeterminate(
    profile_first: bool, unresolved_text: str, resolved_skill: uuid.UUID
) -> None:
    """Section 5 rows 3-4: no text-equality fallback once one side resolved."""
    loose = unresolved(unresolved_text)
    pinned = resolved(resolved_skill)
    assert loose.normalized_representation == pinned.normalized_representation
    pair = (pinned, loose) if profile_first else (loose, pinned)
    result = compare_skill_pair(*pair, CATALOG)
    assert result.outcome is PairOutcome.INDETERMINATE
    assert result.reason == "one_side_unresolved"


def _incoherent() -> SkillResolution:
    return SkillResolution(
        raw_text="Docker",
        normalized_representation="docker",
        status=ResolutionStatus.RESOLVED,
        provenance=ResolutionProvenance.AI_SEMANTIC,
    )


UNCERTAIN = {
    "incoherent": _incoherent(),
    "unresolved_with_id": _unresolved_with_identity(),
    "semantic": resolved(DOCKER, "dockr", ResolutionProvenance.AI_SEMANTIC),
    "indeterminate": indeterminate("Lead"),
    "unresolved": unresolved("Podman"),
    "resolved": resolved(DOCKER),
}


@pytest.mark.parametrize(
    ("first", "second", "reason"),
    [
        ("incoherent", "semantic", "incoherent_resolution"),
        ("incoherent", "indeterminate", "incoherent_resolution"),
        ("incoherent", "unresolved", "incoherent_resolution"),
        ("incoherent", "incoherent", "incoherent_resolution"),
        ("unresolved_with_id", "semantic", "incoherent_resolution"),
        ("unresolved_with_id", "indeterminate", "incoherent_resolution"),
        ("unresolved_with_id", "unresolved", "incoherent_resolution"),
        ("unresolved_with_id", "resolved", "incoherent_resolution"),
        ("semantic", "indeterminate", "non_deterministic_provenance"),
        ("semantic", "unresolved", "non_deterministic_provenance"),
        ("semantic", "semantic", "non_deterministic_provenance"),
        ("indeterminate", "unresolved", "side_indeterminate"),
        ("indeterminate", "resolved", "side_indeterminate"),
        ("indeterminate", "indeterminate", "side_indeterminate"),
    ],
)
def test_uncertain_pairs_report_one_reason_regardless_of_side_order(
    first: str, second: str, reason: str
) -> None:
    """Precedence: incoherent > non-deterministic > indeterminate, in both orders."""
    a, b = UNCERTAIN[first], UNCERTAIN[second]
    forward = compare_skill_pair(a, b, CATALOG)
    backward = compare_skill_pair(b, a, CATALOG)
    assert forward.outcome is backward.outcome is PairOutcome.INDETERMINATE
    assert forward.reason == backward.reason == reason


# --- provenance preservation and determinism -------------------------------------


def test_both_sides_are_preserved_by_value() -> None:
    profile = resolved(KUBERNETES, "K8s", ResolutionProvenance.ALIAS_RULE)
    job = resolved(EKS, "EKS", ResolutionProvenance.ALIAS_RULE)
    result = compare_skill_pair(profile, job, CATALOG)
    assert (result.profile.raw_text, result.profile.canonical_name) == ("K8s", "Kubernetes")
    assert (result.job.raw_text, result.job.canonical_name) == ("EKS", "Amazon EKS")
    assert result.profile.provenance is ResolutionProvenance.ALIAS_RULE
    assert result.job.provenance is ResolutionProvenance.ALIAS_RULE
    assert result.profile.status is result.job.status is ResolutionStatus.RESOLVED


def test_unresolved_sides_keep_their_text_and_no_canonical_name() -> None:
    result = compare_skill_pair(unresolved("Podman"), resolved(DOCKER), CATALOG)
    assert result.profile.canonical_name is None
    assert result.profile.provenance is ResolutionProvenance.UNRESOLVED
    assert result.job.canonical_name == "Docker"


def test_identical_inputs_give_identical_results() -> None:
    """Invariant 6: Stage 2 is re-executable."""
    pairs = [
        (resolved(EKS), resolved(KUBERNETES)),
        (unresolved("Podman"), unresolved("podman")),
        (resolved(DOCKER), unresolved("Podman")),
    ]
    for profile, job in pairs:
        assert compare_skill_pair(profile, job, CATALOG) == compare_skill_pair(
            profile, job, CATALOG
        )
