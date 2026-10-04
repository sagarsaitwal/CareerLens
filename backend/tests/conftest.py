"""Shared test fixtures.

Integration tests run against a real PostgreSQL. SQLite is never used as
a substitute: database constraints are part of the specification, and
SQLite does not enforce them the way PostgreSQL does.
"""

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.main import create_app


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
