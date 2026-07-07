"""Self-consistency tests for the pure-Python Ed25519 on-curve check.

No network / no external test vectors available in this sandbox -- these
tests validate the implementation against facts that are true BY
CONSTRUCTION of the Ed25519 curve (not invented for this module): the base
point B (y = 4/5 mod q) and the identity point (y = 1, x = 0) MUST be
on-curve. See aats/execution/ed25519_field.py module docstring.
"""
from __future__ import annotations

from aats.execution import ed25519_field as ef


def test_base_point_is_on_curve() -> None:
    """B = (x, 4/5 mod q) is the universally-published Ed25519 base point."""
    g_y = (4 * pow(5, ef._Q - 2, ef._Q)) % ef._Q
    compressed = g_y.to_bytes(32, "little")
    assert ef.is_on_curve(compressed) is True


def test_identity_point_is_on_curve() -> None:
    """The identity element has y = 1, x = 0: -0^2 + 1^2 = 1 + d*0^2*1^2 -> 1 = 1."""
    compressed = (1).to_bytes(32, "little")
    assert ef.is_on_curve(compressed) is True


def test_rejects_wrong_length() -> None:
    import pytest

    with pytest.raises(ValueError):
        ef.is_on_curve(b"\x00" * 31)


def test_y_greater_or_equal_q_is_off_curve() -> None:
    # y == q is not a canonically-reduced field element -- must be treated as invalid.
    compressed = ef._Q.to_bytes(32, "little")
    assert ef.is_on_curve(compressed) is False


def test_roughly_half_of_sampled_points_are_on_curve() -> None:
    """Sanity check that is_on_curve is not trivially constant: for a large sample of
    y-coordinates, the on-curve fraction should sit near the theoretical ~50%
    (quadratic residues are exactly half of the nonzero field elements)."""
    import random

    rng = random.Random(1234)
    on_curve_count = 0
    n = 200
    for _ in range(n):
        y = rng.randrange(0, ef._Q)
        if ef.is_on_curve(y.to_bytes(32, "little")):
            on_curve_count += 1
    fraction = on_curve_count / n
    assert 0.3 < fraction < 0.7, f"on-curve fraction {fraction} is implausible for a working check"


def test_deterministic_same_input_same_output() -> None:
    compressed = (12345).to_bytes(32, "little")
    results = {ef.is_on_curve(compressed) for _ in range(5)}
    assert len(results) == 1
