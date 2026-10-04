"""Normalization is mechanical only, and deterministic."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.normalization import is_valid_representation, normalize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Kubernetes", "kubernetes"),
        ("  Kubernetes  ", "kubernetes"),
        ("KUBERNETES", "kubernetes"),
        ("Amazon Web Services", "amazon web services"),
        ("Amazon   Web\tServices", "amazon web services"),
        ("Mid-Level", "mid level"),
        ("Node.js", "node js"),
        ("CI/CD", "ci cd"),
        ("Sr.", "sr"),
        ("Jr", "jr"),
    ],
)
def test_mechanical_normalization(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


def test_normalization_does_not_decide_synonymy() -> None:
    """Normalization collapses spellings, never meanings.

    "k8s" and "kubernetes" are the same skill only because a catalogued
    alias says so — never because normalization merged them.
    """
    assert normalize("K8s") != normalize("Kubernetes")


def test_punctuation_variants_collapse_to_one_representation() -> None:
    """Why "Jr" and "Jr." cannot both be seeded as aliases."""
    assert normalize("Jr") == normalize("Jr.")


@given(st.text())
def test_normalization_is_idempotent(raw: str) -> None:
    once = normalize(raw)
    assert normalize(once) == once


@given(st.text())
def test_normalization_is_deterministic(raw: str) -> None:
    assert normalize(raw) == normalize(raw)


@pytest.mark.parametrize("raw", ["", "   ", "\t\n", "---", "..."])
def test_malformed_mentions_are_invalid(raw: str) -> None:
    """Extraction defects, not unresolved skills.

    They must never enter the catalog or the promotion-evidence pool.
    """
    assert is_valid_representation(raw) is False


@pytest.mark.parametrize("raw", ["Kubernetes", "AWS", "C++"])
def test_well_formed_mentions_are_valid(raw: str) -> None:
    assert is_valid_representation(raw) is True


def test_overlong_mentions_are_rejected() -> None:
    assert is_valid_representation("x" * 256) is False
