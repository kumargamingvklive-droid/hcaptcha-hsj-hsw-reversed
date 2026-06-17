"""AES-256-GCM wire-format crypto (hsw_crypto): round-trips + error paths."""
import os
import pytest
from hcaptcha import hsw_crypto


def test_roundtrip_random():
    key = os.urandom(32)
    for n in (0, 1, 15, 16, 17, 1000, 3072):
        pt = os.urandom(n)
        blob = hsw_crypto.encrypt(pt, key)
        # wire layout: iv(12) || ct(N) || tag(16)
        assert len(blob) == 12 + n + 16
        assert hsw_crypto.decrypt(blob, key) == pt


def test_deterministic_iv():
    key = os.urandom(32)
    iv = os.urandom(12)
    pt = b"hello hcaptcha"
    blob = hsw_crypto.encrypt(pt, key, iv=iv)
    assert blob[:12] == iv
    assert hsw_crypto.decrypt(blob, key) == pt


def test_wrong_key_fails():
    key = os.urandom(32)
    blob = hsw_crypto.encrypt(b"secret", key)
    with pytest.raises(ValueError):
        hsw_crypto.decrypt(blob, os.urandom(32))


def test_tamper_fails():
    key = os.urandom(32)
    blob = bytearray(hsw_crypto.encrypt(b"secret payload", key))
    blob[20] ^= 0x01
    with pytest.raises(ValueError):
        hsw_crypto.decrypt(bytes(blob), key)


@pytest.mark.parametrize("badlen", [16, 24, 31, 33])
def test_bad_key_length(badlen):
    with pytest.raises(ValueError):
        hsw_crypto.encrypt(b"x", os.urandom(badlen))


def test_blob_too_short():
    with pytest.raises(ValueError):
        hsw_crypto.decrypt(b"short", os.urandom(32))
