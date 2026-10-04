"""Profile version minting.

Reads the live editing surface, renders it through the pure snapshot
builder, and persists an immutable ProfileVersion when — and only when —
matching-relevant state has actually changed.

The live tables stay mutable. Nothing here ever updates an existing
ProfileVersion; the database rejects that outright.
"""

import uuid
from collections.abc import Callable
from decimal import Decimal

import redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.locking import distributed_lock, lock_key
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
from app.models.profile import MatchingCriteria, ProfileSkill, ProfileVersion, UserProfile
from app.repositories.catalog import CatalogSnapshot


class NoMatchingRelevantStateError(ValueError):
    """Raised when a snapshot would describe nothing.

    v1 is minted lazily: creating an empty profile row must not create a
    version, because a snapshot of nothing has no value.
    """


def current_version(session: Session, profile_id: uuid.UUID) -> ProfileVersion | None:
    """The newest version for a profile, or None before v1 is minted."""
    return session.scalars(
        select(ProfileVersion)
        .where(ProfileVersion.user_profile_id == profile_id)
        .order_by(ProfileVersion.version_number.desc())
        .limit(1)
    ).first()


def read_state(
    session: Session, profile: UserProfile, catalog: CatalogSnapshot
) -> ProfileState:
    """Collect the live matching-relevant state for a profile.

    Reads only matching-relevant columns. Anything outside that set
    cannot reach the snapshot, and therefore cannot reach a model.

    `catalog` must be an explicitly pinned snapshot: seniority ranks are
    read from it, so a profile is never frozen against whatever the
    catalog happens to look like right now.
    """
    # Ordered explicitly: the snapshot's skill list is compared as an
    # ordered list, and keying the union on skill identity means two
    # entries can legitimately share a normalized representation. Row
    # order must therefore not be left to the planner, or an unchanged
    # profile could mint a version purely because rows came back
    # differently sorted.
    skills = session.scalars(
        select(ProfileSkill)
        .where(ProfileSkill.user_profile_id == profile.id)
        .order_by(ProfileSkill.normalized_representation, ProfileSkill.id)
    ).all()
    criteria = session.scalars(
        select(MatchingCriteria)
        .where(MatchingCriteria.user_profile_id == profile.id)
        .order_by(MatchingCriteria.normalized_representation, MatchingCriteria.id)
    ).all()

    return ProfileState(
        profile_id=profile.id,
        experience_years=profile.experience_years,
        capabilities=tuple(
            CapabilityEntry(
                raw_text=skill.raw_text,
                normalized_representation=skill.normalized_representation,
                status=ResolutionStatus(skill.resolution_status),
                provenance=ResolutionProvenance(skill.resolution_provenance),
                proficiency=Proficiency(skill.proficiency),
                skill_id=skill.skill_id,
                resolved_canonical_name=skill.resolved_canonical_name,
            )
            for skill in skills
        ),
        intents=tuple(
            IntentEntry(
                raw_text=criterion.raw_text,
                normalized_representation=criterion.normalized_representation,
                status=ResolutionStatus(criterion.resolution_status),
                provenance=ResolutionProvenance(criterion.resolution_provenance),
                # Rendered as a string so the snapshot carries the exact
                # persisted NUMERIC rather than a float approximation.
                weight=_decimal_text(criterion.weight),
                is_required=criterion.is_required,
                skill_id=criterion.skill_id,
                resolved_canonical_name=criterion.resolved_canonical_name,
            )
            for criterion in criteria
        ),
        current_seniority=_seniority_reference(profile, "current", catalog),
        target_seniority=_seniority_reference(profile, "target", catalog),
        preferred_locations=tuple(profile.preferred_locations or ()),
        preferred_work_modes=tuple(profile.preferred_work_modes or ()),
    )


