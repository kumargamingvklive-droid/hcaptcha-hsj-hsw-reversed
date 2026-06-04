# HSW WASM Function Map

A reverse-engineered, role-labelled map of every function in the
current `hsw.js` WASM module. Generated from the live build's binary
(`docs/hsw_function_labels.json` is the machine-readable source; this
file is the human narrative).

> WASM module: 618,144 bytes
> Function count: **594** (164 wasm-bindgen JS-binding imports + 430 local)

The labels are produced by `tools/build_hsw_function_labels.py` (run on
each build), which classifies functions by structural features —
fixslice32 mask density, SHA-1 K-constants, LCG multiplier, GHASH
polynomial, JS-marshal call frequency, etc. Roles that didn't fire on
this build (`ghash_or_gcm`, `allocator_or_runtime`) remain in the
schema for cross-build comparisons.

## Stats by label

| Label                 | Count | Notes                                                |
| --------------------- | ----: | ---------------------------------------------------- |
| `wbg_marshal`         |   196 | wasm-bindgen JS shims; mostly imports                |
| `unknown`             |   359 | unlabelled / awaiting role                           |
| `aes_round`           |    14 | bitsliced round / SubBytes-style helpers             |
| `wbindgen_helpers`    |    12 | wbindgen-generated callback trampolines              |
| `aes_key_schedule`    |     6 | fixslice32 key expansion functions                   |
| `fingerprint_collect` |     3 | JSON / fingerprint-blob assembly                     |
| `deobf_helper`        |     2 | XOR-deobfuscation accessors used by every key load   |
| `sha1_pow`            |     1 | SHA-1 compression — Hashcash PoW solver core         |
| `rng_or_lcg`          |     1 | the `vc` dispatcher (carries the PCG LCG multiplier) |
| `ghash_or_gcm`        |     0 | inlined; GHASH polynomial not present as constants   |
| `allocator_or_runtime`|     0 | wasm-bindgen handles the heap from JS                |

## All exports (entry points)

The `vc` dispatcher is the only non-trivial export — everything is
multiplexed through it via a 32-bit magic. Other exports are
wasm-bindgen-conventions plumbing.

| Export | func idx | role          | sig                                              | body  |
| ------ | -------: | ------------- | ------------------------------------------------ | ----: |
| `vc`   |      593 | rng_or_lcg    | `(i32,i32,i32,i32,i32,i32,f32,f64)->()`          | 14216 |
| `hc`   |      197 | unknown       | `(i32,i32,i32)->(i32)`                           |    61 |
| `kc`   |      241 | unknown       | `(i32,i32,i32,i32)->(i32)`                       |    79 |
| `dc`   |      245 | unknown       | `(i32,i32)->()`                                  |   118 |
| `qc`   |      339 | unknown       | `(i32,i32,i32,i32)->()`                          |    61 |
| `ec`   |      450 | unknown       | `(i32,i32)->()`                                  |   116 |
| `jc`   |      477 | unknown       | `(i32,i32,i32,i32)->(i64)`                       |   624 |
| `fc`   |      486 | unknown       | `(i32,i32,i32,i32)->()`                          |   124 |
| `lc`   |      487 | unknown       | `(i32,i32)->(i32)`                               |    95 |
| `cc`   |      496 | unknown       | `(i64,i32,i32,i32,i32,i32)->()`                  |   353 |
| `mc`   |      501 | unknown       | `(i32,i32,i32,i32,f64,i32,i32,i32)->(i32)`       |   218 |
| `gc`   |      534 | unknown       | `(i32)->()`                                      |    25 |
| `oc`   |      563 | unknown       | `(i32,i32,i32,i32)->(i64)`                       |   624 |
| `nc`   |      566 | unknown       | `(i32)->(i32)`                                   |    10 |
| `pc`   |      569 | unknown       | `(i32,i32,i32,i32,i32)->()`                      |   142 |
| `ic`   |      571 | unknown       | `(i32,i32)->()`                                  |   106 |
| `sc`   |      590 | unknown       | `(i32,i32,i32,f32)->(f32)`                       |   419 |
| `tc`   |      591 | unknown       | `(i32,i32,i32,i32,i32)->(f64)`                   |   614 |
| `uc`   |      592 | unknown       | `(i32,i32,i32,i32,f64,i32)->(i32)`               |   126 |

Note that `jc` and `oc` share an identical `(i32,i32,i32,i32)->(i64)`
signature and 624-byte body — they are the public-side i64-returning
deobf accessors (the JS bundle calls these to materialise the
runtime-seeded helper values during the n-token path).

