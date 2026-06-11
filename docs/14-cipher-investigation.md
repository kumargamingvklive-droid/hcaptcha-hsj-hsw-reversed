# N-Token Cipher Investigation

This document captures the deep investigation into hCaptcha's n-token cipher
mode. Status: **cipher mode NOT identified** despite exhaustive testing.

## Build context

Snapshot taken from build `145586e3b50d3491...`. WASM size 607,461 bytes,
sha256 `27776a42a04d4bf3...`.

## Confirmed primitives in `hsw.wasm`

| fn  | Body  | Role                                | Evidence                                    |
| --- | ----- | ----------------------------------- | ------------------------------------------- |
| 268 | 4508B | **SHA-1** compression               | K0=0x5A827999, K1=0x6ED9EBA1, ~K2, ~K3      |
| 276 | 10030B| **ChaCha20** core                   | sigma "expand 32-byte k" (0x61707865 etc.)  |
| 367 | 3144B | **AES-256-fixslice32 block encrypt**| Fixslice masks + Rcon (0x01, 0x02, ... 0x36)|
| 388 | 4125B | **AES-256 key schedule**            | Used only by `vc` dispatcher                |
| 391 | 2865B | **AES round operation** (fixslice)  | masks 0x0f0f0f0f / 0x33333333 / 0x55555555  |

## Live n-token call-pattern (instrumented during `window.hsw(jwt)`)

| fn  | Direct calls in fn 288 | Live invocations | Interpretation                          |
| --- | ---------------------- | ---------------- | --------------------------------------- |
| 367 | 3                      | **236**          | 235 block encrypts + 1 setup (CTR-shape) |
| 391 | 1                      | **121**          | 120 something + 1 finalize              |
| 268 | 5                      | **256**          | Hashcash 8-bit PoW (2^8 trials)         |
| 276 | 4                      | **0**            | DEAD path — ChaCha20 not used for n-token|
| 174 | 2                      | 2                | Orchestrator (contains dead ChaCha20 call)|
| 330 | 5                      | 1                | PoW finalization (only 1 captured)      |

## Captured keys

| Ring        | Records | Value (first 16 bytes)        | Interpretation                                  |
| ----------- | ------- | ----------------------------- | ----------------------------------------------- |
| `f367_a0`   | 236     | `47e574fbd8468a8f...`         | bs(rk0‖rk1) of AES-256 master `1bf04f88...`     |
| `f367_a1`   | 236     | `36e291bd2daa4be0...` (235×)  | Struct snippet (last 8B of `47e574fb...` + tail) |
| `f391_a0`   | 121     | `dfb40cd8b081c6e5...` (120×)  | Round-key window (offset into table)            |
| `f391_a1`   | 121     | `f67a575b1ef0b394...` (62×)   | Sliding window into KS buffer                   |
| `f268_a0`   | 256     | `44d665da42d8cd93...`         | SHA-1 input (Hashcash counter buffer)           |
| `f268_a1`   | 256     | `34d18c10911442af...`         | SHA-1 message offset (= KEY_B bytes 20..52)     |
| `f330_a0`   | 1       | `44d665da42d8cd93...`         | Same as f268_a0                                 |

## Master key recovery

`inv_bitslice(f367_a0[:32])` byte-perfect match to bs(rk0‖rk1) of
**KEY_A** = `1bf04f88ca73b3486d0d4e0886336c35565f9907bb249ce2fab518fb296c8560`.

**KEY_B** = `44d665da42d8cd9339007bdacde4b0a125fd10eb34d18c10911442afb24fcc0c`
appears in the SHA-1 ring `f268_a0` and the helper `f330_a0`.

## Critical finding: keys are HARDCODED in the WASM data section

Both `47e574fb...` and `44d665da...` appear as **literal byte sequences** in
the WASM at offsets 545,747 and 545,795 respectively. The values are NOT
runtime-derived — they live inside the WASM blob:

```
545731:  2f11ae870cf4c344 29f1126543d421a5 ← header bytes
545747:  47e574fbd8468a8f 912a70c59f4cd860 ← bs(rk0‖rk1) of KEY_A
545763:  f90d886fa3b481d1 36e291bd2daa4be0
545779:  291dab008e19efc8 f67a575b1ef0b394
545795:  44d665da42d8cd93 39007bdacde4b0a1 ← KEY_B
545811:  25fd10eb34d18c10 911442afb24fcc0c
545827:  dfb40cd8b081c6e5 aa187df1a4e05a1c
```

The 96-byte pattern `47e574fb...aa187df1a4e05a1c` REPEATS 3× consecutively
starting at offset 545,899 (preceded by a 8-byte `02 01 00 00 00 00 00 00`
record header). The `02 01 00 00 00 00 00 00` pattern appears **162 times**
throughout the WASM at ≈ 337-byte intervals, suggesting a structured table
of precomputed records.

## Wire format (confirmed by math)

