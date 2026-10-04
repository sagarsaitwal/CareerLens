"""ORM models.

Imported here so Base.metadata is fully populated for Alembic
autogenerate and for test schema creation.
"""

from app.models.catalog import (
    CatalogVersion,
    Skill,
    SkillConflict,
    SkillPromotionEvidence,
    SkillRelationship,
    SkillRepresentation,
)
from app.models.profile import (
    MatchingCriteria,
    ProfileSkill,
    ProfileVersion,
    UserProfile,
)
from app.models.seniority import (
    SeniorityLevel,
    SeniorityRelationship,
    SeniorityRepresentation,
)

__all__ = [
    "CatalogVersion",
    "MatchingCriteria",
    "ProfileSkill",
    "ProfileVersion",
    "SeniorityLevel",
    "SeniorityRelationship",
    "SeniorityRepresentation",
    "Skill",
    "SkillConflict",
    "SkillPromotionEvidence",
    "SkillRelationship",
    "SkillRepresentation",
    "UserProfile",
]