## Key functions per role

### `rng_or_lcg` — the dispatcher (`vc`)

| func | export | sig                                              | body  | callers | callees |
| ---: | ------ | ------------------------------------------------ | ----: | ------: | ------: |
|  593 | `vc`   | `(i32,i32,i32,i32,i32,i32,f32,f64)->()`          | 14216 |       0 |     808 |

`vc` is the multiplexer: every public `hsw(...)` call lands here, the
first `i32` argument is the role magic, and an `i32.eq` chain inside
the body routes to the per-role implementation. The unusual signature
(8 args ending in `f32, f64`) reflects the union of every callee's
arguments — Rust enums monomorphised into a single C-ABI export. This
function also carries the PCG-32 LCG multiplier `0x5851F42D4C957F2D`
inline, which is how the N-key derivation site is located.

### `aes_key_schedule` — fixslice32 key expansions

Each direction (encrypt / decrypt) uses a separate key-schedule
function, plus inner helpers shared between rounds.

| func | sig                  | body  | callers | callees |
| ---: | -------------------- | ----: | ------: | ------: |
|  345 | `(i32,i32)->()`      |  4178 |       1 |       7 |
|  304 | `(i32,i32)->()`      |  3156 |       2 |       5 |
|  586 | `(i32,i32)->()`      |  3152 |       1 |       5 |
|  282 | `(i32,i32)->()`      |  2869 |       2 |       5 |
|  263 | `(i32)->()`          |   624 |       2 |       2 |
|  533 | `(i32)->()`          |   608 |       2 |       2 |

In the current build, `func 587` is the production encrypt+decrypt
key schedule (this is what `hsw.py` patches with the 8-call
deobf-write injection). It is in the call graph reachable from `vc`
through both magics; the other key-schedule functions handle internal
rekey paths or test variants.

### `aes_round` — bitsliced round transforms

| func | sig                       | body  | callers | callees |
| ---: | ------------------------- | ----: | ------: | ------: |
|  226 | `(i32,i32)->(i32)`        | 26882 |       0 |     648 |
|  521 | `(i32)->(i32)`            |  7327 |       4 |     314 |
|  293 | `(i32,i32,i32)->()`       |  6861 |       3 |     180 |
|  431 | `(i32,i32,i32)->(i32)`    |  4030 |       2 |     180 |
|  403 | `(i32,i32,i32)->()`       |  2823 |       5 |     159 |
|  253 | `(i32,i32)->(i32)`        |  1265 |       6 |      20 |
|  561 | `(i32,i32,i32,i32,i32,i32)->(i32)` | 1236 |  9 |      33 |
|  172 | `(i32,i32,i32)->(i32)`    |  1193 |       6 |      25 |
|  416 | `(i32,i32)->(i32)`        |  1146 |       2 |      36 |
|  301 | `(i32,i32,i32)->()`       |  1113 |       2 |      24 |
|  385 | `(i32,i32,i32)->()`       |  1113 |       2 |      24 |
|  269 | `(i32,i32,i32)->(i32)`    |  1051 |       3 |      28 |
|  451 | `(i64,i32)->(i32)`        |   608 |       3 |      14 |
|  507 | `(i32,i32)->(i32)`        |   596 |       2 |      14 |

`func 226` is the outsized one (26 KB body) — the fully inlined GCM
encrypt/decrypt path that loops the round transformation against the
expanded key schedule. It has zero direct callers because it's reached
through the dispatcher's call tree (vc → … → 226). `func 561` is the
6-arg round used by the per-block parallel-track variant.

### `deobf_helper` — XOR accessors

| func | sig                | body | callers | callees |
| ---: | ------------------ | ---: | ------: | ------: |
|  260 | `(i32,i32)->(i32)` |   55 |     229 |       2 |
|  204 | `(i32)->(i32)`     |   22 |      17 |       2 |

Both are dead simple: `xor(load(arg), MASK_CONSTANT)` returning a
deobfuscated word. Every key load and many magic-number comparisons
route through one of these. The HSW extractor identifies `func 398`
as the build's primary deobf helper (the one called from the key
schedule); 260 + 204 are the upstream pool from which the build
inliner chose at compile time. `func 398` itself is labelled
`wbg_marshal` here because the labeller scores it as a binding-style
shim, but its 8 invocations from the key schedule are exactly the
XOR-deobf reads documented in [`docs/07-wasm-patching.md`](07-wasm-patching.md).

