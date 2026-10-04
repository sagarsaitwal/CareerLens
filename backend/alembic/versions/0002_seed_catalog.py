"""Seed the initial catalog: skills, seniority ladders, relationships

Revision ID: 0002_seed_catalog
Revises: 0001_catalog_foundation
Create Date: 2026-10-04

Reviewed by hand: yes

A deliberately MINIMAL deterministic seed, not an exhaustive technology
catalog. It exists to exercise the resolution cascade and the identity
invariants.

Deliberately NOT seeded as seniority representations: Lead, Architect,
Associate, Head, Engineer, Software Engineer, Developer. No documented
rule establishes their meaning, so they must resolve to indeterminate or
unknown rather than to a guessed level (DATABASE.md Section 2.7).

seniority_relationship is seeded EMPTY by decision: populating
cross-track bands would assume a specific company's ladder, so
cross-track comparison returns different_track instead.
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_seed_catalog"
down_revision: str | None = "0001_catalog_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATALOG_VERSION_LABEL = "c1"
CATALOG_VERSION_NUMBER = 1

# (canonical display text, [alias display texts])
SKILLS: list[tuple[str, list[str]]] = [
    ("Amazon Web Services", ["AWS", "Amazon AWS"]),
    ("Microsoft Azure", ["Azure"]),
    ("Kubernetes", ["K8s", "kube"]),
    ("Amazon EKS", ["EKS"]),
    ("Docker", []),
    ("Terraform", ["HashiCorp Terraform"]),
    ("Linux", []),
    ("Jenkins", []),
    ("Git", []),
    ("Prometheus", []),
    ("Grafana", []),
    ("Wazuh", []),
    ("OpenSearch", ["Open Search"]),
]

# (from canonical, to canonical, relationship_type)
#
# `family` is directional: EKS is a managed form of Kubernetes, so
# holding EKS evidences Kubernetes substantially while the reverse holds
# only weakly. `adjacent` pairs share a problem domain but are NOT
# substitutable for one another.
SKILL_RELATIONSHIPS: list[tuple[str, str, str]] = [
    ("Amazon EKS", "Kubernetes", "family"),
    ("Prometheus", "Grafana", "adjacent"),
    ("Docker", "Kubernetes", "adjacent"),
]

# track -> ordered levels, each (canonical display text, [aliases]).
# Rank is the 1-based position and is comparable ONLY within its track.
SENIORITY_LADDERS: dict[str, list[tuple[str, list[str]]]] = {
    "IC": [
        ("Intern/Trainee", ["Intern", "Trainee"]),
        ("Junior", ["Jr"]),
        ("Mid", ["Mid-Level", "Intermediate"]),
        ("Senior", ["Sr"]),
        ("Staff", []),
        ("Principal", []),
    ],
    "Management": [
        ("Manager", ["Engineering Manager"]),
        ("Senior Manager", []),
        ("Director", []),
        ("VP", ["Vice President"]),
    ],
}


def _normalize(raw: str) -> str:
    """Mirror of app.domain.normalization.normalize.

    Duplicated deliberately: a migration must keep producing the same
    rows even if the application's normalizer later changes behaviour
    under a new rule_version. Importing application code into a
    migration would make historical migrations mutable.
    """
    import re
    import unicodedata

    folded = unicodedata.normalize("NFKC", raw).casefold()
    separated = re.sub(r"[\s_\-/\\.,;:]+", " ", folded)
    return re.sub(r"\s+", " ", separated).strip()


def upgrade() -> None:
    bind = op.get_bind()

    catalog_version_id = uuid.uuid4()
    bind.execute(
        sa.text(
            "INSERT INTO catalog_version (id, version_number, label, description) "
            "VALUES (:id, :n, :label, :description)"
        ),
        {
            "id": catalog_version_id,
            "n": CATALOG_VERSION_NUMBER,
            "label": CATALOG_VERSION_LABEL,
            "description": "Initial minimal skill and seniority catalog",
        },
    )

    skill_ids: dict[str, uuid.UUID] = {}
    for canonical, aliases in SKILLS:
        skill_id = uuid.uuid4()
        skill_ids[canonical] = skill_id
        bind.execute(
            sa.text(
                "INSERT INTO skill (id, created_from, created_in_version) "
                "VALUES (:id, 'curation', :v)"
            ),
            {"id": skill_id, "v": CATALOG_VERSION_NUMBER},
        )
        _insert_representations(
            bind,
            table="skill_representation",
            owner_column="skill_id",
            owner_id=skill_id,
            canonical=canonical,
            aliases=aliases,
        )

    for from_name, to_name, relationship_type in SKILL_RELATIONSHIPS:
        bind.execute(
            sa.text(
                "INSERT INTO skill_relationship "
                "(id, from_skill_id, to_skill_id, relationship_type, valid_from_version) "
                "VALUES (:id, :from_id, :to_id, :type, :v)"
            ),
            {
                "id": uuid.uuid4(),
                "from_id": skill_ids[from_name],
                "to_id": skill_ids[to_name],
                "type": relationship_type,
                "v": CATALOG_VERSION_NUMBER,
            },
        )

    for track, levels in SENIORITY_LADDERS.items():
        for rank, (canonical, aliases) in enumerate(levels, start=1):
            level_id = uuid.uuid4()
            bind.execute(
                sa.text(
                    "INSERT INTO seniority_level "
                    "(id, track, rank_within_track, valid_from_version) "
                    "VALUES (:id, :track, :rank, :v)"
                ),
                {
                    "id": level_id,
                    "track": track,
                    "rank": rank,
                    "v": CATALOG_VERSION_NUMBER,
                },
            )
            _insert_representations(
                bind,
                table="seniority_representation",
                owner_column="seniority_level_id",
                owner_id=level_id,
                canonical=canonical,
                aliases=aliases,
            )


def _insert_representations(
    bind: sa.engine.Connection,
    *,
    table: str,
    owner_column: str,
    owner_id: uuid.UUID,
    canonical: str,
    aliases: list[str],
) -> None:
    rows = [(canonical, "canonical")] + [(alias, "alias") for alias in aliases]
    for display_text, kind in rows:
        bind.execute(
            sa.text(
                f"INSERT INTO {table} "
                f"(id, {owner_column}, normalized_representation, display_text, "
                "kind, origin, valid_from_version) "
                "VALUES (:id, :owner, :normalized, :display, :kind, 'curation', :v)"
            ),
            {
                "id": uuid.uuid4(),
                "owner": owner_id,
                "normalized": _normalize(display_text),
                "display": display_text,
                "kind": kind,
                "v": CATALOG_VERSION_NUMBER,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    # Ordered by dependency. catalog_version rows are append-only and
    # protected by a trigger, so the seed version is intentionally left
    # in place: a historical MatchResult may still reference it.
    bind.execute(sa.text("DELETE FROM seniority_relationship"))
    bind.execute(sa.text("DELETE FROM seniority_representation"))
    bind.execute(sa.text("DELETE FROM seniority_level"))
    bind.execute(sa.text("DELETE FROM skill_relationship"))
    bind.execute(sa.text("DELETE FROM skill_representation"))
    bind.execute(sa.text("DELETE FROM skill"))
