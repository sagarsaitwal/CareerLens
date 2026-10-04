"""Profile snapshot construction.

Pure-domain tests: no database. The snapshot is the matching input of
record, so these cover its determinism, its union semantics, and the
boundary that keeps non-matching data out of it.
"""

import uuid

import pytest

from app.core.versions import PROFILE_SNAPSHOT_SCHEMA_VERSION
from app.domain.profile import (
    CapabilityEntry,
    IntentEntry,
    ProfileState,
    SeniorityReference,
    build_snapshot,
    snapshots_equivalent,
)
from app.domain.resolution import ResolutionProvenance, ResolutionStatus
from app.domain.seniority import EvidenceSource
from app.models.enums import Proficiency, SeniorityTrack

PROFILE_ID = uuid.uuid4()
AWS_ID = uuid.uuid4()
DOCKER_ID = uuid.uuid4()
SENIOR_ID = uuid.uuid4()
STAFF_ID = uuid.uuid4()


def capability(
    name: str, normalized: str, skill_id: uuid.UUID, proficiency: Proficiency
) -> CapabilityEntry:
    return CapabilityEntry(
        raw_text=name,
        normalized_representation=normalized,
        status=ResolutionStatus.RESOLVED,
        provenance=ResolutionProvenance.EXACT_MATCH,
        proficiency=proficiency,
        skill_id=skill_id,
        resolved_canonical_name=name,
    )


def intent(name: str, normalized: str, weight: str = "0.800") -> IntentEntry:
    return IntentEntry(
        raw_text=name,
        normalized_representation=normalized,
        status=ResolutionStatus.UNRESOLVED,
        provenance=ResolutionProvenance.UNRESOLVED,
        weight=weight,
        is_required=False,
    )


def seniority(
    name: str, track: SeniorityTrack, rank: int, level_id: uuid.UUID
) -> SeniorityReference:
    return SeniorityReference(
        raw_text=name,
        normalized_representation=name.lower(),
        status=ResolutionStatus.RESOLVED,
        provenance=ResolutionProvenance.EXACT_MATCH,
        evidence_source=EvidenceSource.STRUCTURED_FIELD,
        level_id=level_id,
        resolved_level_name=name,
        resolved_track=track,
        resolved_rank_within_track=rank,
    )


def state(**overrides: object) -> ProfileState:
    defaults: dict[str, object] = {
        "profile_id": PROFILE_ID,
        "experience_years": 8,
        "capabilities": (
            capability("Amazon Web Services", "amazon web services", AWS_ID, Proficiency.STRONG),
            capability("Docker", "docker", DOCKER_ID, Proficiency.WORKING),
        ),
        "intents": (),
        "current_seniority": seniority("Senior", SeniorityTrack.IC, 4, SENIOR_ID),
        "target_seniority": None,
    }
    defaults.update(overrides)
    return ProfileState(**defaults)  # type: ignore[arg-type]


# --- determinism ----------------------------------------------------------


def test_snapshot_is_deterministic() -> None:
    assert build_snapshot(state()) == build_snapshot(state())


def test_skill_order_does_not_affect_the_snapshot() -> None:
    """Row order must not mint a spurious version."""
    forward = state()
    reversed_order = state(capabilities=tuple(reversed(forward.capabilities)))
    assert build_snapshot(forward) == build_snapshot(reversed_order)


def test_snapshot_records_its_schema_version() -> None:
    assert build_snapshot(state())["schema_version"] == PROFILE_SNAPSHOT_SCHEMA_VERSION


# --- union semantics ------------------------------------------------------


def test_capability_and_intent_merge_on_one_skill() -> None:
    snapshot = build_snapshot(
        state(intents=(intent("Docker", "docker", "0.500"),))
    )
    docker = next(s for s in snapshot["skills"] if s["normalized_representation"] == "docker")
    assert docker["proficiency"] == Proficiency.WORKING.value
    assert docker["weight"] == "0.500"


def test_capability_without_intent_has_no_weight() -> None:
    snapshot = build_snapshot(state())
    docker = next(s for s in snapshot["skills"] if s["normalized_representation"] == "docker")
    assert "proficiency" in docker
    assert "weight" not in docker