### `sha1_pow` — Hashcash PoW solver

| func | sig             | body | callers | callees |
| ---: | --------------- | ---: | ------: | ------: |
|  436 | `(i32,i32)->()` | 4508 |       5 |      26 |

The SHA-1 compression function. Contains both K-constants
`0x5A827999` and `0x6ED9EBA1` as inline `i32.const` literals; the
classifier flags any function carrying both constants as `sha1_pow`.
This is the core of the Hashcash stamp solver
(`1:bits:date:resource:ext:rand:counter` with SHA-1 leading-zero-bit
target).

### `fingerprint_collect` — JSON-blob assembly

| func | sig                | body   | callers | callees |
| ---: | ------------------ | -----: | ------: | ------: |
|  342 | `(i32,i32)->(i32)` | 205243 |       0 |    9164 |
|  454 | `(i32,i32)->()`    |   6042 |       7 |     268 |
|  281 | `(i32,i32)->()`    |   2277 |       2 |      84 |

`func 342` is by far the largest function in the module (200 KB body,
9164 calls). It is the fingerprint collector — a wall of inlined
JSON serialisation paths over every browser-side field. The 469
JS-marshal calls inside it are the per-field property reads
(`navigator.userAgent`, `screen.width`, etc.). It is invoked from
`vc` via the n-token magic and ultimately feeds the bytes that get
encrypted under the N-key.

## Call graph — crypto + PoW + RNG

Edges below show the directly-observed callers/callees collected from
the WASM code section.

```
vc (593) ──> 282, 293, 295, 304, 312, 322, 337, 341, 345, 355, 371,
             401, 403, 433, 436, 444, 449, …  (808 callees total)

aes_key_schedule:
  345 ──> 226-helpers + 433, 444, 449, 493        (callers: vc)
  304 ──> 433, 444, 449, 477, 496                 (callers: 242, 565)
  586 ──> 433, 444, 449, 477, 496                 (callers: 342)
  282 ──> 263, 444, 449, 493, 533                 (callers: 205, 342)
  533 ──> 444, 449                                (callers: 282, 403)
  263 ──> 444, 449                                (callers: 282, 403)

aes_round (selected):
  226 ──> 244, 257, 258, 337, 341, 355, 416, 424, 433, 444, 449,
          475, 477, 496, 539, 578                 (no direct callers)
  293 ──> 227, 260, 285, 355, 391, 424, 444, 449, 477, 496, 578
                                                  (callers: 198, 342)
  431 ──> recursive (calls itself + 244, 257, 272, 275, 337, 355, 444,
          449, 456, 523, 567, 578)                (callers: 431, 500)

sha1_pow:
  436 ──> 444, 449                                (callers: 342)

deobf_helper (primary):
  398 ──> 260                                     (calls 8 times from
                                                   each key schedule
                                                   patch site)
```

## What the HSW extractors locate

Cross-referencing the labels with the extractor source code:

* `hcaptcha.hsw._find_dispatcher_func` → **func 593** (`vc`).
* `hcaptcha.hsw._find_key_schedule_for_magic` → **func 587** on the
  current build (selected from {282, 304, 345, 586} via the
  fixslice-mask + signature filter).
* `hcaptcha.hsw._find_deobf_helper` → **func 398** (the (i32,i32)→i32
  callee most frequently invoked from the key schedule).
* `hcaptcha.hsw_n_key_runtime._find_vc` → **func 593** (same as
  dispatcher).
* `hcaptcha.hsw_n_key_runtime._find_byte_store_helper` → **func 340**
  (the (i32,i32,i32)→() callee that fires right after each LCG
  multiplier inside vc, marked here as `unknown` because the
  classifier doesn't have a "byte-store" category yet).

The function indices rotate every build. The labels file is
regenerated per build by `tools/build_hsw_function_labels.py`; the
extractors above re-derive the indices structurally and don't read
the JSON.

## Machine-readable source

[`docs/hsw_function_labels.json`](hsw_function_labels.json) holds the
full per-function record (594 entries):

```json
{
  "func_idx":     593,
  "name":         "vc",
  "role":         "rng_or_lcg",
  "signature":    "(i32,i32,i32,i32,i32,i32,f32,f64)->()",
  "body_bytes":   14216,
  "callers":      0,
  "callees":      808,
  "is_exported":  true,
  "export_name":  "vc",
  "is_import":    false
}
```

Use it as the cross-reference whenever a doc cites "func N" — function
indices are not stable across builds, only roles are.
