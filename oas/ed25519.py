"""Pure-Python Ed25519 (RFC 8032), stdlib only, so signing needs no third-party dependency.

This is a straightforward reference-style implementation. It is NOT constant-time and is slow compared with
native libraries (see docs/STATUS.md for measured numbers). It is verified against the RFC 8032 test vectors and
cross-checked against the `cryptography` package in the test suite. If you need hardened signing, replace the
three functions below (`public_key`, `sign`, `verify`) with a native implementation; the rest of the framework only
uses those.
"""
from __future__ import annotations

import hashlib

P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
D = (-121665 * pow(121666, P - 2, P)) % P
I = pow(2, (P - 1) // 4, P)  # sqrt(-1)


def _h(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()


def _inv(x: int) -> int:
    return pow(x, P - 2, P)


def _recover_x(y: int, sign: int) -> int | None:
    if y >= P:
        return None
    x2 = (y * y - 1) * _inv(D * y * y + 1) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * I % P
    if (x * x - x2) % P != 0:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


_GY = 4 * _inv(5) % P
_GX = _recover_x(_GY, 0)
G = (_GX, _GY, 1, _GX * _GY % P)          # extended coordinates (X, Y, Z, T)
_IDENT = (0, 1, 1, 0)


def _add(a, b):
    A = (a[1] - a[0]) * (b[1] - b[0]) % P
    B = (a[1] + a[0]) * (b[1] + b[0]) % P
    C = 2 * a[3] * b[3] * D % P
    Dd = 2 * a[2] * b[2] % P
    E, F, Gg, H = B - A, Dd - C, Dd + C, B + A
    return (E * F % P, Gg * H % P, F * Gg % P, E * H % P)


def _mul(s: int, pt):
    q = _IDENT
    while s > 0:
        if s & 1:
            q = _add(q, pt)
        pt = _add(pt, pt)
        s >>= 1
    return q


def _eq(a, b) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % P == 0 and (a[1] * b[2] - b[1] * a[2]) % P == 0


def _compress(pt) -> bytes:
    zi = _inv(pt[2])
    x, y = pt[0] * zi % P, pt[1] * zi % P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % P)


def _expand(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    h = _h(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed: bytes) -> bytes:
    a, _ = _expand(seed)
    return _compress(_mul(a, G))


def sign(seed: bytes, message: bytes) -> bytes:
    a, prefix = _expand(seed)
    pub = _compress(_mul(a, G))
    r = int.from_bytes(_h(prefix + message), "little") % L
    R = _compress(_mul(r, G))
    k = int.from_bytes(_h(R + pub + message), "little") % L
    s = (r + k * a) % L
    return R + int.to_bytes(s, 32, "little")


def is_small_order(pub: bytes) -> bool:
    """True for public keys of order dividing 8 (e.g. the identity), or that are not valid points."""
    A = _decompress(pub)
    return A is None or _eq(_mul(8, A), _IDENT)


def verify(pub: bytes, message: bytes, signature: bytes) -> bool:
    try:
        if len(pub) != 32 or len(signature) != 64:
            return False
        A = _decompress(pub)
        R = _decompress(signature[:32])
        if A is None or R is None:
            return False
        if _eq(_mul(8, A), _IDENT):      # stricter than RFC 8032: small-order keys admit universal forgeries
            return False
        s = int.from_bytes(signature[32:], "little")
        if s >= L:
            return False
        k = int.from_bytes(_h(signature[:32] + pub + message), "little") % L
        return _eq(_mul(s, G), _add(R, _mul(k, A)))
    except Exception:  # noqa: BLE001 - any malformed input is simply "not valid"
        return False