def test_intent_without_capability_has_no_proficiency() -> None:
    """Aspirational skill: targeted but not held.

    Inventing a proficiency here would turn a truthful `missing` gap
    into a misleading `below-threshold-experience` one.
    """
    snapshot = build_snapshot(state(intents=(intent("Kubernetes", "kubernetes"),)))
    kubernetes = next(
        s for s in snapshot["skills"] if s["normalized_representation"] == "kubernetes"
    )
    assert "weight" in kubernetes
    assert "proficiency" not in kubernetes


# --- seniority ------------------------------------------------------------


def test_target_seniority_absent_stays_null() -> None:
    """Unset target must never be defaulted from current."""
    snapshot = build_snapshot(state())
    assert snapshot["current_seniority"]["resolved_level_name"] == "Senior"
    assert snapshot["target_seniority"] is None


def test_current_and_target_are_independent() -> None:
    snapshot = build_snapshot(
        state(target_seniority=seniority("Staff", SeniorityTrack.IC, 5, STAFF_ID))
    )
    assert snapshot["current_seniority"]["resolved_level_name"] == "Senior"
    assert snapshot["target_seniority"]["resolved_level_name"] == "Staff"


def test_track_is_carried_by_value() -> None:
    snapshot = build_snapshot(state())
    assert snapshot["current_seniority"]["resolved_track"] == SeniorityTrack.IC.value


def test_track_change_is_representable() -> None:
    """Senior IC -> Manager is a track change, not a rank increase."""
    snapshot = build_snapshot(
        state(
            target_seniority=seniority(
                "Manager", SeniorityTrack.MANAGEMENT, 1, uuid.uuid4()
            )
        )
    )
    assert snapshot["current_seniority"]["resolved_track"] == "IC"
    assert snapshot["target_seniority"]["resolved_track"] == "Management"


def test_referenced_ladder_is_frozen() -> None:
    snapshot = build_snapshot(
        state(target_seniority=seniority("Staff", SeniorityTrack.IC, 5, STAFF_ID))
    )
    ladder = {entry["level_name"]: entry for entry in snapshot["seniority_ladder"]}
    assert ladder["Senior"]["rank_within_track"] == 4
    assert ladder["Staff"]["rank_within_track"] == 5


def test_ladder_only_includes_referenced_levels() -> None:
    snapshot = build_snapshot(state())
    assert [entry["level_name"] for entry in snapshot["seniority_ladder"]] == ["Senior"]


# --- the LLM boundary -----------------------------------------------------


@pytest.mark.invariant
def test_snapshot_contains_only_matching_relevant_keys() -> None:
    """The snapshot bounds what may ever cross the LLM boundary.

    Resumes, CTC, recruiter contacts, interview notes and application
    history have no representation in ProfileState, so they cannot
    appear here by construction.
    """
    snapshot = build_snapshot(state())
    assert set(snapshot) == {
        "schema_version",
        "profile_id",
        "experience_years",
        "skills",
        "current_seniority",
        "target_seniority",
        "preferred_locations",
        "preferred_work_modes",
        "seniority_ladder",
        "vocabularies",
    }


@pytest.mark.invariant
def test_profile_state_has_no_field_for_excluded_data() -> None:
    fields = set(ProfileState.__dataclass_fields__)
    forbidden = {"resume", "ctc", "salary", "recruiter", "interview", "application", "notes"}
    assert not {field for field in fields if any(word in field for word in forbidden)}


# --- equivalence / minting ------------------------------------------------


def test_identical_state_is_equivalent() -> None:
    assert snapshots_equivalent(build_snapshot(state()), build_snapshot(state()))


def test_proficiency_change_is_not_equivalent() -> None:
    changed = state(
        capabilities=(
            capability("Amazon Web Services", "amazon web services", AWS_ID, Proficiency.STRONG),
            capability("Docker", "docker", DOCKER_ID, Proficiency.STRONG),
        )
    )
    assert not snapshots_equivalent(build_snapshot(state()), build_snapshot(changed))


def test_added_skill_is_not_equivalent() -> None:
    changed = state(
        capabilities=(
            *state().capabilities,
            capability("Kubernetes", "kubernetes", uuid.uuid4(), Proficiency.LEARNING),
        )
    )
    assert not snapshots_equivalent(build_snapshot(state()), build_snapshot(changed))


