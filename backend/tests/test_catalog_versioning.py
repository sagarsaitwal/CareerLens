"""Historical catalog reproducibility.

The failure these guard against is silent: an unscoped lookup returns
plausible rows from today's catalog while claiming to reproduce a
historical result. Each test therefore resolves at c1, changes the
catalog in c2, and asserts the c1 answer is unchanged.
"""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.domain.normalization import normalize
from app.domain.resolution import ResolutionProvenance, ResolutionStatus, resolve_skill
from app.domain.seniority import (
    EvidenceSource,
    SeniorityComparisonOutcome,
    compare_seniority,
    resolve_seniority,
)
from app.models.catalog import CatalogVersion, SkillRepresentation
from app.models.enums import (
    ConflictType,
    RepresentationKind,
    SeniorityTrack,
    SkillRelationshipType,
)
from app.repositories.catalog import (
    CatalogSnapshot,
    EmptyCatalogError,
    UnknownCatalogVersionError,
)
from app.services.catalog import (
    add_relationship,
    add_representation,
    add_skill,
    close_conflict,
    open_catalog_version,
    open_conflict,
    retire_representation,
)

pytestmark = pytest.mark.integration

C1 = 1

BACKEND_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def unseeded_session(test_database_url: str) -> Iterator[Session]:
    """A database migrated to 0001 only, so `catalog_version` is empty.

    This is a real, reachable state: schema deployed, seed migration not
    yet run. It is produced on a throwaway database rather than by
    deleting rows, because `catalog_version` is append-only by trigger
    and relaxing that to suit a test would undermine the behaviour being
    tested.
    """
    base = make_url(test_database_url)
    scratch = f"{base.database}_unseeded"

    admin = create_engine(
        base.set(database="postgres"), isolation_level="AUTOCOMMIT", future=True
    )
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{scratch}"'))

    scratch_url = base.set(database=scratch).render_as_string(hide_password=False)
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = scratch_url
    get_settings.cache_clear()

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))

    engine = create_engine(scratch_url, future=True)
    try:
        # Stop short of 0002_seed_catalog deliberately.
        command.upgrade(config, "0001_catalog_foundation")
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'))
        admin.dispose()


def at(session: Session, version: int) -> CatalogSnapshot:
    return CatalogSnapshot.at(session, version)


def new_version(session: Session, label: str) -> int:
    return open_catalog_version(session, label).version_number


# --- the headline scenario ------------------------------------------------


@pytest.mark.invariant
def test_alias_retired_in_c2_still_resolves_at_c1(db_session: Session) -> None:
    """The exact failure the review identified.

    c1: K8s -> Kubernetes. c2 retires the alias. Re-executing a
    MatchResult stamped catalog_version=c1 must still see c1 semantics.
    """
    before = resolve_skill("K8s", at(db_session, C1).skills)
    assert before.status is ResolutionStatus.RESOLVED
    assert before.resolved_canonical_name == "Kubernetes"

    c2 = new_version(db_session, "c2")
    retire_representation(
        db_session, normalized=normalize("K8s"), version=at(db_session, c2).version
    )

    after_c1 = resolve_skill("K8s", at(db_session, C1).skills)
    after_c2 = resolve_skill("K8s", at(db_session, c2).skills)

    assert after_c1 == before, "c1 resolution changed after a c2 catalog edit"
    assert after_c2.status is ResolutionStatus.UNRESOLVED


@pytest.mark.invariant
def test_alias_added_in_c2_does_not_exist_at_c1(db_session: Session) -> None:
    """Catalog growth must not leak backwards into history."""
    assert resolve_skill("K8S Cluster", at(db_session, C1).skills).status is (
        ResolutionStatus.UNRESOLVED
    )

    c2 = new_version(db_session, "c2")
    kubernetes = at(db_session, C1).skills.find_by_normalized(normalize("Kubernetes"))
    assert kubernetes is not None
    add_representation(
        db_session,
        skill_id=kubernetes.entry_id,
        display_text="K8S Cluster",
        version=at(db_session, c2).version,
    )

    assert resolve_skill("K8S Cluster", at(db_session, C1).skills).status is (
        ResolutionStatus.UNRESOLVED
    )
    assert resolve_skill("K8S Cluster", at(db_session, c2).skills).status is (
        ResolutionStatus.RESOLVED
    )


def test_representation_rebound_to_a_different_skill_in_c2(db_session: Session) -> None:
    """A representation may move between skills across versions.

    Each version reports its own answer; neither is rewritten.
    """
    c2 = new_version(db_session, "c2")
    snap2 = at(db_session, c2)
    retire_representation(db_session, normalized=normalize("kube"), version=snap2.version)
    other = add_skill(db_session, "Kube Framework", snap2.version)
    add_representation(
        db_session, skill_id=other.id, display_text="kube", version=snap2.version
    )

    at_c1 = resolve_skill("kube", at(db_session, C1).skills)
    at_c2 = resolve_skill("kube", at(db_session, c2).skills)

    assert at_c1.resolved_canonical_name == "Kubernetes"
    assert at_c2.resolved_canonical_name == "Kube Framework"
    assert at_c1.skill_id != at_c2.skill_id


