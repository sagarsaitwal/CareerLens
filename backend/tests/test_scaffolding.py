"""Milestone 1 scaffolding tests.

These cover what exists now: the service starts, the version axes are
present and correctly shaped, and secrets are redacted from logs. The
invariant suite proper arrives with the milestones that implement the
behaviour it protects.
"""

import logging

import pytest

from app.core import versions
from app.core.logging import RedactSecretsFilter


def test_health_returns_ok(client) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_reports_version_axes(client) -> None:
    body = client.get("/api/health").json()
    assert set(body["versions"]) == {"rule_version", "model_version", "catalog_version"}


@pytest.mark.invariant
def test_exactly_four_version_axes_exist() -> None:
    """DATABASE.md invariant 5 / AI-MATCHING.md Section 11.

    Three axes are known without a database; profile_version_id is the
    fourth and is per-row. A fifth axis must never be introduced — the
    seniority catalog shares catalog_version.
    """
    static_axes = set(versions.describe())
    assert static_axes == {"rule_version", "model_version", "catalog_version"}
    all_axes = static_axes | {"profile_version_id"}
    assert len(all_axes) == 4


@pytest.mark.parametrize(
    "message",
    [
        "postgresql+psycopg://careerlens:hunter2@postgres:5432/db",
        "Authorization: Bearer sk-abc123def456",
        'api_key="sk-live-should-not-appear"',
    ],
)
def test_secrets_are_redacted_from_logs(message: str, caplog) -> None:
    logger = logging.getLogger("redaction-test")
    logger.addFilter(RedactSecretsFilter())
    with caplog.at_level(logging.INFO):
        logger.info(message)
    emitted = caplog.text
    for secret in ("hunter2", "sk-abc123def456", "sk-live-should-not-appear"):
        assert secret not in emitted
