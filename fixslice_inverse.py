"""Python port of RustCrypto `aes-soft` fixslice32 bitslice / inv_bitslice.

The fixslice32 representation
-----------------------------
The fixslice32 implementation processes **TWO AES blocks in parallel**
to amortise the bit-permutation cost. So:

* `bitslice` takes **32 input bytes** (two 16-byte blocks A‖B) and
  produces 8 u32 words.
* `inv_bitslice` takes 8 u32 words and produces 32 output bytes (A‖B).

A round key (16 bytes) is stored bitsliced as 8 u32 words where the
round key has been duplicated to fill both halves: bitslice(K ‖ K).
Round keys are stored in linear memory as 32-byte chunks for each
round.

For AES-256:
* Master key  = 32 bytes
* Round key 0 = master[0..16]
* Round key 1 = master[16..32]
* In bitsliced storage: 8 u32 words for round key 0 (≡ bitslice(K0 ‖ K0))
                      + 8 u32 words for round key 1 (≡ bitslice(K1 ‖ K1))

So to recover the master key from the bitsliced round-key buffer:
   master[0..16]  = inv_bitslice(bs_rk0)[0..16]   (or [16..32] — same)
   master[16..32] = inv_bitslice(bs_rk1)[0..16]

The bit-permutation chain (forward = inv) is documented at:
https://github.com/RustCrypto/block-ciphers/blob/master/aes/src/soft/fixslice32.rs
"""
from __future__ import annotations
import struct
import sys
from typing import List


def _u32(x: int) -> int:
    return x & 0xffffffff


def delta_swap(a: int, b: int, shift: int, mask: int) -> tuple[int, int]:
    """delta_swap(a, b, shift, mask):
       t = (a ^ (b >> shift)) & mask
       a ^= t << shift
       b ^=  t
    Returns (a', b'). This is its own inverse — applying twice yields
    the original (a, b).
    """
    a = _u32(a); b = _u32(b)
    t = (a ^ (b >> shift)) & mask
    a ^= _u32(t << shift)
    b ^= t
    return _u32(a), _u32(b)


# ---------------------------------------------------------------------------
# Canonical fixslice32 bitslice / inv_bitslice — 32 bytes ↔ 8 u32
# ---------------------------------------------------------------------------
def bitslice(input32: bytes) -> List[int]:
    """Bitslice 32 bytes (= 2 AES blocks A‖B) into 8 u32 words.

    From rust-crypto aes-soft fixslice32.rs (`bitslice` function).
    Input byte layout: input32[0..16] is block A, input32[16..32] is
    block B. The interleaved unpacking is documented in the source.
    """
    assert len(input32) == 32
    t0 = struct.unpack_from("<I", input32,  0)[0]
    t1 = struct.unpack_from("<I", input32, 16)[0]
    t2 = struct.unpack_from("<I", input32,  4)[0]
    t3 = struct.unpack_from("<I", input32, 20)[0]
    t4 = struct.unpack_from("<I", input32,  8)[0]
    t5 = struct.unpack_from("<I", input32, 24)[0]
    t6 = struct.unpack_from("<I", input32, 12)[0]
    t7 = struct.unpack_from("<I", input32, 28)[0]

    # Forward delta-swap chain
    t1, t0 = delta_swap(t1, t0, 1, 0x55555555)
    t3, t2 = delta_swap(t3, t2, 1, 0x55555555)
    t5, t4 = delta_swap(t5, t4, 1, 0x55555555)
    t7, t6 = delta_swap(t7, t6, 1, 0x55555555)

    t2, t0 = delta_swap(t2, t0, 2, 0x33333333)
    t3, t1 = delta_swap(t3, t1, 2, 0x33333333)
    t6, t4 = delta_swap(t6, t4, 2, 0x33333333)
    t7, t5 = delta_swap(t7, t5, 2, 0x33333333)

    t4, t0 = delta_swap(t4, t0, 4, 0x0f0f0f0f)
    t5, t1 = delta_swap(t5, t1, 4, 0x0f0f0f0f)
    t6, t2 = delta_swap(t6, t2, 4, 0x0f0f0f0f)
    t7, t3 = delta_swap(t7, t3, 4, 0x0f0f0f0f)

    return [t0, t1, t2, t3, t4, t5, t6, t7]


def inv_bitslice(state: List[int]) -> bytes:
    """Inverse of `bitslice` — 8 u32 words → 32 bytes (A‖B).

    Output byte layout matches the input layout of `bitslice`:
    output[0..16] is block A, output[16..32] is block B.
    """
    assert len(state) == 8
    t0, t1, t2, t3, t4, t5, t6, t7 = (_u32(x) for x in state)

    # Inverse delta-swap chain (reverse order)
    t4, t0 = delta_swap(t4, t0, 4, 0x0f0f0f0f)
    t5, t1 = delta_swap(t5, t1, 4, 0x0f0f0f0f)
    t6, t2 = delta_swap(t6, t2, 4, 0x0f0f0f0f)
    t7, t3 = delta_swap(t7, t3, 4, 0x0f0f0f0f)

    t2, t0 = delta_swap(t2, t0, 2, 0x33333333)
    t3, t1 = delta_swap(t3, t1, 2, 0x33333333)
    t6, t4 = delta_swap(t6, t4, 2, 0x33333333)
    t7, t5 = delta_swap(t7, t5, 2, 0x33333333)

    t1, t0 = delta_swap(t1, t0, 1, 0x55555555)
    t3, t2 = delta_swap(t3, t2, 1, 0x55555555)
    t5, t4 = delta_swap(t5, t4, 1, 0x55555555)
    t7, t6 = delta_swap(t7, t6, 1, 0x55555555)

    out = bytearray(32)
    struct.pack_into("<I", out,  0, t0)
    struct.pack_into("<I", out, 16, t1)
    struct.pack_into("<I", out,  4, t2)
    struct.pack_into("<I", out, 20, t3)
    struct.pack_into("<I", out,  8, t4)
    struct.pack_into("<I", out, 24, t5)
    struct.pack_into("<I", out, 12, t6)
    struct.pack_into("<I", out, 28, t7)
    return bytes(out)


