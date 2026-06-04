"""Pure-Python hCaptcha HSW proof-of-work solver (Hashcash, SHA-1).

What HSW's PoW actually is
==========================
``window.hsw(req_jwt)`` runs a Hashcash proof-of-work in WASM. The
algorithm is :mod:`rust-hashcash` 0.3.3 compiled with the ``sha1``
feature flag — we confirmed this empirically:

1. The SHA-1 round constants ``0x5A827999`` and ``0x6ED9EBA1`` appear
   as ``i32.const`` literals in the WASM code section (search for
   ``encode_sleb(0x5A827999)`` patterns; six occurrences each in our
   builds — once per SHA-1 round).
2. The Implex-ltd reverse-engineering project's ``readme.md`` lists
   ``rust-hashcash/0.3.3`` in the crate inventory under "Stamp (proof
   of work)".
3. xxHash3 primes — which a community blog post incorrectly named as
   the PoW hash — are NOT present anywhere in the WASM. (They ARE
   used by hCaptcha for the fingerprint hash, not the PoW.)

Stamp format
============
Standard Hashcash v1::

    1:bits:date:resource:ext:rand:counter

* ``bits``     — leading-zero-bit difficulty, from JWT payload's ``d`` field
* ``date``     — ``YYYYMMDD`` (rust-hashcash's default; the
                 "2006-01-02" reference in hCaptcha's docs is Go's
                 strftime sample, NOT the literal output format)
* ``resource`` — varies per build; usually the JWT payload as a JSON
                 string. See :func:`solve` for how to override.
* ``ext``      — empty string (unused by hCaptcha)
* ``rand``     — 8 alphanumeric characters (the "salt")
* ``counter``  — the searched nonce, incremented from 0

The stamp is valid when SHA-1(stamp).hex() starts with ``ceil(bits/4)``
zero hex digits (rust-hashcash's check; an exact-bit check is also
implemented for bits not divisible by 4).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import secrets
import string
import time
from typing import Any


_ALPHANUM = string.ascii_letters + string.digits


def _format_date(ts: int | float, *, with_seconds: bool = False) -> str:
    """rust-hashcash's date format (UTC).

    Default mode is ``%Y%m%d`` (no separators). hCaptcha uses this
    no-seconds form.
    """
    dt = datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc)
    return dt.strftime("%Y%m%d%H%M%S" if with_seconds else "%Y%m%d")


def _gen_rand(n: int = 8) -> str:
    """8 alphanumeric chars, matching rust-hashcash's default saltchars."""
    return "".join(secrets.choice(_ALPHANUM) for _ in range(n))


def _has_leading_zero_bits(digest: bytes, bits: int) -> bool:
    """Check whether ``digest`` starts with at least ``bits`` zero bits.

    This is the *exact* check (not just the hex-prefix approximation
    rust-hashcash uses for bit counts that are multiples of 4 — we do
    both because hCaptcha difficulties are typically multiples of 4 but
    we want to be precise).
    """
    full_bytes, leftover = divmod(bits, 8)
    if any(b != 0 for b in digest[:full_bytes]):
        return False
    if leftover and (digest[full_bytes] >> (8 - leftover)) != 0:
        return False
    return True


def _build_stamp(bits: int, date: str, resource: str,
                 ext: str, rand: str, counter: int) -> str:
    return f"1:{bits}:{date}:{resource}:{ext}:{rand}:{counter}"


def mint(resource: str, bits: int, *,
         ts: int | float | None = None,
         ext: str = "",
         rand: str | None = None,
         with_seconds: bool = False,
         max_counter: int = 1 << 40) -> dict[str, Any]:
    """Mint a Hashcash stamp.

    Args:
        resource:     The challenge resource. For hCaptcha this is
                      typically the JWT payload as a JSON string.
        bits:         Leading-zero-bit difficulty.
        ts:           Unix seconds; defaults to ``time.time()``.
        ext:          Extension string. Empty for hCaptcha.
        rand:         8-char alphanumeric salt; auto-generated if None.
        with_seconds: Use ``YYYYMMDDhhmmss`` instead of ``YYYYMMDD``.
        max_counter:  Search ceiling. Default 2^40 (a trillion); a
                      bits=20 challenge typically lands in <2 million.

    Returns:
        ``{"stamp": "1:bits:date:...", "rand": rand, "counter": counter,
           "digest": hex_sha1, "iterations": int}``.
    """
    if ts is None:
        ts = time.time()
    if rand is None:
        rand = _gen_rand()
    date = _format_date(ts, with_seconds=with_seconds)

    prefix = f"1:{bits}:{date}:{resource}:{ext}:{rand}:"
    prefix_b = prefix.encode("utf-8")

    counter = 0
    while counter < max_counter:
        candidate = prefix_b + str(counter).encode()
        digest = hashlib.sha1(candidate).digest()
        if _has_leading_zero_bits(digest, bits):
            return {
                "stamp":      candidate.decode("utf-8"),
                "rand":       rand,
                "counter":    counter,
                "digest":     digest.hex(),
                "iterations": counter + 1,
            }
        counter += 1
    raise RuntimeError(
        f"Hashcash search exceeded max_counter={max_counter:_} without "
        f"finding bits={bits} solution")


