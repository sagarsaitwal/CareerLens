"""Profile snapshot construction.

Pure functions, no I/O. The snapshot is the matching input of record, so
building it must be deterministic: the same live state always produces
the same document, byte for byte.

That determinism is what makes outcome-keyed minting possible. A new
ProfileVersion is created only when the snapshot actually differs, so
re-running resolution that changes nothing mints nothing.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.core.versions import PROFILE_SNAPSHOT_SCHEMA_VERSION
from app.domain.resolution import ResolutionProvenance, ResolutionStatus
from app.domain.seniority import EvidenceSource
from app.models.enums import PROFICIENCY_ORDER, Proficiency, SeniorityTrack


@dataclass(frozen=True, slots=True)
class SeniorityReference:
    """One resolved (or unresolved) seniority reference.

    Level name AND track are carried by value so the snapshot survives
    catalog evolution.
    """

    raw_text: str
    normalized_representation: str
    status: ResolutionStatus
    provenance: ResolutionProvenance
    evidence_source: EvidenceSource
    level_id: uuid.UUID | None = None
    resolved_level_name: str | None = None
    resolved_track: SeniorityTrack | None = None
    resolved_rank_within_track: int | None = None


@dataclass(frozen=True, slots=True)
class CapabilityEntry:
    """A ProfileSkill: what the user can do."""

    raw_text: str
    normalized_representation: str
    status: ResolutionStatus
    provenance: ResolutionProvenance
    proficiency: Proficiency
    skill_id: uuid.UUID | None = None
    resolved_canonical_name: str | None = None


@dataclass(frozen=True, slots=True)
class IntentEntry:
    """A MatchingCriteria: what matching should prioritize."""

    raw_text: str
    normalized_representation: str
    status: ResolutionStatus
    provenance: ResolutionProvenance
    weight: str
    is_required: bool
    skill_id: uuid.UUID | None = None
    resolved_canonical_name: str | None = None


@dataclass(frozen=True, slots=True)
class ProfileState:
    """Everything matching-relevant about a profile at one moment.

    Deliberately excludes resumes, CTC, recruiter contacts, interview
    notes and application history. The snapshot built from this defines
    the maximum extent of profile data that may ever cross the LLM
    boundary, so anything absent here can never leak.
    """

    profile_id: uuid.UUID
    experience_years: int | None
    capabilities: tuple[CapabilityEntry, ...]
    intents: tuple[IntentEntry, ...]
    current_seniority: SeniorityReference | None = None
    target_seniority: SeniorityReference | None = None
    # Location and work mode are Stage 2 comparison dimensions
    # (AI-MATCHING.md), so they belong in the matching input of record.
    # Stored order is preserved rather than sorted: nothing documents
    # these as an unordered set, and re-ordering a user's list would
    # discard any priority they expressed by entering them that way.
    preferred_locations: tuple[str, ...] = ()
    preferred_work_modes: tuple[str, ...] = ()


# Capability and intent share the skill-mention shape; the union is the
# whole point of the snapshot, so they are keyed by the same rules.
type Mention = CapabilityEntry | IntentEntry

_AMBIGUOUS = object()


def _identity_of(
    status: ResolutionStatus, skill_id: uuid.UUID | None
) -> uuid.UUID | None:
    return skill_id if status is ResolutionStatus.RESOLVED else None


def _identity_by_text(state: ProfileState) -> dict[str, uuid.UUID]:
    """Map normalized text to the skill it resolved to, where one did.

    Built across capabilities AND intents before any keying, so the
    union is symmetric: it must not matter whether the resolved side of
    a pair is the capability or the intent.

    Text resolving to two different skills is dropped rather than
    arbitrated. That is the catalog contradicting itself, and the one
    thing worse than leaving it unjoined would be picking a winner.
    """
    seen: dict[str, uuid.UUID | object] = {}
    mentions: tuple[Mention, ...] = (*state.capabilities, *state.intents)
    for mention in mentions:
        identity = _identity_of(mention.status, mention.skill_id)
        if identity is None:
            continue
        existing = seen.setdefault(mention.normalized_representation, identity)
        if existing is not identity and existing != identity:
            seen[mention.normalized_representation] = _AMBIGUOUS
    return {
        text: value
        for text, value in seen.items()
        if isinstance(value, uuid.UUID)
    }


def _union_key(
    normalized: str,
    status: ResolutionStatus,
    skill_id: uuid.UUID | None,
    identity_by_text: dict[str, uuid.UUID],
) -> str:
    """The key under which a capability and an intent become one entry.

    Identity first: once a mention resolves, its canonical skill is what
    identifies it. Capability "Kubernetes" and intent "K8s" — different
    text, same resolved skill — are therefore one entry rather than two,
    which plain text keying could never achieve.

    The converse matters just as much: two mentions resolving to
    DIFFERENT skills never share a key, even if some normalization made
    their text identical. Conflicting identities must not silently
    overwrite one another.

    Text remains a fallback join for a mention that has no identity yet,
    because resolution can lag one side of a pair. Dropping that join
    would split a held skill from the intent naming it, and matching
    would then report a gap for something the user demonstrably has.
    """
    identity = _identity_of(status, skill_id) or identity_by_text.get(normalized)
    if identity is not None:
        return f"id:{identity}"
    return f"rep:{normalized}"


def _strongest_capability(capabilities: list[CapabilityEntry]) -> CapabilityEntry:
    """The capability that speaks for a skill the user holds.

    Strongest proficiency wins. Two rows naming one skill ("Kubernetes"
    Strong, "K8s" Learning) are two statements about the same ability,
    and the stronger one is the true claim — the weaker is a stale or
    partial duplicate. Taking the weaker would understate what the user
    can do and manufacture a skill gap against a job that needs it.
    """
    return min(
        capabilities,
        key=lambda capability: (
            # Negated so the strongest sorts first, letting every
            # tie-break below run ascending like _representative's.
            -PROFICIENCY_ORDER.index(capability.proficiency),
            capability.normalized_representation,
            capability.raw_text,
        ),
    )


def _strongest_intent(intents: list[IntentEntry]) -> IntentEntry:
    """The intent that speaks for what matching should prioritize.

    Required dominates optional, then higher weight dominates lower.
    Both rules point the same way: a duplicate must never be able to
    weaken a requirement the user actually expressed. Demoting a
    required skill to optional would let a job missing it score as a
    good match.
    """
    return min(
        intents,
        key=lambda intent: (
            not intent.is_required,
            -Decimal(intent.weight),
            intent.normalized_representation,
            intent.raw_text,
        ),
    )


def _representative(
    capabilities: list[CapabilityEntry], intents: list[IntentEntry]
) -> Mention:
    """The mention whose text and provenance stand for the whole entry.

    The snapshot schema carries one raw_text, one normalized
    representation and one provenance per entry, so a merged group must
    elect a single representative. Three rules, in strict order:

    1. A RESOLVED mention always wins. The entry may be keyed on an
       identity only some of its members established, and an entry
       reporting `unresolved` while carrying that identity is exactly
       the contradiction `resolved_requires_identity` forbids at row
       level. Identity coherence outranks everything below.

    2. Among those, the row that supplied the winning PROFICIENCY, then
       the row that supplied the winning WEIGHT. This is co-origination:
       raw_text is evidence of how the user expressed the skill, so
       pairing "K8s" with a Strong proficiency that came from the
       "Kubernetes" row would attribute a claim to a row that never made
       it. Preferring the winning capability's row makes raw_text and
       proficiency describe one real row instead of a composite of two.

    3. Otherwise the lexicographically smallest text, purely for
       stability.

    Co-origination can hold for only one axis, because capability and
    intent are different tables and an entry may draw from both. The
    capability is preferred since "what the user holds" is the more
    concrete evidence claim; the intent axis is elected independently by
    `_strongest_intent` either way.
    """
    mentions: list[Mention] = [*capabilities, *intents]
    preferred: list[Mention] = []
    if capabilities:
        preferred.append(_strongest_capability(capabilities))
    if intents:
        preferred.append(_strongest_intent(intents))

    def co_origin_rank(mention: Mention) -> int:
        for position, winner in enumerate(preferred):
            if winner is mention:
                return position
        return len(preferred)

    return min(
        mentions,
        key=lambda mention: (
            mention.status is not ResolutionStatus.RESOLVED,
            co_origin_rank(mention),
            mention.normalized_representation,
            mention.raw_text,
        ),
    )


def build_snapshot(state: ProfileState) -> dict[str, Any]:
    """Render a profile state as the immutable snapshot document.

    Skills are a UNION keyed by skill identity: an entry may carry
    capability, intent, or both. That union is what lets an aspirational
    skill (intent with no capability) stay truthfully distinct from a
    held one.

    The output does not depend on the order mentions arrive in. Mentions
    are grouped by identity first, and every value the group contributes
    is then chosen by an explicit rule over the whole group rather than
    by whichever row happened to be processed last:

      - proficiency comes from the STRONGEST capability in the group
      - weight and is_required come from the STRONGEST intent, where
        required beats optional and higher weight beats lower
      - raw_text, normalized representation and provenance come from an
        elected representative: resolved mentions first, then the row
        that supplied the winning proficiency, so the entry's text and
        its proficiency describe one real row rather than a composite

    Every one of those rules is a total order with a deterministic
    tie-break, groups are visited in sorted key order, and the final
    list is sorted again, so the same state always produces the same
    document byte for byte. That is what outcome-keyed minting relies
    on: a snapshot that varied with row order would mint versions for
    changes that never happened.
    """
    identity_by_text = _identity_by_text(state)

    capabilities_by_key: dict[str, list[CapabilityEntry]] = {}
    for capability in state.capabilities:
        capabilities_by_key.setdefault(
            _union_key(
                capability.normalized_representation,
                capability.status,
                capability.skill_id,
                identity_by_text,
            ),
            [],
        ).append(capability)

    intents_by_key: dict[str, list[IntentEntry]] = {}
    for intent in state.intents:
        intents_by_key.setdefault(
            _union_key(
                intent.normalized_representation,
                intent.status,
                intent.skill_id,
                identity_by_text,
            ),
            [],
        ).append(intent)

    merged: dict[str, dict[str, Any]] = {}
    for key in sorted({*capabilities_by_key, *intents_by_key}):
        capabilities = capabilities_by_key.get(key, [])
        intents = intents_by_key.get(key, [])
        representative = _representative(capabilities, intents)

        entry = _mention(
            representative.raw_text,
            representative.normalized_representation,
            representative.status,
            representative.provenance,
            representative.skill_id,
            representative.resolved_canonical_name,
        )
        # Capability and intent stay separate dimensions of one entry: a
        # skill the user holds and a skill they are targeting are
        # different claims, and an entry may carry either or both.
        if capabilities:
            entry["proficiency"] = _strongest_capability(capabilities).proficiency.value
        if intents:
            strongest = _strongest_intent(intents)
            entry["weight"] = strongest.weight
            entry["is_required"] = strongest.is_required
        merged[key] = entry

    # Sorted on the entry's own normalized text rather than the internal
    # union key, so the document stays readable and does not order itself
    # by opaque UUIDs.
    skills = sorted(
        merged.values(),
        key=lambda entry: (entry["normalized_representation"], entry["skill_id"] or ""),
    )

    return {
        "schema_version": PROFILE_SNAPSHOT_SCHEMA_VERSION,
        "profile_id": str(state.profile_id),
        "experience_years": state.experience_years,
        "skills": skills,
        "current_seniority": _seniority(state.current_seniority),
        # Absent target means no preference was expressed. It is never
        # defaulted from current: those are different claims.
        "target_seniority": _seniority(state.target_seniority),
        "preferred_locations": list(state.preferred_locations),
        "preferred_work_modes": list(state.preferred_work_modes),
        "seniority_ladder": _referenced_ladder(state),
        "vocabularies": {
            "proficiency_levels": [level.value for level in PROFICIENCY_ORDER],
        },
    }


def snapshots_equivalent(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Whether two snapshots describe the same matching-relevant state.

    Used for outcome-keyed minting. The catalog version is deliberately
    NOT part of this comparison: if the catalog moved but every resolved
    value is identical, nothing matching-relevant changed and no version
    should be minted.
    """
    return left == right