# --- never silently fall back --------------------------------------------


@pytest.mark.invariant
def test_unknown_version_raises_rather_than_using_the_latest(
    db_session: Session,
) -> None:
    """A historical lookup must never silently use the current catalog."""
    with pytest.raises(UnknownCatalogVersionError):
        CatalogSnapshot.at(db_session, 9999)


@pytest.mark.invariant
def test_current_raises_when_no_catalog_version_exists(
    unseeded_session: Session,
) -> None:
    """An unseeded catalog must fail loudly, not invent a version.

    Exercised against a real reachable state: schema migrated to 0001
    with the seed migration not yet applied. Silently substituting a
    default — `c0` from app.core.versions, or any other — would let a
    MatchResult record a catalog_version that never described a
    catalog.
    """
    assert (
        unseeded_session.scalar(select(func.count()).select_from(CatalogVersion)) == 0
    )

    with pytest.raises(EmptyCatalogError):
        CatalogSnapshot.current(unseeded_session)


def test_current_advances_with_new_versions(db_session: Session) -> None:
    assert CatalogSnapshot.current(db_session).version.version_number == C1
    c2 = new_version(db_session, "c2")
    assert CatalogSnapshot.current(db_session).version.version_number == c2


@pytest.mark.invariant
def test_repositories_cannot_be_built_without_a_version() -> None:
    """The boundary makes an unscoped lookup hard to write by accident."""
    import inspect

    from app.repositories.catalog import SkillRepresentationRepository

    parameters = inspect.signature(SkillRepresentationRepository.__init__).parameters
    assert "version" in parameters
    assert parameters["version"].default is inspect.Parameter.empty


# --- conflicts ------------------------------------------------------------


@pytest.mark.invariant
def test_conflict_opened_in_c2_does_not_affect_c1(db_session: Session) -> None:
    """Conflict state is historical, not retroactive."""
    before = resolve_skill("Terraform", at(db_session, C1).skills)
    assert before.status is ResolutionStatus.RESOLVED

    c2 = new_version(db_session, "c2")
    open_conflict(
        db_session,
        normalized=normalize("Terraform"),
        conflict_type=ConflictType.REPRESENTATION_COLLISION,
        candidate_skill_ids=[uuid.uuid4(), uuid.uuid4()],
        version=at(db_session, c2).version,
    )

    assert resolve_skill("Terraform", at(db_session, C1).skills) == before
    assert resolve_skill("Terraform", at(db_session, c2).skills).status is (
        ResolutionStatus.INDETERMINATE
    )


@pytest.mark.invariant
def test_conflict_closed_in_c2_still_blocks_at_c1(db_session: Session) -> None:
    """Re-executing c1 must still see the conflict that was open then."""
    c2 = new_version(db_session, "c2")
    conflict = open_conflict(
        db_session,
        normalized=normalize("Grafana"),
        conflict_type=ConflictType.CONTRADICTORY_RESOLUTION_CANDIDATES,
        candidate_skill_ids=[uuid.uuid4()],
        version=at(db_session, c2).version,
    )
    assert resolve_skill("Grafana", at(db_session, c2).skills).status is (
        ResolutionStatus.INDETERMINATE
    )

    c3 = new_version(db_session, "c3")
    close_conflict(db_session, conflict=conflict, version=at(db_session, c3).version)

    assert resolve_skill("Grafana", at(db_session, c2).skills).status is (
        ResolutionStatus.INDETERMINATE
    ), "closing a conflict in c3 rewrote c2 semantics"
    assert resolve_skill("Grafana", at(db_session, c3).skills).status is (
        ResolutionStatus.RESOLVED
    )


# --- relationships --------------------------------------------------------


@pytest.mark.invariant
def test_relationship_added_in_c2_is_absent_at_c1(db_session: Session) -> None:
    """Relationship state affects matching, so it must be versioned too."""
    snap1 = at(db_session, C1)
    docker = snap1.skills.find_by_normalized(normalize("Docker"))
    terraform = snap1.skills.find_by_normalized(normalize("Terraform"))
    assert docker is not None and terraform is not None

    assert not snap1.skill_relationships.has_relationship(
        docker.entry_id, terraform.entry_id, SkillRelationshipType.ADJACENT
    )

    c2 = new_version(db_session, "c2")
    add_relationship(
        db_session,
        from_skill_id=docker.entry_id,
        to_skill_id=terraform.entry_id,
        relationship_type=SkillRelationshipType.ADJACENT,
        version=at(db_session, c2).version,
    )

    assert not at(db_session, C1).skill_relationships.has_relationship(
        docker.entry_id, terraform.entry_id, SkillRelationshipType.ADJACENT
    )
    assert at(db_session, c2).skill_relationships.has_relationship(
        docker.entry_id, terraform.entry_id, SkillRelationshipType.ADJACENT
    )


