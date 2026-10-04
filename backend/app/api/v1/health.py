"""Health and readiness endpoints.

Readiness distinguishes the two failure classes documented in
AI-MATCHING.md Section 12: PostgreSQL is required for the service to be
useful at all, whereas Redis degrades throughput rather than
correctness.
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from app.core import versions
from app.db.session import engine

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    versions: dict[str, str]


class ReadinessResponse(BaseModel):
    database: Literal["up", "down"]
    ready: bool


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", versions=versions.describe())


@router.get("/ready", response_model=ReadinessResponse)
def ready() -> ReadinessResponse:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        database: Literal["up", "down"] = "up"
    except Exception:
        database = "down"
    return ReadinessResponse(database=database, ready=database == "up")
