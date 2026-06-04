"""Unified pure-Python HSW client — encrypt, decrypt, PoW solve.

After ``HSWKeyFetcher`` has produced the master keys, this class lets
you do everything ``window.hsw`` does **without running the WASM**:

* ``encrypt(plaintext)``  — AES-256-GCM with the recovered encrypt key
* ``decrypt(blob)``       — AES-256-GCM with the recovered decrypt key
* ``solve(req_jwt)``      — Hashcash + SHA-1 proof-of-work

Construct from a fetched key bundle::

    from hcaptcha import KeyFetcher, HSW
    keys = KeyFetcher().fetch()
    hsw  = HSW.from_keys(keys)

    blob = hsw.encrypt(b"some payload")          # iv || ct || tag
    pt   = hsw.decrypt(server_response_bytes)
    stamp = hsw.solve(req_jwt)["stamp"]          # 1:bits:date:res:..:nonce
"""
from __future__ import annotations

from typing import Any

from . import hsw_crypto, hsw_pow


class HSW:
    """Pure-Python equivalent of ``window.hsw`` — no WASM dependency."""

    def __init__(self, *, encrypt_key: bytes, decrypt_key: bytes):
        if len(encrypt_key) != 32:
            raise ValueError(f"encrypt_key must be 32 bytes, got {len(encrypt_key)}")
        if len(decrypt_key) != 32:
            raise ValueError(f"decrypt_key must be 32 bytes, got {len(decrypt_key)}")
        self._enc = encrypt_key
        self._dec = decrypt_key

    @classmethod
    def from_keys(cls, keys: dict) -> "HSW":
        """Construct from a ``KeyFetcher().fetch()`` result."""
        h = keys.get("hsw", keys)
        return cls(
            encrypt_key=bytes.fromhex(h["encrypt_key"]),
            decrypt_key=bytes.fromhex(h["decrypt_key"]),
        )

    # ---- AEAD --------------------------------------------------------

    def encrypt(self, plaintext: bytes, *, iv: bytes | None = None) -> bytes:
        """AES-256-GCM encrypt -> ``iv(12) || ct(N) || tag(16)``.

        Equivalent to ``window.hsw(1, plaintext)``.
        """
        return hsw_crypto.encrypt(plaintext, self._enc, iv=iv)

    def decrypt(self, blob: bytes) -> bytes:
        """AES-256-GCM decrypt of HSW wire format.

        Equivalent to ``window.hsw(0, blob)``.
        """
        return hsw_crypto.decrypt(blob, self._dec)

    # ---- PoW ---------------------------------------------------------

    def solve(self, req_jwt: str, *, ts: float | None = None,
              resource_field: str = "payload_json") -> dict[str, Any]:
        """Solve the Hashcash PoW for an hCaptcha ``req`` JWT.

        Equivalent to ``window.hsw(req_jwt)``. See
        :func:`hsw_pow.solve_jwt` for ``resource_field`` semantics.
        """
        return hsw_pow.solve_jwt(req_jwt, ts=ts, resource_field=resource_field)


__all__ = ["HSW"]