```
raw_blob = ct(N) ‖ tag(16) ‖ iv(12) ‖ ver(1)
   where N = total_bytes - 29
```

Build `145586e3b50d3491` live sample:
- total bytes (base64-decoded) = 3,788
- ct = 3,759 bytes
- tag = `457a0030a0ebdac92f5c7794c202b78d`
- iv = `ea00996085a7783e943a9736`
- ver = `0x02`

## Ciphers ruled out (with KEY_A and KEY_B, all wire layouts tested)

All tested with both keys, all 24 permutations of (ct, tag, iv, ver), and
multiple AAD candidates (empty, ver, iv, ver+iv):

- AES-256-GCM
- AES-256-CCM
- AES-256-OCB3
- AES-256-EAX
- AES-256-SIV (32-byte and 64-byte key variants)
- AES-256-GCM-SIV (where supported by pycryptodome)
- AES-128 variants with K1/K2 halves of each candidate
- AES-256-CFB, OFB, CBC
- AES-CTR with multiple counter encodings (LE/BE u32/u64, prefix/initial-value)
- ChaCha20-Poly1305 with both keys
- ChaCha20 stream + KDF-derived keys (SHA-256, SHA-512, HKDF-style)
- AES-CTR + HMAC-SHA1 (truncated to 16 bytes); AES-CTR + HMAC-SHA256
- AES-CMAC over (ct, iv+ct, ver+iv+ct, etc.)
- Sliding-window brute over all 32-byte windows in WASM (20,000+ tested)
- Per-record key extraction from the 162 `02 01 00 ...` table entries

## Negative findings

- Poly1305 R-clamp **0x0ffffffc** absent from WASM (canonical Poly1305 not present)
- BUT 0x0fffffff and 0x03ffffff ARE present — could indicate variant impl
- ChaCha20 sigma constants present but function called 0× during n-token
- No GHASH polynomial 0xE100000000000000 in WASM (GCM ruled out)

## Open question

How is the live n-token (3,788 bytes) actually encrypted? Despite:
- Master key bit-perfect verified via inv_bitslice
- Wire format byte-accurate
- All standard AEAD modes exhausted

The cipher operation remains opaque. The next-step hypothesis: instrument
the **plaintext input** by patching fn 367's *prologue to dump the actual
16-byte block being encrypted via call_indirect or by reading the
memory region pointed to by arg1's struct at a known internal offset*.
The "static arg1" we observe is a struct pointer; the actual block input
must be at some offset inside that struct.

Alternative: **deobfuscate the data flow** through the obfuscated memory
helpers fn 376/584 (which apply XOR-with-rolling-key on per-page basis)
to determine where in linear memory the plaintext sits before being
consumed.

## Build rotation observation

During this investigation, the WASM build rotated from
`145586e3b50d3491...` to `587df480b9aaa94c...` mid-analysis. New build
exports have completely different signatures:

| Export | Old build (`145586e3...`) | New build (`587df480...`) |
| ------ | -------------------------- | -------------------------- |
| `ec`   | fn 286, 6-arg `(i32×6)→i32`| fn 446, 2-arg `(i32,i32)→()`|
| `pc`   | fn 293, 1-arg `(i32)→()`    | fn 292, 3-arg `(i32×3)→i32` |
| `vc`   | fn 594, 9-arg              | fn 593, 9-arg               |

This means our function-index identification needs to be re-run per build
rotation. WASM sha256 also rotated: `27776a42a04d4bf3` → `c080d05b53f52c57`,
total bytes 607,461 → 628,977.

## Plaintext capture attempt

A targeted instrumentation of fn 286 (the OLD ec) on the new build (where
fn 286 ISN'T ec) captured a 128-byte cycling pattern at arg0:
`02debed61b62b2f80c71e309a4b4627f6b4438b5cc0b2592dcd23268de19c828355227188783c9cc10b38d75b9ffcdfc6f5fe7ae911af31e08f03d5699e95d1462be38205b7a7bf60ac5665742632b41d4c91bc52d6e2f23d25c51e324cd11df`

And at fn 367 arg1 (32→64 byte expanded dump), 64 bytes starting with:
`120000000501040b090e0d0c0f0a0207060003085476d02fe95dde0f887f5eb0f`

The `0501040b090e0d0c0f0a0207060003 08` prefix is a PERMUTATION OF 0..15
(no repeats), consistent with a fixslice32 ShiftRows byte-permutation table
or similar AES internal state arrangement. The 18 (=0x12) prefix is a Rust
`Vec<u8>` length or struct discriminant.

## Status

Cipher mode remains opaque after extensive end-to-end investigation. The
KEY itself is recovered with bit-perfect certainty (verified via fixslice
inv_bitslice round-trip), but no standard AEAD construction reproduces the
n-token under the recovered key + observed wire format + any known counter
format. Recovery requires either (a) custom cipher mode identification from
deeper inside-fn 367 disassembly, or (b) a Rust-source reproduction matching
the wbg-bindgen Promise state machine.