# ---------------------------------------------------------------------------
# Self-test — round-trip + replication property
# ---------------------------------------------------------------------------
def _selftest():
    import os
    # 1. Round-trip random 32 bytes
    inp = os.urandom(32)
    bs = bitslice(inp)
    out = inv_bitslice(bs)
    assert out == inp, f"round-trip fail:\n  in:  {inp.hex()}\n  out: {out.hex()}"

    # 2. Replicated input: if both blocks are the same (K ‖ K), the
    #    bitsliced state has a property and inv_bitslice gives K ‖ K.
    key = os.urandom(16)
    bs_dup = bitslice(key + key)
    out_dup = inv_bitslice(bs_dup)
    assert out_dup[:16] == out_dup[16:32] == key, "replication fail"

    print("self-test PASS: bitslice / inv_bitslice round-trip + replication")


# ---------------------------------------------------------------------------
# Brute extract HSW master key
# ---------------------------------------------------------------------------
def brute_master_key(wasm: bytes, ct: bytes, tag: bytes, iv: bytes,
                     expected_pt: bytes, aads: list = None) -> tuple[int, bytes] | None:
    """For each pair of 32-byte windows at offsets (off, off+32) in
    the WASM, treat them as bitsliced round-keys 0 and 1 of an
    AES-256 key. Apply inv_bitslice to each, take first 16 bytes
    (block A) of each as master_key[0..16] and [16..32]. Test as
    AES-256-GCM. Return (offset, key) on first match."""
    from Crypto.Cipher import AES
    aads = aads or [b""]
    n = len(wasm)
    for off in range(0, n - 64, 4):
        bs0 = list(struct.unpack_from("<8I", wasm, off))
        bs1 = list(struct.unpack_from("<8I", wasm, off + 32))
        # Fast skip: all-zero or all-0xff
        if all(w == 0 for w in bs0) or all(w == 0xffffffff for w in bs0):
            continue

        rk0 = inv_bitslice(bs0)
        rk1 = inv_bitslice(bs1)

        # If the implementation duplicates the round key for the two
        # halves, rk0[0..16] should equal rk0[16..32]. If not, this
        # window isn't a replicated round key.
        candidates = []
        if rk0[:16] == rk0[16:32]:
            # standard pattern: take first 16 bytes
            candidates.append(rk0[:16] + rk1[:16])
        # also try concatenation of both halves separately (in case
        # the two halves of a single bitsliced 32-byte chunk hold the
        # full master key directly):
        candidates.append(rk0[:32])
        candidates.append(rk0[:16] + rk1[16:32])
        candidates.append(rk0[16:32] + rk1[:16])

        for key in candidates:
            if len(key) != 32: continue
            # Quick entropy / ascii filter
            if key.count(0) > 24 or key.count(0xff) > 24: continue
            if all(0x20 <= b < 0x7f for b in key): continue
            for aad in aads:
                try:
                    c = AES.new(key, AES.MODE_GCM, nonce=iv)
                    if aad: c.update(aad)
                    if c.decrypt_and_verify(ct, tag) == expected_pt:
                        return off, key
                except Exception:
                    pass
    return None


def main():
    _selftest()
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--brute", action="store_true",
                   help="brute force inv_bitslice against current hsw WASM")
    p.add_argument("--mode", default="ct||tag||iv",
                   choices=["ct||tag||iv", "iv||ct||tag", "ct||iv||tag",
                            "tag||ct||iv", "tag||iv||ct", "iv||tag||ct"])
    args = p.parse_args()

    if not args.brute:
        return

    from keyfetcher_hsw import HSWBridge, HSWAnalyzer
    import version as _v
    version = _v.latest_version()
    info = HSWAnalyzer(version).analyze()
    wasm = bytes.fromhex(info["wasm_bytes_hex"])

    bridge = HSWBridge(version)
    pt = b"X" * 32
    blob = bridge.encrypt(pt)
    print(f"blob = {blob.hex()}")

    L = len(blob); pt_len = len(pt)
    layouts = {
        "ct||tag||iv":     (blob[:pt_len],   blob[pt_len:pt_len+16], blob[pt_len+16:pt_len+28]),
        "iv||ct||tag":     (blob[12:12+pt_len], blob[12+pt_len:12+pt_len+16], blob[:12]),
        "ct||iv||tag":     (blob[:pt_len],   blob[L-16:L], blob[pt_len:pt_len+12]),
        "tag||ct||iv":     (blob[16:16+pt_len], blob[:16], blob[16+pt_len:16+pt_len+12]),
        "tag||iv||ct":     (blob[28:28+pt_len], blob[:16], blob[16:28]),
        "iv||tag||ct":     (blob[28:28+pt_len], blob[12:28], blob[:12]),
    }
    ct, tag, iv = layouts[args.mode]
    print(f"layout {args.mode}: ct={len(ct)} tag={len(tag)} iv={len(iv)}")

    aads = [b"", version.encode(), bytes.fromhex(version)]
    print(f"brute against {len(wasm)} byte WASM × {len(aads)} AADs × 4 candidate orderings...")
    hit = brute_master_key(wasm, ct, tag, iv, pt, aads=aads)
    if hit:
        off, k = hit
        print(f"\n*** HIT @ wasm offset 0x{off:x}")
        print(f"    master key: {k.hex()}")
    else:
        print(f"no hit with layout {args.mode}")


if __name__ == "__main__":
    main()
