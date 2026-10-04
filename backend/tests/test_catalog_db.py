"""Catalog database integration tests.

Run against a real PostgreSQL. Each test proves a documented constraint
actually rejects a violating row — application-level validation is not
sufficient for these invariants.

Version-scoped reproducibility has its own suite in
tests/test_catalog_versioning.py.
"""

import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.domain.normalization import normalize
from app.domain.resolution import ResolutionProvenance, ResolutionStatus, resolve_skill
from app.domain.seniority import EvidenceSource, resolve_seniority
from app.models.catalog import (
    CatalogVersion,
    Skill,
    SkillPromotionEvidence,
    SkillRelationship,
    SkillRepresentation,
)
from app.models.enums import (
    ConflictType,
    RepresentationKind,
    RepresentationOrigin,
    SeniorityTrack,
    SkillOrigin,
    SkillRelationshipType,
)
from app.models.seniority import SeniorityLevel, SeniorityRelationship, SeniorityRepresentation
from app.repositories.catalog import CatalogSnapshot, live_at
from app.services.catalog import add_skill, open_conflict

pytestmark = pytest.mark.integration


def snapshot(session: Session) -> CatalogSnapshot:
    return CatalogSnapshot.current(session)


def current_version(session: Session) -> int:
    return snapshot(session).version.version_number


def make_skill(session: Session, canonical: str) -> Skill:
    return add_skill(session, canonical, snapshot(session).version)


# --- creation -------------------------------------------------------------


def test_canonical_skill_creation(db_session: Session) -> None:
    skill = make_skill(db_session, "Pulumi")
    representations = db_session.scalars(
        select(SkillRepresentation).where(SkillRepresentation.skill_id == skill.id)
    ).all()
    assert len(representations) == 1
    assert representations[0].kind == RepresentationKind.CANONICAL.value


def test_alias_creation(db_session: Session) -> None:
    skill = make_skill(db_session, "Pulumi")
    version = snapshot(db_session).version
    db_session.add(
        SkillRepresentation(
            skill_id=skill.id,
            normalized_representation=normalize("Pulumi IaC"),
            display_text="Pulumi IaC",
            kind=RepresentationKind.ALIAS.value,
            origin=RepresentationOrigin.CURATION.value,
            valid_from_version=version.version_number,
        )
    )
    db_session.flush()
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(SkillRepresentation)
            .where(SkillRepresentation.skill_id == skill.id)
        )
        == 2
    )


# --- the identity invariant ----------------------------------------------


def add_representation_row(
    session: Session, skill_id: uuid.UUID, text_value: str, kind: RepresentationKind
) -> None:
    session.add(
        SkillRepresentation(
            skill_id=skill_id,
            normalized_representation=normalize(text_value),
            display_text=text_value,
            kind=kind.value,
            origin=RepresentationOrigin.CURATION.value,
            valid_from_version=current_version(session),
        )
    )


@pytest.mark.invariant
def test_duplicate_normalized_representation_is_rejected(db_session: Session) -> None:
    """DATABASE.md invariant 6, scoped per version by the EXCLUDE constraint."""
    skill = make_skill(db_session, "Pulumi")
    add_representation_row(db_session, skill.id, "pulumi", RepresentationKind.ALIAS)
    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.invariant
def test_representation_collision_between_different_skills_is_rejected(
    db_session: Session,
) -> None:
    """Invariant 7: a representation binds to at most one catalog entry."""
    make_skill(db_session, "Pulumi")
    other = make_skill(db_session, "Crossplane")
    add_representation_row(db_session, other.id, "Pulumi", RepresentationKind.ALIAS)
    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.invariant
