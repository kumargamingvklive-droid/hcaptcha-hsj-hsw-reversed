"""n-token AES-256-CTR cipher (the solved cipher, docs/19): keystream,
wire splitting, decrypt round-trip, and the plaintext-shape validator."""
import base64
import os
import pytest
from Crypto.Cipher import AES
from hcaptcha import hsw_n_token_decrypt as ntd


def _make_token(pt, key, iv, ver=0x02, start=0):
    ks = ntd.ctr_keystream(key, iv, (len(pt) + 15) // 16, start=start)
    ct = bytes(a ^ b for a, b in zip(pt, ks))
    wire = ct + os.urandom(16) + iv + bytes([ver])     # ct||tag||iv||ver
    return base64.b64encode(wire).decode()


def test_ctr_keystream_matches_manual():
    key, iv = os.urandom(32), os.urandom(12)
    ks = ntd.ctr_keystream(key, iv, 3)
    ecb = AES.new(key, AES.MODE_ECB)
    expect = b"".join(ecb.encrypt(iv + i.to_bytes(4, "big")) for i in range(3))
    assert ks == expect


def test_split_wire():
    raw = b"C" * 100 + b"T" * 16 + b"I" * 12 + b"\x02"
    ct, tag, iv = ntd._split_wire(raw)
    assert ct == b"C" * 100 and tag == b"T" * 16 and iv == b"I" * 12


def test_split_wire_too_short():
    assert ntd._split_wire(b"\x00" * 10) is None


def test_decrypt_roundtrip():
    key, iv = os.urandom(32), os.urandom(12)
    pt = os.urandom(3072)
    tok = _make_token(pt, key, iv)
    assert ntd.decrypt_n_token_ctr(tok, key) == pt


def test_decrypt_counter_start():
    key, iv = os.urandom(32), os.urandom(12)
    pt = os.urandom(512)
    tok = _make_token(pt, key, iv, start=1)
    assert ntd.decrypt_n_token_ctr(tok, key, counter_start=1) == pt


def test_plaintext_validator_accepts_record_table():
    # 328-byte records each headed by u64=1, like the real n-token plaintext
    rec = b"\x01\x00\x00\x00\x00\x00\x00\x00" + os.urandom(320)
    pt = rec * 6
    assert ntd._looks_like_ntoken_plaintext(pt) is True


def test_plaintext_validator_rejects_noise():
    assert ntd._looks_like_ntoken_plaintext(os.urandom(3000)) is False
    assert ntd._looks_like_ntoken_plaintext(b"") is False


def test_static_decrypt_finds_ctr_key():
    key, iv = os.urandom(32), os.urandom(12)
    rec = b"\x01\x00\x00\x00\x00\x00\x00\x00" + os.urandom(320)
    pt = rec * 6
    tok = _make_token(pt, key, iv)
    raw = base64.b64decode(tok)
    res = ntd._static_decrypt(raw, [os.urandom(32), key])  # decoy + real
    assert res is not None and res.plaintext == pt and res.wire_format.startswith("ctr/")


def test_roundkey_inversion_recovers_master():
    """Core of recover_ntoken_master_live: given the bitsliced fixslice
    round-key array the n-token cipher reads (rk0..rk14), invert rk0 and rk1
    back to the 32-byte AES-256 master. This is the exact math the live
    recovery runs on the deobf-captured round keys — validated here offline
    against the canonical fixslice schedule for random keys."""
    from hcaptcha.tools import fixslice as fs
    for _ in range(8):
        master = os.urandom(32)
        rk = fs.aes256_key_schedule(master)          # 120 words, as the WASM stores
        # invert exactly as recover_ntoken_master_live does
        a, _ = fs.inv_bitslice(list(rk[0:8]))        # rk0 is pure -> M[:16]
        ch = list(rk[8:16])
        fs.sub_bytes_nots(ch); fs.shift_rows_1(ch)   # undo rk1's transforms
        cc, _ = fs.inv_bitslice(ch)                  # -> M[16:]
        recovered = a + cc
        assert recovered == master
        # self-verification gate the live recovery uses (120/120 schedule match)
        assert fs.aes256_key_schedule(recovered) == rk
