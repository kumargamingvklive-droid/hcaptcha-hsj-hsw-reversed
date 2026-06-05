# HSW — Rust crate inventory

This document enumerates every Rust crate identifiable inside the current
hCaptcha `hsw.js` WebAssembly module (`data/hsw_cached.wasm`,
sha256 `750faf0da6a9e64a…`, 581 769 B, 430 local functions, 164 imports,
20 exports, 186 data segments / 61 045 B rodata).

## How this list was generated

The build is **fully stripped** of identifying metadata:

  - No `.debug_info` or DWARF custom sections.
  - No Rust panic strings, no `src/…/lib.rs` source paths, no `cargo`
    fragments — `grep -aE 'rust|panic|src/|cargo|\.rs'` on the WASM
    returns **zero** matches.
  - No WASM `name` custom section (zero custom sections of any kind).
  - All function exports are 2-letter rotating handles (`cc`, `dc`,
    `ec`, …, `vc`). The 164 wbg imports are 1-letter handles
    (`a.a`, `a.b`, … `a.A`, `a.B`, …) under a single module `"a"` —
    the canonical heavily-minified wasm-bindgen shim shape.
  - Every internal Rust string survives only as 32-byte XOR-encrypted
    deobfuscator blobs (see `hsw_deobf.blobs.bin`).

Crate identification therefore relies on three orthogonal signals:

  1. **Magic-constant fingerprints** in the code section's
     `i32.const` / `i64.const` literals. Algorithm-specific constants
     (XxHash3 PRIME64 quadruple, ChaCha20 sigma words, SHA-1 K-rounds,
     fixslice32 bit-permutation masks) are extracted by parsing every
     function body with `src/hcaptcha/tools/wasm_disasm.py` and
     bucketing constant operands per function.
  2. **Opcode-histogram fingerprints** — the i64.mul/i64.rotl/i64.xor
     dominance of XxHash3, the i32.xor/i32.add/i32.rotl shape of
     SHA-1, the fixslice32 XOR+AND+ROL signature of bit-sliced AES.
  3. **Cross-reference against Implex's 2023 RE of hCaptcha 1.40.x**,
     which published the exact crate-version graph for an earlier
     build. Where our constant evidence matches the same algorithm
     family, we inherit the precise minor-version from Implex unless
     the constants disagree.

