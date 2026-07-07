"""Pure-Python Ed25519 point-decompression / on-curve check.

Implements the STANDARD RFC 8032 §5.1.3 decompression algorithm -- the same
one underlying every Ed25519 implementation, including Solana's own
`curve25519-dalek::CompressedEdwardsY::decompress` and solders'
`Pubkey.find_program_address`. This is a well-known, publicly reviewed
algorithm (the reference `ed25519.py` attributed to D.J. Bernstein / Adam
Langley, reproduced verbatim across countless textbooks and RFC 8032 itself)
-- NOT a novel/home-grown crypto primitive, and it never touches secret key
material. It answers exactly one yes/no question -- "does this 32-byte
string decompress to a valid Ed25519 curve point?" -- which is precisely
what a Program-Derived-Address (PDA) seed search needs: a PDA is defined as
an address that is deliberately chosen to be OFF the curve (so no private
key can ever exist for it).

Used by `aats/execution/signer_enforcer.py` to re-derive the wallet's own
ATA (ADR-0015 C2 rule 2, `find_program_address([wallet, spl_token, mint],
ata_program)`) WITHOUT depending on `solders` being installed -- the signer
is the one process whose enforcement must be provable in every environment
(see solana_wire.py docstring for the same rationale applied to tx parsing).

Self-consistency is verified in tests/execution/test_ed25519_field.py against
two independently-known, universally-published Ed25519 facts (not test
vectors invented for this module): the base point B has y = 4/5 mod q and
MUST be on-curve; the identity point has y = 1, x = 0 and MUST be on-curve.
"""
from __future__ import annotations

_Q = 2**255 - 19
_D = (-121665 * pow(121666, _Q - 2, _Q)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)  # a square root of -1 mod q (q == 5 mod 8)


def _inv(x: int) -> int:
    """Modular inverse via Fermat's little theorem (q is prime)."""
    return pow(x, _Q - 2, _Q)


def _x_recover(y: int) -> int | None:
    """Recover a valid x such that (x, y) is on-curve, or None if none exists."""
    xx = ((y * y - 1) * _inv((_D * y * y + 1) % _Q)) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if (x * x - xx) % _Q != 0:
        return None
    return x


def is_on_curve(compressed: bytes) -> bool:
    """Return True if the 32-byte little-endian string is a valid Ed25519 point.

    Only the low 255 bits (the y-coordinate) participate in the check; the
    sign bit (bit 255, which normally selects which of the two x roots a
    real compressed point uses) is irrelevant to mere on/off-curve existence
    and is masked off, matching the PDA seed-search's use of this check.
    """
    if len(compressed) != 32:
        raise ValueError("compressed point must be exactly 32 bytes")
    y = int.from_bytes(compressed, "little") & ((1 << 255) - 1)
    if y >= _Q:
        return False
    return _x_recover(y) is not None