def check(stamp: str, *, expected_resource: str | None = None,
          expected_bits: int | None = None) -> dict[str, Any]:
    """Validate a Hashcash stamp.

    Args:
        stamp:             The colon-separated stamp string.
        expected_resource: If given, also assert the stamp's resource
                           field matches.
        expected_bits:     If given, also assert the stamp claims at
                           least this difficulty.

    Returns ``{"valid": bool, "bits": int, "date": str, ...}``.
    Raises ``ValueError`` if the stamp is malformed.
    """
    parts = stamp.split(":")
    if len(parts) != 7:
        raise ValueError(f"hashcash stamp must have 7 fields, got {len(parts)}")
    version, bits_s, date, resource, ext, rand, counter = parts
    if version != "1":
        raise ValueError(f"hashcash version must be 1, got {version!r}")
    bits = int(bits_s)
    digest = hashlib.sha1(stamp.encode("utf-8")).digest()
    valid = _has_leading_zero_bits(digest, bits)
    if expected_resource is not None and resource != expected_resource:
        valid = False
    if expected_bits is not None and bits < expected_bits:
        valid = False
    return {
        "valid":    valid,
        "version":  version,
        "bits":     bits,
        "date":     date,
        "resource": resource,
        "ext":      ext,
        "rand":     rand,
        "counter":  int(counter),
        "digest":   digest.hex(),
    }


def solve_jwt(req_jwt: str, *, ts: int | float | None = None,
              resource_field: str = "payload_json") -> dict[str, Any]:
    """Solve the PoW for an hCaptcha ``req`` JWT.

    Args:
        req_jwt: The JWT string from ``window.hcaptcha`` config /
                 ``getcaptcha`` response.
        ts:      Override timestamp (defaults to now).
        resource_field:
            How to derive the Hashcash ``resource`` from the JWT.
            One of:

            * ``"payload_json"`` — the JWT payload re-serialized as JSON
              (matches what the WASM receives — ``JSON.stringify(payload)``).
              This is the default and what the WASM does.
            * ``"s"`` — just the JWT payload's ``s`` field.
            * ``"full"`` — the full JWT string ``h.p.s``.

    Returns the :func:`mint` dict plus ``"jwt_payload"``.
    """
    import base64
    parts = req_jwt.split(".")
    if len(parts) != 3:
        raise ValueError(f"JWT must have 3 dot-separated parts, got {len(parts)}")

    def _b64u(s):
        s = s + "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(s.replace("_", "/").replace("-", "+"))

    payload = json.loads(_b64u(parts[1]))
    bits = int(payload.get("d", 0))
    if bits <= 0:
        raise ValueError(f"JWT has no positive difficulty (d={payload.get('d')!r})")

    if resource_field == "payload_json":
        # JSON.stringify with no separators argument uses {", "} in browsers;
        # but the WASM receives exactly what JS produced, which we can't fully
        # replicate without knowing the JS engine. Compact form is the default.
        resource = json.dumps(payload, separators=(",", ":"))
    elif resource_field == "s":
        resource = str(payload.get("s", ""))
    elif resource_field == "full":
        resource = req_jwt
    else:
        raise ValueError(f"unknown resource_field={resource_field!r}")

    out = mint(resource=resource, bits=bits, ts=ts)
    out["jwt_payload"] = payload
    out["resource_used"] = resource[:60] + "..." if len(resource) > 60 else resource
    return out


__all__ = ["mint", "check", "solve_jwt"]