def mint_version_if_changed(
    session: Session,
    profile: UserProfile,
    catalog: CatalogSnapshot,
) -> ProfileVersion | None:
    """Create a new ProfileVersion if matching-relevant state changed.

    Returns the new version, or None when nothing changed. Minting is
    keyed on the OUTCOME, not on this function having been called, so a
    no-op sweep mints nothing.

    `catalog` is an explicitly pinned snapshot, recorded on the version
    so a historical re-execution knows exactly which catalog applied.
    """
    state = read_state(session, profile, catalog)
    if not _has_matching_relevant_state(state):
        raise NoMatchingRelevantStateError(
            "profile has no matching-relevant state; nothing to snapshot"
        )

    snapshot = build_snapshot(state)
    latest = current_version(session, profile.id)
    if latest is not None and snapshots_equivalent(latest.snapshot, snapshot):
        return None

    version = ProfileVersion(
        user_profile_id=profile.id,
        version_number=1 if latest is None else latest.version_number + 1,
        schema_version=PROFILE_SNAPSHOT_SCHEMA_VERSION,
        catalog_version_at_creation=catalog.version.version_number,
        snapshot=snapshot,
    )
    session.add(version)
    session.flush()

    # Advancing the pointer edits the LIVE profile row, never the
    # version it points at.
    profile.current_profile_version_id = version.id
    session.flush()
    return version


def profile_version_lock_key(profile_id: uuid.UUID) -> str:
    """The lock guarding minting for one profile.

    Scoped per profile, not globally: two different profiles minting at
    once contend for nothing, and a global lock would serialize all
    minting for no safety gain.
    """
    return lock_key("profile_version_mint", str(profile_id))


def mint_version_with_lock(
    session_factory: Callable[[], Session],
    profile_id: uuid.UUID,
    catalog_version_number: int,
    redis_client: redis.Redis,
) -> ProfileVersion | None:
    """Mint under the distributed lock ARCHITECTURE.md requires.

    Owns its own session so the whole read-compare-insert sequence is
    one transaction that commits BEFORE the lock is released. Were the
    commit to land after, a waiting caller could acquire the lock, read a
    stale latest version, and compute the same version_number — which the
    uniqueness constraint would then reject, turning a race into an
    error instead of convergence.

    Convergence is the point: concurrent callers with identical state
    produce exactly one new version, and the ones that lost the race
    return None because by the time they look, nothing has changed.
    `uq_profile_version_profile_number` remains the backstop if the lock
    is ever bypassed.

    Takes a session factory and ids rather than live objects, because
    callers are concurrent and a Session is not safe to share across
    threads.
    """
    with (
        distributed_lock(redis_client, profile_version_lock_key(profile_id)),
        session_factory() as session,
    ):
        profile = session.get(UserProfile, profile_id)
        if profile is None:
            raise LookupError(f"no profile {profile_id}")

        # Pinned explicitly, never `.current()`: the caller's chosen
        # catalog version is what the resulting snapshot is stamped
        # with, and two racing callers must agree on it.
        catalog = CatalogSnapshot.at(session, catalog_version_number)
        version = mint_version_if_changed(session, profile, catalog)
        session.commit()
        return version


def _has_matching_relevant_state(state: ProfileState) -> bool:
    return bool(
        state.capabilities
        or state.intents
        or state.current_seniority
        or state.target_seniority
        or state.preferred_locations
        or state.preferred_work_modes
        or state.experience_years is not None
    )


def _decimal_text(value: Decimal | float | None) -> str:
    return format(Decimal(str(value)), "f")


def _seniority_reference(
    profile: UserProfile, prefix: str, catalog: CatalogSnapshot
) -> SeniorityReference | None:
    """Read one prefixed seniority reference off the profile row.

    Returns None when unset. An unset target is NOT inferred from
    current: those are different claims.

    The rank is read from the PINNED catalog rather than stored on the
    profile, so the frozen ladder in the snapshot reflects the catalog
    version this version is stamped with.
    """
    raw_text = getattr(profile, f"{prefix}_seniority_raw_text")
    if raw_text is None:
        return None

    normalized = getattr(profile, f"{prefix}_seniority_normalized")
    track = getattr(profile, f"{prefix}_seniority_resolved_track")

    rank: int | None = None
    if normalized:
        record = catalog.seniority.find_by_normalized(normalized)
        if record is not None:
            rank = record.level.rank_within_track

    return SeniorityReference(
        raw_text=raw_text,
        normalized_representation=normalized,
        status=ResolutionStatus(getattr(profile, f"{prefix}_seniority_resolution_status")),
        provenance=ResolutionProvenance(
            getattr(profile, f"{prefix}_seniority_resolution_provenance")
        ),
        evidence_source=EvidenceSource(
            getattr(profile, f"{prefix}_seniority_evidence_source")
        ),
        level_id=getattr(profile, f"{prefix}_seniority_level_id"),
        resolved_level_name=getattr(profile, f"{prefix}_seniority_resolved_level_name"),
        resolved_track=SeniorityTrack(track) if track else None,
        resolved_rank_within_track=rank,
    )