def test_seeded_family_relationship_is_visible_at_c1(db_session: Session) -> None:
    snap = at(db_session, C1)
    eks = snap.skills.find_by_normalized(normalize("Amazon EKS"))
    kubernetes = snap.skills.find_by_normalized(normalize("Kubernetes"))
    assert eks is not None and kubernetes is not None
    assert snap.skill_relationships.has_relationship(
        eks.entry_id, kubernetes.entry_id, SkillRelationshipType.FAMILY
    )


# --- seniority ------------------------------------------------------------


@pytest.mark.invariant
def test_seniority_resolution_is_historically_reproducible(db_session: Session) -> None:
    """Seniority gets the same guarantee as skills, not a weaker one."""
    before = resolve_seniority("Sr", at(db_session, C1).seniority, EvidenceSource.TITLE)
    assert before.resolved_level_name == "Senior"

    c2 = new_version(db_session, "c2")
    db_session.execute(
        _retire_seniority_representation(normalize("Sr"), c2)
    )
    db_session.flush()

    after_c1 = resolve_seniority("Sr", at(db_session, C1).seniority, EvidenceSource.TITLE)
    after_c2 = resolve_seniority("Sr", at(db_session, c2).seniority, EvidenceSource.TITLE)

    assert after_c1 == before
    assert after_c2.status is ResolutionStatus.UNRESOLVED


def test_seniority_comparison_is_stable_across_versions(db_session: Session) -> None:
    senior = resolve_seniority("Senior", at(db_session, C1).seniority, EvidenceSource.TITLE)
    staff = resolve_seniority("Staff", at(db_session, C1).seniority, EvidenceSource.TITLE)
    assert compare_seniority(senior, staff) is SeniorityComparisonOutcome.ABOVE

    new_version(db_session, "c2")
    senior_again = resolve_seniority(
        "Senior", at(db_session, C1).seniority, EvidenceSource.TITLE
    )
    assert senior_again == senior


@pytest.mark.invariant
def test_cross_track_stays_different_track_at_every_version(
    db_session: Session,
) -> None:
    """No invented cross-track ranking, in any version."""
    c2 = new_version(db_session, "c2")
    for version in (C1, c2):
        snap = at(db_session, version)
        principal = resolve_seniority("Principal", snap.seniority, EvidenceSource.TITLE)
        director = resolve_seniority("Director", snap.seniority, EvidenceSource.TITLE)
        assert principal.resolved_track is SeniorityTrack.IC
        assert director.resolved_track is SeniorityTrack.MANAGEMENT
        assert compare_seniority(principal, director) is (
            SeniorityComparisonOutcome.DIFFERENT_TRACK
        )


# --- invariants preserved under versioning --------------------------------


@pytest.mark.invariant
def test_historical_resolution_never_emits_ai_semantic(db_session: Session) -> None:
    c2 = new_version(db_session, "c2")
    for version in (C1, c2):
        for raw in ["Kubernetes", "K8s", "unknown-thing", ""]:
            result = resolve_skill(raw, at(db_session, version).skills)
            assert result.provenance is not ResolutionProvenance.AI_SEMANTIC


def test_closed_rows_are_retained_not_deleted(db_session: Session) -> None:
    """Closing is the only form of removal; history must survive."""
    total_before = db_session.scalar(
        select(func.count()).select_from(SkillRepresentation)
    )
    c2 = new_version(db_session, "c2")
    retire_representation(
        db_session, normalized=normalize("K8s"), version=at(db_session, c2).version
    )
    total_after = db_session.scalar(select(func.count()).select_from(SkillRepresentation))

    assert total_after == total_before, "a catalog change deleted history"


def test_retired_alias_keeps_its_canonical_kind(db_session: Session) -> None:
    c2 = new_version(db_session, "c2")
    retire_representation(
        db_session, normalized=normalize("K8s"), version=at(db_session, c2).version
    )
    row = db_session.scalars(
        select(SkillRepresentation).where(
            SkillRepresentation.normalized_representation == normalize("K8s")
        )
    ).one()
    assert row.kind == RepresentationKind.ALIAS.value
    assert row.valid_to_version == c2


def _retire_seniority_representation(normalized: str, version: int):
    from app.models.seniority import SeniorityRepresentation

    return (
        SeniorityRepresentation.__table__.update()
        .where(
            SeniorityRepresentation.__table__.c.normalized_representation == normalized,
            SeniorityRepresentation.__table__.c.valid_to_version.is_(None),
        )
        .values(valid_to_version=version)
    )