def _mention(
    raw_text: str,
    normalized: str,
    status: ResolutionStatus,
    provenance: ResolutionProvenance,
    skill_id: uuid.UUID | None,
    canonical_name: str | None,
) -> dict[str, Any]:
    return {
        "raw_text": raw_text,
        "normalized_representation": normalized,
        "resolution_status": status.value,
        "resolution_provenance": provenance.value,
        # Provenance pointer only. Interpretation relies on the
        # canonical name carried by value beside it.
        "skill_id": str(skill_id) if skill_id else None,
        "resolved_canonical_name": canonical_name,
    }


def _seniority(reference: SeniorityReference | None) -> dict[str, Any] | None:
    if reference is None:
        return None
    return {
        "raw_text": reference.raw_text,
        "normalized_representation": reference.normalized_representation,
        "resolution_status": reference.status.value,
        "resolution_provenance": reference.provenance.value,
        "evidence_source": reference.evidence_source.value,
        "level_id": str(reference.level_id) if reference.level_id else None,
        "resolved_level_name": reference.resolved_level_name,
        "resolved_track": (
            reference.resolved_track.value if reference.resolved_track else None
        ),
        "resolved_rank_within_track": reference.resolved_rank_within_track,
    }


def _referenced_ladder(state: ProfileState) -> list[dict[str, Any]]:
    """Freeze the ladder entries this snapshot actually references.

    Only the referenced levels, not the whole catalog: rank is only
    meaningful within a track, and a comparison needs exactly the levels
    involved.
    """
    entries: dict[str, dict[str, Any]] = {}
    for reference in (state.current_seniority, state.target_seniority):
        if reference is None or reference.resolved_level_name is None:
            continue
        if reference.resolved_track is None or reference.resolved_rank_within_track is None:
            continue
        key = f"{reference.resolved_track.value}:{reference.resolved_level_name}"
        entries[key] = {
            "level_id": str(reference.level_id) if reference.level_id else None,
            "level_name": reference.resolved_level_name,
            "track": reference.resolved_track.value,
            "rank_within_track": reference.resolved_rank_within_track,
        }
    return [entries[key] for key in sorted(entries)]