def test_setting_a_target_is_not_equivalent() -> None:
    changed = state(target_seniority=seniority("Staff", SeniorityTrack.IC, 5, STAFF_ID))
    assert not snapshots_equivalent(build_snapshot(state()), build_snapshot(changed))


def test_proficiency_has_exactly_three_levels() -> None:
    """No numeric score, and no additional levels."""
    assert [level.value for level in Proficiency] == ["Strong", "Working", "Learning"]


# --- location and work mode -----------------------------------------------


def test_preferences_default_to_empty_lists() -> None:
    snapshot = build_snapshot(state())
    assert snapshot["preferred_locations"] == []
    assert snapshot["preferred_work_modes"] == []


def test_preferences_reach_the_snapshot() -> None:
    """Stage 2 compares location and work mode, so they must be frozen.

    A ProfileVersion is the matching input of record. If these lived
    only on the live profile row, a historical match could not be
    re-executed without silently picking up today's preferences.
    """
    snapshot = build_snapshot(
        state(
            preferred_locations=("Bengaluru", "Pune"),
            preferred_work_modes=("remote", "hybrid"),
        )
    )
    assert snapshot["preferred_locations"] == ["Bengaluru", "Pune"]
    assert snapshot["preferred_work_modes"] == ["remote", "hybrid"]


def test_preference_order_is_preserved() -> None:
    """Entry order may encode priority, so it is not sorted away."""
    snapshot = build_snapshot(state(preferred_locations=("Pune", "Bengaluru")))
    assert snapshot["preferred_locations"] == ["Pune", "Bengaluru"]


def test_changing_a_preferred_location_is_not_equivalent() -> None:
    before = build_snapshot(state(preferred_locations=("Pune",)))
    after = build_snapshot(state(preferred_locations=("Bengaluru",)))
    assert not snapshots_equivalent(before, after)


def test_changing_a_preferred_work_mode_is_not_equivalent() -> None:
    before = build_snapshot(state(preferred_work_modes=("remote",)))
    after = build_snapshot(state(preferred_work_modes=("onsite",)))
    assert not snapshots_equivalent(before, after)


# --- identity-first union -------------------------------------------------


def resolved_intent(
    name: str, normalized: str, skill_id: uuid.UUID, weight: str = "0.800"
) -> IntentEntry:
    return IntentEntry(
        raw_text=name,
        normalized_representation=normalized,
        status=ResolutionStatus.RESOLVED,
        provenance=ResolutionProvenance.ALIAS_RULE,
        weight=weight,
        is_required=False,
        skill_id=skill_id,
        resolved_canonical_name=name,
    )


def test_different_text_resolving_to_one_skill_merges() -> None:
    """"K8s" and "Kubernetes" are one skill once both resolve.

    Text keying could never see this: the strings differ. Identity is
    what makes the union correct.
    """
    kubernetes_id = uuid.uuid4()
    snapshot = build_snapshot(
        state(
            capabilities=(
                capability("Kubernetes", "kubernetes", kubernetes_id, Proficiency.STRONG),
            ),
            intents=(resolved_intent("Kubernetes", "k8s", kubernetes_id, "0.900"),),
        )
    )
    assert len(snapshot["skills"]) == 1
    entry = snapshot["skills"][0]
    assert entry["skill_id"] == str(kubernetes_id)
    assert entry["proficiency"] == Proficiency.STRONG.value
    assert entry["weight"] == "0.900"


def test_conflicting_identities_do_not_overwrite_each_other() -> None:
    """Same text, two different skills: two entries, both intact.

    Silently collapsing these would let one resolution destroy the
    other, and matching would act on a skill the user never named.
    """
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    snapshot = build_snapshot(
        state(
            capabilities=(capability("Go", "go", first_id, Proficiency.STRONG),),
            intents=(resolved_intent("Go", "go", second_id, "0.700"),),
        )
    )
    assert len(snapshot["skills"]) == 2
    identities = {entry["skill_id"] for entry in snapshot["skills"]}
    assert identities == {str(first_id), str(second_id)}


