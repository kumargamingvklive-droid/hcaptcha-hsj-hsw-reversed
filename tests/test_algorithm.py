"""Coverage for algorithm.py (the HSJ-style AES + custom encoding).

This module is not on the main extraction path, but it implements the
legacy hsj ``ct||tag||iv||0x00`` AES-256-GCM format and a custom list
encoding. Tests document and pin its behaviour (resolves its prior
"dead code, unclear purpose" status)."""
import os
import pytest
from hcaptcha import algorithm


def _key_hex():
    return os.urandom(32).hex()


def test_hsj_encryption_roundtrip():
    enc = algorithm.HSJEncryption(_key_hex())
    msg = "the quick brown fox"
    blob = enc.encrypt(msg)
    assert enc.decrypt(blob) == msg


def test_encoding_roundtrip():
    # Encoding is a Caesar-style transform over ASCII letters only.
    e = algorithm.Encoding()
    for s in ("hello", "HelloWorld", "abcXYZ", "x" * 200):
        assert e.decode(e.encode(s)) == s


def test_hash_is_deterministic():
    h1 = algorithm.Hash("payload")
    h2 = algorithm.Hash("payload")
    assert h1.crc32() == h2.crc32()
    assert h1.xx64() == h2.xx64()


def test_hash_differs_on_input():
    assert algorithm.Hash("a").crc32() != algorithm.Hash("b").crc32()