def test_alias_cannot_collide_with_a_canonical_name(db_session: Session) -> None:
    """Canonical names and aliases share ONE uniqueness namespace."""
    make_skill(db_session, "Terraform Cloud")
    other = make_skill(db_session, "Spacelift")
    add_representation_row(db_session, other.id, "Terraform Cloud", RepresentationKind.ALIAS)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_a_skill_cannot_have_two_live_canonical_representations(
    db_session: Session,
) -> None:
    skill = make_skill(db_session, "Pulumi")
    add_representation_row(
        db_session, skill.id, "Pulumi Platform", RepresentationKind.CANONICAL
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_empty_representation_is_rejected(db_session: Session) -> None:
    skill = make_skill(db_session, "Pulumi")
    db_session.add(
        SkillRepresentation(
            skill_id=skill.id,
            normalized_representation="",
            display_text="",
            kind=RepresentationKind.ALIAS.value,
            origin=RepresentationOrigin.CURATION.value,
            valid_from_version=current_version(db_session),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_inverted_validity_range_is_rejected(db_session: Session) -> None:
    skill = make_skill(db_session, "Pulumi")
    db_session.add(
        SkillRepresentation(
            skill_id=skill.id,
            normalized_representation=normalize("Pulumi Alias"),
            display_text="Pulumi Alias",
            kind=RepresentationKind.ALIAS.value,
            origin=RepresentationOrigin.CURATION.value,
            valid_from_version=1,
            valid_to_version=1,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


# --- relationships --------------------------------------------------------


def test_self_relationship_is_rejected(db_session: Session) -> None:
    skill = make_skill(db_session, "Pulumi")
    db_session.add(
        SkillRelationship(
            from_skill_id=skill.id,
            to_skill_id=skill.id,
            relationship_type=SkillRelationshipType.FAMILY.value,
            valid_from_version=current_version(db_session),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_invalid_relationship_type_is_rejected(db_session: Session) -> None:
    a = make_skill(db_session, "Pulumi")
    b = make_skill(db_session, "Crossplane")
    db_session.add(
        SkillRelationship(
            from_skill_id=a.id,
            to_skill_id=b.id,
            relationship_type="identical",
            valid_from_version=current_version(db_session),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.invariant
def test_family_relationships_are_directional(db_session: Session) -> None:
    """Invariant 8: holding EKS evidences Kubernetes, not the reverse."""
    snap = snapshot(db_session)
    eks = snap.skills.find_by_normalized(normalize("Amazon EKS"))
    kubernetes = snap.skills.find_by_normalized(normalize("Kubernetes"))
    assert eks is not None and kubernetes is not None

    assert snap.skill_relationships.has_relationship(
        eks.entry_id, kubernetes.entry_id, SkillRelationshipType.FAMILY
    )
    assert not snap.skill_relationships.has_relationship(
        kubernetes.entry_id, eks.entry_id, SkillRelationshipType.FAMILY
    )


# --- promotion evidence ---------------------------------------------------


@pytest.mark.invariant
def test_promotion_evidence_counts_independent_postings(db_session: Session) -> None:
    """Invariant 17: re-ingesting one posting cannot inflate evidence."""
    posting_id = uuid.uuid4()
    for _ in range(2):
        db_session.add(
            SkillPromotionEvidence(
                normalized_representation="somenewtechnologyx",
                raw_text_observed="SomeNewTechnologyX",
                job_posting_id=posting_id,
            )
        )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_evidence_from_distinct_postings_accumulates(db_session: Session) -> None:
    for _ in range(3):
        db_session.add(
            SkillPromotionEvidence(
                normalized_representation="somenewtechnologyx",
                raw_text_observed="SomeNewTechnologyX",
                job_posting_id=uuid.uuid4(),
            )
        )
    db_session.flush()
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(SkillPromotionEvidence)
            .where(SkillPromotionEvidence.normalized_representation == "somenewtechnologyx")
        )
        == 3
    )


def test_extraction_confidence_is_bounded(db_session: Session) -> None:
    db_session.add(
        SkillPromotionEvidence(
            normalized_representation="x",
            raw_text_observed="X",
            job_posting_id=uuid.uuid4(),
            extraction_confidence=1.5,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


# --- catalog versioning ---------------------------------------------------


def test_seed_created_a_catalog_version(db_session: Session) -> None:
    version = snapshot(db_session).version
    assert version.label == "c1"
    assert version.version_number == 1


@pytest.mark.invariant
def test_catalog_version_rows_cannot_be_updated(db_session: Session) -> None:
    """Invariant 19: the version record is append-only."""
    version = snapshot(db_session).version
    with pytest.raises(DBAPIError):
        db_session.execute(
            text("UPDATE catalog_version SET label = 'tampered' WHERE id = :id"),
            {"id": version.id},
        )


@pytest.mark.invariant
def test_catalog_version_rows_cannot_be_deleted(db_session: Session) -> None:
    version = snapshot(db_session).version
    with pytest.raises(DBAPIError):
        db_session.execute(
            text("DELETE FROM catalog_version WHERE id = :id"), {"id": version.id}
        )


def test_duplicate_catalog_version_number_is_rejected(db_session: Session) -> None:
    db_session.add(CatalogVersion(version_number=1, label="c1-duplicate"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_catalog_entities_record_their_originating_version(db_session: Session) -> None:
    skill = db_session.scalars(select(Skill).limit(1)).one()
    assert skill.created_in_version == 1


def test_seeded_rows_are_live_with_no_upper_bound(db_session: Session) -> None:
    open_ended = db_session.scalar(
        select(func.count())
        .select_from(SkillRepresentation)
        .where(SkillRepresentation.valid_to_version.is_(None))
    )
    total = db_session.scalar(select(func.count()).select_from(SkillRepresentation))
    assert open_ended == total


# --- seniority ------------------------------------------------------------


def test_seed_created_both_ladders(db_session: Session) -> None:
    version = current_version(db_session)
    ic = db_session.scalars(
        select(SeniorityLevel)
        .where(
            SeniorityLevel.track == SeniorityTrack.IC.value,
            live_at(SeniorityLevel, version),
        )
        .order_by(SeniorityLevel.rank_within_track)
    ).all()
    management = db_session.scalars(
        select(SeniorityLevel)
        .where(
            SeniorityLevel.track == SeniorityTrack.MANAGEMENT.value,
            live_at(SeniorityLevel, version),
        )
        .order_by(SeniorityLevel.rank_within_track)
    ).all()

    assert [level.rank_within_track for level in ic] == [1, 2, 3, 4, 5, 6]
    assert [level.rank_within_track for level in management] == [1, 2, 3, 4]


@pytest.mark.invariant
def test_duplicate_live_rank_within_a_track_is_rejected(db_session: Session) -> None:
    """Invariant 22: rank is unique within a track at any given version."""
    db_session.add(
        SeniorityLevel(
            track=SeniorityTrack.IC.value,
            rank_within_track=1,
            valid_from_version=current_version(db_session),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_same_rank_in_the_other_track_is_allowed(db_session: Session) -> None:
    """IC rank 1 and Management rank 1 coexist and mean nothing to each other."""
    version = current_version(db_session)
    ic = db_session.scalars(
        select(SeniorityLevel).where(
            SeniorityLevel.track == SeniorityTrack.IC.value,
            SeniorityLevel.rank_within_track == 1,
            live_at(SeniorityLevel, version),
        )
    ).one()
    management = db_session.scalars(
        select(SeniorityLevel).where(
            SeniorityLevel.track == SeniorityTrack.MANAGEMENT.value,
            SeniorityLevel.rank_within_track == 1,
            live_at(SeniorityLevel, version),
        )
    ).one()
    assert ic.id != management.id


def test_invalid_track_is_rejected(db_session: Session) -> None:
    db_session.add(
        SeniorityLevel(
            track="Unknown",
            rank_within_track=99,
            valid_from_version=current_version(db_session),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.invariant
def test_cross_track_relationships_are_seeded_empty(db_session: Session) -> None:
    """With no live rows, cross-track comparison returns different_track."""
    assert db_session.scalar(select(func.count()).select_from(SeniorityRelationship)) == 0
    assert snapshot(db_session).seniority_relationships.cross_track_bands() == []


@pytest.mark.invariant
@pytest.mark.parametrize("title", ["Lead", "Architect", "Associate", "Head", "Engineer"])
def test_ambiguous_titles_are_absent_from_the_catalog(
    db_session: Session, title: str
) -> None:
    found = db_session.scalars(
        select(SeniorityRepresentation).where(
            SeniorityRepresentation.normalized_representation == normalize(title)
        )
    ).first()
    assert found is None


# --- resolution against the seeded catalog --------------------------------


def test_seeded_alias_resolves_to_its_canonical_skill(db_session: Session) -> None:
    skills = snapshot(db_session).skills
    canonical = resolve_skill("Amazon Web Services", skills)
    alias = resolve_skill("AWS", skills)

    assert alias.status is ResolutionStatus.RESOLVED
    assert alias.provenance is ResolutionProvenance.ALIAS_RULE
    assert alias.resolved_canonical_name == "Amazon Web Services"
    assert alias.skill_id == canonical.skill_id


def test_seeded_canonical_resolves_exactly(db_session: Session) -> None:
    result = resolve_skill("Kubernetes", snapshot(db_session).skills)
    assert result.provenance is ResolutionProvenance.EXACT_MATCH
    assert result.resolved_canonical_name == "Kubernetes"


def test_k8s_and_kubernetes_are_the_same_skill(db_session: Session) -> None:
    skills = snapshot(db_session).skills
    assert resolve_skill("K8s", skills).skill_id == resolve_skill("Kubernetes", skills).skill_id


def test_uncatalogued_skill_is_unresolved(db_session: Session) -> None:
    result = resolve_skill("SomeNewTechnologyX", snapshot(db_session).skills)
    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.skill_id is None


def test_open_conflict_blocks_resolution(db_session: Session) -> None:
    open_conflict(
        db_session,
        normalized=normalize("Kubernetes"),
        conflict_type=ConflictType.REPRESENTATION_COLLISION,
        candidate_skill_ids=[uuid.uuid4(), uuid.uuid4()],
        version=snapshot(db_session).version,
    )

    result = resolve_skill("Kubernetes", snapshot(db_session).skills)
    assert result.status is ResolutionStatus.INDETERMINATE
    assert result.reason == "open_catalog_conflict"


def test_seeded_seniority_alias_resolves(db_session: Session) -> None:
    result = resolve_seniority("Sr", snapshot(db_session).seniority, EvidenceSource.TITLE)
    assert result.status is ResolutionStatus.RESOLVED
    assert result.resolved_level_name == "Senior"
    assert result.resolved_track is SeniorityTrack.IC


def test_seeded_management_level_resolves_to_its_track(db_session: Session) -> None:
    result = resolve_seniority(
        "Director", snapshot(db_session).seniority, EvidenceSource.TITLE
    )
    assert result.resolved_track is SeniorityTrack.MANAGEMENT


@pytest.mark.invariant
def test_ambiguous_title_stays_indeterminate_against_the_real_catalog(
    db_session: Session,
) -> None:
    result = resolve_seniority("Lead", snapshot(db_session).seniority, EvidenceSource.TITLE)
    assert result.status is ResolutionStatus.INDETERMINATE
    assert result.resolved_level_name is None


def test_skill_origin_enum_is_recorded(db_session: Session) -> None:
    skill = make_skill(db_session, "Pulumi")
    assert skill.created_from == SkillOrigin.CURATION.value
