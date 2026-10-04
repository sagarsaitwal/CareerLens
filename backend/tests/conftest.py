"""Shared test fixtures.

Integration tests run against a real PostgreSQL. SQLite is never used as
a substitute: database constraints are part of the specification, and
SQLite does not enforce them the way PostgreSQL does.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.main import create_app

BACKEND_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set; integration tests need real PostgreSQL")
    return url


@pytest.fixture(scope="session")
def engine(test_database_url: str) -> Iterator[Engine]:
    eng = create_engine(test_database_url, future=True)
    try:
        with eng.connect() as conn:
            # Fail loudly rather than silently falling back to another
            # database: constraint behaviour is what these tests verify.
            backend = conn.dialect.name
            assert backend == "postgresql", f"expected postgresql, got {backend}"
            conn.execute(text("SELECT 1"))
    except AssertionError:
        raise
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    yield eng
    eng.dispose()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(scope="session")
def migrated_engine(engine: Engine, test_database_url: str) -> Iterator[Engine]:
    """A test database with every migration applied.

    Migrations are run rather than metadata.create_all, so the tests
    exercise the real DDL — including the triggers and partial indexes
    that autogenerate cannot produce and that several documented
    invariants depend on.
    """
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = test_database_url
    get_settings.cache_clear()

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))

    try:
        # Start from a known-empty schema so a rerun is deterministic.
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        command.upgrade(config, "head")
        yield engine
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()


@pytest.fixture
def db_session(migrated_engine: Engine) -> Iterator[Session]:
    """A session rolled back after each test, so cases stay isolated."""
    connection = migrated_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
