"""Database session management.

PostgreSQL is the source of truth. The connection is reached through an
environment-configured URL only, so the database can later move to a
separate host without touching the domain model.
"""

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def _build_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url.get_secret_value(),
        # Conservative pool for the 4 GB minimum target, within the
        # PostgreSQL max_connections budget set in docker-compose.
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        future=True,
    )


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a transactional session."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