def test_unresolved_intent_still_joins_a_resolved_capability() -> None:
    """Resolution can lag one side of a pair.

    Splitting them would report a gap for a skill the user holds, so
    normalized text remains a fallback join where no identity exists.
    """
    snapshot = build_snapshot(state(intents=(intent("Docker", "docker", "0.500"),)))
    docker = next(
        entry
        for entry in snapshot["skills"]
        if entry["normalized_representation"] == "docker"
    )
    assert docker["proficiency"] == Proficiency.WORKING.value
    assert docker["weight"] == "0.500"
    assert docker["skill_id"] == str(DOCKER_ID)


def test_union_is_symmetric_in_which_side_resolved() -> None:
    """Whether capability or intent resolved first must not matter."""
    terraform_id = uuid.uuid4()
    unresolved_capability = CapabilityEntry(
        raw_text="Terraform",
        normalized_representation="terraform",
        status=ResolutionStatus.UNRESOLVED,
        provenance=ResolutionProvenance.UNRESOLVED,
        proficiency=Proficiency.LEARNING,
    )
    snapshot = build_snapshot(
        state(
            capabilities=(unresolved_capability,),
            intents=(resolved_intent("Terraform", "terraform", terraform_id),),
        )
    )
    assert len(snapshot["skills"]) == 1
    entry = snapshot["skills"][0]
    # The entry adopts the identity that was actually established.
    assert entry["skill_id"] == str(terraform_id)
    assert entry["resolution_status"] == ResolutionStatus.RESOLVED.value
    assert entry["proficiency"] == Proficiency.LEARNING.value


def test_identity_union_stays_deterministic_under_row_order() -> None:
    kubernetes_id = uuid.uuid4()
    forward = state(
        capabilities=(
            capability("Kubernetes", "kubernetes", kubernetes_id, Proficiency.STRONG),
            capability("Docker", "docker", DOCKER_ID, Proficiency.WORKING),
        ),
        intents=(resolved_intent("Kubernetes", "k8s", kubernetes_id),),
    )
    backward = state(
        capabilities=tuple(reversed(forward.capabilities)),
        intents=forward.intents,
    )
    assert build_snapshot(forward) == build_snapshot(backward)


# --- same-kind merge policy -----------------------------------------------
#
# Two rows may resolve to one skill by different text, because the live
# tables are unique on (profile, normalized_representation) and not on
# identity. Collapsing them is correct; letting whichever row happened
# to be processed last decide the outcome is not.


def capability_at(
    name: str, normalized: str, skill_id: uuid.UUID, proficiency: Proficiency
) -> CapabilityEntry:
    return capability(name, normalized, skill_id, proficiency)


def resolved_intent_at(
    name: str,
    normalized: str,
    skill_id: uuid.UUID,
    weight: str,
    is_required: bool = False,
) -> IntentEntry:
    return IntentEntry(
        raw_text=name,
        normalized_representation=normalized,
        status=ResolutionStatus.RESOLVED,
        provenance=ResolutionProvenance.ALIAS_RULE,
        weight=weight,
        is_required=is_required,
        skill_id=skill_id,
        resolved_canonical_name=name,
    )


def test_strongest_proficiency_wins_across_duplicate_capabilities() -> None:
    """Two rows for one skill: the stronger claim is the true one.

    Taking the weaker would understate what the user can do and invent
    a skill gap against a job that needs it.
    """
    kubernetes_id = uuid.uuid4()
    snapshot = build_snapshot(
        state(
            capabilities=(
                capability_at("Kubernetes", "kubernetes", kubernetes_id, Proficiency.STRONG),
                capability_at("K8s", "k8s", kubernetes_id, Proficiency.LEARNING),
            ),
            intents=(),
        )
    )
    assert len(snapshot["skills"]) == 1
    assert snapshot["skills"][0]["proficiency"] == Proficiency.STRONG.value


def test_duplicate_capability_merge_ignores_input_order() -> None:
    kubernetes_id = uuid.uuid4()
    strong = capability_at("Kubernetes", "kubernetes", kubernetes_id, Proficiency.STRONG)
    learning = capability_at("K8s", "k8s", kubernetes_id, Proficiency.LEARNING)

    forward = build_snapshot(state(capabilities=(strong, learning), intents=()))
    reverse = build_snapshot(state(capabilities=(learning, strong), intents=()))

    assert snapshots_equivalent(forward, reverse)
    assert forward["skills"][0]["proficiency"] == Proficiency.STRONG.value


