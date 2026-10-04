"""Profile persistence and version immutability.

Run against a real PostgreSQL: immutability here is a database
guarantee, not an application convention, so it is tested against the
actual trigger.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.domain.normalization import normalize
from app.domain.resolution import ResolutionProvenance, ResolutionStatus, resolve_skill
from app.domain.seniority import EvidenceSource, resolve_seniority
from app.models.enums import Proficiency, SeniorityTrack
from app.models.profile import MatchingCriteria, ProfileSkill, ProfileVersion, UserProfile
from app.repositories.catalog import CatalogSnapshot
from app.services.profile import (
    NoMatchingRelevantStateError,
    current_version,
    mint_version_if_changed,
)

pytestmark = pytest.mark.integration


def catalog(session: Session) -> CatalogSnapshot:
    return CatalogSnapshot.current(session)


def make_profile(session: Session, label: str = "Platform Engineer") -> UserProfile:
    profile = UserProfile(label=label, experience_years=8)
    session.add(profile)
    session.flush()
    return profile


def add_skill(
    session: Session,
    profile: UserProfile,
    raw_text: str,
    proficiency: Proficiency = Proficiency.WORKING,
) -> ProfileSkill:
    """Add a capability, resolving its identity against the pinned catalog."""
    resolution = resolve_skill(raw_text, catalog(session).skills)
    skill = ProfileSkill(
        user_profile_id=profile.id,
        raw_text=resolution.raw_text,
        normalized_representation=resolution.normalized_representation,
        skill_id=resolution.skill_id,
        resolution_status=resolution.status.value,
        resolution_provenance=resolution.provenance.value,
        resolved_canonical_name=resolution.resolved_canonical_name,
        catalog_version_at_resolution=catalog(session).version.version_number,
        proficiency=proficiency.value,
    )
    session.add(skill)
    session.flush()
    return skill


def set_seniority(
    session: Session, profile: UserProfile, prefix: str, raw_text: str
) -> None:
    resolution = resolve_seniority(
        raw_text, catalog(session).seniority, EvidenceSource.STRUCTURED_FIELD
    )
    setattr(profile, f"{prefix}_seniority_raw_text", resolution.raw_text)
    setattr(
        profile, f"{prefix}_seniority_normalized", resolution.normalized_representation
    )
    setattr(profile, f"{prefix}_seniority_level_id", resolution.level_id)
    setattr(profile, f"{prefix}_seniority_resolution_status", resolution.status.value)
    setattr(
        profile, f"{prefix}_seniority_resolution_provenance", resolution.provenance.value
    )
    setattr(
        profile, f"{prefix}_seniority_resolved_level_name", resolution.resolved_level_name
    )
    setattr(
        profile,
        f"{prefix}_seniority_resolved_track",
        resolution.resolved_track.value if resolution.resolved_track else None,
    )
    setattr(
        profile, f"{prefix}_seniority_evidence_source", resolution.evidence_source.value
    )
    setattr(
        profile,
        f"{prefix}_seniority_catalog_version",
        catalog(session).version.version_number,
    )
    session.flush()


# --- profile and version creation ----------------------------------------


def test_profile_creation(db_session: Session) -> None:
    profile = make_profile(db_session)
    assert profile.id is not None
    assert profile.current_profile_version_id is None, "v1 must be lazy"


def test_empty_profile_mints_no_version(db_session: Session) -> None:
    """A snapshot of nothing has no value."""
    profile = UserProfile(label="Empty")
    db_session.add(profile)
    db_session.flush()

    with pytest.raises(NoMatchingRelevantStateError):
        mint_version_if_changed(db_session, profile, catalog(db_session))

    assert current_version(db_session, profile.id) is None


def test_profile_version_creation(db_session: Session) -> None:
    profile = make_profile(db_session)
    add_skill(db_session, profile, "AWS", Proficiency.STRONG)
    set_seniority(db_session, profile, "current", "Senior")

    version = mint_version_if_changed(db_session, profile, catalog(db_session))

    assert version is not None
    assert version.version_number == 1
    assert profile.current_profile_version_id == version.id
    assert version.catalog_version_at_creation == catalog(db_session).version.version_number


def test_a_profile_can_have_multiple_versions(db_session: Session) -> None:
    profile = make_profile(db_session)
    add_skill(db_session, profile, "AWS", Proficiency.STRONG)
    v1 = mint_version_if_changed(db_session, profile, catalog(db_session))

    add_skill(db_session, profile, "Docker", Proficiency.WORKING)
    v2 = mint_version_if_changed(db_session, profile, catalog(db_session))

    assert v1 is not None and v2 is not None
    assert [v.version_number for v in (v1, v2)] == [1, 2]
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(ProfileVersion)
            .where(ProfileVersion.user_profile_id == profile.id)
        )
        == 2
    )


def test_version_creation_works_after_a_previous_version_exists(
    db_session: Session,
) -> None:
    profile = make_profile(db_session)
    add_skill(db_session, profile, "AWS")
    mint_version_if_changed(db_session, profile, catalog(db_session))

    add_skill(db_session, profile, "Terraform")
    third = mint_version_if_changed(db_session, profile, catalog(db_session))
    assert third is not None and third.version_number == 2


def test_unchanged_state_mints_nothing(db_session: Session) -> None:
    """Minting is keyed on outcome, not on having been called."""
    profile = make_profile(db_session)
    add_skill(db_session, profile, "AWS")
    mint_version_if_changed(db_session, profile, catalog(db_session))

    assert mint_version_if_changed(db_session, profile, catalog(db_session)) is None


# --- the headline scenario ------------------------------------------------


@pytest.mark.invariant
def test_new_version_does_not_mutate_the_previous_one(db_session: Session) -> None:
    """v1: AWS, Docker, Senior IC. v2 adds Kubernetes and a Staff target.

    v1 must remain exactly as it was.
    """
    profile = make_profile(db_session)
    add_skill(db_session, profile, "AWS", Proficiency.STRONG)
    add_skill(db_session, profile, "Docker", Proficiency.WORKING)
    set_seniority(db_session, profile, "current", "Senior")
    v1 = mint_version_if_changed(db_session, profile, catalog(db_session))
    assert v1 is not None
    v1_snapshot = dict(v1.snapshot)
    v1_id = v1.id

    add_skill(db_session, profile, "Kubernetes", Proficiency.LEARNING)
    set_seniority(db_session, profile, "target", "Staff")
    v2 = mint_version_if_changed(db_session, profile, catalog(db_session))
    assert v2 is not None

    db_session.expire_all()
    reloaded = db_session.get(ProfileVersion, v1_id)
    assert reloaded is not None
    assert reloaded.snapshot == v1_snapshot

    v1_skills = {s["normalized_representation"] for s in reloaded.snapshot["skills"]}
    v2_skills = {s["normalized_representation"] for s in v2.snapshot["skills"]}
    assert "kubernetes" not in v1_skills
    assert "kubernetes" in v2_skills
    assert reloaded.snapshot["target_seniority"] is None
    assert v2.snapshot["target_seniority"]["resolved_level_name"] == "Staff"


@pytest.mark.invariant
def test_historical_version_survives_live_profile_edits(db_session: Session) -> None:
    """Historical meaning must not depend on current mutable state."""
    profile = make_profile(db_session)
    skill = add_skill(db_session, profile, "AWS", Proficiency.STRONG)
    v1 = mint_version_if_changed(db_session, profile, catalog(db_session))
    assert v1 is not None
    original = dict(v1.snapshot)

    # Freely mutate the LIVE surface: that is what it is for.
    skill.proficiency = Proficiency.LEARNING.value
    profile.label = "Renamed"
    db_session.flush()
    db_session.expire_all()

    reloaded = db_session.get(ProfileVersion, v1.id)
    assert reloaded is not None
    assert reloaded.snapshot == original


# --- database-enforced immutability ---------------------------------------


@pytest.mark.invariant
def test_profile_version_cannot_be_updated(db_session: Session) -> None:
    """Enforced by trigger, not by application convention."""
    profile = make_profile(db_session)
    add_skill(db_session, profile, "AWS")
    version = mint_version_if_changed(db_session, profile, catalog(db_session))
    assert version is not None

    with pytest.raises(DBAPIError):
        db_session.execute(
            text("UPDATE profile_version SET schema_version = 'tampered' WHERE id = :id"),
            {"id": version.id},
        )


@pytest.mark.invariant
def test_profile_version_snapshot_cannot_be_rewritten(db_session: Session) -> None:
    profile = make_profile(db_session)
    add_skill(db_session, profile, "AWS")
    version = mint_version_if_changed(db_session, profile, catalog(db_session))
    assert version is not None

    with pytest.raises(DBAPIError):
        db_session.execute(
            text("UPDATE profile_version SET snapshot = '{}'::jsonb WHERE id = :id"),
            {"id": version.id},
        )


@pytest.mark.invariant
def test_profile_version_cannot_be_deleted(db_session: Session) -> None:
    profile = make_profile(db_session)
    add_skill(db_session, profile, "AWS")
    version = mint_version_if_changed(db_session, profile, catalog(db_session))
    assert version is not None

    with pytest.raises(DBAPIError):
        db_session.execute(
            text("DELETE FROM profile_version WHERE id = :id"), {"id": version.id}
        )


@pytest.mark.invariant
def test_new_versions_remain_creatable(db_session: Session) -> None:
    """Immutability must not have been achieved by making it read-only."""
    profile = make_profile(db_session)
    add_skill(db_session, profile, "AWS")
    assert mint_version_if_changed(db_session, profile, catalog(db_session)) is not None
    add_skill(db_session, profile, "Docker")
    assert mint_version_if_changed(db_session, profile, catalog(db_session)) is not None


# --- constraints ----------------------------------------------------------


def test_duplicate_version_number_is_rejected(db_session: Session) -> None:
    profile = make_profile(db_session)
    add_skill(db_session, profile, "AWS")
    mint_version_if_changed(db_session, profile, catalog(db_session))

    db_session.add(
        ProfileVersion(
            user_profile_id=profile.id,
            version_number=1,
            schema_version="ps1",
            catalog_version_at_creation=catalog(db_session).version.version_number,
            snapshot={},
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_profile_skill_is_unique_per_representation(db_session: Session) -> None:
    profile = make_profile(db_session)
    add_skill(db_session, profile, "AWS")
    with pytest.raises(IntegrityError):
        add_skill(db_session, profile, "AWS")


def test_invalid_proficiency_is_rejected(db_session: Session) -> None:
    profile = make_profile(db_session)
    db_session.add(
        ProfileSkill(
            user_profile_id=profile.id,
            raw_text="AWS",
            normalized_representation="aws",
            resolution_status=ResolutionStatus.UNRESOLVED.value,
            resolution_provenance=ResolutionProvenance.UNRESOLVED.value,
            proficiency="Expert",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_resolved_skill_must_carry_identity(db_session: Session) -> None:
    """A resolved mention claiming no canonical name is incoherent."""
    profile = make_profile(db_session)
    db_session.add(
        ProfileSkill(
            user_profile_id=profile.id,
            raw_text="AWS",
            normalized_representation="aws",
            skill_id=None,
            resolution_status=ResolutionStatus.RESOLVED.value,
            resolution_provenance=ResolutionProvenance.EXACT_MATCH.value,
            resolved_canonical_name=None,
            proficiency=Proficiency.STRONG.value,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_invalid_skill_foreign_key_is_rejected(db_session: Session) -> None:
    profile = make_profile(db_session)
    db_session.add(
        ProfileSkill(
            user_profile_id=profile.id,
            raw_text="Ghost",
            normalized_representation="ghost",
            skill_id=uuid.uuid4(),
            resolution_status=ResolutionStatus.RESOLVED.value,
            resolution_provenance=ResolutionProvenance.EXACT_MATCH.value,
            resolved_canonical_name="Ghost",
            proficiency=Proficiency.STRONG.value,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_invalid_profile_foreign_key_is_rejected(db_session: Session) -> None:
    db_session.add(
        ProfileSkill(
            user_profile_id=uuid.uuid4(),
            raw_text="AWS",
            normalized_representation="aws",
            resolution_status=ResolutionStatus.UNRESOLVED.value,
            resolution_provenance=ResolutionProvenance.UNRESOLVED.value,
            proficiency=Proficiency.STRONG.value,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_criteria_weight_must_be_within_range(db_session: Session) -> None:
    profile = make_profile(db_session)
    db_session.add(
        MatchingCriteria(
            user_profile_id=profile.id,
            raw_text="Kubernetes",
            normalized_representation="kubernetes",
            resolution_status=ResolutionStatus.UNRESOLVED.value,
            resolution_provenance=ResolutionProvenance.UNRESOLVED.value,
            weight=Decimal("1.500"),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_negative_experience_is_rejected(db_session: Session) -> None:
    db_session.add(UserProfile(label="Bad", experience_years=-1))
    with pytest.raises(IntegrityError):
        db_session.flush()


# --- catalog integration --------------------------------------------------


def test_skill_reference_resolves_to_the_canonical_catalog_skill(
    db_session: Session,
) -> None:
    profile = make_profile(db_session)
    skill = add_skill(db_session, profile, "K8s")

    assert skill.resolution_status == ResolutionStatus.RESOLVED.value
    assert skill.resolved_canonical_name == "Kubernetes"
    assert skill.skill_id is not None
    assert skill.normalized_representation == normalize("K8s")


def test_unresolved_skill_is_stored_without_identity(db_session: Session) -> None:
    profile = make_profile(db_session)
    skill = add_skill(db_session, profile, "SomeNewTechnologyX")
    assert skill.resolution_status == ResolutionStatus.UNRESOLVED.value
    assert skill.skill_id is None


def test_current_and_target_seniority_are_stored_separately(
    db_session: Session,
) -> None:
    profile = make_profile(db_session)
    set_seniority(db_session, profile, "current", "Senior")

    assert profile.current_seniority_resolved_level_name == "Senior"
    assert profile.target_seniority_resolved_level_name is None
    assert profile.target_seniority_raw_text is None


@pytest.mark.invariant
def test_target_seniority_does_not_default_to_current(db_session: Session) -> None:
    profile = make_profile(db_session)
    set_seniority(db_session, profile, "current", "Senior")
    add_skill(db_session, profile, "AWS")
    version = mint_version_if_changed(db_session, profile, catalog(db_session))

    assert version is not None
    assert version.snapshot["current_seniority"]["resolved_level_name"] == "Senior"
    assert version.snapshot["target_seniority"] is None


@pytest.mark.invariant
def test_ic_and_management_tracks_stay_distinct(db_session: Session) -> None:
    """Senior IC -> Manager is a track change, not a rank increase."""
    profile = make_profile(db_session)
    set_seniority(db_session, profile, "current", "Senior")
    set_seniority(db_session, profile, "target", "Manager")
    version = mint_version_if_changed(db_session, profile, catalog(db_session))

    assert version is not None
    assert version.snapshot["current_seniority"]["resolved_track"] == SeniorityTrack.IC.value
    assert (
        version.snapshot["target_seniority"]["resolved_track"]
        == SeniorityTrack.MANAGEMENT.value
    )


@pytest.mark.invariant
def test_ambiguous_seniority_stays_indeterminate(db_session: Session) -> None:
    profile = make_profile(db_session)
    set_seniority(db_session, profile, "current", "Lead")

    assert profile.current_seniority_resolution_status == (
        ResolutionStatus.INDETERMINATE.value
    )
    assert profile.current_seniority_resolved_level_name is None


@pytest.mark.invariant
def test_snapshot_pins_the_catalog_version_it_used(db_session: Session) -> None:
    """A historical version must know which catalog applied."""
    profile = make_profile(db_session)
    add_skill(db_session, profile, "AWS")
    pinned = catalog(db_session).version.version_number
    version = mint_version_if_changed(db_session, profile, catalog(db_session))

    assert version is not None
    assert version.catalog_version_at_creation == pinned


# --- identity coherence invariants ----------------------------------------


@pytest.mark.invariant
def test_profile_skill_resolved_without_identity_is_rejected(
    db_session: Session,
) -> None:
    profile = make_profile(db_session)
    db_session.add(
        ProfileSkill(
            user_profile_id=profile.id,
            raw_text="Docker",
            normalized_representation="docker",
            skill_id=None,
            resolution_status=ResolutionStatus.RESOLVED.value,
            resolution_provenance=ResolutionProvenance.EXACT_MATCH.value,
            resolved_canonical_name=None,
            proficiency=Proficiency.WORKING.value,
        )
    )
    with pytest.raises(IntegrityError, match="resolved_requires_identity"):
        db_session.flush()


@pytest.mark.invariant
def test_profile_skill_unresolved_carrying_identity_is_rejected(
    db_session: Session,
) -> None:
    """An unresolved row must not assert a resolution that never happened."""
    profile = make_profile(db_session)
    known = resolve_skill("Docker", catalog(db_session).skills)
    db_session.add(
        ProfileSkill(
            user_profile_id=profile.id,
            raw_text="Docker",
            normalized_representation="docker",
            skill_id=known.skill_id,
            resolution_status=ResolutionStatus.UNRESOLVED.value,
            resolution_provenance=ResolutionProvenance.UNRESOLVED.value,
            resolved_canonical_name=known.resolved_canonical_name,
            proficiency=Proficiency.WORKING.value,
        )
    )
    with pytest.raises(IntegrityError, match="resolved_requires_identity"):
        db_session.flush()


@pytest.mark.invariant
def test_profile_skill_indeterminate_carrying_identity_is_rejected(
    db_session: Session,
) -> None:
    """Indeterminate means evidence exists but could not be pinned.

    Letting it carry a skill_id would quietly turn "cannot decide" into
    a decision.
    """
    profile = make_profile(db_session)
    known = resolve_skill("Docker", catalog(db_session).skills)
    db_session.add(
        ProfileSkill(
            user_profile_id=profile.id,
            raw_text="Docker",
            normalized_representation="docker",
            skill_id=known.skill_id,
            resolution_status=ResolutionStatus.INDETERMINATE.value,
            resolution_provenance=ResolutionProvenance.UNRESOLVED.value,
            resolved_canonical_name=known.resolved_canonical_name,
            proficiency=Proficiency.WORKING.value,
        )
    )
    with pytest.raises(IntegrityError, match="resolved_requires_identity"):
        db_session.flush()


@pytest.mark.invariant
def test_profile_skill_partial_identity_is_rejected(db_session: Session) -> None:
    """Half an identity is still a contradiction.

    The previous form of this constraint compared two booleans, which
    let an unresolved row keep a skill_id as long as the canonical name
    stayed NULL.
    """
    profile = make_profile(db_session)
    known = resolve_skill("Docker", catalog(db_session).skills)
    db_session.add(
        ProfileSkill(
            user_profile_id=profile.id,
            raw_text="Docker",
            normalized_representation="docker",
            skill_id=known.skill_id,
            resolution_status=ResolutionStatus.UNRESOLVED.value,
            resolution_provenance=ResolutionProvenance.UNRESOLVED.value,
            resolved_canonical_name=None,
            proficiency=Proficiency.WORKING.value,
        )
    )
    with pytest.raises(IntegrityError, match="resolved_requires_identity"):
        db_session.flush()


@pytest.mark.invariant
def test_matching_criteria_resolved_without_identity_is_rejected(
    db_session: Session,
) -> None:
    """Intent carries its own identity, so it needs the same guarantee."""
    profile = make_profile(db_session)
    db_session.add(
        MatchingCriteria(
            user_profile_id=profile.id,
            raw_text="Kubernetes",
            normalized_representation="kubernetes",
            skill_id=None,
            resolution_status=ResolutionStatus.RESOLVED.value,
            resolution_provenance=ResolutionProvenance.EXACT_MATCH.value,
            resolved_canonical_name=None,
            weight=Decimal("0.800"),
            is_required=False,
        )
    )
    with pytest.raises(IntegrityError, match="resolved_requires_identity"):
        db_session.flush()


@pytest.mark.invariant
def test_matching_criteria_unresolved_carrying_identity_is_rejected(
    db_session: Session,
) -> None:
    profile = make_profile(db_session)
    known = resolve_skill("Docker", catalog(db_session).skills)
    db_session.add(
        MatchingCriteria(
            user_profile_id=profile.id,
            raw_text="Docker",
            normalized_representation="docker",
            skill_id=known.skill_id,
            resolution_status=ResolutionStatus.UNRESOLVED.value,
            resolution_provenance=ResolutionProvenance.UNRESOLVED.value,
            resolved_canonical_name=known.resolved_canonical_name,
            weight=Decimal("0.500"),
            is_required=False,
        )
    )
    with pytest.raises(IntegrityError, match="resolved_requires_identity"):
        db_session.flush()


@pytest.mark.invariant
def test_seniority_resolved_without_identity_is_rejected(db_session: Session) -> None:
    profile = make_profile(db_session)
    profile.current_seniority_raw_text = "Senior"
    profile.current_seniority_normalized = "senior"
    profile.current_seniority_resolution_status = ResolutionStatus.RESOLVED.value
    profile.current_seniority_resolution_provenance = (
        ResolutionProvenance.EXACT_MATCH.value
    )
    profile.current_seniority_evidence_source = EvidenceSource.STRUCTURED_FIELD.value
    with pytest.raises(IntegrityError, match="resolved_requires_identity"):
        db_session.flush()


@pytest.mark.invariant
def test_seniority_unresolved_carrying_identity_is_rejected(
    db_session: Session,
) -> None:
    profile = make_profile(db_session)
    resolution = resolve_seniority(
        "Senior", catalog(db_session).seniority, EvidenceSource.STRUCTURED_FIELD
    )
    profile.target_seniority_raw_text = "something unrecognised"
    profile.target_seniority_normalized = "something unrecognised"
    profile.target_seniority_resolution_status = ResolutionStatus.UNRESOLVED.value
    profile.target_seniority_resolution_provenance = (
        ResolutionProvenance.UNRESOLVED.value
    )
    profile.target_seniority_level_id = resolution.level_id
    profile.target_seniority_resolved_level_name = resolution.resolved_level_name
    profile.target_seniority_resolved_track = (
        resolution.resolved_track.value if resolution.resolved_track else None
    )
    profile.target_seniority_evidence_source = EvidenceSource.STRUCTURED_FIELD.value
    with pytest.raises(IntegrityError, match="resolved_requires_identity"):
        db_session.flush()


@pytest.mark.invariant
def test_wholly_unset_seniority_is_permitted(db_session: Session) -> None:
    """No claim is not a contradiction.

    An unset target especially: it means no preference was expressed,
    which the constraint must not confuse with a failed resolution.
    """
    profile = make_profile(db_session)
    db_session.flush()
    assert profile.target_seniority_resolution_status is None


def test_resolved_seniority_with_full_identity_is_permitted(
    db_session: Session,
) -> None:
    profile = make_profile(db_session)
    set_seniority(db_session, profile, "current", "Senior")
    assert profile.current_seniority_level_id is not None


def test_unresolved_seniority_without_identity_is_permitted(
    db_session: Session,
) -> None:
    """Resolution legitimately fails; that must remain storable."""
    profile = make_profile(db_session)
    set_seniority(db_session, profile, "current", "Wizard of Platform")
    assert profile.current_seniority_resolution_status == (
        ResolutionStatus.UNRESOLVED.value
    )
    assert profile.current_seniority_level_id is None


# --- location and work mode in the snapshot -------------------------------


def test_preferences_are_captured_in_the_snapshot(db_session: Session) -> None:
    profile = make_profile(db_session)
    profile.preferred_locations = ["Bengaluru", "Pune"]
    profile.preferred_work_modes = ["remote"]
    db_session.flush()

    version = mint_version_if_changed(db_session, profile, catalog(db_session))
    assert version is not None
    assert version.snapshot["preferred_locations"] == ["Bengaluru", "Pune"]
    assert version.snapshot["preferred_work_modes"] == ["remote"]


def test_changing_a_preference_mints_a_new_version(db_session: Session) -> None:
    """Location and work mode are Stage 2 inputs, so a change is material."""
    profile = make_profile(db_session)
    profile.preferred_locations = ["Pune"]
    db_session.flush()
    first = mint_version_if_changed(db_session, profile, catalog(db_session))
    assert first is not None

    profile.preferred_locations = ["Pune", "Bengaluru"]
    db_session.flush()
    second = mint_version_if_changed(db_session, profile, catalog(db_session))
    assert second is not None
    assert second.version_number == first.version_number + 1


def test_unchanged_preferences_mint_nothing(db_session: Session) -> None:
    profile = make_profile(db_session)
    profile.preferred_work_modes = ["hybrid"]
    db_session.flush()
    assert mint_version_if_changed(db_session, profile, catalog(db_session)) is not None
    assert mint_version_if_changed(db_session, profile, catalog(db_session)) is None


def test_profile_with_only_preferences_can_be_snapshotted(db_session: Session) -> None:
    """Preferences alone are matching-relevant state."""
    profile = UserProfile(label="Relocating", preferred_locations=["Remote"])
    db_session.add(profile)
    db_session.flush()
    assert mint_version_if_changed(db_session, profile, catalog(db_session)) is not None


# --- identity-first union, against real resolution ------------------------


def test_alias_and_canonical_merge_into_one_entry(db_session: Session) -> None:
    """A capability and an intent naming one skill by different text.

    Resolution gives both the same skill_id, and the snapshot must show
    one skill rather than two near-duplicates.
    """
    profile = make_profile(db_session)
    canonical = resolve_skill("Kubernetes", catalog(db_session).skills)
    alias = resolve_skill("K8s", catalog(db_session).skills)
    if canonical.skill_id is None or alias.skill_id != canonical.skill_id:
        pytest.skip("catalog has no K8s alias for Kubernetes")

    add_skill(db_session, profile, "Kubernetes", Proficiency.STRONG)
    db_session.add(
        MatchingCriteria(
            user_profile_id=profile.id,
            raw_text=alias.raw_text,
            normalized_representation=alias.normalized_representation,
            skill_id=alias.skill_id,
            resolution_status=alias.status.value,
            resolution_provenance=alias.provenance.value,
            resolved_canonical_name=alias.resolved_canonical_name,
            weight=Decimal("0.900"),
            is_required=True,
        )
    )
    db_session.flush()

    version = mint_version_if_changed(db_session, profile, catalog(db_session))
    assert version is not None
    entries = [
        entry
        for entry in version.snapshot["skills"]
        if entry["skill_id"] == str(canonical.skill_id)
    ]
    assert len(entries) == 1
    assert entries[0]["proficiency"] == Proficiency.STRONG.value
    assert entries[0]["weight"] == "0.900"
    assert entries[0]["is_required"] is True