A reproducer for the scan is in [How to verify](#how-to-verify).

## Confirmed crates (high confidence)

These crates have both a unique constant fingerprint AND an
opcode-shape match against the reference algorithm.

| Crate            | Version             | Purpose                                                                     | Evidence                                                                                                                                                                                                                                                                                                                          | crates.io                                          |
| ---------------- | ------------------- | --------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| `twox-hash`      | 1.6.x (~ 1.6.0–1.6.3) | XxHash3 64-bit hash; fingerprints browser env props with seed 5575352424011909552 | All four PRIME64 i64 constants present, concentrated in one mixer function: PRIME64_1 `0x9E3779B185EBCA87` ×24, PRIME64_2 `0xC2B2AE3D27D4EB4F` ×17, PRIME64_3 `0x165667B19E3779F9` ×4, PRIME64_5 `0x27D4EB2F165667C5` ×4. Concentrated in fn 232 (16 / 9 / 4 / 4) with a small tail in fn 234. Op histogram of fn 232: i64.mul + i64.rotl + i64.xor dominance — textbook XxHash3 mixer. | https://crates.io/crates/twox-hash                 |
| `aes`            | 0.7.x (likely 0.7.5)  | AES block cipher in the fixslice32 bit-sliced software backend (no AES-NI in WASM) | Complete fixslice32 mask set as `i32.const` literals across the AES cluster (fns 388, 425, 471, 477, 520, 548, etc.): `0x55555555` ×78, `0x33333333` ×92, `0x0F0F0F0F` ×124, `0xF0F0F0F0` (24 plus combined sources), `0x03030303` ×24, `0xFCFCFCFC` ×24, `0xC0C0C0C0` ×24, plus the unique fixslice ShiftRows helpers `0x33003300` ×48, `0x030F0C00` ×32, `0x0F000F00` ×32, `0x0C0F0300` ×16. No forward or inverse AES SBOX table appears in the data section, ruling out the lookup-table backend. | https://crates.io/crates/aes                       |
| `rand_chacha`    | 0.2.x (likely 0.2.2)  | ChaCha20 CSPRNG; seeds AES key/IV streams and n-token randomness            | All four ChaCha20 "expand 32-byte k" sigma words present 16× each in fn 327: `0x61707865` ('expa'), `0x3320646E` ('nd 3'), `0x79622D32` ('2-by'), `0x6B206574` ('te k'). Op histogram of fn 327 is dominated by i64.shl / i64.or / i64.xor / i32.rotl — the classic ChaCha quarter-round.                                                                                            | https://crates.io/crates/rand_chacha               |
| `sha-1`          | ~0.10               | SHA-1 hashing; the inner hash used by the Hashcash PoW solver               | `i32.const 0x5A827999` (K0) ×20 and `i32.const 0x6ED9EBA1` (K1) ×20 both concentrated in fn 314. K2 / K3 (`0x8F1BBCDC` / `0xCA62C1D6`) do **not** appear as plain literals — they are precomputed into the derived constants the assembler emits when the compiler hoists `K xor previous`. Op histogram of fn 314: i32.xor + i32.add + i32.rotl dominance — textbook SHA-1 round. | https://crates.io/crates/sha-1                     |
| `rust-hashcash`  | 0.3.3               | Hashcash v1 PoW stamp generator (`1:bits:date:resource:ext:rand:counter`)   | The bundle exposes a hashcash-shaped stamp format via `window.hsw(jwt)` whose verification we have round-tripped against `rust-hashcash 0.3.3` (sha1 feature). The wasm SHA-1 backend (fn 314 above) is reachable from the PoW caller, never from the AES path.                                                                       | https://crates.io/crates/rust-hashcash             |
| `wasm-bindgen`   | ~0.2.79 (Implex era) | JS ↔ Rust marshalling glue — the entire HSW JS surface                       | Module exposes the canonical wbg shape: 164 function imports all under module `"a"` with 1-letter handles (`a.a` … `a.A` …); 20 exports under 2-letter handles (`cc` … `vc`); single linear memory `mc`; single mutable i32 global (the `__wbindgen_stack` cell). 196 of 430 local functions are labeled `wbg_marshal` by `tools/refine_hsw_labels.py`. | https://crates.io/crates/wasm-bindgen              |
| `hashbrown`      | ~0.11 (Implex era) | Hash-map backing store for any `HashMap`/`HashSet` in the Rust source       | Hashbrown's SSE2-emulating control byte `EMPTY = 0x80808080` appears as `i32.const` ×11 (top: fn 561 ×6, fn 397 ×2, fn 474 ×2). The SIMD-free fallback path is the WASM-relevant one.                                                                                                                                                | https://crates.io/crates/hashbrown                 |

## Probable crates (medium confidence)

These crates are required to compose the confirmed crates' surface or
have weaker (single-constant or single-pattern) evidence.

| Crate           | Version             | Purpose                                                                | Evidence                                                                                                                                                                                                                                                                                  | crates.io                                       |
| --------------- | ------------------- | ---------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| `cipher`        | 0.3.x (per Implex 0.3.0) | RustCrypto generic block-cipher traits used by `aes` + `ctr`           | No own constants; presence inferred from the co-existence of `aes 0.7` + `ctr`-style streaming and from the Implex 1.40.x dependency graph that locked `cipher = 0.3.0` for the same `aes 0.7.5`.                                                                                          | https://crates.io/crates/cipher                 |
| `ctr`           | 0.8.x (per Implex 0.8.0) | CTR-mode stream-cipher adapter over AES (the counter half of AES-256-GCM and any raw AES-CTR stream) | AES round functions (254 / 309 / 445 / 474 / 503 / 511) are tightly clustered with ChaCha20 (fn 327) and called from the giant dispatcher fn 213 in the exact pattern `ctr + aes` produces (counter increment + block encrypt + xor). Implex documented `ctr 0.8.0` for the same crate graph. | https://crates.io/crates/ctr                    |
| `aes-gcm`       | 0.9.x (per Implex)  | AES-256-GCM AEAD — the cipher used for `encrypt_req_data` / `decrypt_resp_data` and for the n-token (envelope) | The bundle's `window.hsw(0, blob)` and `window.hsw(1, bytes)` paths are verified end-to-end as AES-256-GCM with `iv12 ‖ ct ‖ tag16` framing. AES-GCM 0.9 over `aes 0.7 + ctr 0.8` is the canonical RustCrypto stack — and matches Implex's documented 1.40.x graph.                          | https://crates.io/crates/aes-gcm                |
| `ghash`         | 0.4.x               | GHASH polynomial multiplier for AES-GCM tags                            | No standalone constant fingerprint (GHASH multiplication is a table-free Karatsuba in this build), but the `aes-gcm 0.9` API requires it — and the AES dispatcher fn 213 emits the carryless-multiply pattern (chained i64.shl / i64.xor over a fixed reduction polynomial 0xE1) consistent with `ghash 0.4`. | https://crates.io/crates/ghash                  |
| `wasm-bindgen-futures` | ~0.4 (Implex era) | Bridges Rust `Future`s to JS `Promise`s — required by `window.hsw(jwt)`'s Promise-executor entry path | The n-token entry is reached via an export (current build: `ec`) whose call graph routes through fn 548 (192 146 B body, 14 direct callers) — the wbg-bindgen Promise dispatcher. This dispatcher only exists when `wasm-bindgen-futures` is in the dependency tree.                       | https://crates.io/crates/wasm-bindgen-futures   |
| `getrandom`     | ~0.2                | CSPRNG seed source — used by `rand_chacha` to seed itself                | The `rand_chacha` crate cannot self-seed; `getrandom` is the only blessed source in `rand 0.8` ecosystem. We see 3 `a.*` wbg imports whose call sites are reachable only from the `rand_chacha` cluster (fn 327), consistent with `getrandom`'s browser shim (`crypto.getRandomValues`).      | https://crates.io/crates/getrandom              |
| `dlmalloc` (or `wee_alloc`) | unknown    | Heap allocator — Rust + wasm builds need one of these                    | Single `i32.const 0x80000000` (the `HIGH_BIT` sentinel used by dlmalloc to flag in-use chunks) at fn 548. Could also be `wee_alloc`; we can't distinguish without panic strings. Implex's 1.40.x build used `dlmalloc` 0.2, which is also the wasm-bindgen default. | https://crates.io/crates/dlmalloc               |

## Speculative crates (low confidence)

Crates whose presence is plausible but for which we have NO
constant-level or opcode-level evidence in this stripped build. Listed
because they were in Implex's 1.40.x graph and the algorithmic surface
HSW exposes is broadly the same.

| Crate         | Version (Implex 1.40.x) | Purpose                                              | Evidence                                                                                                            | crates.io                                  |
| ------------- | ---------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| `js-sys`      | ~0.3                   | Typed bindings to JS built-ins (Array, Date, etc.)   | Implex graph standard; required by the 164 `a.*` wbg imports — but we cannot fingerprint it without symbol names.   | https://crates.io/crates/js-sys            |
| `web-sys`     | ~0.3                   | Typed bindings to Web platform APIs                  | Probably present given the fingerprint blob surface (`navigator.*`, `screen.*` etc.) — invisible to constant scan.  | https://crates.io/crates/web-sys           |
| `serde`       | ~1.0                   | Serialization framework                              | No magic constants survive. The bundle marshals JSON-shaped payloads, but those could equally be hand-rolled.       | https://crates.io/crates/serde             |
| `serde_json`  | ~1.0                   | JSON (de)serialization                               | Same — no fingerprint. The wbg layer marshals JS strings, so it's possible Rust never touches JSON directly.        | https://crates.io/crates/serde_json        |
| `base64`      | ~0.13                  | base64 encode/decode for the n-token wire format     | The n-token is emitted base64-encoded; the encoding is either JS-side (`btoa`) or done in Rust via this crate.       | https://crates.io/crates/base64            |
| `block-buffer`| ~0.9                   | Generic block-buffering helper for hash crates       | Required by `sha-1 0.10`; structurally invisible.                                                                    | https://crates.io/crates/block-buffer      |
| `digest`      | ~0.9                   | RustCrypto hash trait                                | Required by `sha-1`; structurally invisible.                                                                          | https://crates.io/crates/digest            |
| `generic-array` | ~0.14                | Type-level-length arrays used by RustCrypto crates   | Required by `aes` and `digest`; structurally invisible.                                                              | https://crates.io/crates/generic-array     |
| `subtle`      | ~2.4                   | Constant-time comparison primitives                  | Required by `aes-gcm` for tag verification; structurally invisible.                                                  | https://crates.io/crates/subtle            |
| `byteorder`   | ~1.4                   | Endianness conversions                               | Plausible inside `twox-hash` and `sha-1`; invisible.                                                                  | https://crates.io/crates/byteorder         |

## Crate version diff vs Implex's 2023 inventory

The Implex 2023 reverse-engineering documented the crate graph for
hCaptcha bundle version 1.40.x. Comparing against the current build:

  | Crate          | Implex 1.40.x       | Current build              | Status                                              |
  | -------------- | ------------------- | -------------------------- | --------------------------------------------------- |
  | `twox-hash`    | 1.6.0               | 1.6.x (constants identical) | **Unchanged major.minor**; patch level indeterminable |
  | `aes`          | 0.7.5               | 0.7.x (fixslice32 masks identical) | **Unchanged major.minor**                            |
  | `rand_chacha`  | 0.2.2               | 0.2.x (sigma words intact)  | **Unchanged major.minor**                            |
  | `cipher`       | 0.3.0               | 0.3.x (inferred)            | **Unchanged**                                       |
  | `ctr`          | 0.8.0               | 0.8.x (inferred)            | **Unchanged**                                       |
  | `aes-gcm`      | 0.9.x               | 0.9.x (round-trip verified) | **Unchanged**                                       |
  | `sha-1`        | not separately versioned | ~0.10 (K0/K1 present)  | Likely **unchanged** (RustCrypto SHA-1 hasn't bumped) |
  | `rust-hashcash`| 0.3.3               | 0.3.3 (stamp format identical) | **Unchanged**                                    |
  | `wasm-bindgen` | ~0.2.79             | ~0.2.79 (shim shape identical) | **Unchanged**                                     |
  | `hashbrown`    | not documented      | ~0.11 (`0x80808080` control byte) | Added to inventory                                |

**Net diff: nothing has rotated.** hCaptcha's crate graph for the
WASM PoW bundle has been frozen since at least the Implex 2023 work.
What rotates per ~10-minute rebuild is:

  - **Function indices** (fn 388 in this build = fn 282 in Implex's
    1.40.x dump; KS in this build is fn 425 vs Implex's fn 205)
  - **Export name letters** (`vc` / `ec` rotate every build; the
    underlying export ordering is stable)
  - **Master keys** (`encrypt_key`, `decrypt_key`, `n_key`) — each
    build draws fresh 32-byte values
  - **Deobfuscator blob XOR pads** (changes every build)

The crate graph itself, and therefore the algorithm choices, are
**build-invariant**.

## XOR keys and obfuscation constants (complete inventory)

hCaptcha layers TWO levels of compile-time string/rodata obfuscation
on top of the stock Rust+wasm-bindgen output. Each level uses its own
XOR-key pool. Every value below ROTATES per build — only the **count**
and **structural role** stay invariant. The values quoted here are
samples from two specific builds; the methodology in
[`tools/refine_hsw_labels.py`](../tools/refine_hsw_labels.py) re-derives
them dynamically on any build.

### 1. Build-time obfuscation pair (the high-occurrence XOR mask)

Two i32 constants embedded as `i32.const` literals in nearly every
function in the WASM. They appear with EQUAL occurrence counts —
classic XOR-decryption pair (one is the pad, the other the
inverse/modular complement). These are emitted by hCaptcha's
build-time obfuscator that runs after `wasm-opt -Oz`.

| Build              | Constant 1     | Constant 2     | Occurrences each | Distinct funcs |
| ------------------ | -------------- | -------------- | ---------------- | -------------- |
| Build `750faf0d…`  | `0x4B4529C7`   | `0xF847A8D2`   | 2259             | many           |
| Build `8d181839…`  | `0x6683AC90`   | `0x01763D17`   | 1448             | 128            |

Note: `0x4B4529C7 XOR 0xF847A8D2 = 0xB3028115`, and
`0x6683AC90 XOR 0x01763D17 = 0x67F59187` — no obvious relationship
between the two builds' pairs. They are FRESH per build.

### 2. Inline deobf-consumer XOR keys (compile-time pads)

A second pool of 3-6 i32 constants per build, embedded inline by
each function that reads from a `data` segment that the obfuscator
encrypted. These are the compile-time pads the deobf consumer XORs
the encrypted blob bytes against to recover the plaintext rodata.
139 functions in build `750faf0d…` (the "deobf_consumer" role —
see [`docs/11-hsw-function-map.md`](./11-hsw-function-map.md))
carry at least two of these keys.

| Build              | XOR pad keys                                                                     |
| ------------------ | -------------------------------------------------------------------------------- |
| Build `750faf0d…`  | `0x69169F2B`, `0xBD31F8F6`, `0xFB42E581`, `0x8304F247`, `0x77782831`, `0xE586D82B` |
| Build `8d181839…`  | `0x116FCB42` (in 143 funcs), `0x6683AC90` + `0x01763D17` (128 funcs each — note these are SHARED with set 1 on this build, suggesting builds are consolidating the two pools) |

The 6-key set in build `750faf0d…` partitions into:

  - **96-bit triple** for large blob decoding: `0x69169F2B`, `0xBD31F8F6`, `0xFB42E581`
  - **64-bit pair** for smaller blobs:          `0x8304F247`, `0x77782831`
  - **single-word key**, rotates with offset:   `0xE586D82B`

### 3. AES fixslice32 bit-permutation masks (NOT XOR keys)

These are XORed against the AES state during bit-sliced ShiftRows /
SubBytes / MixColumns, but they are **algorithm constants** (part of
the `aes 0.7.x` crate's fixslice32 implementation), not obfuscation
XOR keys. They are constant across all builds and across all hCaptcha
versions that ship the `aes` crate at version 0.7.x:

| Constant      | Role                                  |
| ------------- | ------------------------------------- |
| `0x55555555`  | fixslice bit-permute (low bits)       |
| `0x33333333`  | fixslice bit-permute (mid bits)       |
| `0x0F0F0F0F`  | fixslice bit-permute (high nibbles)   |
| `0xF0F0F0F0`  | fixslice bit-permute (low nibbles)    |
| `0x03030303`  | inv-ShiftRows row-3 helper            |
| `0xFCFCFCFC`  | inv-ShiftRows row-3 helper            |
| `0xC0C0C0C0`  | inv-ShiftRows row-3 helper            |
| `0x33003300`  | fixslice ShiftRows column shuffle     |
| `0x030F0C00`  | fixslice ShiftRows column shuffle     |
| `0x0F000F00`  | fixslice ShiftRows column shuffle     |
| `0x0C0F0300`  | fixslice ShiftRows column shuffle     |

Listed here for completeness only — these aren't XOR keys you can
use to decrypt anything; they're internal AES round constants.

### 4. Hash / RNG algorithm constants (also NOT XOR keys)

For full disclosure, these appear in the WASM and participate in XOR
operations but are stock algorithm constants, not obfuscation pads:

| Constant                          | Algorithm                | Role                          |
| --------------------------------- | ------------------------ | ----------------------------- |
| `0x9E3779B185EBCA87`              | XxHash3 PRIME64\_1       | mix multiplier                |
| `0xC2B2AE3D27D4EB4F`              | XxHash3 PRIME64\_2       | mix multiplier                |
| `0x165667B19E3779F9`              | XxHash3 PRIME64\_3       | mix multiplier                |
| `0x27D4EB2F165667C5`              | XxHash3 PRIME64\_5       | mix multiplier                |
| `0x5A827999`                      | SHA-1 K0                 | round constant (rounds 0–19)  |
| `0x6ED9EBA1`                      | SHA-1 K1                 | round constant (rounds 20–39) |
| `0x70E44324`, `0x359D3E2A`        | SHA-1 K2/K3 (computed)   | derived at runtime — K2 and K3 are NOT embedded as direct literals |
| `0x5851F42D4C957F2D`              | PCG / rand\_pcg          | LCG multiplier                |
| `0x61707865`, `0x3320646E`,        | ChaCha20 "expand 32-byte k" sigma words (`expa`, `nd 3`, `2-by`, `te k`) |
| `0x79622D32`, `0x6B206574`        |                          |                               |

### 5. Bonus: the embedded SipHash default key

The ASCII string `"somepseudorandomlygeneratedbytes"` (32 bytes) is
embedded as four `i64.const` literals in two functions (typical
`ahash::fallback::FALLBACK_KEYS` shape or Rust std's `RandomState`
default-when-no-getrandom path):

```
0x736F6D6570736575   "somepseu"
0x646F72616E646F6D   "dorandom"
0x6C7967656E657261   "lygenera"
0x7465646279746573   "tedbytes"
```

These are **hardcoded by ahash / std**, not by hCaptcha — they're
the public default SipHash key for HashMap entropy and have no role
in the live key material.

### Re-derive the obfuscation XOR keys on any build

Use the snippet in [`tools/refine_hsw_labels.py`](../tools/refine_hsw_labels.py)
(constants `DEOBF_KEYS` near the top). Or run this directly:

```python
from collections import defaultdict
from hcaptcha.tools.wasm_disasm import WasmModule
from hcaptcha import version as v
from hcaptcha.hsw_bridge import HSWAnalyzer

mod = WasmModule(bytes.fromhex(HSWAnalyzer(v.latest_version()).analyze()["wasm_bytes_hex"]))
per_const_funcs = defaultdict(set)
for f in mod.functions:
    if f["func_idx"] < len(mod.imports): continue
    for n, ops, _, _ in (mod.decode_function(f["func_idx"]) or []):
        if n == "i32.const" and ops:
            val = ops[0] & 0xFFFFFFFF
            if val > 0x10000:
                per_const_funcs[val].add(f["func_idx"])

# Inline XOR pads = consts appearing in 30+ distinct functions, not AES masks
AES = {0x55555555, 0x33333333, 0x0F0F0F0F, 0xF0F0F0F0,
       0x33003300, 0x030F0C00, 0x0F000F00, 0x0C0F0300}
for val, fns in sorted(per_const_funcs.items(), key=lambda x: -len(x[1])):
    if val in AES or len(fns) < 30: continue
    print(f"  0x{val:08x}  {len(fns)} distinct funcs")
```

## How to verify

All evidence in this document is reproducible from a single
fresh `data/hsw_cached.wasm` using only Python and the in-tree
`wasm_disasm.py`. No external tooling required.

### 1. Verify magic constants

```sh
cd C:\Users\Administrator\Desktop\HSJ
python -c "
import sys; sys.path.insert(0, 'src')
from collections import Counter
from hcaptcha.tools.wasm_disasm import WasmModule
mod = WasmModule(open('data/hsw_cached.wasm','rb').read())

TARGETS = {
    # XxHash3 PRIME64 (twox-hash)
    0x9E3779B185EBCA87 - (1<<64): 'twox-hash PRIME64_1',
    0xC2B2AE3D27D4EB4F:           'twox-hash PRIME64_2',
    0x165667B19E3779F9:           'twox-hash PRIME64_3',
    0x27D4EB2F165667C5:           'twox-hash PRIME64_5',
    # ChaCha20 sigma (rand_chacha)
    0x61707865: 'rand_chacha expa',
    0x3320646E: 'rand_chacha nd 3',
    0x79622D32: 'rand_chacha 2-by',
    0x6B206574: 'rand_chacha te k',
    # SHA-1 round constants (sha-1)
    0x5A827999: 'sha-1 K0',
    0x6ED9EBA1: 'sha-1 K1',
    # fixslice32 masks (aes)
    0x55555555:  'fixslice32 0x55555555',
    0x33333333:  'fixslice32 0x33333333',
    0x0F0F0F0F:  'fixslice32 0x0F0F0F0F',
    -0x10101010: 'fixslice32 0xF0F0F0F0',
    0x33003300:  'fixslice32 0x33003300',
    0x030F0C00:  'fixslice32 0x030F0C00',
    0x0F000F00:  'fixslice32 0x0F000F00',
    0x0C0F0300:  'fixslice32 0x0C0F0300',
    # hashbrown control byte
    -0x7F7F7F80: 'hashbrown EMPTY 0x80808080',
}
per_fn, totals = {}, Counter()
for fn in mod.functions:
    for name, ops, _, _ in (mod.decode_function(fn['func_idx']) or []):
        if name in ('i32.const','i64.const') and ops and ops[0] in TARGETS:
            label = TARGETS[ops[0]]
            totals[label] += 1
            per_fn.setdefault(label, Counter())[fn['func_idx']] += 1
for label, cnt in sorted(totals.items(), key=lambda x: -x[1]):
    print(f'{label}: {cnt} total, top fns: {per_fn[label].most_common(3)}')
"
```

Expected output (current build, sha256 `750faf0d…`):

```
fixslice32 0x0F0F0F0F: 124 total, top fns: [(388, 30), (520, 30), (425, 16)]
fixslice32 0x33333333: 92 total, top fns: [(388, 30), (520, 30), (425, 8)]
fixslice32 0x55555555: 78 total, top fns: [(388, 23), (520, 23), (425, 8)]
fixslice32 0x33003300: 48 total, top fns: [(477, 24), (548, 24)]
fixslice32 0x0F000F00: 32 total, top fns: [(425, 8), (471, 8), (477, 8)]
fixslice32 0x030F0C00: 32 total, top fns: [(477, 16), (548, 16)]
twox-hash PRIME64_1: 24 total, top fns: [(232, 16), (234, 8)]
sha-1 K0: 20 total, top fns: [(314, 20)]
sha-1 K1: 20 total, top fns: [(314, 20)]
twox-hash PRIME64_2: 17 total, top fns: [(232, 9), (234, 8)]
rand_chacha te k: 16 total, top fns: [(327, 16)]
rand_chacha 2-by: 16 total, top fns: [(327, 16)]
rand_chacha nd 3: 16 total, top fns: [(327, 16)]
rand_chacha expa: 16 total, top fns: [(327, 16)]
hashbrown EMPTY 0x80808080: 11 total, top fns: [(561, 6), (397, 2), (474, 2)]
twox-hash PRIME64_3: 4 total, top fns: [(232, 4)]
twox-hash PRIME64_5: 4 total, top fns: [(232, 4)]
```

Function indices will rotate per build; constant counts and the
co-location pattern are invariant.

### 2. Confirm no debug strings survive

```sh
python -c "
import re
d = open('data/hsw_cached.wasm','rb').read()
for tell in [b'rust', b'panic', b'cargo', b'.rs', b'twox', b'aes-', b'rand_', b'chacha']:
    hits = sum(1 for s in re.findall(rb'[\x20-\x7e]{8,}', d) if tell in s.lower())
    print(f'{tell!r}: {hits} hits')
"
```

Expected: zero hits across all telltales.

### 3. Confirm 164-import wbg shim shape (wasm-bindgen)

```sh
python -c "
import sys; sys.path.insert(0, 'src')
from collections import Counter
from hcaptcha.tools.wasm_disasm import WasmModule
mod = WasmModule(open('data/hsw_cached.wasm','rb').read())
imps = [i for i in mod.imports if i['kind']=='func']
print('import count:', len(imps))
print('import modules:', dict(Counter(i['module'] for i in imps)))
print('export count:', len(mod.exports))
"
```

Expected: `import count: 164`, `import modules: {'a': 164}`,
`export count: 20`.

### 4. End-to-end key extraction smoke test

```sh
cd C:\Users\Administrator\Desktop\HSJ
PYTHONPATH=src python -m hcaptcha
```

This drives the bundle through pure Node + jsdom (no Playwright),
patches the AES key-schedule site, and returns all 6 master keys
(`hsj.{n_key, response_decrypt_key, payload_encrypt_key}` +
`hsw.{encrypt_key, decrypt_key, n_key}`) with `verified: true` for
each. Run time: ~18 s on a warm cache. See
[`docs/12-hsw-complete-summary.md`](./12-hsw-complete-summary.md) for
the full extraction pipeline.