def test_middle_proficiency_does_not_beat_strong() -> None:
    """Three-way duplicates still elect the strongest, not the median."""
    skill_id = uuid.uuid4()
    snapshot = build_snapshot(
        state(
            capabilities=(
                capability_at("B", "b", skill_id, Proficiency.WORKING),
                capability_at("A", "a", skill_id, Proficiency.LEARNING),
                capability_at("C", "c", skill_id, Proficiency.STRONG),
            ),
            intents=(),
        )
    )
    assert len(snapshot["skills"]) == 1
    assert snapshot["skills"][0]["proficiency"] == Proficiency.STRONG.value


def test_required_intent_dominates_optional() -> None:
    """A duplicate must never be able to demote a stated requirement.

    Demoting it would let a job missing the skill score as a good
    match.
    """
    kubernetes_id = uuid.uuid4()
    snapshot = build_snapshot(
        state(
            capabilities=(),
            intents=(
                resolved_intent_at("Kubernetes", "kubernetes", kubernetes_id, "0.900", True),
                resolved_intent_at("K8s", "k8s", kubernetes_id, "0.100", False),
            ),
        )
    )
    assert len(snapshot["skills"]) == 1
    assert snapshot["skills"][0]["is_required"] is True
    assert snapshot["skills"][0]["weight"] == "0.900"


def test_required_wins_even_when_its_weight_is_lower() -> None:
    """Required outranks weight; it is the stronger statement of intent."""
    skill_id = uuid.uuid4()
    snapshot = build_snapshot(
        state(
            capabilities=(),
            intents=(
                resolved_intent_at("Low but required", "aaa", skill_id, "0.100", True),
                resolved_intent_at("High but optional", "zzz", skill_id, "0.900", False),
            ),
        )
    )
    assert snapshot["skills"][0]["is_required"] is True
    assert snapshot["skills"][0]["weight"] == "0.100"


def test_higher_weight_wins_when_required_matches() -> None:
    skill_id = uuid.uuid4()
    snapshot = build_snapshot(
        state(
            capabilities=(),
            intents=(
                resolved_intent_at("Low", "aaa", skill_id, "0.250", False),
                resolved_intent_at("High", "zzz", skill_id, "0.750", False),
            ),
        )
    )
    assert snapshot["skills"][0]["weight"] == "0.750"


def test_weight_is_compared_numerically_not_as_text() -> None:
    """"0.9" is heavier than "0.10"; string ordering says otherwise."""
    skill_id = uuid.uuid4()
    snapshot = build_snapshot(
        state(
            capabilities=(),
            intents=(
                resolved_intent_at("Ten", "aaa", skill_id, "0.100", False),
                resolved_intent_at("Ninety", "zzz", skill_id, "0.900", False),
            ),
        )
    )
    assert snapshot["skills"][0]["weight"] == "0.900"


def test_duplicate_intent_merge_ignores_input_order() -> None:
    kubernetes_id = uuid.uuid4()
    strong = resolved_intent_at("Kubernetes", "kubernetes", kubernetes_id, "0.900", True)
    weak = resolved_intent_at("K8s", "k8s", kubernetes_id, "0.100", False)

    forward = build_snapshot(state(capabilities=(), intents=(strong, weak)))
    reverse = build_snapshot(state(capabilities=(), intents=(weak, strong)))

    assert snapshots_equivalent(forward, reverse)
    assert forward["skills"][0]["is_required"] is True
    assert forward["skills"][0]["weight"] == "0.900"


def test_fully_tied_duplicates_resolve_deterministically() -> None:
    """Nothing distinguishes these but the text, so the text decides."""
    skill_id = uuid.uuid4()
    first = resolved_intent_at("Zed", "zed", skill_id, "0.500", False)
    second = resolved_intent_at("Alpha", "alpha", skill_id, "0.500", False)

    forward = build_snapshot(state(capabilities=(), intents=(first, second)))
    reverse = build_snapshot(state(capabilities=(), intents=(second, first)))
    assert snapshots_equivalent(forward, reverse)


