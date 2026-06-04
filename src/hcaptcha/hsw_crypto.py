"""Pure-Python HSW AES-256-GCM encrypt + decrypt.

Once you have the master keys (from :class:`hcaptcha.HSWKeyFetcher`) you
do NOT need to keep running the WASM bundle through ``HSWBridge`` to do
the AEAD crypto. This module is the native Python implementation of
the wire format:

* ``encrypt(plaintext, key) -> blob``  where ``blob = iv(12) || ct(N) || tag(16)``
* ``decrypt(blob, key) -> plaintext``

Both keys are 32 bytes (AES-256). Empty AAD. Random 12-byte IV per encrypt.

Tested against the bundle: blobs produced by ``hsw_encrypt(pt, encrypt_key)``
decrypt cleanly through ``window.hsw(0, blob)`` and vice-versa.
"""
from __future__ import annotations

import os
from Crypto.Cipher import AES

IV_LEN = 12
TAG_LEN = 16


def encrypt(plaintext: bytes, key: bytes, *, iv: bytes | None = None) -> bytes:
    """AES-256-GCM encrypt with HSW's wire format.

    Output layout (matches what ``window.hsw(1, plaintext)`` produces):
        iv(12) || ct(N) || tag(16)
    where N == len(plaintext).
    """
    if len(key) != 32:
        raise ValueError(f"HSW key must be 32 bytes, got {len(key)}")
    if iv is None:
        iv = os.urandom(IV_LEN)
    if len(iv) != IV_LEN:
        raise ValueError(f"IV must be {IV_LEN} bytes, got {len(iv)}")

    cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
    ct, tag = cipher.encrypt_and_digest(plaintext)
    return iv + ct + tag


def decrypt(blob: bytes, key: bytes) -> bytes:
    """AES-256-GCM decrypt of HSW wire format.

    Raises ``ValueError`` on auth failure (wrong key, corrupted blob, etc.).
    """
    if len(key) != 32:
        raise ValueError(f"HSW key must be 32 bytes, got {len(key)}")
    if len(blob) < IV_LEN + TAG_LEN:
        raise ValueError(
            f"HSW blob too short: {len(blob)} < {IV_LEN + TAG_LEN}")

    iv  = blob[:IV_LEN]
    tag = blob[-TAG_LEN:]
    ct  = blob[IV_LEN:-TAG_LEN]
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
    try:
        return cipher.decrypt_and_verify(ct, tag)
    except ValueError as e:
        raise ValueError(f"HSW decrypt failed (bad key or tampered blob): {e}")


__all__ = ["encrypt", "decrypt", "IV_LEN", "TAG_LEN"]