def test_capability_and_intent_dimensions_stay_separate() -> None:
    """One entry, two independent axes.

    Merging on identity must not let a capability acquire an intent's
    weight or an intent acquire a proficiency it never had. Holding a
    skill and targeting it are different claims.
    """
    skill_id = uuid.uuid4()
    snapshot = build_snapshot(
        state(
            capabilities=(
                capability_at("Kubernetes", "kubernetes", skill_id, Proficiency.WORKING),
            ),
            intents=(resolved_intent_at("K8s", "k8s", skill_id, "0.600", True),),
        )
    )
    assert len(snapshot["skills"]) == 1
    entry = snapshot["skills"][0]
    assert entry["proficiency"] == Proficiency.WORKING.value
    assert entry["weight"] == "0.600"
    assert entry["is_required"] is True
    assert entry["skill_id"] == str(skill_id)


def test_capability_only_and_intent_only_entries_keep_their_gaps() -> None:
    """The merge must not back-fill the axis a skill genuinely lacks."""
    held_id = uuid.uuid4()
    wanted_id = uuid.uuid4()
    snapshot = build_snapshot(
        state(
            capabilities=(capability_at("Held", "held", held_id, Proficiency.STRONG),),
            intents=(resolved_intent_at("Wanted", "wanted", wanted_id, "0.800"),),
        )
    )
    held = next(e for e in snapshot["skills"] if e["skill_id"] == str(held_id))
    wanted = next(e for e in snapshot["skills"] if e["skill_id"] == str(wanted_id))
    assert "weight" not in held
    assert "is_required" not in held
    assert "proficiency" not in wanted


def test_duplicate_capabilities_and_intents_merge_together_in_any_order() -> None:
    """The whole policy at once, under a shuffled input."""
    skill_id = uuid.uuid4()
    caps = (
        capability_at("Kubernetes", "kubernetes", skill_id, Proficiency.STRONG),
        capability_at("K8s", "k8s", skill_id, Proficiency.LEARNING),
    )
    ints = (
        resolved_intent_at("Kubernetes", "kubernetes", skill_id, "0.400", True),
        resolved_intent_at("K8s", "k8s", skill_id, "0.950", False),
    )
    forward = build_snapshot(state(capabilities=caps, intents=ints))
    reverse = build_snapshot(
        state(capabilities=tuple(reversed(caps)), intents=tuple(reversed(ints)))
    )

    assert snapshots_equivalent(forward, reverse)
    assert len(forward["skills"]) == 1
    entry = forward["skills"][0]
    assert entry["proficiency"] == Proficiency.STRONG.value
    # Required dominates, so the 0.400 required intent wins outright
    # over the heavier optional one.
    assert entry["is_required"] is True
    assert entry["weight"] == "0.400"


def test_representative_prefers_a_resolved_mention() -> None:
    """The entry's text and provenance come from a mention with identity."""
    skill_id = uuid.uuid4()
    unresolved = CapabilityEntry(
        raw_text="aaa-unresolved",
        normalized_representation="aaa-unresolved",
        status=ResolutionStatus.UNRESOLVED,
        provenance=ResolutionProvenance.UNRESOLVED,
        proficiency=Proficiency.LEARNING,
    )
    snapshot = build_snapshot(
        state(
            capabilities=(unresolved,),
            intents=(
                resolved_intent_at("Terraform", "aaa-unresolved", skill_id, "0.500"),
            ),
        )
    )
    entry = snapshot["skills"][0]
    assert entry["resolution_status"] == ResolutionStatus.RESOLVED.value
    assert entry["skill_id"] == str(skill_id)
    assert entry["resolution_provenance"] == ResolutionProvenance.ALIAS_RULE.value


def test_duplicate_merge_does_not_cross_skill_identities() -> None:
    """The policy applies within one identity, never across two."""
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    snapshot = build_snapshot(
        state(
            capabilities=(
                capability_at("Go lang", "go lang", first_id, Proficiency.STRONG),
                capability_at("Golang", "golang", second_id, Proficiency.LEARNING),
            ),
            intents=(),
        )
    )
    assert len(snapshot["skills"]) == 2
    by_identity = {e["skill_id"]: e["proficiency"] for e in snapshot["skills"]}
    assert by_identity[str(first_id)] == Proficiency.STRONG.value
    assert by_identity[str(second_id)] == Proficiency.LEARNING.value


def test_representative_text_co_originates_with_winning_proficiency() -> None:
    """The entry's text and its proficiency describe ONE real row.

    "Kubernetes" was entered at Strong and "K8s" at Learning. Strong is
    the surviving claim, so the surviving text must be "Kubernetes" —
    attributing Strong to the row that said Learning would misreport
    the evidence.
    """
    kubernetes_id = uuid.uuid4()
    strong = capability_at("Kubernetes", "kubernetes", kubernetes_id, Proficiency.STRONG)
    learning = capability_at("K8s", "k8s", kubernetes_id, Proficiency.LEARNING)

    for capabilities in ((strong, learning), (learning, strong)):
        snapshot = build_snapshot(state(capabilities=capabilities, intents=()))
        entry = snapshot["skills"][0]
        assert entry["proficiency"] == Proficiency.STRONG.value
        assert entry["raw_text"] == "Kubernetes"
        assert entry["normalized_representation"] == "kubernetes"


def test_co_origination_holds_even_when_text_sorts_last() -> None:
    """Co-origination is not lexicographic order wearing a disguise."""
    skill_id = uuid.uuid4()
    snapshot = build_snapshot(
        state(
            capabilities=(
                capability_at("Aaa", "aaa", skill_id, Proficiency.LEARNING),
                capability_at("Zzz", "zzz", skill_id, Proficiency.STRONG),
            ),
            intents=(),
        )
    )
    entry = snapshot["skills"][0]
    assert entry["proficiency"] == Proficiency.STRONG.value
    assert entry["raw_text"] == "Zzz"


def test_representative_falls_back_to_winning_intent_without_capability() -> None:
    """With no capability, the winning intent's row represents the entry."""
    skill_id = uuid.uuid4()
    snapshot = build_snapshot(
        state(
            capabilities=(),
            intents=(
                resolved_intent_at("Aaa", "aaa", skill_id, "0.100", False),
                resolved_intent_at("Zzz", "zzz", skill_id, "0.900", False),
            ),
        )
    )
    entry = snapshot["skills"][0]
    assert entry["weight"] == "0.900"
    assert entry["raw_text"] == "Zzz"


@pytest.mark.invariant
def test_identity_coherence_outranks_co_origination() -> None:
    """A resolved mention represents the entry even if it did not win.

    The strongest capability here is UNRESOLVED. Electing it would make
    the entry report `unresolved` while carrying the identity it is
    keyed on — the contradiction `resolved_requires_identity` forbids at
    row level. Identity coherence wins; co-origination yields.
    """
    skill_id = uuid.uuid4()
    unresolved_strong = CapabilityEntry(
        raw_text="K8s",
        normalized_representation="k8s",
        status=ResolutionStatus.UNRESOLVED,
        provenance=ResolutionProvenance.UNRESOLVED,
        proficiency=Proficiency.STRONG,
    )
    resolved_weak = capability_at(
        "Kubernetes", "k8s-resolved", skill_id, Proficiency.LEARNING
    )
    # The unresolved mention joins via the text fallback.
    unresolved_sharing_text = CapabilityEntry(
        raw_text="K8s",
        normalized_representation="k8s-resolved",
        status=ResolutionStatus.UNRESOLVED,
        provenance=ResolutionProvenance.UNRESOLVED,
        proficiency=Proficiency.STRONG,
    )
    assert unresolved_strong is not unresolved_sharing_text

    snapshot = build_snapshot(
        state(capabilities=(unresolved_sharing_text, resolved_weak), intents=())
    )
    entry = snapshot["skills"][0]
    assert entry["resolution_status"] == ResolutionStatus.RESOLVED.value
    assert entry["skill_id"] == str(skill_id)
    # Strongest proficiency still survives; only the representative text
    # is forced away from the winning row.
    assert entry["proficiency"] == Proficiency.STRONG.value
    assert entry["raw_text"] == "Kubernetes"
